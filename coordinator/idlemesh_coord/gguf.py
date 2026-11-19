"""Minimal GGUF reader and llama.cpp v0.9.0 partition rules (FR 30: per-partition download).

Reads the header and tensor table of a GGUF file (no tensor data), and reproduces how llama.cpp
assigns layers and the output head to devices from a --tensor-split, so the coordinator can tell each
device exactly which byte ranges of the model file it will hold. Verified against llama.cpp's own
load log and RPC cache in Phase 1 (experiments/llamacpp-feasibility/results/phase1-findings.md, round 3).
"""
import struct
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"GGUF"
# ggml type id -> (elements per block, bytes per block)
TYPE_SIZES = {
    0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24), 8: (32, 34), 9: (32, 36),
    10: (256, 84), 11: (256, 110), 12: (256, 144), 13: (256, 176), 14: (256, 210), 15: (256, 292),
    16: (256, 66), 17: (256, 74), 18: (256, 98), 19: (256, 50), 20: (32, 18), 21: (256, 110), 22: (256, 82),
    23: (256, 136), 24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8), 29: (256, 56), 30: (1, 2),
    34: (256, 54), 35: (256, 66), 39: (32, 17),
}
# GGUF metadata value types
_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
STRING, ARRAY = 8, 9

# llama.cpp RPC only consults its tensor cache above this size (ggml-rpc.cpp HASH_THRESHOLD, strict >).
RPC_HASH_THRESHOLD = 10 * 1024 * 1024


@dataclass(frozen=True)
class Tensor:
    name: str
    ggml_type: int
    shape: tuple[int, ...]
    offset: int   # absolute offset of the tensor data in the file
    nbytes: int


class GGUFError(ValueError):
    pass


class _Reader:
    def __init__(self, f):
        self.f = f

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        data = self.f.read(size)
        if len(data) != size:
            raise GGUFError("truncated GGUF header")
        return struct.unpack("<" + fmt, data)[0]

    def string(self) -> str:
        n = self.take("Q")
        if n > 1 << 24:
            raise GGUFError("implausible string length in GGUF header")
        return self.f.read(n).decode("utf-8", errors="replace")

    def value(self, vtype: int):
        if vtype in _SCALARS:
            return self.take(_SCALARS[vtype])
        if vtype == STRING:
            return self.string()
        if vtype == ARRAY:
            itype, n = self.take("I"), self.take("Q")
            if itype in _SCALARS:  # skip large numeric arrays (token scores and types) without decoding
                fmt = _SCALARS[itype]
                self.f.seek(struct.calcsize("<" + fmt) * n, 1)
                return None
            return [self.value(itype) for _ in range(n)] if itype != STRING else [self.string() for _ in range(n)]
        raise GGUFError(f"unknown GGUF value type {vtype}")


def read(path: str | Path) -> tuple[dict, list[Tensor]]:
    """Metadata (large arrays omitted) and the tensor table with absolute data offsets."""
    with open(path, "rb") as f:
        r = _Reader(f)
        if f.read(4) != MAGIC:
            raise GGUFError("not a GGUF file")
        version = r.take("I")
        if version < 2:
            raise GGUFError(f"unsupported GGUF version {version}")
        n_tensors, n_kv = r.take("Q"), r.take("Q")
        meta = {}
        for _ in range(n_kv):
            key = r.string()
            meta[key] = r.value(r.take("I"))
        infos = []
        for _ in range(n_tensors):
            name = r.string()
            dims = tuple(r.take("Q") for _ in range(r.take("I")))
            gtype, rel = r.take("I"), r.take("Q")
            infos.append((name, gtype, dims, rel))
        align = int(meta.get("general.alignment", 32))
        data_start = (f.tell() + align - 1) // align * align
    tensors = []
    for name, gtype, dims, rel in infos:
        if gtype not in TYPE_SIZES:
            raise GGUFError(f"tensor {name} has unsupported ggml type {gtype}")
        block, size = TYPE_SIZES[gtype]
        n = 1
        for d in dims:
            n *= d
        tensors.append(Tensor(name, gtype, dims, data_start + rel, n // block * size))
    # Each tensor must fit before the next one (catches a wrong type size or a corrupt table).
    ordered = sorted(tensors, key=lambda t: t.offset)
    for a, b in zip(ordered, ordered[1:]):
        if a.offset + a.nbytes > b.offset:
            raise GGUFError(f"tensor {a.name} overlaps {b.name}")
    return meta, tensors


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def layer_devices(n_layer: int, shares: list[float], n_gpu_layers: int = 999) -> list[int]:
    """Device index for each repeating layer plus the output layer (index n_layer), as llama.cpp computes it.

    Mirrors llama_model::load_tensors in v0.9.0, including float32 accumulation, so the result matches
    the backend exactly rather than approximately.
    """
    splits, total = [], 0.0
    for s in shares:
        total = _f32(total + _f32(s))
        splits.append(total)
    splits = [_f32(s / total) if total else _f32((i + 1) / len(shares)) for i, s in enumerate(splits)]
    start = max(n_layer + 1 - n_gpu_layers, 0)
    active = min(n_gpu_layers, n_layer + 1)
    out = []
    for il in range(n_layer + 1):
        if il < start or il - start >= active:
            out.append(-1)  # stays on the host CPU
        else:
            out.append(bisect_right(splits, _f32((il - start) / active)))
    return out


def stage_tensors(tensors: list[Tensor], n_layer: int, shares: list[float]) -> list[list[dict]]:
    """For each device in execution order, the file ranges it will hold.

    Repeating-layer tensors (blk.N.*) follow their layer; output.weight and output_norm follow the output
    layer. When the model ties its output to the token embedding (no output.weight), llama.cpp copies
    token_embd onto the output device, so that range is listed there too. token_embd itself stays on the
    host CPU and is never sent to a worker.
    """
    devs = layer_devices(n_layer, shares)
    by_name = {t.name: t for t in tensors}
    out: list[list[dict]] = [[] for _ in shares]

    def add(dev: int, t: Tensor, role: str) -> None:
        if dev >= 0:
            out[dev].append({"name": t.name, "role": role, "offset": t.offset, "nbytes": t.nbytes,
                             "rpc_cached": t.nbytes > RPC_HASH_THRESHOLD})

    for t in tensors:
        if t.name.startswith("blk."):
            layer = int(t.name.split(".")[1])
            add(devs[layer] if layer < n_layer else devs[n_layer], t, "layer")
        elif t.name.startswith("output"):
            add(devs[n_layer], t, "output")
    if "output.weight" not in by_name and "token_embd.weight" in by_name:
        add(devs[n_layer], by_name["token_embd.weight"], "output (tied to token_embd)")
    return out


def tensor_split(ranges: list[tuple[int, int]], n_layer: int) -> list[float]:
    """--tensor-split values that make llama.cpp place exactly the planned layer ranges.

    llama.cpp divides n_layer + 1 slots (the output layer counts as one), so passing raw layer counts
    shifts boundaries. Each split point is put halfway between the last layer of one stage and the first
    layer of the next, and the result is checked against llama.cpp's own rule before use.
    """
    if not ranges or ranges[0][0] != 0 or ranges[-1][1] != n_layer or any(
            a[1] != b[0] for a, b in zip(ranges, ranges[1:])) or any(e <= s for s, e in ranges):
        raise ValueError("layer ranges must be contiguous, non-empty, and cover every layer")
    bounds = [(e - 0.5) / (n_layer + 1) for _, e in ranges[:-1]] + [1.0]
    shares = [bounds[0]] + [b - a for a, b in zip(bounds, bounds[1:])]
    expected = [i for i, (s, e) in enumerate(ranges) for _ in range(s, e)] + [len(ranges) - 1]
    if layer_devices(n_layer, shares) != expected:
        raise ValueError("could not express the planned layer ranges as a llama.cpp tensor split")
    return shares
