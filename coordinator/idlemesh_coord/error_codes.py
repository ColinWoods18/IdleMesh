"""The shared error-code catalog (contracts/error-codes.json), loaded once.

The catalog is the single source of truth for codes used by the coordinator and the agent (D-11).
Tests check that every code literal in the coordinator's source is catalogued.
"""
import json
from functools import lru_cache
from pathlib import Path

# Installed from the repository (editable install) the catalog sits two levels above this package.
_CATALOG = Path(__file__).resolve().parents[2] / "contracts" / "error-codes.json"


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict]:
    data = json.loads(_CATALOG.read_text(encoding="utf-8"))
    return {c["code"]: c for c in data["codes"]}


def is_known(code: str) -> bool:
    return code in catalog()
