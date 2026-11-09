"""GGUF reading and llama.cpp v0.9.0 layer placement rules (FR 30, Phase 1 round 3)."""
import random
import struct

import pytest

from difa import gguf


def test_raw_layer_counts_shift_the_boundary_but_exact_split_does_not():
    # llama.cpp divides n_layer + 1 slots, so "-ts 19,5" on 24 layers puts 20 layers on the first device.
    assert gguf.layer_devices(24, [19, 5]).count(0) == 20
    shares = gguf.tensor_split([(0, 19), (19, 24)], 24)
    devs = gguf.layer_devices(24, shares)
    assert devs[:24].count(0) == 19 and devs[24] == 1  # output layer goes to the last device


@pytest.mark.parametrize("n_layer", [8, 24, 32, 64, 80, 126])
def test_exact_split_reproduces_any_contiguous_plan(n_layer):
    rng = random.Random(n_layer)
    for _ in range(200):
        k = rng.randint(1, min(5, n_layer))
        cuts = sorted(rng.sample(range(1, n_layer), k - 1))
        bounds = [0, *cuts, n_layer]
        gguf.tensor_split(list(zip(bounds, bounds[1:])), n_layer)  # raises if llama.cpp would differ


def test_invalid_plans_are_rejected():
    for ranges in ([], [(1, 24)], [(0, 10), (11, 24)], [(0, 0), (0, 24)], [(0, 10)]):
        with pytest.raises(ValueError):
            gguf.tensor_split(ranges, 24)


def _tiny_gguf(path):
    """Two Q8_0 layer tensors, an output tensor, and an F32 norm, 32-byte aligned."""
    def s(x: str) -> bytes:
        return struct.pack("<Q", len(x)) + x.encode()
    meta = s("general.architecture") + struct.pack("<I", 8) + s("toy") + s("toy.block_count") + struct.pack("<II", 4, 2)
    infos, offset = b"", 0
    specs = [("blk.0.ffn_up.weight", 8, (32, 4)), ("blk.1.ffn_up.weight", 8, (32, 4)),
             ("output_norm.weight", 0, (32,)), ("output.weight", 8, (32, 8))]
    sizes = []
    for name, gtype, dims in specs:
        block, size = gguf.TYPE_SIZES[gtype]
        n = 1
        for d in dims:
            n *= d
        nbytes = n // block * size
        infos += s(name) + struct.pack("<I", len(dims)) + b"".join(struct.pack("<Q", d) for d in dims)
        infos += struct.pack("<IQ", gtype, offset)
        sizes.append(nbytes)
        offset += (nbytes + 31) // 32 * 32
    header = b"GGUF" + struct.pack("<IQQ", 3, len(specs), 2) + meta + infos
    header += b"\0" * ((-len(header)) % 32)
    path.write_bytes(header + b"\1" * offset)
    return sizes


def test_reader_and_stage_tensors(tmp_path):
    sizes = _tiny_gguf(tmp_path / "toy.gguf")
    meta, tensors = gguf.read(tmp_path / "toy.gguf")
    assert meta["toy.block_count"] == 2 and [t.nbytes for t in tensors] == sizes
    stages = gguf.stage_tensors(tensors, 2, gguf.tensor_split([(0, 1), (1, 2)], 2))
    assert [t["name"] for t in stages[0]] == ["blk.0.ffn_up.weight"]
    assert {t["name"] for t in stages[1]} == {"blk.1.ffn_up.weight", "output_norm.weight", "output.weight"}
    assert not any(t["rpc_cached"] for s in stages for t in s)  # all far below the 10 MiB threshold


def test_reader_rejects_non_gguf(tmp_path):
    (tmp_path / "x").write_bytes(b"NOPE" + b"\0" * 64)
    with pytest.raises(gguf.GGUFError):
        gguf.read(tmp_path / "x")
