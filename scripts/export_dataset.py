"""Export an anonymised research dataset from a coordinator database (S6.5). Columns: docs/telemetry-schema.md.

    python scripts/export_dataset.py --db ~/.local/share/idlemesh/coordinator.sqlite3 --out dataset/ \\
        --seed 2026-12 [--from 2026-11-01] [--to 2026-12-01]

What leaves the coordinator:
- Idle samples and state changes from nodes whose owner's current consent has `idle_data`, and network figures
  from nodes whose consent has `diagnostics`. Revoked devices and nodes without consent are left out.
- Nodes renamed `node_01`, `node_02`, ... in an order set by `--seed`. The coordinator's node keys, device IDs,
  names, org and site IDs and receive times are not exported. Without the seed the aliases cannot be linked
  back to node keys; keep it private.
- Timestamps in UTC to the second, so hour-of-week analysis works.

Reproducible: the same database, seed and range give byte-identical files. `manifest.json` lists the
parameters, row counts and SHA-256 of each file. The database is copied before reading, so the export never
changes it (the coordinator can keep running).
"""
import argparse
import csv
import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SALT_FILE = "telemetry-salt"  # idlemesh_coord.telemetry.SALT_FILE; read only, never created here
FORMAT = "idlemesh-dataset/1"

TABLES = {
    "idle_samples.csv": ("idle_data", "SELECT node, ts, seconds_since_input, session_locked, cpu_util, gpu_util,"
                         " mem_free_gb, on_battery, battery_pct, state, contribution_level, level_reason"
                         " FROM idle_samples WHERE ts >= ? AND ts < ? ORDER BY node, ts"),
    "node_events.csv": ("idle_data", "SELECT node, ts, kind, from_state, to_state FROM node_events"
                        " WHERE ts >= ? AND ts < ? ORDER BY node, ts, kind, to_state"),
    "net_edges.csv": ("diagnostics", "SELECT src AS node, dst AS peer, ts, method, rtt_ms_p50, rtt_ms_p95,"
                      " bandwidth_mbps, samples FROM net_edges WHERE ts >= ? AND ts < ? ORDER BY src, ts, dst, method"),
}


def node_keys(db_dir: Path, conn: sqlite3.Connection) -> dict[str, dict]:
    """Node key -> {"idle_data", "diagnostics"} for every unrevoked device, from its current consent."""
    salt_path = db_dir / SALT_FILE
    salt = bytes.fromhex(salt_path.read_text().strip()) if salt_path.exists() else None
    out = {}
    for d in conn.execute("SELECT id, consent FROM devices WHERE status != 'revoked'"):
        key = "n_" + hmac.new(salt, d["id"].encode(), hashlib.sha256).hexdigest()[:24] if salt else d["id"]
        consent = json.loads(d["consent"] or "{}")
        out[key] = {"idle_data": bool(consent.get("idle_data")), "diagnostics": bool(consent.get("diagnostics"))}
    return out


def aliases(keys: list[str], seed: str) -> dict[str, str]:
    order = sorted(keys, key=lambda k: hmac.new(seed.encode(), k.encode(), hashlib.sha256).hexdigest())
    width = max(2, len(str(len(order))))
    return {k: f"node_{i + 1:0{width}d}" for i, k in enumerate(order)}


def _utc(text: str | None, default: str) -> str:
    if not text:
        return default
    d = datetime.fromisoformat(text)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _second(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def export(db: Path, out: Path, seed: str, start: str | None = None, end: str | None = None) -> dict:
    lo, hi = _utc(start, "0001-01-01T00:00:00+00:00"), _utc(end, "9999-12-31T00:00:00+00:00")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.sqlite3"
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        try:
            consent = node_keys(db.parent, conn)
            names = aliases(list(consent), seed)
            files = {}
            for name, (needs, sql) in TABLES.items():
                rows = [r for r in conn.execute(sql, (lo, hi)) if consent.get(r["node"], {}).get(needs)]
                cols = list(rows[0].keys()) if rows else [c[0] for c in conn.execute(sql, (lo, hi)).description]
                path = out / name
                with path.open("w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f, lineterminator="\n")
                    w.writerow(cols)
                    for r in rows:
                        rec = dict(r)
                        rec["node"] = names[rec["node"]]
                        if "peer" in rec:
                            rec["peer"] = names.get(rec["peer"], "coordinator" if rec["peer"] == "coordinator"
                                                    else "other")
                        rec["ts"] = _second(rec["ts"])
                        w.writerow(["" if rec[c] is None else rec[c] for c in cols])
                files[name] = {"rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            conn.close()
    manifest = {"format": FORMAT, "schema_version": version, "from": lo, "to": hi,
                "seed_sha256": hashlib.sha256(seed.encode()).hexdigest(), "nodes": len(names), "files": files,
                "schema": "docs/telemetry-schema.md"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path, help="the coordinator's SQLite database")
    ap.add_argument("--out", required=True, type=Path, help="directory for the CSV files and manifest.json")
    ap.add_argument("--seed", required=True, help="sets the node aliases; the same seed gives the same aliases")
    ap.add_argument("--from", dest="start", help="first time to include (ISO 8601, UTC if no offset)")
    ap.add_argument("--to", dest="end", help="first time to leave out")
    a = ap.parse_args(argv)
    if not a.db.exists():
        print(f"no database at {a.db}", file=sys.stderr)
        return 2
    m = export(a.db.expanduser(), a.out, a.seed, a.start, a.end)
    print(json.dumps({k: m[k] for k in ("nodes", "files")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
