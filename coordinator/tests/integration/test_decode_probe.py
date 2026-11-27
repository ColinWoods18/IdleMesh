"""The coordinator offers and serves the decode-probe reference model to workers (S5.6)."""
from conftest import GB

from idlemesh_coord.catalog import MODEL_FILES, PROBE_MODEL
from idlemesh_coord.runtime.base import RuntimeFailure

CAPS = {"os": "windows", "arch": "x86_64", "backend_digest": "fake-runtime-0.1", "formats": ["gguf"],
        "allocatable_bytes": 4 * GB, "mem_bandwidth_gbps": 50, "rtt_ms": 2, "power": "ac"}
WORKER = {"state": "running", "endpoint": "127.0.0.1:50052", "rss_bytes": 0, "peak_rss_bytes": 0, "cap_kills": 0}


class _Runtime:
    """Stands in for the llama.cpp runtime: only `model_path` matters here."""

    def __init__(self, inner, path):
        self._inner, self._path = inner, path

    def model_path(self, name):
        if self._path is None:
            raise RuntimeFailure("MODEL_MISSING", "not downloaded", retryable=False)
        assert name == PROBE_MODEL
        return self._path

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _device(client, user) -> dict:
    code = user.post("/api/v1/devices/enrollment-codes").json()["code"]
    r = client.post("/api/v1/devices/enroll", json={"code": code, "name": "W", "public_key": "k", "capabilities": CAPS,
                                                    "policy": {"memory_cap_bytes": 4 * GB}})
    assert r.status_code == 201, r.text
    return r.json()


def _beat(client, dev, caps=CAPS, worker=WORKER):
    body = {"capabilities": caps, **({"worker": worker} if worker else {})}
    r = client.post(f"/api/v1/devices/{dev['device_id']}/heartbeat", json=body,
                    headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 200, r.text
    return r.json()


def _with_model(app, tmp_path, data=b"gguf-bytes"):
    path = tmp_path / MODEL_FILES[PROBE_MODEL]["file"]
    path.write_bytes(data)
    app.state.runtime = _Runtime(app.state.runtime, path)
    return path


def test_a_worker_without_a_probe_is_offered_the_reference_model_and_can_fetch_it(client, group, app, tmp_path):
    _with_model(app, tmp_path)
    dev = _device(client, group["alice"])
    offer = _beat(client, dev)["decode_probe_model"]
    assert offer == {"catalog": PROBE_MODEL, "file": MODEL_FILES[PROBE_MODEL]["file"],
                     "sha256": MODEL_FILES[PROBE_MODEL]["sha256"], "nbytes": 10, "license": "apache-2.0",
                     "url": f"/api/v1/devices/{dev['device_id']}/probe-model"}
    r = client.get(offer["url"], headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 200 and r.content == b"gguf-bytes"
    # Another device's token, or none, gets nothing.
    other = _device(client, group["alice"])
    assert client.get(offer["url"], headers={"Authorization": f"Bearer {other['device_token']}"}).status_code == 401
    assert client.get(offer["url"]).status_code == 401


def test_no_offer_once_probed_or_without_a_worker(client, group, app, tmp_path):
    _with_model(app, tmp_path)
    dev = _device(client, group["alice"])
    probed = {**CAPS, "decode_probe": {"model": PROBE_MODEL, "tokens_per_s": 52.6, "backend": "cpu", "threads": 4}}
    assert _beat(client, dev, caps=probed)["decode_probe_model"] is None
    assert _beat(client, dev, worker=None)["decode_probe_model"] is None


def test_without_the_model_file_nothing_is_offered_and_the_fetch_is_refused(client, group, app):
    app.state.runtime = _Runtime(app.state.runtime, None)
    dev = _device(client, group["alice"])
    assert _beat(client, dev)["decode_probe_model"] is None
    r = client.get(f"/api/v1/devices/{dev['device_id']}/probe-model",
                   headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PROBE_MODEL_UNAVAILABLE"


def test_the_fake_runtime_offers_nothing(client, group):
    assert _beat(client, _device(client, group["alice"]))["decode_probe_model"] is None
