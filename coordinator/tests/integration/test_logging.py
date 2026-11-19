"""Logs through the real API: one request can be followed by its IDs, and nothing private is logged (S1.7, S1.9)."""
import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from conftest import MEMBER_PASS, OWNER_PASS
from idlemesh_coord import obs

PROMPT = "PROMPT-SENTINEL-5c1e what is my exam answer"


@pytest.fixture
def logs():
    """Every log record from the coordinator, as parsed JSON lines."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(obs.JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)

    def lines() -> list[dict]:
        h.flush()
        return [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
    lines.raw = lambda: buf.getvalue()
    yield lines
    root.removeHandler(h)
    root.setLevel(old_level)


def _chat(user):
    r = user.post("/api/v1/requests", json={"model": "group-default", "visibility": "private",
                                            "messages": [{"role": "user", "content": PROMPT}],
                                            "max_output_tokens": 16})
    assert r.status_code == 202, r.text
    with user.client.stream("GET", r.json()["events_url"], headers=user._h()) as s:
        list(s.iter_lines())
    return r


def test_one_request_is_traceable_by_its_ids(client, group, deployed, reference_devices, logs):
    r = _chat(group["alice"])
    rid = r.json()["request_id"]
    assert r.headers["X-Trace-Id"]
    mine = [x for x in logs() if x.get("request_id") == rid]
    messages = [x["message"] for x in mine]
    assert "request admitted" in messages and "request started" in messages and "request completed" in messages
    assert all(x["deployment_id"] == deployed["deployment_id"] for x in mine)
    started = next(x for x in mine if x["message"] == "request started")
    assert sorted(started["fields"]["nodes"]) == sorted(d["device_id"] for d in reference_devices.values())
    # The admission's access log line shares the HTTP exchange's trace ID with the response header.
    assert any(x.get("trace_id") == r.headers["X-Trace-Id"] and x["logger"] == "idlemesh_coord.access"
               for x in logs())


def test_heartbeats_carry_node_ids_and_the_callers_trace_id(client, group, deployed, reference_devices, logs):
    dev = reference_devices["hp"]
    hb = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat",
                     json={"capabilities": dev["capabilities"], "paused": False},
                     headers={"Authorization": f"Bearer {dev['device_token']}", "X-Trace-Id": "agent-trace-1"})
    assert hb.status_code == 200 and hb.headers["X-Trace-Id"] == "agent-trace-1"
    assert "active_request_ids" in hb.json()
    line = next(x for x in logs() if x.get("trace_id") == "agent-trace-1")
    assert line["node_id"] == dev["device_id"] and line["level"] == "debug"  # routine traffic


def test_nothing_private_reaches_the_logs(client, group, deployed, reference_devices, logs):
    alice = group["alice"]
    _chat(alice)
    key = alice.post("/api/v1/credentials", json={"name": "ci", "scopes": ["inference"]}).json()
    client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key['secret']}"},
                json={"model": "group-default", "messages": [{"role": "user", "content": PROMPT}]})
    client.post("/api/v1/auth/login", json={"group_id": group["group"]["group_id"], "login": "alice",
                                            "passphrase": "wrong " + OWNER_PASS})
    raw = logs.raw()
    dev = reference_devices["mac"]
    for secret in (PROMPT, "PROMPT-SENTINEL", OWNER_PASS, MEMBER_PASS, key["secret"], dev["device_token"],
                   alice.sid, alice.csrf):
        assert secret not in raw, f"leaked: {secret[:12]}..."
    assert raw  # the flow did log


def test_unhandled_errors_become_a_catalogued_traceable_500(tmp_path, logs):
    from idlemesh_coord.api.app import create_app
    from idlemesh_coord.config import Settings
    from idlemesh_coord.runtime.fake import FakeRuntime

    app = create_app(Settings(db_path=str(tmp_path / "x.sqlite3")), FakeRuntime())

    @app.get("/boom")
    async def boom():
        raise RuntimeError("unexpected")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    body = r.json()
    assert r.status_code == 500 and body["error"]["code"] == "INTERNAL"
    tid = r.headers["X-Trace-Id"]
    assert tid in body["error"]["message"]
    err = next(x for x in logs() if x.get("error_code") == "INTERNAL" and x["message"] == "unhandled error")
    assert err["trace_id"] == tid and "RuntimeError: unexpected" in err["exception"]
