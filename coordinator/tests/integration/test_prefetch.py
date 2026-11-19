"""Partition prefetch before load (FR 30) with real agents, real workers, and llama.cpp's weight cache.

The Mac is given a low bandwidth so the HP runs last and holds the 138 MiB output head, the one tensor
in this small model above llama.cpp's 10 MiB cache threshold. The HP agent must download exactly that
range, verify it, file it under the name llama.cpp computes, and the load must then skip sending it.
Synthetic prompts only.
"""
import json

import httpx
import pytest
from live import (BIN, HAVE_STACK, MODELS, all_ready, coordinator, enroll_agent, free_port, mac_heartbeat,
                  owner_client, start_agent, stop_agents, until)

from idlemesh_coord.config import Settings
from idlemesh_coord.runtime.llamacpp import LlamaCppRuntime

pytestmark = pytest.mark.skipif(not HAVE_STACK, reason="agent binary, llama.cpp build, or model not present")

HEAD_BYTES = 144_643_072
HEAD_CACHE_NAME = "c5d93d2d0af14837"  # name ggml-rpc gave this tensor when it cached it itself (Phase 1)
MIB = 1 << 20


@pytest.fixture
def coordinator_source(monkeypatch):
    monkeypatch.setenv("IDLEMESH_MODEL_SOURCE", "coordinator")
    assert Settings().model_source == "coordinator"


def _deployment(c, gid):
    return c.get(f"/api/v1/groups/{gid}/deployment").json()


def test_prefetched_head_is_loaded_from_the_device_cache(tmp_path, coordinator_source):
    rt = LlamaCppRuntime(str(BIN), str(MODELS), "Mac Studio", {}, log_dir=str(tmp_path))
    cache, log, agents = tmp_path / "hp-cache", tmp_path / "hp.log", []
    with coordinator(tmp_path, rt) as (base, app):
        c, gid = owner_client(base)
        with mac_heartbeat(c, base, bandwidth=5):
            try:
                state = enroll_agent(c, base, tmp_path, "HP EliteBook", "hp-elitebook")
                agents.append(start_agent(state, log, "--worker-port", str(free_port()), "--worker-threads", "4",
                                          "--tunnel-listen", f"127.0.0.1:{free_port()}", "--cache-dir", str(cache),
                                          "--accept-license", "apache-2.0"))
                until(lambda: all_ready(c, gid, 2), 30, "two ready devices")
                r = c.post(f"/api/v1/groups/{gid}/deployment",
                           json={"model": "qwen2.5-0.5b-q8", "require_all": True, "prefetch": True})
                assert r.status_code == 202, r.text
                body = r.json()
                hp_stage = next(s for s in body["explanation"]["stages"] if s["name"] == "HP EliteBook")
                assert hp_stage["holds_output_head"], "test assumes the HP runs last"
                [summary] = body["prefetch"]
                assert summary["tensors"] == 1 and summary["bytes_prefetch"] == HEAD_BYTES

                # Only this device's own ranges are served, and only as ranges.
                tok = json.loads(state.read_text())
                url = f"{base}/api/v1/devices/{tok['device_id']}/model-file"
                auth = {"Authorization": f"Bearer {tok['device_token']}"}
                assert httpx.get(url, headers=auth).status_code == 416
                assert httpx.get(url, headers={**auth, "Range": "bytes=0-1023"}).status_code == 403
                assert httpx.get(url, headers={"Range": "bytes=0-1023"}).status_code == 401

                dep = until(lambda: (d := _deployment(c, gid))["state"] in ("ready", "failed") and d, 120,
                            "prefetch and load")
                assert dep["state"] == "ready", dep
                cached = cache / "rpc" / HEAD_CACHE_NAME
                assert cached.exists() and cached.stat().st_size == HEAD_BYTES
                sent = app.state.tunnels.bytes_to_devices(body["deployment_id"])
                stage_bytes = summary["bytes_stage"]
                # Without the cache the whole stage (about 304 MiB) crosses the link; now the head stays home.
                assert sent < stage_bytes - HEAD_BYTES + 32 * MIB, (sent, stage_bytes)

                req = c.post("/api/v1/requests", json={"model": "group-default", "visibility": "private",
                                                       "max_output_tokens": 12,
                                                       "messages": [{"role": "user", "content": "Name a colour."}]})
                st = until(lambda: (s := c.get(req.json()["status_url"]).json())["status"] in
                           ("completed", "failed") and s, 90, "answer")
                assert st["status"] == "completed", st
            finally:
                stop_agents(agents)
    text = log.read_text()
    assert "prefetch started: 1 tensors" in text and "prefetch ready" in text, text


def test_unaccepted_license_fails_the_deployment(tmp_path, coordinator_source):
    rt = LlamaCppRuntime(str(BIN), str(MODELS), "Mac Studio", {}, log_dir=str(tmp_path))
    cache, agents = tmp_path / "hp-cache", []
    with coordinator(tmp_path, rt) as (base, _):
        c, gid = owner_client(base)
        with mac_heartbeat(c, base, bandwidth=5):
            try:
                state = enroll_agent(c, base, tmp_path, "HP EliteBook", "hp-elitebook")
                agents.append(start_agent(state, tmp_path / "hp.log", "--worker-port", str(free_port()),
                                          "--cache-dir", str(cache)))
                until(lambda: all_ready(c, gid, 2), 30, "two ready devices")
                r = c.post(f"/api/v1/groups/{gid}/deployment",
                           json={"model": "qwen2.5-0.5b-q8", "require_all": True, "prefetch": True})
                assert r.status_code == 202, r.text
                dep = until(lambda: (d := _deployment(c, gid))["state"] in ("ready", "failed") and d, 30, "failure")
                assert dep["state"] == "failed" and dep["error"]["code"] == "LICENSE_NOT_ACCEPTED", dep
                assert not (cache / "rpc").exists() or not any((cache / "rpc").iterdir())
            finally:
                stop_agents(agents)
