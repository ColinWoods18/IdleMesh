"""Availability predictor v0: per-node idle probability by hour of the week (S6.1, C1).

A node is **available** when its owner is away: the agent reports state `idle`, or `serving` (it only serves
while idle). `active`, `sleeping` and `draining` are not available, and so is a gap in the samples longer than
`max_gap_s` (the agent was off, or the machine asleep).

For each hour of the week (Monday 00:00 is bucket 0, in the coordinator's local time) and each horizon N, the
model counts the available samples and how many of them stayed available for the next N minutes. A sample whose
next N minutes run past the end of the data is censored and left out. The estimate shrinks towards the node's
own rate over the whole week when an hour has few samples, and that rate shrinks towards the fleet's:

    p = (stayed + s * prior) / (n + s)

with s = `prior_strength`. The same applies to the chance of being available at all in that hour.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

AVAILABLE_STATES = frozenset({"idle", "serving"})
HOURS = 168
FLEET_PRIOR = 0.5  # with no data anywhere, a coin toss


@dataclass(frozen=True)
class Sample:
    ts: datetime          # timezone-aware
    available: bool


@dataclass
class Counts:
    """Counts for one node: `seen[h]`/`avail[h]` per hour of the week, and per horizon `n[h]`/`stayed[h]`."""
    seen: list[int] = field(default_factory=lambda: [0] * HOURS)
    avail: list[int] = field(default_factory=lambda: [0] * HOURS)
    n: dict[int, list[int]] = field(default_factory=dict)
    stayed: dict[int, list[int]] = field(default_factory=dict)

    def horizon(self, minutes: int) -> tuple[list[int], list[int]]:
        return (self.n.setdefault(minutes, [0] * HOURS), self.stayed.setdefault(minutes, [0] * HOURS))

    def add(self, other: "Counts") -> None:
        for i in range(HOURS):
            self.seen[i] += other.seen[i]
            self.avail[i] += other.avail[i]
        for m in other.n:
            n, k = self.horizon(m)
            for i in range(HOURS):
                n[i] += other.n[m][i]
                k[i] += other.stayed[m][i]


def hour_of_week(ts: datetime, tz: tzinfo | None = None) -> int:
    """0 for Monday 00:00-00:59 up to 167 for Sunday 23:00-23:59, in `tz` (default: this machine's local time)."""
    local = ts.astimezone(tz)
    return local.weekday() * 24 + local.hour


def runs(samples: list[Sample], interval_s: float, max_gap_s: float) -> list[tuple[datetime, datetime, bool]]:
    """Availability runs as (start, end, censored). A run ends at the first unavailable sample, or one interval
    after its last sample when a gap or the end of the data follows. `censored` is True only for a run cut by the
    end of the data, whose real end is unknown."""
    out = []
    start = last = None
    for s in samples:
        if start is not None and (s.ts - last).total_seconds() > max_gap_s:
            out.append((start, last + timedelta(seconds=interval_s), False))
            start = None
        if s.available:
            start = s.ts if start is None else start
        elif start is not None:
            out.append((start, s.ts, False))
            start = None
        last = s.ts
    if start is not None:
        out.append((start, last + timedelta(seconds=interval_s), True))
    return out


def count(samples: list[Sample], horizons: list[int], interval_s: float, max_gap_s: float,
               tz: tzinfo | None = None) -> Counts:
    """Count one node's samples (oldest first) into hour-of-week buckets, in one pass."""
    c = Counts()
    for m in horizons:
        c.horizon(m)
    hours = [hour_of_week(s.ts, tz) for s in samples]
    for s, h in zip(samples, hours):
        c.seen[h] += 1
        c.avail[h] += s.available
    i = 0
    for start, end, censored in runs(samples, interval_s, max_gap_s):
        while i < len(samples) and samples[i].ts < start:
            i += 1
        while i < len(samples) and samples[i].ts < end:
            ts, h = samples[i].ts, hours[i]
            for m in horizons:
                stays = ts + timedelta(minutes=m) <= end
                if not (censored and not stays):
                    n, k = c.horizon(m)
                    n[h] += 1
                    k[h] += stays
            i += 1
    return c


def shrink(k: int, n: int, prior: float, strength: float) -> float:
    return (k + strength * prior) / (n + strength)


@dataclass(frozen=True)
class Estimate:
    p_stay: float          # P(available for the next `horizon_min` minutes | available now)
    p_available: float     # P(available at a random moment of this hour)
    hour: int
    horizon_min: int
    samples: int           # available samples behind p_stay in this hour
    basis: str             # "hour": this hour has data; "node": only other hours; "fleet": no data for this node

    def as_dict(self) -> dict:
        return {"p_stay": round(self.p_stay, 4), "p_available": round(self.p_available, 4), "hour_of_week": self.hour,
                "horizon_min": self.horizon_min, "samples": self.samples, "basis": self.basis}


def fleet_rates(nodes: list[Counts], horizon: int) -> tuple[float, float]:
    """The fleet's overall (p_stay, p_available), the prior for every node."""
    total = Counts()
    for c in nodes:
        total.add(c)
    n, k = total.horizon(horizon)
    return (shrink(sum(k), sum(n), FLEET_PRIOR, 1.0), shrink(sum(total.avail), sum(total.seen), FLEET_PRIOR, 1.0))


def estimate(c: Counts, hour: int, horizon: int, fleet: tuple[float, float], strength: float) -> Estimate:
    n, k = c.horizon(horizon)
    node_stay = shrink(sum(k), sum(n), fleet[0], strength)
    node_avail = shrink(sum(c.avail), sum(c.seen), fleet[1], strength)
    basis = "hour" if n[hour] else ("node" if sum(n) else "fleet")
    return Estimate(p_stay=shrink(k[hour], n[hour], node_stay, strength),
                    p_available=shrink(c.avail[hour], c.seen[hour], node_avail, strength),
                    hour=hour, horizon_min=horizon, samples=n[hour], basis=basis)


def availability(e: Estimate, available_now: bool | None) -> float:
    """The probability that a node is usable for the next `horizon_min` minutes, the placement term (S6.3).
    A node that is idle now needs only to stay idle. A node that is not (or whose state is unknown) has to become
    idle and then stay, so its chance is p_available x p_stay."""
    return e.p_stay if available_now else e.p_available * e.p_stay
