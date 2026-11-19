"""Agent control channel over HTTPS with the coordinator certificate pinned at enrollment (spec 6.4).

A real coordinator serves the API over TLS with its self-signed control certificate; a real agent must
refuse to send its one-time code to a server with any other certificate, pin the full fingerprint on
success, heartbeat over the pinned channel, and refuse plain HTTP to a non-loopback coordinator.
"""
import json
import ssl
import subprocess
import threading

import httpx
import pytest
import uvicorn
from live import AGENT, PASS, free_port, until

from idlemesh_coord.api.app import create_app
from idlemesh_coord.config import CONTROL_KEEP_ALIVE_S, Settings
from idlemesh_coord.runtime.fake import FakeRuntime
from idlemesh_coord.transport import CoordinatorIdentity

pytestmark = pytest.mark.skipif(not AGENT.exists(), reason="agent binary not built")


@pytest.fixture
def tls_coordinator(tmp_path):
    control = CoordinatorIdentity(str(tmp_path / "data"), "coordinator-control", "DIFA coordinator control")
    impostor = CoordinatorIdentity(str(tmp_path / "impostor"), "coordinator-control", "DIFA coordinator control")
    port = free_port()
    app = create_app(Settings(db_path=str(tmp_path / "idlemesh.sqlite3"), control_cert=str(control.cert_path),
                              argon2_fast=True, cookie_secure=True), FakeRuntime(0))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           timeout_keep_alive=CONTROL_KEEP_ALIVE_S,
                                           ssl_certfile=str(control.cert_path), ssl_keyfile=str(control.key_path)))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    until(lambda: server.started, 20, "coordinator start")
    yield f"https://127.0.0.1:{port}", control, impostor
    server.should_exit = True
    thread.join(20)


def enroll(base: str, code: str, state, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(AGENT), "enroll", "--coordinator", base, "--code", code, "--name", "Ubuntu VM",
                           "--memory-cap-gb", "1", "--state", str(state), "--simulate", "ubuntu-vm",
                           *extra], capture_output=True, text=True, timeout=60)


def test_agent_pins_the_coordinator_certificate(tls_coordinator, tmp_path):
    base, control, impostor = tls_coordinator
    ctx = ssl.create_default_context(cafile=str(control.cert_path))
    ctx.check_hostname = False
    c = httpx.Client(base_url=base, timeout=60, verify=ctx)
    g = c.post("/api/v1/groups", json={"name": "TLS", "owner_login": "owner", "owner_display_name": "Owner",
                                       "passphrase": PASS}).json()
    info = c.post("/api/v1/auth/login", json={"group_id": g["group_id"], "login": "owner", "passphrase": PASS}).json()
    c.headers["X-CSRF-Token"] = info["csrf_token"]
    out = c.post("/api/v1/devices/enrollment-codes").json()
    assert out["coordinator_fingerprint"] == control.fingerprint
    state = tmp_path / "vm.json"

    # Wrong certificate: the agent fails the handshake and never sends the code.
    bad = enroll(base, out["code"], state, "--coordinator-fingerprint", impostor.fingerprint[:16])
    assert bad.returncode != 0 and "does not match the fingerprint" in bad.stderr, bad.stderr
    assert not state.exists()
    # No fingerprint for an https coordinator, and plain http to a network address: refused up front.
    assert "--coordinator-fingerprint" in enroll(base, out["code"], state).stderr
    lan = enroll("http://10.255.255.1:8765", out["code"], state, "--coordinator-fingerprint", "0" * 16)
    assert lan.returncode != 0 and "refusing plain HTTP" in lan.stderr, lan.stderr

    # Right certificate (16-digit prefix, in groups as people type it): the same code still works.
    typed = " ".join(control.fingerprint[i:i + 4] for i in range(0, 16, 4))
    ok = enroll(base, out["code"], state, "--coordinator-fingerprint", typed)
    assert ok.returncode == 0, ok.stderr
    saved = json.loads(state.read_text())
    assert saved["coordinator_fingerprint"] == control.fingerprint  # full 64 digits pinned from now on

    run = subprocess.run([str(AGENT), "run", "--state", str(state), "--beats", "3"], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0 and run.stdout.count("heartbeat ok") == 3, run.stdout + run.stderr
    # Reported RTT is the network's: server handling time is subtracted and kept connections avoid a TLS
    # handshake per heartbeat (Round 6 saw 83-191 ms on Wi-Fi without this).
    rtts = [float(line.rsplit("rtt=", 1)[1].rstrip("ms")) for line in run.stdout.splitlines() if "rtt=" in line]
    assert rtts and rtts[-1] < 20, rtts
    beat = c.post(f"/api/v1/devices/{saved['device_id']}/heartbeat",
                  headers={"Authorization": f"Bearer {saved['device_token']}"},
                  json={"capabilities": {"os": "linux", "arch": "x86_64", "backend_digest": "fake-runtime-0.1",
                                         "formats": ["gguf"], "allocatable_bytes": 10**9,
                                         "mem_bandwidth_gbps": 10, "rtt_ms": 1, "power": "ac"}})
    assert beat.status_code == 200 and beat.headers["server-timing"].startswith("app;dur=")
    assert "server-timing" not in c.get(f"/api/v1/groups/{g['group_id']}/devices").headers
    devs = c.get(f"/api/v1/groups/{g['group_id']}/devices").json()["devices"]
    assert [d["state"] for d in devs] == ["ready"]

    # A coordinator that later presents another certificate is not trusted with the device token.
    saved["coordinator_fingerprint"] = impostor.fingerprint
    state.write_text(json.dumps(saved))
    run = subprocess.run([str(AGENT), "run", "--state", str(state), "--beats", "1"], capture_output=True, text=True,
                         timeout=60)
    assert "heartbeat ok" not in run.stdout and "coordinator unreachable" in run.stdout, run.stdout
