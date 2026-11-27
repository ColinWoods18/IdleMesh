"""Fit the availability predictor from the store, and answer questions with it (S6.1).

`recompute` reads each node's idle samples over the last `window_days`, counts them by hour of the week
(`model.count`) and stores the counts in `node_availability`, one row per node. It runs on a timer
(`predictor.recompute_every_s`, hourly by default); the counts are a few kilobytes per node, so `Predictor`
keeps them all in memory between runs.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from .. import obs
from ..store import Store, iso, now, parse
from . import model
from .model import AVAILABLE_STATES, Counts, Estimate, Sample

log = logging.getLogger("idlemesh_coord.predictor")


@dataclass(frozen=True)
class PredictorPolicy:
    """Site config `predictor`."""
    window_days: float = 28.0
    horizons_min: tuple[int, ...] = (30, 60, 120)
    default_horizon_min: int = 60
    prior_strength: float = 10.0
    interval_s: float = 60.0      # the agents' sample interval (`telemetry.interval_s`)
    max_gap_factor: float = 3.0   # a gap longer than this many intervals counts as unavailable

    @property
    def max_gap_s(self) -> float:
        return self.interval_s * self.max_gap_factor


def samples(store: Store, node: str, since: datetime, until: datetime | None = None) -> list[Sample]:
    sql, args = "SELECT ts, state FROM idle_samples WHERE node = ? AND ts >= ?", [node, iso(since)]
    if until is not None:
        sql, args = sql + " AND ts < ?", [*args, iso(until)]
    return [Sample(parse(r["ts"]), r["state"] in AVAILABLE_STATES) for r in store.all(sql + " ORDER BY ts", tuple(args))]


def fit(store: Store, nodes: list[str], policy: PredictorPolicy, since: datetime, until: datetime | None = None,
        tz: tzinfo | None = None) -> dict[str, tuple[Counts, int]]:
    """Counts per node from the samples in [since, until): {node: (counts, samples)}."""
    out = {}
    for node in nodes:
        s = samples(store, node, since, until)
        out[node] = (model.count(s, list(policy.horizons_min), policy.interval_s, policy.max_gap_s, tz), len(s))
    return out


def _dump(c: Counts) -> str:
    return json.dumps({"seen": c.seen, "avail": c.avail, "n": {str(m): v for m, v in c.n.items()},
                       "stayed": {str(m): v for m, v in c.stayed.items()}}, separators=(",", ":"))


def _load(text: str) -> Counts:
    d = json.loads(text)
    return Counts(seen=d["seen"], avail=d["avail"], n={int(m): v for m, v in d["n"].items()},
                  stayed={int(m): v for m, v in d["stayed"].items()})


def recompute(store: Store, nodes: list[str], policy: PredictorPolicy, tz: tzinfo | None = None) -> dict:
    """Refit every node in `nodes` (node keys of enrolled, unrevoked devices); drop rows for any other node."""
    at = now()
    since = at - timedelta(days=policy.window_days)
    fitted = fit(store, nodes, policy, since, None, tz)
    with store.tx() as c:
        keep = set(nodes)
        gone = [r["node"] for r in c.execute("SELECT node FROM node_availability").fetchall() if r["node"] not in keep]
        c.executemany("DELETE FROM node_availability WHERE node = ?", [(n,) for n in gone])
        c.executemany("INSERT INTO node_availability (node, counts, samples, window_from, updated_at) VALUES (?,?,?,?,?)"
                      " ON CONFLICT(node) DO UPDATE SET counts = excluded.counts, samples = excluded.samples,"
                      " window_from = excluded.window_from, updated_at = excluded.updated_at",
                      [(n, _dump(cnt), k, iso(since), iso(at)) for n, (cnt, k) in fitted.items()])
    summary = {"nodes": len(fitted), "samples": sum(k for _, k in fitted.values()), "removed": len(gone)}
    obs.log(log, logging.INFO, "availability predictor refitted", **summary)
    return summary


class Predictor:
    """The fitted counts in memory. Rebuilt from the store after each `recompute` (`reload`)."""

    def __init__(self, counts: dict[str, Counts], policy: PredictorPolicy, tz: tzinfo | None = None,
                 updated_at: str | None = None):
        self.counts, self.policy, self.tz, self.updated_at = counts, policy, tz, updated_at
        self._fleet = {m: model.fleet_rates(list(counts.values()), m) for m in policy.horizons_min}

    @classmethod
    def from_store(cls, store: Store, policy: PredictorPolicy, tz: tzinfo | None = None) -> "Predictor":
        rows = store.all("SELECT node, counts, updated_at FROM node_availability")
        return cls({r["node"]: _load(r["counts"]) for r in rows}, policy, tz,
                   max((r["updated_at"] for r in rows), default=None))

    def horizon(self, minutes: int | None) -> int:
        """The configured horizon at or above `minutes` (the longest if none is)."""
        hs = sorted(self.policy.horizons_min)
        if minutes is None:
            return self.policy.default_horizon_min if self.policy.default_horizon_min in hs else hs[0]
        return next((h for h in hs if h >= minutes), hs[-1])

    def estimate(self, node: str, at: datetime | None = None, horizon_min: int | None = None) -> Estimate:
        h = self.horizon(horizon_min)
        return model.estimate(self.counts.get(node) or Counts(), model.hour_of_week(at or now(), self.tz), h,
                              self._fleet[h], self.policy.prior_strength)

    def availability(self, node: str, node_state: str | None, at: datetime | None = None,
                     horizon_min: int | None = None) -> tuple[float, Estimate]:
        """The placement term for a node in its current state (S6.3), with the estimate behind it."""
        e = self.estimate(node, at, horizon_min)
        return model.availability(e, None if node_state is None else node_state in AVAILABLE_STATES), e

    def week(self, node: str, horizon_min: int | None = None) -> list[dict]:
        """The node's 168 hourly estimates, Monday 00:00 first."""
        h = self.horizon(horizon_min)
        c = self.counts.get(node) or Counts()
        return [model.estimate(c, i, h, self._fleet[h], self.policy.prior_strength).as_dict() for i in range(model.HOURS)]
