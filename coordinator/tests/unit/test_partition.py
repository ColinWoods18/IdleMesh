"""partition.py: per-tensor hashes with a side-car cache, and per-device prefetch manifests (FR 30)."""
import hashlib
import json

import pytest

from idlemesh_coord import partition

DATA = bytes(range(256)) * 64  # 16 KiB of known bytes


def _tensor(name, offset, nbytes, rpc_cached=True):
    return {"name": name, "offset": offset, "nbytes": nbytes, "rpc_cached": rpc_cached}


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(DATA)
    return p


def test_hashes_cover_exactly_the_byte_range(model):
    t = [_tensor("a", 0, 100), _tensor("b", 1000, 5000)]
    h = partition.tensor_hashes(model, "file-hash", t)
    assert h == {"0:100": hashlib.sha256(DATA[:100]).hexdigest(),
                 "1000:5000": hashlib.sha256(DATA[1000:6000]).hexdigest()}


def test_side_car_cache_is_reused_and_reset_when_the_file_changes(model):
    t = [_tensor("a", 0, 100)]
    partition.tensor_hashes(model, "v1", t)
    side = model.with_name(model.name + partition.SIDE_CAR)
    cache = json.loads(side.read_text())
    assert cache["file_sha256"] == "v1" and "0:100" in cache["tensors"]
    # A planted value proves a second call reads the cache instead of rehashing.
    cache["tensors"]["0:100"] = "cached"
    side.write_text(json.dumps(cache))
    assert partition.tensor_hashes(model, "v1", t)["0:100"] == "cached"
    # A different file hash discards the cache.
    assert partition.tensor_hashes(model, "v2", t)["0:100"] == hashlib.sha256(DATA[:100]).hexdigest()


def test_corrupt_side_car_is_ignored(model):
    model.with_name(model.name + partition.SIDE_CAR).write_text("{not json")
    assert partition.tensor_hashes(model, "v1", [_tensor("a", 0, 10)])["0:10"] == hashlib.sha256(DATA[:10]).hexdigest()


def test_range_past_the_end_of_the_file_is_an_error(model):
    with pytest.raises(ValueError, match="ends inside tensor big"):
        partition.tensor_hashes(model, "v1", [_tensor("big", len(DATA) - 10, 100)])


class _Runtime:
    def __init__(self, path, per_stage):
        self.path, self.per_stage = path, per_stage

    def model_path(self, name):
        return self.path

    def stage_tensors(self, name, stages):
        return self.per_stage


def test_manifest_lists_only_cacheable_tensors_per_device(model):
    per_stage = [[_tensor("tok_embd", 0, 4000), _tensor("norm", 4000, 64, rpc_cached=False)],
                 [_tensor("blk.1", 4064, 8000), _tensor("output", 12064, 4000)]]
    stages = [{"device_id": "dev_a"}, {"device_id": "dev_b"}]
    meta = {"sha256": "file-hash", "license": "apache-2.0"}
    out = partition.build(_Runtime(model, per_stage), "m", meta, stages, "https://example.invalid/m.gguf")
    a, b = out["dev_a"], out["dev_b"]
    assert [t["name"] for t in a["tensors"]] == ["tok_embd"]
    assert a["bytes_prefetch"] == 4000 and a["bytes_stage"] == 4064  # the small norm still travels over RPC
    assert [t["name"] for t in b["tensors"]] == ["blk.1", "output"] and b["bytes_prefetch"] == b["bytes_stage"]
    assert b["tensors"][0]["sha256"] == hashlib.sha256(DATA[4064:12064]).hexdigest()
    assert a["license"] == "apache-2.0" and a["file_sha256"] == "file-hash" and a["model"] == "m"
