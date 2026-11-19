"""Print each node's idle timeline from the coordinator database (Sprint 2 demo).

One row per hour, one character per few minutes:

  I idle    a active    S serving    d draining    z asleep    (blank) no data

Usage: python scripts/idle_timeline.py [--db PATH] [--hours 24] [--device NAME] [--width 30]

The default database is the development server's (~/.local/share/difa/dev.sqlite3). Telemetry is stored
under pseudonymous node keys; device names are shown by recomputing each key with the site's salt, which
lives next to the database and never leaves this machine.
"""
import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coordinator"))

from idlemesh_coord.telemetry import SALT_FILE, NodeKeys  # noqa: E402

CHAR = {"idle": "I", "active": "a", "serving": "S", "draining": "d", "sleeping": "z"}


def sleep_spans(events: list[tuple[datetime, str]]) -> list[tuple[datetime, datetime]]:
    """Intervals that start with a change to sleeping and end at the next change."""
    spans = []
    for (t, to), nxt in zip(events, events[1:] + [(None, None)]):
        if to == "sleeping" and nxt[0]:
            spans.append((t, nxt[0]))
    return spans


def strip(samples: list[tuple[datetime, str]], spans, start: datetime, width: int, step: timedelta) -> str:
    out = []
    for i in range(width):
        a, b = start + i * step, start + (i + 1) * step
        states = Counter(s for t, s in samples if a <= t < b)
        if states:
            out.append(CHAR.get(states.most_common(1)[0][0], "?"))
        elif any(s < b and a < e for s, e in spans):
            out.append("z")
        else:
            out.append(" ")
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(Path.home() / ".local" / "share" / "difa" / "dev.sqlite3"))
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--device", help="only this device name")
    ap.add_argument("--width", type=int, default=30, help="characters per hour")
    args = ap.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"no database at {db_path}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    pseudonymized = (db_path.parent / SALT_FILE).exists()
    keys = NodeKeys(str(db_path.parent), pseudonymized)
    names = {keys.key(i): n for i, n in db.execute("SELECT id, name FROM devices")}
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    begin = end - timedelta(hours=args.hours)
    step = timedelta(hours=1) / args.width
    nodes = [r[0] for r in db.execute("SELECT DISTINCT node FROM idle_samples WHERE ts >= ?", (begin.isoformat(),))]
    if not nodes:
        print(f"no idle samples in the last {args.hours} h")
        return
    for node in nodes:
        name = names.get(node, node)
        if args.device and name != args.device:
            continue
        samples = [(datetime.fromisoformat(t), s) for t, s in db.execute(
            "SELECT ts, state FROM idle_samples WHERE node = ? AND ts >= ? ORDER BY ts", (node, begin.isoformat()))]
        events = [(datetime.fromisoformat(t), to) for t, to in db.execute(
            "SELECT ts, to_state FROM node_events WHERE node = ? AND ts >= ? ORDER BY ts", (node, begin.isoformat()))]
        dropped = db.execute("SELECT COALESCE(SUM(count), 0) FROM telemetry_drops WHERE node = ? AND at >= ?",
                             (node, begin.isoformat())).fetchone()[0]
        totals = Counter(s for _, s in samples)
        share = ", ".join(f"{s} {100 * n / len(samples):.0f}%" for s, n in totals.most_common())
        print(f"\n{name}  ({len(samples)} samples; {share}; {dropped} dropped)")
        spans = sleep_spans(events)
        for h in range(args.hours):
            hour = begin + timedelta(hours=h)
            row = strip(samples, spans, hour, args.width, step)
            if row.strip():
                print(f"  {hour.astimezone():%a %H:%M}  |{row}|")
    print("\n  I idle  a active  S serving  d draining  z asleep  (blank) no data")


if __name__ == "__main__":
    main()
