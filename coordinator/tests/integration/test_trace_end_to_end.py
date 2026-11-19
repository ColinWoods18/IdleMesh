"""Sprint 1 demo, automated: one request traced from the coordinator to a real agent through JSON logs (S1.7).

A live coordinator (fake runtime, slowed so the request spans several heartbeats) serves a real agent
process. The agent learns the running request's ID from its heartbeat replies and tags its log lines;
scripts/trace.py then finds the request in both components' logs.
"""
import importlib.util
import json
import logging
import subprocess
import time
from pathlib import Path

import pytest
from live import AGENT, coordinator, owner_client, until

from idlemesh_coord import obs
from idlemesh_coord.runtime.fake import FakeRuntime

pytestmark = pytest.mark.skipif(not AGENT.exists(), reason="agent binary not built")

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("trace_script", ROOT / "scripts" / "trace.py")
trace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trace)


def test_request_followed_from_coordinator_to_agent(tmp_path):
    coord_log = tmp_path / "coordinator.log"
    handler = logging.FileHandler(coord_log, encoding="utf-8")
    handler.setFormatter(obs.JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    agent = None
    try:
        with coordinator(tmp_path, FakeRuntime(token_delay_s=0.6)) as (base, _app):
            c, gid = owner_client(base)
            code = c.post("/api/v1/devices/enrollment-codes").json()["code"]
            state = tmp_path / "node" / "state.json"
            state.parent.mkdir()
            enrolled = subprocess.run([str(AGENT), "enroll", "--coordinator", base, "--code", code, "--name",
                                       "Lab laptop", "--memory-cap-gb", "4", "--state", str(state), "--simulate",
                                       "ubuntu-vm"], capture_output=True, text=True, timeout=60)
            assert enrolled.returncode == 0, enrolled.stderr
            agent = subprocess.Popen([str(AGENT), "run", "--state", str(state)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            until(lambda: [d["state"] for d in c.get(f"/api/v1/groups/{gid}/devices").json()["devices"]]
                  == ["ready"], 30, "agent heartbeating")
            dep = c.post(f"/api/v1/groups/{gid}/deployment", json={"model": "fixture-small"})
            assert dep.status_code == 200, dep.text
            r = c.post("/api/v1/requests", json={"model": "group-default", "visibility": "private",
                                                 "messages": [{"role": "user", "content": "synthetic prompt"}],
                                                 "max_output_tokens": 64})
            assert r.status_code == 202, r.text
            rid = r.json()["request_id"]
            until(lambda: c.get(f"/api/v1/requests/{rid}").json()["status"] == "completed", 60, "request done")
            time.sleep(6)  # one more heartbeat, so the agent logs that the request has finished
    finally:
        if agent:
            agent.terminate()
            agent.wait(15)
        root.removeHandler(handler)
        handler.close()
        root.setLevel(old_level)

    agent_log = state.parent / "logs" / "idlemesh-agent.log"
    found = trace.select(trace.read([str(coord_log), str(agent_log)]), rid, None)
    components = {x["component"] for x in found}
    assert components == {"coordinator", "agent"}, [trace.show(x) for x in found]
    messages = [x["message"] for x in found]
    for expected in ("request admitted", "request started", "request completed"):
        assert expected in messages
    serving = next(x for x in found if x["component"] == "agent" and x["message"].startswith("serving requests"))
    assert serving["message"] == f"serving requests: {rid}"
    assert serving["deployment_id"] == dep.json()["deployment_id"]
    # The heartbeat that told the agent about the request has the same trace ID on both sides.
    assert any(x["component"] == "coordinator" and x.get("trace_id") == serving["trace_id"]
               for x in trace.read([str(coord_log)]))
    # Nothing in either log carries the device token.
    token = json.loads(state.read_text())["device_token"]
    assert token not in coord_log.read_text(encoding="utf-8") + agent_log.read_text(encoding="utf-8")
