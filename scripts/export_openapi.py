"""Write the coordinator's OpenAPI contract to contracts/http/openapi.json (spec 9.2, Phase 0)."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coordinator"))

from idlemesh_coord.api.app import create_app  # noqa: E402
from idlemesh_coord.config import Settings  # noqa: E402

# Windows cannot delete the SQLite file while the app still holds it open; skip that cleanup error.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    app = create_app(Settings(db_path=os.path.join(tmp, "contract.sqlite3")))
    spec = app.openapi()

out = ROOT / "contracts" / "http" / "openapi.json"
out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
print(f"wrote {out.relative_to(ROOT)}: {len(spec['paths'])} paths, OpenAPI {spec['openapi']}")
