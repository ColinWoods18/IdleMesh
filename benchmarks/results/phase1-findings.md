# Phase 1 findings so far (2026-11-05)

Environment: Mac Studio M2 Ultra, 128 GB, macOS 15.6.1. llama.cpp v0.9.0 (cd959bae99), Metal + RPC.
Model: qwen2.5-0.5b-instruct-q8_0.gguf (sha256 ca59ca7f…844e), Apache-2.0. Synthetic prompts only.
Raw results: `phase1-llamacpp-20261105T222651Z.json` and `.md` (100 warm requests per topology,
256-token prompts, 128 output tokens, greedy decoding).

**Scope warning.** Every "remote" worker here is a CPU-only `ggml-rpc-server` process on the same Mac,
over loopback. Nothing below measures the HP laptop, the Ubuntu VM, or an internet path, and the
0.5B model fits every device, so this is a cooperation (Test A) result, not a capacity result.
Round 4 (below) repeats Test A on the real HP and Ubuntu VM over the LAN.

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

## Round 4: Test A on three physical machines (2026-11-16)

Raw results: `pilot-3machine-20261116T212640Z.json`, `pilot-3machine-20261116T213110Z.json`,
`pilot-3machine-20261116T213925Z.json`, the operator notes in `note.md`, and llama-server's own timings from
the coordinator's adapter logs (`dep_c8dcec727fa62772`, `dep_de9220e550f0fb71`). Same model, llama.cpp
v0.9.0 on all three machines, run with `docs/testing/three_machine_test.md`. Synthetic prompts only.
Network: Wi-Fi (reported by the operator after the run).

| Stage | Device | Layers | Weights | Transport | Heartbeat RTT |
|---|---|---|---:|---|---:|
| 1 | HP EliteBook (Windows, CPU), 10.10.150.105 | 0 | 15.1 MiB | mTLS | 42.4–52.9 ms |
| 2 | Ubuntu VM (CPU), 10.10.132.197 | 1 | 15.1 MiB | mTLS | 14.7–18.5 ms |
| 3 | Mac Studio (Metal), coordinator | 2–23 + output | 470.6 MiB | local | 3.4–3.6 ms |

**The system works end to end on real devices.** Across two deployments and three benches, 50 of 50
requests completed with no errors. Both remote stages ran through the mutual-TLS tunnel. Loaded bytes
per device matched the plan, so there was no `PARTITION_MISMATCH`. A deployment was ready 8.1 s after
`pilot deploy`. Prefetch reported 100% at once: no tensor on the HP or the VM is above llama.cpp's
10 MiB cache threshold, as predicted in Round 3.

**Speed is set entirely by the network.** One device alone (Mac, Metal) decodes at 305 tokens/s. The
three-machine split decodes at about 20.

| Deployment | Predicted ms/token | Measured ms/token (llama.cpp, token-weighted) | Median per request (p10–p90) | Prediction error |
|---|---:|---:|---:|---:|
| `dep_c8dcec727fa62772` (benches 1–2) | 78.5 | 49.7 | 48.3 (43.3–68.1) | +58% |
| `dep_de9220e550f0fb71` (bench 3) | 71.7 | 47.0 | 44.9 (36.4–52.1) | +53% |

The bench's own end-to-end figure (median 15–16 tokens/s) also includes prompt processing and polling.
A fit of elapsed time against tokens gives a fixed cost of 0.33–0.64 s per request, plus 50–51 ms per
token. Prompt processing ran at 7.5–9.1 ms per prompt token, against 0.07 ms on the Mac alone. Each
prompt chunk pays the same round trips, so short prompts are also network-bound.

**The latency model misses the spec 7.2 target (within 30%) on real links, but on the safe side.** It
predicts too slow by 53–58%. The fitted constants from Round 2 are not the cause: the round-trip input is.

- The planner takes each remote stage's RTT from the agent's heartbeat. That figure is an HTTP request
  to the coordinator, so it includes connection setup and the coordinator's request handling as well
  as the network.
- For the VM, the heartbeat reported 14.7–18.5 ms, but ICMP ping from the Mac measured 9.2 ms average
  (7.0–12.7 ms). The HP did not answer ping (Windows blocks it by default), so its true RTT was not
  measured.
- The heartbeat RTTs sum to 57–68 ms, which is more than the whole measured token time.
- Taking the Mac's compute (5.3 ms) and the fitted backend overhead (4.3 ms) out of the measured 47 ms
  leaves about 37 ms per token for the two remote round trips. That is consistent with roughly 9 ms
  for the VM and 25–30 ms for the HP.

Fix: measure RTT from the coordinator over the established tunnel (for example, a small echo on the
tunnel, or TCP connect time to the advertised endpoint), and use that in the latency model in place of
heartbeat RTT. Keep heartbeat RTT for liveness only. Then re-run this test to check the 30% target.

**The links themselves are slow, as expected on Wi-Fi.** The run was over Wi-Fi, and the HP and the VM
are on different subnets (10.10.150.0 and 10.10.132.0), so traffic to them is also routed. A switched
wired LAN typically gives 1 ms or less, against the measured 9 ms to the VM. The HP is the dominant
stage. Running it wired, on the Mac's subnet, would likely roughly double decode speed. Wi-Fi does not
excuse the prediction miss: spec 7.2 asks the prediction to match the trace on whatever path is used,
and Wi-Fi is the most likely household path. It does widen the spread of per-token times (p90 up to
68 ms), which is why the round-trip input now uses a mean over back-to-back samples rather than a single
heartbeat.

**Shutdown aborts llama-server after a worker leaves.** In two deployments (`dep_6847a20f5bea35a3`,
`dep_c8dcec727fa62772`), llama-server printed "Remote RPC server crashed or returned malformed response"
(`ggml-rpc.cpp:685`) and aborted. Both times this happened during "cleaning up before exit", after all
requests had completed. That fits the owner-control drain path (4a–4d): the device stops its worker
first, then the coordinator unloads. Results are not affected, and the deployment drains as designed.
However, every drain leaves a crash trace, which will hide real crashes in the logs. Options:
- stop llama-server before tunnels close on a planned unload;
- classify an abort after "cleaning up before exit" as an expected drain in the adapter log.

The owner-control checks (4a–4f) were run, but their outcomes were not recorded in `note.md`. The logs
show only that drains happened.

**Changes made after this round (2026-11-16; run on the physical machines in Round 5).**
- Just before loading, the coordinator times 16 back-to-back llama.cpp RPC round trips through each
  remote stage's tunnel (HELLO, then DEVICE_COUNT). It replaces the heartbeat RTT with the mean of those
  samples and predicts again. The plan keeps the heartbeat-based prediction as `planned_latency_budget`
  and records each stage's `rtt_source`. The measurement is reused for planning for 15 minutes. If it
  fails, the deployment still loads, using the heartbeat value for that stage.
- `pilot bench` fits time per token across requests, compares it with the as-loaded prediction, and
  reports `prediction_error_pct` and `within_30pct`. `--network` records how each machine is connected.
- On a drain, the coordinator now stops llama-server without its RPC cleanup and writes
  `difa: drain (reason)` to the adapter log. This replaces the abort trace that appeared before. The
  integration test now checks that the log contains no abort after a cap drain.

**Still open after this round.**
- Re-run the three-machine test with the tunnel RTT change to check the 30% target on Wi-Fi.
- If possible, also run it with the HP wired, to compare.
- Record the 4a–4f results.
- The capacity tests (B and C) need the larger model, which is a 58 GB download and needs approval.

## Round 5: tunnel-timed round trips on three physical machines (2026-11-16)

Raw results: `pilot-3machine-20261116T223828Z.json` (commit bbc5d3d), plus llama-server's timings from
the adapter log `dep_1cee4af27a85105a`. Same machines, model and llama.cpp build as Round 4. Network:
Wi-Fi on all three machines (`Mac=wifi HP=wifi VM=wifi`).

| Stage | Heartbeat RTT (planned) | Tunnel RTT mean (median, min–p90), 16 samples |
|---|---:|---:|
| HP EliteBook, layer 0 | 48.4 ms | 16.72 ms (14.70, 10.79–31.35) |
| Ubuntu VM, layer 1 | 17.3 ms | 9.33 ms (8.95, 6.26–12.21) |
| Mac Studio, layers 2–23 + output | local | local |

The tunnel measurement on the VM (9.3 ms) agrees with the ICMP ping from Round 4 (9.2 ms). The HP's
heartbeat overstated its path by about three times.

| | ms/token | tokens/s | Error against measured |
|---|---:|---:|---:|
| Planned (heartbeat RTT) | 76.49 | 13.1 | +73% |
| As loaded (tunnel RTT) | 36.04 | 27.7 | −18% (llama.cpp) / −22% (bench fit) |
| Measured, llama.cpp token-weighted (median 43.4, p10–p90 35.3–52.4) | 44.2 | 22.6 | |
| Measured, bench fit across requests | 45.92 | 21.8 | |

**The latency model now meets the spec 7.2 target on real links.** The as-loaded prediction is within
30 percent of the trace (−18 to −22 percent). All 20 requests completed. The bench reports
`within_30pct: true`. The prediction now errs on the fast side by 8–10 ms per token. Likely
contributors, none yet isolated:
- The fitted backend constants come from plaintext loopback workers (Round 2). They do not include the
  tunnel's own cost, which Round 3 measured at 0.8–1.3 ms per token per stage.
- Wi-Fi jitter while tokens stream. The HP's p90 round trip (31 ms) is almost twice its median, and
  16 idle samples may underweight that tail.
- The per-token round-trip count `k = 1.02` was fitted on loopback and may differ on lossy links.

Adding the measured tunnel cost per mTLS stage would bring the prediction to about 38 ms (−14
percent). Treat that as the next calibration step, not something to fit to this one run: refit
`first_remote_ms`, `extra_remote_ms` and `k` with mTLS stages, using at least two network setups.

**Decode speed was unchanged:** about 22 tokens/s, against 20–21 in Round 4. As expected, only the
prediction changed, not the path. Prompt processing ran at 8.7 ms per prompt token (median).

**Not exercised in this run:** owner-control drains (4a–4f). The deployment was not drained, so the new
clean-stop path (`difa: drain`) has only been checked by the integration test so far.

**Phase 1 gate status after Round 5 (spec section 14):**
- Done:
  - valid cross-platform execution (macOS Metal, Windows CPU, Linux CPU);
  - actual remote computation through authenticated tunnels;
  - owner limits enforced (Round 2 and the integration tests);
  - a predicted per-token time within 30 percent on real Wi-Fi links (and within 7 percent in Round 6).
- Still to do:
  - the Test C owner-budget result against the strongest single-device baseline (needs the larger
    model, about 58 GB);
  - an approved security path. The control channel moved to HTTPS after this round (see below); what is
    left is review, rotation, and the Phase 2 gRPC-over-mTLS contract;
  - the recorded hardware decision;
  - user review of measured waits (3–5 users).

## Security: control channel on HTTPS with a pinned coordinator certificate (2026-11-16)

Before this change, enrollment, heartbeats and weight downloads from the coordinator ran over plain
HTTP. They carry the device token, and the enrollment code, which anyone on the network path could read
or replay. Now:

- **The coordinator serves the API over HTTPS.** `scripts/dev_server.py` creates a self-signed ECDSA
  P-256 certificate (`coordinator-control.crt`, kept in the data directory, valid 825 days). It is
  separate from the worker-tunnel client certificate. Plain HTTP (`--insecure-http`) is refused on
  anything but loopback.
- **Trust comes with the enrollment code.** The code reply carries the certificate's SHA-256 fingerprint,
  which `pilot.py code` prints. `difa-agent enroll --coordinator https://… --coordinator-fingerprint X`
  checks the server certificate against X during the TLS handshake, so the code is never sent to a server
  with any other certificate. X needs at least 16 hex digits: matching 64 bits within a code's
  10-minute life is not practical. On success the agent stores all 64 digits and accepts only that
  certificate for heartbeats and for weight downloads from the coordinator.
- **The agent refuses plain HTTP to any non-loopback coordinator,** including state files from earlier
  enrollments, so devices must enroll again. Downloads from a model's origin use ordinary public-CA
  HTTPS and never carry the device token.
- **Tested end to end** (`tests/integration/test_control_tls.py`) with a real agent against a real HTTPS
  coordinator:
  - a wrong fingerprint fails, and the same code still works afterwards, which shows it was never sent;
  - a 16-digit prefix typed in groups enrolls the device and pins the full fingerprint;
  - heartbeats run over the pinned channel;
  - a changed certificate stops heartbeats;
  - `http://` to a LAN address is refused.

**Still open on the security path.**
- Neither the control certificate nor the tunnel certificates are rotated, and the agent does not
  check expiry.
- Browsers see a self-signed certificate. The spec's browser-trusted HTTPS route for members is a
  separate Phase 1 feasibility item.
- On Windows, the agent's state file is not yet restricted to the owner.
- The spec's target remains gRPC over mutual TLS for agents (Phase 2).
- An independent security review is still required.

## Round 6: three physical machines with the HTTPS control channel (2026-11-16)

Raw results: `pilot-3machine-20261116T233456Z.json` (commit 40e80cc), plus llama-server's timings from
the adapter log `dep_a014e07679c9f018`. Same machines, model and llama.cpp build. Network:
`Mac=ethernet HP=wifi VM=wifi` (the Mac was on Wi-Fi in Round 5).

**The HTTPS control channel works on all three operating systems.** Every device enrolled again over
HTTPS with a fingerprint-pinned coordinator certificate. Heartbeats, prefetch and deployment all ran over
it. 20 of 20 requests completed, with both remote stages on mutual TLS.

| Stage | Heartbeat RTT (now over HTTPS) | Tunnel RTT mean (median, min–p90), 16 samples |
|---|---:|---:|
| HP EliteBook, layer 0 | 190.6 ms | 17.09 ms (15.04, 11.50–25.92) |
| Ubuntu VM, layer 1 | 82.5 ms | 10.25 ms (7.35, 4.71–25.20) |
| Mac Studio, layers 2–23 + output | local | local |

| | ms/token | tokens/s | Error against measured |
|---|---:|---:|---:|
| Planned (heartbeat RTT) | 287.95 | 3.5 | +616% |
| As loaded (tunnel RTT) | 37.28 | 26.8 | −7% (llama.cpp) / −6% (bench fit) |
| Measured, llama.cpp token-weighted (median 39.2, p10–p90 34.1–49.7) | 40.2 | 24.9 | |
| Measured, bench fit across requests | 39.78 | 25.1 | |

**The latency model is within 7 percent of the trace.** That compares with −18 to −22 percent in Round
5. Decode rose from about 22 to 25 tokens/s with the Mac on Ethernet. The tunnel round trips were almost
unchanged, so most of the gain is likely on the Mac's side of each hop. One run is not enough to
separate that from Wi-Fi variation.

**Heartbeat RTT is now useless as a network estimate.** It rose from 17–48 ms to 83–191 ms. The
cause:
- uvicorn closes idle keep-alive connections after 5 seconds, which equals the heartbeat interval;
- so nearly every heartbeat opens a new TCP connection and a new TLS handshake, with an ECDSA
  signature;
- the agent times the whole request.

It did no harm to placement, because every remote stage was re-measured through its tunnel before load.
But the first plan's prediction (288 ms/token) is meaningless, and the planner's latency score still
uses heartbeat RTT. Fixes, both small:
1. Keep control connections open longer than the heartbeat interval (for example
   `timeout_keep_alive=30` in `scripts/dev_server.py`).
2. Report the time of the HTTP request on a warm connection, or a separate lightweight ping, rather than
   connection setup.

**Not exercised in this run:** owner-control drains (4a–4f) with the new clean stop.

**Fixed after Round 6 (not yet run on the physical machines).**
- `scripts/dev_server.py` now keeps idle control connections open for 30 seconds
  (`CONTROL_KEEP_ALIVE_S`), so heartbeats every 5 seconds reuse one TLS connection.
- The coordinator adds `Server-Timing: app;dur=<ms>` to heartbeat replies only. The agent subtracts that
  from the request time and reports the median of its last five heartbeats. One cold connection or Wi-Fi
  stall no longer moves the fallback RTT.
- `tests/integration/test_control_tls.py` checks the header, and checks that the reported RTT over HTTPS
  on loopback stays under 20 ms. The agent must be rebuilt on the HP and the VM.
