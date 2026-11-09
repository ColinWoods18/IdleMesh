"""Helpers for tests that run a real coordinator over HTTP with real agent, worker, and llama.cpp processes."""
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn

from difa.api.app import create_app
from difa.config import Settings

WORK = Path.home() / ".cache" / "difa-phase1"
BIN = WORK / "llama.cpp" / "build" / "bin"
MODELS = WORK / "models"
MODEL_FILE = MODELS / "qwen2.5-0.5b-instruct-q8_0.gguf"
AGENT = Path.home() / ".cache" / "difa-target" / "debug" / "difa-agent"
DIGEST = "llama.cpp-v0.9.0"
PASS = "live integration test passphrase"
GB = 10**9

HAVE_STACK = AGENT.exists() and (BIN / "ggml-rpc-server").exists() and MODEL_FILE.exists()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def until(pred, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


@contextmanager
def coordinator(tmp_path, runtime):
    """Serve a coordinator on a free loopback port; yields (base_url, app)."""
    port = free_port()
    app = create_app(Settings(db_path=str(tmp_path / "difa.sqlite3")), runtime)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    until(lambda: server.started, 20, "coordinator start")
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(20)


def owner_client(base: str) -> tuple[httpx.Client, str]:
    c = httpx.Client(base_url=base, timeout=120)
    g = c.post("/api/v1/groups", json={"name": "Live", "owner_login": "owner", "owner_display_name": "Owner",
                                       "passphrase": PASS}).json()
    info = c.post("/api/v1/auth/login", json={"group_id": g["group_id"], "login": "owner", "passphrase": PASS}).json()
    c.headers["X-CSRF-Token"] = info["csrf_token"]
    return c, g["group_id"]


@contextmanager
def mac_heartbeat(c: httpx.Client, base: str, bandwidth: float = 159):
    """Enroll the coordinator's own Metal device and keep it heartbeating (it has no agent-run worker)."""
    code = c.post("/api/v1/devices/enrollment-codes").json()["code"]
    caps = {"os": "macos", "arch": "aarch64", "backend_digest": DIGEST, "formats": ["gguf"],
            "allocatable_bytes": 64 * GB, "mem_bandwidth_gbps": bandwidth, "rtt_ms": 0, "power": "ac"}
    mac = c.post("/api/v1/devices/enroll", json={"code": code, "name": "Mac Studio", "public_key": "k",
                                                 "capabilities": caps,
                                                 "policy": {"memory_cap_bytes": 64 * GB}}).json()
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            httpx.post(f"{base}/api/v1/devices/{mac['device_id']}/heartbeat", json={"capabilities": caps},
                       headers={"Authorization": f"Bearer {mac['device_token']}"}, timeout=10)
            stop.wait(2)
    threading.Thread(target=beat, daemon=True).start()
    try:
        yield mac
    finally:
        stop.set()


def enroll_agent(c: httpx.Client, base: str, tmp_path, name: str, profile: str, cap_gb: float = 1) -> Path:
    code = c.post("/api/v1/devices/enrollment-codes").json()["code"]
    state = tmp_path / f"{profile}.json"
    out = subprocess.run([str(AGENT), "enroll", "--coordinator", base, "--code", code, "--name", name,
                          "--memory-cap-gb", str(cap_gb), "--state", str(state), "--simulate", profile,
                          "--backend-digest", DIGEST], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return state


def start_agent(state: Path, log: Path, *extra: str) -> subprocess.Popen:
    return subprocess.Popen([str(AGENT), "run", "--state", str(state), "--worker-bin", str(BIN / "ggml-rpc-server"),
                             *extra], stdout=open(log, "w"), stderr=subprocess.STDOUT)


def stop_agents(agents) -> None:
    for p in agents:
        p.terminate()
    for p in agents:
        p.wait(15)


def all_ready(c: httpx.Client, gid: str, n: int, local: str = "Mac Studio"):
    """n devices ready, and every agent-managed worker reported running (enrollment alone is not enough)."""
    devs = c.get(f"/api/v1/groups/{gid}/devices").json()["devices"]
    return (len(devs) == n and all(d["state"] == "ready" and (d["worker_running"] or d["name"] == local)
                                   for d in devs) and devs)
