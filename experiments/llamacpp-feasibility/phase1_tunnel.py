"""Cost of carrying llama.cpp RPC over the mutual-TLS worker tunnel (DEV 08 interim).

Topology: Metal plus one CPU rpc-server worker (0.8/0.2 split). Each path is measured plaintext and
through the tunnel (coordinator forwarder in idlemesh_coord.transport -> TLS 1.3 -> `idlemesh-agent tunnel` ->
worker on loopback), at 0 ms and at 20 ms simulated round trip (experiments/llamacpp-feasibility/netem_relay.py between the
forwarder and the agent). Records load time, which streams the worker's weights over the link, and
median decode time per token. Everything runs on one Mac with synthetic prompts.
Usage: python experiments/llamacpp-feasibility/phase1_tunnel.py
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "coordinator"))
from phase1_latency import BIN, WORK, Relays, measure, wait_port  # noqa: E402

from idlemesh_coord.transport import CoordinatorIdentity, TunnelManager  # noqa: E402

AGENT = Path.home() / ".cache" / "idlemesh-target" / "debug" / "idlemesh-agent"
WORKER, TUNNEL, RELAY = 50092, 50093, 60093


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logs = WORK / "logs" / f"tunnel-{stamp}"
    logs.mkdir(parents=True, exist_ok=True)
    coord, device = CoordinatorIdentity(str(logs / "coord")), CoordinatorIdentity(str(logs / "device"))
    procs = [subprocess.Popen([str(BIN / "ggml-rpc-server"), "-H", "127.0.0.1", "-p", str(WORKER), "-d", "CPU",
                               "-t", "4"], stdout=open(logs / "rpc.log", "w"), stderr=subprocess.STDOUT)]
    procs.append(subprocess.Popen([str(AGENT), "tunnel", "--listen", f"127.0.0.1:{TUNNEL}", "--worker-port",
                                   str(WORKER), "--cert", str(device.cert_path), "--key", str(device.key_path),
                                   "--peer-cert", str(coord.cert_path)],
                                  stdout=open(logs / "tunnel.log", "w"), stderr=subprocess.STDOUT))
    relays, tunnels = Relays(), TunnelManager(coord)
    runs = {}
    try:
        wait_port(WORKER)
        wait_port(TUNNEL)
        for rtt in (0, 20):
            for mode in ("plaintext", "mtls"):
                name = f"{mode}-rtt{rtt}"
                target = WORKER if mode == "plaintext" else TUNNEL
                if rtt:
                    relays.start(RELAY, target, rtt)
                    target = RELAY
                endpoint = (f"127.0.0.1:{target}" if mode == "plaintext"
                            else tunnels.open(name, f"127.0.0.1:{target}", device.cert_pem))
                print(f"== {name}", flush=True)
                runs[name] = measure(name, "MTL0,RPC0", "0.8,0.2", [endpoint], None, requests=5 if not rtt else 3,
                                     n_predict=64 if not rtt else 32, log_dir=logs)
                runs[name]["mode"], runs[name]["rtt_ms"] = mode, rtt
                print(json.dumps({k: runs[name][k] for k in ("load_s", "decode_ms", "prompt_ms")}), flush=True)
                tunnels.close(name)
                relays.stop_all()
    finally:
        tunnels.close_all()
        relays.stop_all()
        for p in procs:
            p.terminate()
            p.wait(15)
    summary = []
    for rtt in (0, 20):
        a, b = runs[f"plaintext-rtt{rtt}"], runs[f"mtls-rtt{rtt}"]
        summary.append({"rtt_ms": rtt, "decode_ms_plain": a["decode_ms"], "decode_ms_mtls": b["decode_ms"],
                        "decode_overhead_ms": round(b["decode_ms"] - a["decode_ms"], 3),
                        "load_s_plain": a["load_s"], "load_s_mtls": b["load_s"]})
    out = {"benchmark": "phase1-tunnel", "utc": stamp, "runs": runs, "summary": summary,
           "note": "One Mac; worker is a local CPU process; relay simulates delay. Synthetic prompts."}
    path = HERE / "results" / f"phase1-tunnel-{stamp}.json"
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
