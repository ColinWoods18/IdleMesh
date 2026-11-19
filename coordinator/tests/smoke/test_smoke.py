"""Smoke tests: is the coordinator up, and does its main path work end to end? Fast, run first.

    python -m pytest -m smoke                 # just these (about 10 s)
    IDLEMESH_SMOKE_URL=https://host:8765 IDLEMESH_SMOKE_CA=path/to/coordinator-control.crt python -m pytest -m smoke
                                              # also check a coordinator that is already running elsewhere

test_in_process walks the main path inside pytest. test_served starts the coordinator as a real uvicorn
process on a free loopback port with a throwaway database, so it proves the server starts and answers
over HTTP on whichever machine runs it (Windows, macOS, Linux). Neither touches the development database.
The fake runtime is used: this checks product contracts, not distributed inference.
"""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import GB, OWNER_PASS

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[3]
CAPS = {"os": "test", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
        "allocatable_bytes": 8 * GB, "mem_bandwidth_gbps": 50, "rtt_ms": 2, "power": "ac"}
CONSENT = {"idle_data": True, "diagnostics": True, "policy_version": "v0"}


def _sample() -> dict:
    return {"kind": "idle", "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "seconds_since_input": 900, "session_locked": True, "cpu_util": 0.02, "mem_free_gb": 6.0,
            "on_battery": False, "battery_pct": None, "state": "idle"}


def walk(http) -> None:
    """The main path, over any httpx-compatible client: group, sign-in, device, telemetry, deployment, chat.

    Each step asserts with the response body in the message, so a failure says what the server answered.
    """
    r = http.get("/openapi.json")
    assert r.status_code == 200 and "/api/v1/requests" in r.json()["paths"], r.text[:300]

    r = http.post("/api/v1/groups", json={"name": "Smoke", "owner_login": "owner", "owner_display_name": "Owner",
                                         "passphrase": OWNER_PASS})
    assert r.status_code == 201, r.text
    gid = r.json()["group_id"]
    r = http.post("/api/v1/auth/login", json={"group_id": gid, "login": "owner", "passphrase": OWNER_PASS})
    assert r.status_code == 200, r.text
    sid = r.cookies.get("idlemesh_session")
    http.cookies.clear()
    owner = {"Cookie": f"idlemesh_session={sid}", "X-CSRF-Token": r.json()["csrf_token"]}
    assert http.get("/api/v1/auth/session", headers=owner).status_code == 200

    # One device, enrolled with consent, heartbeating and reporting telemetry.
    code = http.post("/api/v1/devices/enrollment-codes", headers=owner).json()["code"]
    r = http.post("/api/v1/devices/enroll", json={"code": code, "name": "Smoke PC", "public_key": "k",
                                                 "capabilities": CAPS, "policy": {"memory_cap_bytes": 6 * GB},
                                                 "consent": CONSENT})
    assert r.status_code == 201, r.text
    dev = r.json()
    bearer = {"Authorization": f"Bearer {dev['device_token']}"}
    r = http.post(f"/api/v1/devices/{dev['device_id']}/heartbeat", headers=bearer,
                  json={"capabilities": CAPS, "node_state": "idle"})
    assert r.status_code == 200 and r.json()["telemetry"]["interval_s"] > 0, r.text
    r = http.post(f"/api/v1/devices/{dev['device_id']}/telemetry", headers=bearer,
                  json={"schema_version": 1, "records": [_sample()]})
    assert r.status_code == 200 and r.json() == {"accepted": 1, "duplicates": 0}, r.text
    t = http.get(f"/api/v1/groups/{gid}/devices/{dev['device_id']}/idle", headers=owner).json()
    assert len(t["samples"]) == 1 and t["samples"][0]["state"] == "idle", t

    # Deploy the small fixture model on that device and stream one answer.
    r = http.post(f"/api/v1/groups/{gid}/deployment", headers=owner, json={"model": "fixture-small"})
    assert r.status_code == 200, r.text
    r = http.post("/api/v1/requests", headers=owner,
                  json={"model": "group-default", "visibility": "private", "max_output_tokens": 16,
                        "messages": [{"role": "user", "content": "smoke test"}]})
    assert r.status_code == 202, r.text
    with http.stream("GET", r.json()["events_url"], headers=owner) as s:
        events = [json.loads(line[6:]) for line in s.iter_lines() if line.startswith("data: ")]
    assert events and events[-1]["type"] == "completed", events[-3:]
    assert any(e["type"] == "token" for e in events)

    # The OpenAI-compatible endpoint with an API key.
    key = http.post("/api/v1/credentials", headers=owner, json={"name": "smoke"}).json()["secret"]
    r = http.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                  json={"model": "group-default", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"], r.text


def test_in_process(client):
    walk(client)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(tmp_path):
    """The coordinator as its own process, on loopback, with a throwaway database. Yields its base URL."""
    port = _free_port()
    env = {**os.environ, "IDLEMESH_DB": str(tmp_path / "smoke.sqlite3"), "IDLEMESH_ARGON2_FAST": "1",
           "IDLEMESH_FAKE_TOKEN_DELAY": "0.001", "PYTHONPATH": str(ROOT / "coordinator"), "PYTHONUTF8": "1"}
    for var in ("IDLEMESH_SITE_CONFIG", "IDLEMESH_RUNTIME", "IDLEMESH_TUNNEL_DIR", "IDLEMESH_CONTROL_CERT"):
        env.pop(var, None)
    log = open(tmp_path / "server.log", "w+")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "idlemesh_coord.api.app:app_from_env", "--factory",
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                log.seek(0)
                pytest.fail(f"coordinator exited with {proc.returncode} during startup:\n{log.read()[-3000:]}")
            try:
                if httpx.get(url + "/openapi.json", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                log.seek(0)
                pytest.fail(f"coordinator did not answer within 30 s:\n{log.read()[-3000:]}")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def test_served(served):
    with httpx.Client(base_url=served, timeout=30) as http:
        walk(http)


@pytest.mark.skipif(not os.environ.get("IDLEMESH_SMOKE_URL"), reason="IDLEMESH_SMOKE_URL not set")
def test_remote():
    """A coordinator already running elsewhere (a dev box over SSH, a site server). Creates a 'Smoke' group there."""
    verify = os.environ.get("IDLEMESH_SMOKE_CA") or True
    with httpx.Client(base_url=os.environ["IDLEMESH_SMOKE_URL"].rstrip("/"), timeout=60, verify=verify) as http:
        walk(http)
