"""The error-code catalog is complete and the generated doc is current (S1.8)."""
import re
import subprocess
import sys
from pathlib import Path

from idlemesh_coord.error_codes import catalog

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "coordinator" / "idlemesh_coord"

# Upper-case string literals that are not error codes.
NOT_CODES = {"POST", "PATCH", "DELETE", "PUT", "GET", "UNSAFE", "GGUF", "COMMIT", "ROLLBACK", "TERMINAL",
             "MTL0"}  # llama.cpp device name
LITERAL = re.compile(r"""["']([A-Z][A-Z0-9_]{2,})["']""")


def _literals() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in SOURCE.rglob("*.py"):
        for m in LITERAL.finditer(path.read_text(encoding="utf-8")):
            word = m.group(1)
            if word in NOT_CODES or word.startswith("IDLEMESH_"):
                continue
            found.setdefault(word, []).append(path.relative_to(ROOT).as_posix())
    return found


def test_every_code_used_by_the_coordinator_is_catalogued():
    missing = {code: files for code, files in _literals().items() if code not in catalog()}
    assert not missing, f"add these to contracts/error-codes.json: {missing}"


def test_catalog_entries_are_well_formed():
    areas = {"api", "auth", "node", "worker", "model", "deploy", "queue", "chat"}
    for code, c in catalog().items():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", code)
        assert c["area"] in areas and isinstance(c["retryable"], bool) and c["meaning"].endswith((".", ".)"))
        assert c["http"] is None or 400 <= c["http"] <= 599


def test_generated_doc_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_error_codes.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
