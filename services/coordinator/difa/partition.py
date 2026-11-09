"""Per-device partition manifests for local prefetch (FR 30).

With llama.cpp RPC, llama-server pushes every weight to its worker over the link at load time (Phase 1:
198 MiB took 69 s at 25 Mbps). Its RPC worker can instead load a weight from a local cache file named by
the tensor's FNV-1a hash, for weight tensors larger than 10 MiB. A manifest tells one device which byte
ranges of the pinned model file it will hold, with a SHA-256 for each, so its agent can fetch just
those ranges (from the model's origin or from the coordinator), verify them, and fill that cache before
the deployment loads. Tensors at or below the threshold still travel over RPC; for large models they are
a small share of the bytes.
"""
import hashlib
import json
from pathlib import Path

SIDE_CAR = ".difa-tensors.json"


def tensor_hashes(path: Path, file_sha256: str, tensors: list[dict]) -> dict[str, str]:
    """SHA-256 per tensor range, cached next to the model file and keyed by the file's own hash."""
    side = path.with_name(path.name + SIDE_CAR)
    cache: dict = {}
    if side.exists():
        try:
            cache = json.loads(side.read_text())
        except ValueError:
            cache = {}
    if cache.get("file_sha256") != file_sha256:
        cache = {"file_sha256": file_sha256, "tensors": {}}
    known: dict[str, str] = cache["tensors"]
    missing = [t for t in tensors if f"{t['offset']}:{t['nbytes']}" not in known]
    if missing:
        with open(path, "rb") as f:
            for t in missing:
                f.seek(t["offset"])
                h, left = hashlib.sha256(), t["nbytes"]
                while left:
                    chunk = f.read(min(left, 8 << 20))
                    if not chunk:
                        raise ValueError(f"model file ends inside tensor {t['name']}")
                    h.update(chunk)
                    left -= len(chunk)
                known[f"{t['offset']}:{t['nbytes']}"] = h.hexdigest()
        side.write_text(json.dumps(cache))
    return {f"{t['offset']}:{t['nbytes']}": known[f"{t['offset']}:{t['nbytes']}"] for t in tensors}


def build(runtime, model_name: str, model_meta: dict, stages: list[dict], source_url: str) -> dict[str, dict]:
    """Manifest for every stage, keyed by device id. Stage order is execution order."""
    path = runtime.model_path(model_name)
    per_stage = runtime.stage_tensors(model_name, stages)
    cached = [t for ts in per_stage for t in ts if t["rpc_cached"]]
    hashes = tensor_hashes(path, model_meta["sha256"], cached)
    out = {}
    for st, ts in zip(stages, per_stage):
        fetch = [{"name": t["name"], "offset": t["offset"], "nbytes": t["nbytes"],
                  "sha256": hashes[f"{t['offset']}:{t['nbytes']}"]} for t in ts if t["rpc_cached"]]
        total = sum(t["nbytes"] for t in ts)
        out[st["device_id"]] = {
            "model": model_name, "file_sha256": model_meta["sha256"], "license": model_meta["license"],
            "url": source_url, "tensors": fetch,
            "bytes_prefetch": sum(t["nbytes"] for t in fetch), "bytes_stage": total,
        }
    return out
