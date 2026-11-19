"""Sprint 2 demo, automated: a real agent reports idle samples, and an outage loses nothing (S2.4).

The agent samples every second here (sites use 30-60 s). The coordinator is stopped while the agent keeps
sampling into its on-disk buffer, then restarted on the same port and database; the buffered samples
arrive and the timeline has no gap.
"""
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import pytest
import uvicorn
from live import AGENT, free_port, owner_client, until

from idlemesh_coord.api.app import create_app
from idlemesh_coord.config import CONTROL_KEEP_ALIVE_S, Settings
from idlemesh_coord.runtime.fake import FakeRuntime

pytestmark = pytest.mark.skipif(not AGENT.exists(), reason="agent binary not built")

OUTAGE_S = 12


@contextmanager
def serve(db_path, port):
    app = create_app(Settings(db_path=str(db_path)), FakeRuntime(0.01))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           timeout_keep_alive=CONTROL_KEEP_ALIVE_S))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    until(lambda: server.started, 20, "coordinator start")
    try:
        yield app
    finally:
        server.should_exit = True
        thread.join(20)


def _samples(app) -> list[str]:
    return [r["ts"] for r in app.state.store.all("SELECT ts FROM idle_samples ORDER BY ts")]


def test_idle_samples_survive_a_coordinator_outage(tmp_path):
    port, db = free_port(), tmp_path / "idlemesh.sqlite3"
    base = f"http://127.0.0.1:{port}"
    state = tmp_path / "node" / "state.json"
    state.parent.mkdir()
    agent = None
    try:
        with serve(db, port) as app:
            c, gid = owner_client(base)
            code = c.post("/api/v1/devices/enrollment-codes").json()["code"]
            enrolled = subprocess.run([str(AGENT), "enroll", "--coordinator", base, "--code", code, "--name", "Dev PC",
                                       "--memory-cap-gb", "2", "--state", str(state), "--simulate", "ubuntu-vm",
                                       "--consent-idle-data"], capture_output=True, text=True, timeout=60)
            assert enrolled.returncode == 0, enrolled.stderr
            agent = subprocess.Popen([str(AGENT), "run", "--state", str(state), "--telemetry-interval-s", "1"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            until(lambda: len(_samples(app)) >= 3, 30, "first samples")
            device = c.get(f"/api/v1/groups/{gid}/devices").json()["devices"][0]
            assert device["node_state"] in ("idle", "active")
            before = len(_samples(app))
            c.close()
        # Coordinator down: the agent keeps sampling into its buffer.
        time.sleep(OUTAGE_S)
        buffered = list((state.parent / "telemetry").iterdir())
        assert any(f.stat().st_size > 0 for f in buffered), buffered
        with serve(db, port) as app:
            until(lambda: len(_samples(app)) >= before + OUTAGE_S, 45, "buffered samples to arrive")
            ts = [datetime.fromisoformat(t) for t in _samples(app)]
            gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
            assert max(gaps) < 3, f"gap in the timeline: {max(gaps):.1f} s"
            events = app.state.store.all("SELECT from_state, to_state FROM node_events ORDER BY ts")
            assert events and events[0]["from_state"] is None  # the agent's first state, at startup
    finally:
        if agent:
            agent.terminate()
            agent.wait(15)
