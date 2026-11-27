"""A synthetic fleet of idle samples, for tests and for the evaluation notebook before real data exists (S6.2).

Not a model of any real workstation. Each kind of node follows a weekly routine with random breaks, so an
hour-of-week predictor has something to learn and something it cannot:

- `office`: in use on weekdays from about 09:00 to 17:00, with a lunch break and short breaks; idle otherwise.
- `lab`: random sessions of 30 to 180 minutes, more often in the afternoon on weekdays.
- `server`: idle nearly all the time, with a rare maintenance login.
- `laptop`: like `office`, but asleep (no samples) on most nights and weekends.
"""
import random
from datetime import datetime, timedelta, tzinfo

from ..store import Store, iso

KINDS = ("office", "lab", "server", "laptop")


def _busy(kind: str, local: datetime, rng: random.Random, state: dict) -> str | None:
    """'active', 'idle', or None (asleep) for one minute."""
    wd, h = local.weekday(), local.hour + local.minute / 60
    if state.get("until") and local < state["until"]:
        return state["now"]
    weekday = wd < 5
    if kind in ("office", "laptop"):
        start, stop = 9 + state["late"], 17 + state["late"]
        working = weekday and start <= h < stop and not (12.25 <= h < 13)
        if kind == "laptop" and not (weekday and 8 <= h < 19):
            return None
        if working:
            s = "idle" if rng.random() < 0.02 else "active"  # a coffee break
            state.update(now=s, until=local + timedelta(minutes=rng.randint(5, 20) if s == "idle" else 1))
            return s
        return "idle"
    if kind == "lab":
        rate = (0.012 if 13 <= h < 18 else 0.006 if 9 <= h < 13 else 0.001) if weekday else 0.0005
        if rng.random() < rate:
            state.update(now="active", until=local + timedelta(minutes=rng.randint(30, 180)))
            return "active"
        return "idle"
    if rng.random() < 0.0003:  # server
        state.update(now="active", until=local + timedelta(minutes=rng.randint(5, 30)))
        return "active"
    return "idle"


def fleet(store: Store, start: datetime, days: int, kinds: dict[str, str], seed: int = 1,
          tz: tzinfo | None = None, interval_s: int = 60) -> int:
    """Write `days` of one-minute samples from `start` for each node in `kinds` ({node: kind}). Returns rows."""
    rng = random.Random(seed)
    rows = 0
    for node, kind in sorted(kinds.items()):
        state: dict = {"late": rng.choice([-0.5, 0, 0, 0.5, 1])}
        batch = []
        for i in range(days * 24 * 3600 // interval_s):
            ts = start + timedelta(seconds=i * interval_s)
            local = ts.astimezone(tz)
            if local.hour == 0 and local.minute == 0:
                state["late"] = rng.choice([-0.5, 0, 0, 0.5, 1])  # a new day, a new start time
            s = _busy(kind, local, rng, state)
            if s is None:
                continue
            batch.append((node, iso(ts), 900 if s == "idle" else 2, 0, 0.05, 8.0, 0, None, s, iso(ts)))
        with store.tx() as c:
            c.executemany("INSERT OR IGNORE INTO idle_samples (node, ts, seconds_since_input, session_locked, cpu_util,"
                          " mem_free_gb, on_battery, battery_pct, state, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          batch)
        rows += len(batch)
    return rows
