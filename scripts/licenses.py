"""Generate docs/licenses.md: every dependency with its licence and what that licence asks of us (S1.10).

    python scripts/licenses.py           # needs cargo on PATH and the coordinator installed in this Python
    python scripts/licenses.py --check   # exit 1 if any dependency's licence is not on the approved list

Sources: the Rust lockfile (cargo metadata, all platforms) and the Python packages the coordinator
needs at run time, with their transitive dependencies, read from the installed package metadata.
Hand-written sections cover what the generator cannot see: llama.cpp, models, and the observability
stack that is planned but not yet shipped.
"""
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "licenses.md"

# Licences that let us ship and sell IdleMesh with notices kept (no copyleft on our code).
PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Zlib", "Unicode-3.0", "Unlicense",
              "BSL-1.0", "CDLA-Permissive-2.0", "PSF-2.0", "0BSD", "MIT-0", "Apache-2.0 WITH LLVM-exception", "MPL-2.0"}
# MPL-2.0 is file-level copyleft: fine to ship unmodified; changes to those files must be shared.
OBLIGATIONS = {
    "MIT": "Keep the copyright and licence notice.",
    "Apache-2.0": "Keep notices and any NOTICE file; state changes to modified files; patent grant.",
    "BSD-2-Clause": "Keep the copyright and licence notice.",
    "BSD-3-Clause": "Keep notices; do not use the authors' names to endorse the product.",
    "ISC": "Keep the copyright and licence notice.",
    "Zlib": "Keep the notice in source; do not misrepresent the origin.",
    "Unicode-3.0": "Keep the copyright and permission notice.",
    "Unlicense": "None (public-domain dedication).",
    "BSL-1.0": "Keep the licence text with source distributions.",
    "CDLA-Permissive-2.0": "Keep the licence text with the data (Mozilla's CA root list in webpki-roots).",
    "PSF-2.0": "Keep the copyright and licence notice.",
    "MPL-2.0": "Ship unmodified, or publish changes to the MPL-covered files.",
    "0BSD": "None.",
    "MIT-0": "None (MIT without the attribution requirement).",
}


def _choose(expr: str) -> str:
    """The licence we rely on from an SPDX expression: a permissive option of an OR, or the whole AND."""
    expr = expr.replace("/", " OR ").strip()
    if " OR " in expr and " AND " not in expr:
        options = [o.strip("() ") for o in expr.split(" OR ")]
        for preferred in ("MIT", "Apache-2.0", "BSD-3-Clause", "ISC"):
            if preferred in options:
                return preferred
        return next((o for o in options if o in PERMISSIVE), options[0])
    return expr


def _parts(chosen: str) -> list[str]:
    return [p.strip("() ") for p in re.split(r"\s+AND\s+|\s+OR\s+", chosen) if p.strip("() ")]


def rust() -> list[dict]:
    out = subprocess.run(["cargo", "metadata", "--format-version", "1", "--locked"], cwd=ROOT / "agent",
                         capture_output=True, text=True, check=True).stdout
    m = json.loads(out)
    ids = {n["id"] for n in m["resolve"]["nodes"]}
    rows = []
    for p in m["packages"]:
        if p["id"] in ids and p["name"] != "idlemesh-agent":
            lic = p.get("license") or "UNKNOWN"
            rows.append({"name": p["name"], "version": p["version"], "license": lic, "chosen": _choose(lic)})
    return sorted(rows, key=lambda r: r["name"])


def _py_license(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split(" :: ")[-1] for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    mapping = {"MIT License": "MIT", "Apache Software License": "Apache-2.0", "BSD License": "BSD-3-Clause",
               "ISC License (ISCL)": "ISC", "Python Software Foundation License": "PSF-2.0",
               "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0"}
    for c in classifiers:
        if c in mapping:
            return mapping[c]
    raw = (md.get("License") or "").strip().splitlines()
    return raw[0][:40] if raw else "UNKNOWN"


def python() -> list[dict]:
    seen: dict[str, dict] = {}
    todo = ["idlemesh-coordinator"]
    while todo:
        name = todo.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        if key != "idlemesh-coordinator":
            lic = _py_license(dist)
            seen[key] = {"name": dist.metadata["Name"], "version": dist.version, "license": lic,
                         "chosen": _choose(lic)}
        else:
            seen[key] = {}
        for req in dist.requires or []:
            if "extra ==" in req:
                continue  # optional extras are not installed for the coordinator
            todo.append(re.split(r"[\s;<>=!~\[(]", req, maxsplit=1)[0])
    return sorted((v for v in seen.values() if v), key=lambda r: r["name"].lower())


def problems(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not all(p in PERMISSIVE for p in _parts(r["chosen"]))]


def render(rs: list[dict], py: list[dict]) -> str:
    def table(rows):
        lines = ["| Package | Version | Licence (as declared) | We rely on |", "|---|---|---|---|"]
        lines += [f"| {r['name']} | {r['version']} | {r['license']} | {r['chosen']} |" for r in rows]
        return lines

    used = sorted({p for r in rs + py for p in _parts(r["chosen"])})
    bad = problems(rs + py)
    out = [
        "# Licences",
        "",
        "<!-- Dependency tables are generated by scripts/licenses.py. Edit the hand-written sections in the script. -->",
        "",
        "First-pass licence inventory (sprint 1, S1.10). It lists everything IdleMesh depends on, which",
        "licence applies, and what that licence asks of us. IdleMesh may be sold later (PROJECT_PLAN Phase 4),",
        "so shipped components must be permissively licensed with their notices kept.",
        "",
        "**Not legal advice.** Before any commercial release, the university's tech-transfer office should",
        "review this file, together with the IP questions in PROJECT_PLAN Phase 4.",
        "",
        "## Summary",
        "",
        f"- **Rust agent:** {len(rs)} crates (all platforms in the lockfile).",
        f"- **Python coordinator:** {len(py)} packages needed at run time.",
        f"- **Non-permissive dependencies:** {len(bad)}" + (". " if not bad else ": see below. ") +
        "Where a crate offers a choice (\"MIT OR Apache-2.0\"), we rely on the permissive option shown.",
        "",
        "### What the licences in use ask of us",
        "",
        "| Licence | Obligation when we ship it |",
        "|---|---|",
        *[f"| {lic} | {OBLIGATIONS.get(lic, 'Review before release.')} |" for lic in used],
        "",
        "In practice: the installer and the About screen must carry a third-party notices file with these",
        "copyright notices and licence texts (to build in sprint 10 with the installers, D-07).",
        "",
        "## Our own code",
        "",
        "| Component | Licence now | Note |",
        "|---|---|---|",
        "| DIFA code inherited from Michael Todd | `UNLICENSED` (agent `Cargo.toml`), none stated elsewhere | Needs a written assignment or licence before any commercial step (PROJECT_PLAN Phase 4). |",
        "| IdleMesh code | Not yet chosen | University IP policy decides; see Phase 4. |",
        "",
        "## Bundled and external components (hand-written)",
        "",
        "| Component | Licence | How it is used | Obligation and risk |",
        "|---|---|---|---|",
        "| llama.cpp and ggml (pinned build) | MIT | Worker processes (`rpc-server`, `llama-server`); later patched for the stage engine (D-09) | Keep the MIT notice; publish nothing extra. Our patch can stay private or go upstream. |",
        "| Models (GGUF) | Per model: Apache-2.0, MIT, Llama Community, Gemma terms, ... | Supplied by customers through the Ollama-style interface (D-10); none bundled | The customer accepts each model's licence when adding it; the pilot's models are chosen and checked by FAIRlab. |",
        "| NVIDIA CUDA runtime (if bundled for GPU builds) | NVIDIA EULA, redistributable components | GPU worker builds (sprint 10) | Ship only the redistributable libraries; keep the EULA notice. |",
        "| Prometheus | Apache-2.0 | Planned metrics store (D-11) | Permissive. |",
        "| OpenTelemetry SDKs | Apache-2.0 | Planned tracing (D-11) | Permissive. |",
        "| Grafana, Loki, Tempo | AGPL-3.0 | Dashboards and log storage (sprint 3) | **Do not ship or modify for customers.** Fine to run unmodified for our own campus deployment; for the product, use a permissive alternative or have customers install them separately. |",
        "| TimescaleDB | Apache-2.0 (core) / Timescale License (TSL) | Possible telemetry database | Use the Apache-2.0 edition only, or plain PostgreSQL. |",
        "",
    ]
    if bad:
        out += ["## Needs review", "", *table(bad), ""]
    out += ["## Rust crates (agent)", "", *table(rs), "", "## Python packages (coordinator)", "", *table(py), ""]
    return "\n".join(out)


def main() -> int:
    rs, py = rust(), python()
    bad = problems(rs + py)
    if "--check" in sys.argv:
        for r in bad:
            print(f"needs review: {r['name']} {r['version']}: {r['license']}")
        return 1 if bad else 0
    TARGET.write_text(render(rs, py), encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}: {len(rs)} crates, {len(py)} Python packages, {len(bad)} to review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
