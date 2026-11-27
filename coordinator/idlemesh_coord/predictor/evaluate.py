"""Offline evaluation of the availability predictor (S6.2), also used for the live accuracy metric (S6.4).

Fit on the samples before a split time, then score every available sample after it whose outcome is known:
the prediction is `p_stay` for the sample's hour of the week, and the outcome is whether the node stayed
available for the horizon. Reported:

- **Brier score**: mean of (p - y)^2. 0 is perfect; 0.25 is a constant 0.5.
- **Skill** against two baselines fitted on the same data: the fleet's overall rate (one number for every node
  and hour), and each node's own overall rate (no hour of the week). Skill = 1 - Brier / Brier(baseline);
  above 0 means the hour-of-week model helps.
- **Calibration**: predictions in equal-width bins, with the observed rate in each. A calibrated model has the
  observed rate close to the bin's mean prediction.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from ..store import Store, iso, now
from . import model
from .model import Counts, Sample
from .service import PredictorPolicy, fit, samples


def outcomes(s: list[Sample], horizon_min: int, interval_s: float, max_gap_s: float) -> list[tuple[datetime, bool]]:
    """(time, stayed available for `horizon_min`) for each available sample whose outcome is known."""
    out = []
    i = 0
    for start, end, censored in model.runs(s, interval_s, max_gap_s):
        while i < len(s) and s[i].ts < start:
            i += 1
        while i < len(s) and s[i].ts < end:
            stays = s[i].ts + timedelta(minutes=horizon_min) <= end
            if not (censored and not stays):
                out.append((s[i].ts, stays))
            i += 1
    return out


@dataclass
class Score:
    n: int = 0
    brier: float = 0.0
    brier_fleet: float = 0.0
    brier_node: float = 0.0
    stayed: int = 0
    sum_p: float = 0.0

    def add(self, p: float, p_fleet: float, p_node: float, y: bool) -> None:
        self.n += 1
        self.sum_p += p
        self.stayed += y
        self.brier += (p - y) ** 2
        self.brier_fleet += (p_fleet - y) ** 2
        self.brier_node += (p_node - y) ** 2

    def as_dict(self) -> dict:
        if not self.n:
            return {"n": 0}
        b, bf, bn = self.brier / self.n, self.brier_fleet / self.n, self.brier_node / self.n
        return {"n": self.n, "mean_p": round(self.sum_p / self.n, 4), "observed_rate": round(self.stayed / self.n, 4),
                "brier": round(b, 4),
                "brier_fleet_rate": round(bf, 4), "brier_node_rate": round(bn, 4),
                "skill_vs_fleet": round(1 - b / bf, 4) if bf else None,
                "skill_vs_node": round(1 - b / bn, 4) if bn else None}


def evaluate(store: Store, nodes: list[str], policy: PredictorPolicy, split: datetime, end: datetime,
             horizon_min: int | None = None, tz: tzinfo | None = None, bins: int = 10) -> dict:
    """Fit on [split - window_days, split), score [split, end). `nodes` are node keys."""
    h = horizon_min or policy.default_horizon_min
    fitted = fit(store, nodes, policy, split - timedelta(days=policy.window_days), split, tz)
    counts = {n: c for n, (c, _) in fitted.items()}
    fleet = model.fleet_rates(list(counts.values()), h)
    total, per_node = Score(), {}
    cal = [[0, 0.0, 0] for _ in range(bins)]  # n, sum of p, stayed
    for node in nodes:
        c = counts.get(node) or Counts()
        n, k = c.horizon(h)
        p_node = model.shrink(sum(k), sum(n), fleet[0], policy.prior_strength)
        score = per_node.setdefault(node, Score())
        for ts, y in outcomes(samples(store, node, split, end), h, policy.interval_s, policy.max_gap_s):
            p = model.estimate(c, model.hour_of_week(ts, tz), h, fleet, policy.prior_strength).p_stay
            total.add(p, fleet[0], p_node, y)
            score.add(p, fleet[0], p_node, y)
            b = min(int(p * bins), bins - 1)
            cal[b][0] += 1
            cal[b][1] += p
            cal[b][2] += y
    return {
        "horizon_min": h, "split": split.isoformat(), "end": end.isoformat(), "window_days": policy.window_days,
        "fleet_rate": round(fleet[0], 4), **total.as_dict(),
        "calibration": [{"bin": [i / bins, (i + 1) / bins], "n": n, "mean_p": round(sp / n, 4),
                         "observed": round(y / n, 4)} for i, (n, sp, y) in enumerate(cal) if n],
        "nodes": {node: s.as_dict() for node, s in per_node.items()},
    }


FLEET = "*"  # predictor_accuracy.node for the whole fleet


def record_accuracy(store: Store, nodes: list[str], policy: PredictorPolicy, at: datetime | None = None,
                    tz: tzinfo | None = None, lookback_h: float = 24.0) -> dict:
    """The live accuracy metric (S6.4): score the last `lookback_h` against a model fitted only on the data before
    it, for each horizon, and store one row per node and one for the fleet. Drift shows as mean_p moving away
    from observed, or Brier rising, over time."""
    at = at or now()
    split = at - timedelta(hours=lookback_h)
    rows = []
    for h in policy.horizons_min:
        r = evaluate(store, nodes, policy, split, at, h, tz)
        for node, s in [(FLEET, r), *r["nodes"].items()]:
            if s.get("n"):
                rows.append((iso(at), node, h, s["n"], s["mean_p"], s["observed_rate"], s["brier"],
                             s["brier_node_rate"]))
    with store.tx() as c:
        c.executemany("INSERT OR REPLACE INTO predictor_accuracy (ts, node, horizon_min, n, mean_p, observed, brier,"
                      " brier_node) VALUES (?,?,?,?,?,?,?,?)", rows)
    return {"rows": len(rows), "scored": sum(r[3] for r in rows if r[1] == FLEET)}
