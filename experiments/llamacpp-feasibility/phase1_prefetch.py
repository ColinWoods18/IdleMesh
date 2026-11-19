"""Load time with and without a device-side weight cache over a capped link (FR 30).

Topology: Metal plus one CPU rpc-server worker that runs last and so holds the output head (shares
0.5/0.5), behind a relay capped at 50 Mbps with 20 ms round trip. The worker runs with llama.cpp's
weight cache (-c). Run 1 starts with an empty cache, so every weight crosses the link (llama.cpp also
saves the large ones); run 2 then finds them cached, which is the state the DIFA agent's prefetch
produces before a deployment loads. Synthetic prompts. Usage: python experiments/llamacpp-feasibility/phase1_prefetch.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "coordinator"))
from phase1_latency import BIN, MODEL, WORK, Relays, measure, wait_port  # noqa: E402

from idlemesh_coord import gguf  # noqa: E402

WORKER, RELAY, MBPS, RTT = 50102, 60102, 50, 20


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logs = WORK / "logs" / f"prefetch-{stamp}"
    cache = logs / "cache"
    cache.mkdir(parents=True)
    meta, tensors = gguf.read(MODEL)
    n_layer = int(meta[f"{meta['general.architecture']}.block_count"])
    stages = gguf.stage_tensors(tensors, n_layer, [0.5, 0.5])
    worker_bytes = sum(t["nbytes"] for t in stages[1])
    cacheable = sum(t["nbytes"] for t in stages[1] if t["rpc_cached"])
    env = {**os.environ, "LLAMA_CACHE": str(cache)}
    proc = subprocess.Popen([str(BIN / "ggml-rpc-server"), "-H", "127.0.0.1", "-p", str(WORKER), "-d", "CPU",
                             "-t", "4", "-c"], env=env, stdout=open(logs / "rpc.log", "w"), stderr=subprocess.STDOUT)
    relays = Relays()
    runs = {}
    try:
        wait_port(WORKER)
        relays.start(RELAY, WORKER, RTT, MBPS)
        for name in ("cold-cache", "warm-cache"):
            print(f"== {name}", flush=True)
            runs[name] = measure(name, "MTL0,RPC0", "0.5,0.5", [f"127.0.0.1:{RELAY}"], None, requests=2,
                                 n_predict=16, log_dir=logs)
            print(json.dumps({k: runs[name][k] for k in ("load_s", "decode_ms", "buffers_mib")}), flush=True)
    finally:
        relays.stop_all()
        proc.terminate()
        proc.wait(15)
    files = sorted(p.name for p in (cache / "rpc").iterdir())
    out = {"benchmark": "phase1-prefetch", "utc": stamp, "link": {"mbps": MBPS, "rtt_ms": RTT},
           "worker_stage_bytes": worker_bytes, "worker_cacheable_bytes": cacheable, "cache_files": files,
           "runs": runs,
           "load_saving_s": round(runs["cold-cache"]["load_s"] - runs["warm-cache"]["load_s"], 2),
           "expected_saving_s": round(cacheable * 8 / (MBPS * 1e6), 2),
           "note": "One Mac; worker is a local CPU process behind a userspace relay. Synthetic prompts."}
    path = HERE / "results" / f"phase1-prefetch-{stamp}.json"
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in ("worker_stage_bytes", "worker_cacheable_bytes", "cache_files",
                                          "load_saving_s", "expected_saving_s")}, indent=2))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
