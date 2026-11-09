# Phase 1 findings so far (2026-11-05)

Environment: Mac Studio M2 Ultra, 128 GB, macOS 15.6.1. llama.cpp v0.9.0 (cd959bae99), Metal + RPC.
Model: qwen2.5-0.5b-instruct-q8_0.gguf (sha256 ca59ca7f…844e), Apache-2.0. Synthetic prompts only.
Raw results: `phase1-llamacpp-20261105T222651Z.json` and `.md` (100 warm requests per topology,
256-token prompts, 128 output tokens, greedy decoding).

**Scope warning.** Every "remote" worker here is a CPU-only `ggml-rpc-server` process on the same Mac,
over loopback. Nothing below measures the HP laptop, the Ubuntu VM, or an internet path, and the
0.5B model fits every device, so this is a cooperation (Test A) result, not a capacity result.

## Results

| Topology | Decode t/s | ms/token | First token p95 | Greedy match vs Metal |
|---|---:|---:|---:|---:|
| Metal only (baseline) | 303.1 | 3.30 | 23.7 ms | 100% |
| CPU only, 4 threads | 229.1 | 4.37 | 230.8 ms | 100% |
| Metal + 1 RPC CPU worker | 139.2 | 7.18 | 132.4 ms | 100% |
| Metal + 2 RPC CPU workers | 114.5 | 8.73 | 136.4 ms | 100% |

All four topologies met the pre-declared acceptance rule (non-empty output; at least 90% of prompts
matching the baseline's first 32 greedy tokens) and the partition check (every worker holds weights,
taken from llama.cpp's own load log).

## Findings that change the spec or design

1. **Metal budget confirmed.** Metal reports `recommendedMaxWorkingSetSize = 103,079 MB` (98,304 MiB
   usable to llama.cpp), inside the spec 3.2 planning range of 90 to 110 GB.
2. **Owner memory caps are not enforced by llama.cpp.** An RPC worker advertises all host memory
   (131,072 MiB) and has no cap option. FR 07 must be enforced by the agent: refuse placements above the
   owner cap and run the worker under an OS memory limit.
3. **Registration is not participation.** A 0.89/0.08/0.03 tensor split gave the third worker zero of
   24 layers while it remained connected. The harness and adapter now reject any placement where a
   stage holds no weights. This is the spec 3.1 Test A warning, observed in practice.
4. **The output head lands on the last device.** llama.cpp assigns the output layer (137.9 MiB here,
   about 1 GB for a 70B-class model) to the final device in execution order. The planner now runs the
   strongest device last and counts the head against its budget; the head can close the narrow Test C
   window by itself.
5. **RPC buffer names are misleading.** llama.cpp v0.9.0 labels every RPC buffer `RPC0[host:port]`;
   only the endpoint identifies the device. Partition evidence must key on the endpoint.
6. **The bandwidth probe must be per backend (FR 06).** The agent's single-thread copy measures
   76 GB/s on this Mac, while Metal decodes at an effective 159 GB/s on this small model (and the
   nominal hardware figure is 800 GB/s). A CPU copy cannot stand in for accelerator bandwidth.
7. **The latency model needs a fixed per-hop term.** Using effective per-backend bandwidth from the
   single-device runs, the weights/bandwidth model predicts 3.72 ms and 4.43 ms per token for the 2- and
   3-device topologies; measured values are 7.18 ms and 8.73 ms. About 3.5 ms per token is fixed RPC
   overhead for the first remote stage and about 0.85 ms for the next, on loopback, before any network
   round trip. Without that term the model misses by 49%, outside the spec's 30% agreement target. For
   a 70B-class model the compute term is hundreds of milliseconds, so this overhead matters less there
   than network round trips will.
8. **llama-server defaults are unsafe for this product.** It starts with no API key and CORS open to
   all origins. The adapter binds to 127.0.0.1 and sets a random API key; a release backend must do
   better (spec 6.4).

## Next measurements needed

- Real HP and VM workers on their own hardware, LAN and internet paths, with measured round trips.
- Agent-side memory caps around the RPC worker, then FR 07 compliance tests.
- A per-backend bandwidth or decode probe in the agent, feeding the planner.
- Add the per-hop overhead term to the latency budget and recheck the 30% target.
- A configuration that exceeds the Mac's owner budget (Test C) with a real model.

## Round 2: latency model, simulated network, owner caps (2026-11-05)

Raw results: `phase1-latency-20261105T225814Z.json`. Same machine and model; RPC workers are local CPU
processes and network paths come from `benchmarks/netem_relay.py`, a userspace relay that adds delay
and bandwidth caps without changing system settings.

**Latency model now meets the spec 7.2 target on held-out topologies.** Per token:
`sum(W_i / BW_backend) + O_first + O_extra * (remote stages - 1) + k * sum(remote RTT) + traffic / link bandwidth`.
Fitted on two calibration topologies only: O_first = 3.58 ms, O_extra = 0.74 ms, k = 1.02 round trips
per token per remote stage. Effective decode bandwidth: Metal 156 GB/s, CPU 4 threads 113 GB/s,
CPU 2 threads 71 GB/s on this model.

| Held-out topology | Predicted ms/token | Measured | Error | Bandwidth-only model error |
|---|---:|---:|---:|---:|
| Metal + 1 worker, even split | 7.72 | 8.79 | -12.2% | -52.9% |
| Metal + 2 workers | 8.38 | 9.19 | -8.8% | -55.8% |
| Metal + 3 workers | 10.08 | 10.75 | -6.2% | -53.2% |
| Metal + 2 workers, each behind 80 ms | 173.7 | 175.3 | -0.9% | not applicable |

**Round trips add up per remote stage.** Decode time rose linearly with RTT (7.9, 29.2, 89.5, 161.9
ms/token at 0, 20, 80, 150 ms), about one round trip per token per remote stage. Two remote stages at
80 ms each cost about 175 ms per token. Over the internet the round trips, not compute, set the speed.

**llama.cpp RPC sends about 0.62 MB per token per remote stage.** With 20 ms RTT, decode went from
29 ms/token uncapped to 78, 129, and 228 ms/token at 100, 50, and 25 Mbps; all three caps imply
0.61 to 0.63 MB per token. A typical home upload of 10 to 25 Mbps therefore limits each remote stage to
roughly 2 to 5 tokens per second before any delay. This is likely graph serialisation and will grow
with larger models, so it must be remeasured on the Test C model, and it is a strong argument for a
transport that does not resend the graph each token.

**Model loading streams weights over the RPC link.** 198 MiB took 18.5 s at 100 Mbps and 69 s at
25 Mbps. At pilot scale (several GB per remote stage) loading over a home connection takes tens of
minutes, which is why FR 30 (download each partition locally and verify it) matters for this backend.

**Owner memory caps are now enforced by the agent.** `difa-agent run --worker-bin ...` starts and
supervises the llama.cpp worker only while contribution is allowed and the lease is valid, kills it
when resident memory exceeds the owner's cap (250 ms watchdog), keeps it stopped until the cap is
raised, and halts it on SIGTERM or Ctrl-C. The coordinator treats a stopped worker or a paused device
as a drain and marks the deployment unavailable. Tested end to end with real agents and workers:
lowering a cap mid-deployment killed the worker, drained the deployment, returned 503 to new requests,
and recovered when the cap was raised. Limitation: memory can exceed the cap for up to one watchdog
interval; hard prevention needs OS limits (cgroups, Job Objects) in Phase 2.

**Per-backend decode probe.** The agent can time real decoding of the catalog reference model with
llama-bench (`--decode-probe-bench`, `--decode-probe-model`); the coordinator converts tokens per
second into effective bandwidth and uses it in planning, recording the source in each stage.

## Round 3: secure worker link and partition prefetch (2026-11-09)

Raw results: `phase1-tunnel-20261109T215057Z.json`, `phase1-prefetch-20261109T215844Z.json`. Same machine
and model; workers are local CPU processes; network paths come from `netem_relay.py`.

**Remote workers now run behind mutual TLS (interim answer to DEV 08).** The agent generates an ECDSA
P-256 key and certificate at enrollment and sends the certificate as the device's public key; the
coordinator returns its own certificate. `difa-agent run --tunnel-listen` keeps the llama.cpp worker on
loopback and exposes only a TLS 1.3 listener that accepts exactly the pinned coordinator certificate. The
coordinator opens a loopback forwarder per remote stage, so `llama-server` still only talks to loopback,
and checks the device certificate byte for byte. Tested: a deployment through the tunnel generates
normally; clients with no certificate or another certificate are refused before reaching the worker; an
impostor device at the right address fails the handshake; the agent refuses to expose a plaintext
worker on a network address unless `--insecure-plaintext-worker` is given.

| Path | Plaintext ms/token | mTLS ms/token | Overhead | Load, plaintext | Load, mTLS |
|---|---:|---:|---:|---:|---:|
| Loopback | 7.51 | 8.28 | +0.77 | 0.73 s | 0.92 s |
| 20 ms RTT | 28.74 | 30.07 | +1.33 | 2.05 s | 2.57 s |

Most of the overhead is the extra loopback hop through the Python forwarder, not the cryptography. At
internet round trips it is a few percent; a native forwarder would remove most of it.

**Planned and actual layer splits disagreed.** llama.cpp divides `n_layer + 1` slots (the output layer
counts), so passing layer counts as `--tensor-split` moves boundaries: `-ts 19,5` on this 24-layer model
puts 20 layers on the first device. The adapter now computes split points halfway between planned
boundaries (`difa/gguf.py`, checked against llama.cpp's own rule on 1,200 random plans in the unit tests) and compares
each device's loaded bytes with the plan; a difference above 2 percent fails the deployment.

**llama.cpp's worker can load weights from a local cache, which makes FR 30 practical.** With `-c`, the
RPC worker looks up a weight tensor by its FNV-1a hash before llama-server sends it, but only for weight
tensors above 10 MiB. Prefetch (`"prefetch": true` on a deployment) uses this: the coordinator reads the
GGUF tensor table, works out which byte ranges each device will hold, and sends a manifest with a SHA-256
per range; the agent fetches those ranges with HTTP Range requests (from the model's origin, or from the
coordinator on a LAN), verifies them, and stores them under the hash name llama.cpp expects. The
deployment waits in `preparing` until every device reports its share verified, and fails cleanly if a
download fails or the owner has not accepted the model license (`--accept-license`).

- Tested end to end: the device downloaded exactly the 138 MiB output-head range, filed it as
  `c5d93d2d0af14837` (the name llama.cpp itself gave this tensor), and during load the bytes sent to that
  device dropped below stage size minus that tensor.
- At 50 Mbps and 20 ms, load time fell from 53.0 s to 30.0 s. The predicted saving (cached bytes over link
  speed) was 23.1 s; measured 23.05 s.
- Coverage depends on tensor sizes. In this 0.5B model only the output head is above 10 MiB. In a
  70B-class model (hidden 8192, 8 KV heads) every attention-query, attention-output and feed-forward
  tensor is above the threshold and only the key/value projections and norms are not: an estimated 98
  percent of weight bytes are cacheable, so load over a home link would be dominated by the remaining
  ~12 MB per layer. To be confirmed on the Test C model's own tensor table.

**Limitations.** The control channel (enrollment, heartbeats, the model-file endpoint) is still plain
HTTP with bearer tokens, so use it only on a trusted network until it moves to TLS. Certificates have no
rotation; removing a device stops the coordinator dialling it, but the agent keeps its certificate. On
Windows the agent's state file (which holds the device key) is not yet restricted to the owner. Prefetch
does not yet check free disk space against an owner limit.
