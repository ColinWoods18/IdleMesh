"""Follow one request across the coordinator, agent and worker logs (D-11, sprint 1 demo).

    python scripts/trace.py REQUEST_ID coordinator.log node-a/logs/idlemesh-agent.log node-b/...

Prints, in time order, every JSON log line that names the request (``request_id``, or ``request_ids`` on
agent lines), every line that shares a ``trace_id`` with those, and, with ``--context``, the lines from the
same deployment during the request's lifetime (worker output and node events that carry no request ID).
Rotated files (``.log.1``, ``.log.2``...) can be passed too. Lines that are not JSON are skipped.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta


def read(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(line, dict) and "ts" in line:
                    line["_file"] = p
                    out.append(line)
    return out


def _ts(line: dict) -> datetime:
    return datetime.fromisoformat(line["ts"])


def select(lines: list[dict], request_id: str, context_s: float | None) -> list[dict]:
    named = [x for x in lines if x.get("request_id") == request_id or request_id in x.get("request_ids", [])]
    if not named:
        return []
    traces = {x["trace_id"] for x in named if x.get("trace_id")}
    chosen = {id(x) for x in named} | {id(x) for x in lines if x.get("trace_id") in traces}
    if context_s is not None:
        deployments = {x["deployment_id"] for x in named if x.get("deployment_id")}
        start = min(_ts(x) for x in named) - timedelta(seconds=context_s)
        end = max(_ts(x) for x in named) + timedelta(seconds=context_s)
        chosen |= {id(x) for x in lines if x.get("deployment_id") in deployments and start <= _ts(x) <= end}
    return sorted((x for x in lines if id(x) in chosen), key=_ts)


def show(line: dict) -> str:
    who = line.get("component", "?")
    if line.get("node_id") and who != "coordinator":
        who += f"@{line['node_id']}"
    code = f" [{line['error_code']}]" if line.get("error_code") else ""
    return f"{line['ts']}  {line.get('level', ''):5}  {who:28}  {line.get('message', '')}{code}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request_id")
    ap.add_argument("logs", nargs="+", help="coordinator and agent JSON log files")
    ap.add_argument("--context", type=float, metavar="SECONDS", nargs="?", const=5.0,
                    help="also show same-deployment lines from SECONDS before to SECONDS after (default 5)")
    ap.add_argument("--json", action="store_true", help="print the matching lines as JSON")
    args = ap.parse_args()
    found = select(read(args.logs), args.request_id, args.context)
    if not found:
        print(f"no log lines mention {args.request_id}", file=sys.stderr)
        return 1
    for line in found:
        line.pop("_file", None)
        print(json.dumps(line) if args.json else show(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
