"""Run the coordinator for development.

Defaults: loopback only, fake runtime. The database and the coordinator's tunnel certificate live in
~/.local/share/difa/ so they stay out of synced folders.

  --llamacpp         use the llama.cpp adapter with the Phase 1 build and models in ~/.cache/difa-phase1
                     (this Mac's own stage is "Mac Studio" unless DIFA_LLAMACPP_LOCAL_DEVICE says otherwise)
  --host ADDRESS     listen on a LAN address so real devices can enroll. The control channel is still
                     plain HTTP with bearer tokens in this prototype: use only on a network you trust.
  --model-source coordinator   devices prefetch their weights from this coordinator instead of the
                     model's origin (good for a LAN; FR 30)

Usage: python scripts/dev_server.py [--port 8765] [--fresh] [--llamacpp] [--host 192.168.1.10]
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coordinator"))

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--fresh", action="store_true", help="start from an empty development database")
parser.add_argument("--llamacpp", action="store_true", help="use the llama.cpp adapter (Phase 1 build)")
parser.add_argument("--host", default="127.0.0.1", help="listen address (default loopback only)")
parser.add_argument("--model-source", choices=["origin", "coordinator"], default=None)
args = parser.parse_args()

if args.llamacpp:
    phase1 = Path.home() / ".cache" / "difa-phase1"
    os.environ.setdefault("DIFA_RUNTIME", "llamacpp")
    os.environ.setdefault("DIFA_LLAMACPP_BIN", str(phase1 / "llama.cpp" / "build" / "bin"))
    os.environ.setdefault("DIFA_MODEL_DIR", str(phase1 / "models"))
    os.environ.setdefault("DIFA_LLAMACPP_LOCAL_DEVICE", "Mac Studio")
if args.model_source:
    os.environ["DIFA_MODEL_SOURCE"] = args.model_source
if args.host not in ("127.0.0.1", "::1", "localhost"):
    print(f"WARNING: listening on {args.host}. Agent control traffic is plain HTTP in this prototype; worker "
          "traffic is mutual TLS only for agents run with --tunnel-listen. Trusted networks only.", flush=True)

data = Path(os.environ.get("DIFA_DATA_DIR", Path.home() / ".local" / "share" / "difa"))
data.mkdir(parents=True, exist_ok=True)
db = data / "dev.sqlite3"
if args.fresh:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
os.environ.setdefault("DIFA_DB", str(db))

import uvicorn  # noqa: E402

uvicorn.run("difa.api.app:app_from_env", factory=True, host=args.host, port=args.port, log_level="info")
