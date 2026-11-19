"""Generate docs/error-codes.md from contracts/error-codes.json.

    python scripts/gen_error_codes.py          # write the file
    python scripts/gen_error_codes.py --check  # exit 1 if the file is out of date (used by tests and CI)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "contracts" / "error-codes.json"
TARGET = ROOT / "docs" / "error-codes.md"

AREAS = [
    ("api", "Request handling"),
    ("auth", "Sign-in and permissions"),
    ("node", "Nodes and the agent"),
    ("worker", "Workers and the tunnel"),
    ("model", "Models"),
    ("deploy", "Deployments, placement and prefetch"),
    ("queue", "Queue and generation"),
    ("chat", "Conversations"),
]


def render() -> str:
    catalog = json.loads(SOURCE.read_text(encoding="utf-8"))
    out = [
        "# Error codes",
        "",
        "<!-- Generated from contracts/error-codes.json by scripts/gen_error_codes.py. Do not edit by hand. -->",
        "",
        "Every error that IdleMesh returns or logs carries one of these stable codes (D-11). The same codes",
        "are used by the coordinator (API responses and JSON logs) and the agent (JSON logs and reports), so",
        "one code can be followed across components and counted in metrics.",
        "",
        "- **HTTP** is the status the coordinator's API returns with that code; `—` means the code appears",
        "  only in logs, events or agent reports.",
        "- **Retry** says whether trying again later can succeed without anything changing.",
        "- To add a code: edit `contracts/error-codes.json`, then run `python scripts/gen_error_codes.py`.",
        "  Tests fail if code uses a code that is not in the catalog, or if this file is out of date.",
        "  Never reuse or change the meaning of a released code; retire it instead.",
        "",
    ]
    for area, title in AREAS:
        rows = [c for c in catalog["codes"] if c["area"] == area]
        if not rows:
            continue
        out += [f"## {title}", "", "| Code | HTTP | Retry | Meaning |", "|---|---|---|---|"]
        for c in sorted(rows, key=lambda c: c["code"]):
            http = str(c["http"]) if c["http"] is not None else "—"
            out.append(f"| `{c['code']}` | {http} | {'yes' if c['retryable'] else 'no'} | {c['meaning']} |")
        out.append("")
    unknown = {c["area"] for c in catalog["codes"]} - {a for a, _ in AREAS}
    if unknown:
        raise SystemExit(f"unknown areas in the catalog: {sorted(unknown)}")
    return "\n".join(out)


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET.relative_to(ROOT)} is out of date: run python scripts/gen_error_codes.py")
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
