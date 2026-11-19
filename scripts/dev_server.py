"""Run the coordinator for development.

Defaults: loopback only, fake runtime. The database and the coordinator's tunnel certificate live in
~/.local/share/difa/ so they stay out of synced folders.

  --llamacpp         use the llama.cpp adapter with the Phase 1 build and models in ~/.cache/difa-phase1
                     (this Mac's own stage is "Mac Studio" unless IDLEMESH_LLAMACPP_LOCAL_DEVICE says otherwise)
  --host ADDRESS     listen on a LAN address so real devices can enroll.
  --insecure-http    serve plain HTTP instead of HTTPS (loopback only; tests and local tools)

The control API is served over HTTPS with a self-signed certificate kept in the data directory
(coordinator-control.crt). Its SHA-256 fingerprint is printed at startup and returned with every
enrollment code; agents refuse to enroll with a coordinator presenting any other certificate.
  --model-source coordinator   devices prefetch their weights from this coordinator instead of the
                     model's origin (good for a LAN; FR 30)

Usage: python scripts/dev_server.py [--port 8765] [--fresh] [--llamacpp] [--host 192.168.1.10]
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coordinator"))

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--fresh", action="store_true", help="start from an empty development database")
parser.add_argument("--llamacpp", action="store_true", help="use the llama.cpp adapter (Phase 1 build)")
parser.add_argument("--host", default="127.0.0.1", help="listen address (default loopback only)")
parser.add_argument("--model-source", choices=["origin", "coordinator"], default=None)
parser.add_argument("--insecure-http", action="store_true", help="plain HTTP; allowed only on loopback")
args = parser.parse_args()
loopback = args.host in ("127.0.0.1", "::1", "localhost")
if args.insecure_http and not loopback:
    sys.exit("--insecure-http is allowed only on loopback: device tokens would cross the network in clear text.")

if args.llamacpp:
    phase1 = Path.home() / ".cache" / "difa-phase1"
    os.environ.setdefault("IDLEMESH_RUNTIME", "llamacpp")
    os.environ.setdefault("IDLEMESH_LLAMACPP_BIN", str(phase1 / "llama.cpp" / "build" / "bin"))
    os.environ.setdefault("IDLEMESH_MODEL_DIR", str(phase1 / "models"))
    os.environ.setdefault("IDLEMESH_LLAMACPP_LOCAL_DEVICE", "Mac Studio")
if args.model_source:
    os.environ["IDLEMESH_MODEL_SOURCE"] = args.model_source
data = Path(os.environ.get("IDLEMESH_DATA_DIR", Path.home() / ".local" / "share" / "difa"))
data.mkdir(parents=True, exist_ok=True)
tls = {}
if not args.insecure_http:
    from idlemesh_coord.transport import CoordinatorIdentity, grouped  # noqa: E402

    control = CoordinatorIdentity(str(data), "coordinator-control", "DIFA coordinator control")
    os.environ["IDLEMESH_CONTROL_CERT"] = str(control.cert_path)
    os.environ.setdefault("IDLEMESH_COOKIE_SECURE", "1")
    tls = {"ssl_certfile": str(control.cert_path), "ssl_keyfile": str(control.key_path)}
    print(f"control API: https://{args.host}:{args.port}  certificate {control.cert_path}", flush=True)
    print(f"coordinator fingerprint (SHA-256): {grouped(control.fingerprint)}", flush=True)
if not loopback:
    print(f"Listening on {args.host}: devices enroll with https and the fingerprint above. Worker traffic is "
          "mutual TLS for agents run with --tunnel-listen.", flush=True)
db = data / "dev.sqlite3"
if args.fresh:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
os.environ.setdefault("IDLEMESH_DB", str(db))

import uvicorn  # noqa: E402

from idlemesh_coord.config import CONTROL_KEEP_ALIVE_S  # noqa: E402

# Keep idle connections open well past the 5-second heartbeat interval. With uvicorn's default (5 s) nearly
# every heartbeat paid a new TCP connection and TLS handshake (Phase 1 Round 6).
uvicorn.run("idlemesh_coord.api.app:app_from_env", factory=True, host=args.host, port=args.port, log_level="info",
            timeout_keep_alive=CONTROL_KEEP_ALIVE_S, **tls)
