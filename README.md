# DIFA prototype

Phase 0 prototype of **Distributed Inference For All**, built against
`DIFA_Product_Specification_v0_3.docx` and `DIFA_Design_Whitepaper_v0_3.docx` in the parent folder.

> **The inference runtime is fake.** It streams deterministic text and reports which devices it
> "ran" on. Everything here exercises the product contracts (groups, privacy, queueing,
> placement, owner controls, API); none of it is evidence that distributed inference works.
> That evidence is Phase 1.

## What is here

| Path | What it is |
|---|---|
| `services/coordinator/difa/domain/` | Pure rules: roles and visibility (`access.py`), request lifecycle, fair queue, memory ledger, filter-and-score placement with latency budget |
| `services/coordinator/difa/api/` | FastAPI gateway: native `/api/v1` and the OpenAI-compatible `/v1` subset |
| `services/coordinator/difa/scheduler.py` | Per-group admission, reservation, execution, cancellation, device-loss handling |
| `services/coordinator/difa/runtime/` | Adapter contract (`base.py`) and the fake runtime |
| `services/coordinator/difa/catalog.py` | Synthetic model fixtures, including `pilot-test-c` sized for the v0.3 pilot decision |
| `agents/device/` | Rust `difa-agent`: one-time-code enrollment, capability and bandwidth probes, owner policy enforced locally, heartbeats and leases |
| `contracts/http/openapi.json` | Exported HTTP contract |
| `tests/` | Unit and integration tests (pytest) |
| `scripts/` | Dev server, three-device smoke test, contract export |

## Run it

Python 3.10+ and Rust stable. The virtual environment and Cargo build output live outside this
iCloud folder so they are not synced.

```bash
python3 -m venv ~/.venvs/difa
~/.venvs/difa/bin/python -m pip install -e ".[dev]"
```

```bash
~/.venvs/difa/bin/python -m pytest -q
```

```bash
(cd agents/device && CARGO_TARGET_DIR=~/.cache/difa-target cargo test)
```

Start the coordinator on loopback (database in `~/.local/share/difa/`), then, in another terminal,
run the smoke test, which enrolls three simulated reference devices with real agent processes:

```bash
~/.venvs/difa/bin/python scripts/dev_server.py --fresh
```

```bash
(cd agents/device && CARGO_TARGET_DIR=~/.cache/difa-target cargo build) && ~/.venvs/difa/bin/python scripts/smoke.py
```

Interactive API docs are at http://127.0.0.1:8765/docs while the server runs.

Regenerate the contract after API changes:

```bash
~/.venvs/difa/bin/python scripts/export_openapi.py
```

## Phase 1: llama.cpp feasibility (in progress)

`services/coordinator/difa/runtime/llamacpp.py` is a real backend adapter behind the same contract as
the fake runtime. The coordinator host runs `llama-server` on its own Metal device and reaches other
stages through llama.cpp RPC workers; the placement pipeline's layer counts become `--tensor-split`
shares, and the adapter refuses a deployment unless llama.cpp's load log shows weights on every stage.
`benchmarks/phase1_llamacpp.py` measures single-device baselines and cooperation topologies with
100 warm requests each and a pre-declared greedy-match acceptance rule; results land in
`benchmarks/results/`.

Research-only constraints, as the spec requires: llama.cpp RPC is upstream-documented as insecure, so
`llama-server` only ever talks to loopback (bound to 127.0.0.1 with a random API key), and all prompts are
synthetic. A remote worker is reached through a **mutual-TLS tunnel** (`difa.transport` on the
coordinator, `difa-agent run --tunnel-listen` on the device): the worker itself stays on the device's
loopback, both ends pin certificates exchanged at enrollment, and the agent refuses to expose a plaintext
worker on a network address. Devices can **prefetch** their share of the model before a deployment loads
(`"prefetch": true`): the coordinator sends each agent the exact byte ranges it will hold with a SHA-256
each, and the agent fills llama.cpp's weight cache so those weights never cross the link at load time.

One-time setup (pinned build and model, outside iCloud):

```bash
brew install cmake && git clone --depth 1 --branch v0.9.0 https://github.com/ggml-org/llama.cpp.git ~/.cache/difa-phase1/llama.cpp
```

```bash
cd ~/.cache/difa-phase1/llama.cpp && cmake -B build -DGGML_METAL=ON -DGGML_RPC=ON -DLLAMA_CURL=OFF && cmake --build build -j --target llama-server llama-bench ggml-rpc-server
```

```bash
mkdir -p ~/.cache/difa-phase1/models && curl -L -o ~/.cache/difa-phase1/models/qwen2.5-0.5b-instruct-q8_0.gguf https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q8_0.gguf
```

```bash
~/.venvs/difa/bin/python benchmarks/phase1_llamacpp.py
```

### Connecting the real HP and Ubuntu VM

Step-by-step guides for the three-machine test, including every install, are in `docs/testing/`:
[three_machine_test.md](docs/testing/three_machine_test.md) (start here),
[mac_test_setup.md](docs/testing/mac_test_setup.md), [windows_test_setup.md](docs/testing/windows_test_setup.md),
and [ubuntu_test_setup.md](docs/testing/ubuntu_test_setup.md). `scripts/pilot.py` drives the coordinator
for the test. The summary below is the short version.

On the Mac, start the coordinator on its LAN address with the llama.cpp adapter, sign in at
http://127.0.0.1:8765/docs, create the group, and create one enrollment code per device:

```bash
~/.venvs/difa/bin/python scripts/dev_server.py --fresh --llamacpp --host 0.0.0.0 --model-source coordinator
```

On each device, build llama.cpp v0.9.0 with RPC (CPU is enough) and the agent, then enroll and run. The
example uses Linux paths; on Windows build the same targets and pass the `.exe` paths. Replace the
addresses with the Mac's and the device's own LAN addresses.

```bash
git clone --depth 1 --branch v0.9.0 https://github.com/ggml-org/llama.cpp.git ~/difa/llama.cpp && cd ~/difa/llama.cpp && cmake -B build -DGGML_RPC=ON -DLLAMA_CURL=OFF && cmake --build build -j --target ggml-rpc-server llama-bench
```

```bash
cd agents/device && cargo build --release
```

```bash
./target/release/difa-agent enroll --coordinator http://MAC_LAN_IP:8765 --code CODE --name "Ubuntu VM" --memory-cap-gb 8 --backend-digest llama.cpp-v0.9.0 --state ~/difa/agent.json
```

```bash
./target/release/difa-agent run --state ~/difa/agent.json --worker-bin ~/difa/llama.cpp/build/bin/ggml-rpc-server --worker-threads 4 --tunnel-listen 0.0.0.0:50100 --tunnel-advertise DEVICE_LAN_IP:50100 --accept-license apache-2.0
```

Open only the tunnel port (50100 here) to the Mac in the device's firewall; the worker port is never
exposed. Then deploy with `{"model": "qwen2.5-0.5b-q8", "require_all": true, "prefetch": true}` and
watch `GET /api/v1/groups/{group_id}/deployment` move from `preparing` to `ready`. On the HP, follow
your workplace's policy on installing software and opening ports.

Full write-up with numbers: [benchmarks/results/phase1-findings.md](benchmarks/results/phase1-findings.md).
Findings so far that change the design:

- Metal on the Mac Studio reports a working-set limit of 103,079 MB, inside the spec 3.2 planning range.
- An RPC worker advertises all host memory and has no memory-cap option, so the agent now supervises
  the worker and kills it above the owner's cap (`difa-agent run --worker-bin <ggml-rpc-server>`); the
  coordinator drains the deployment when a worker stops or a device pauses.
- A small tensor-split share can round a device down to zero layers while it still appears as a
  participant; the harness and adapter now fail any placement where a stage holds no weights.
- llama.cpp puts the output head on the last device. The planner now orders stages so the strongest
  device runs last and counts the head against its budget.
- llama.cpp labels every RPC buffer `RPC0[host:port]`; only the endpoint identifies the device.
- The latency model now includes fitted backend overhead, round trips, and per-token traffic; it predicts
  held-out topologies within 13 percent (spec target 30). Each token costs about one round trip and about
  0.62 MB of RPC traffic per remote stage, so home upload bandwidth and latency will dominate WAN speed.
- `benchmarks/netem_relay.py` simulates delay and bandwidth caps on loopback; `benchmarks/phase1_latency.py`
  runs the calibration, hold-out validation, delay sweep, and bandwidth sweep.
- Passing layer counts as `--tensor-split` moves boundaries, because llama.cpp divides layers plus the output
  slot. The adapter now computes exact split points (`difa/gguf.py`) and checks each device's loaded bytes
  against the plan.
- The mutual-TLS tunnel costs about 0.8 ms per token on loopback and 1.3 ms at 20 ms round trip
  (`benchmarks/phase1_tunnel.py`).
- llama.cpp's worker cache only covers weight tensors above 10 MiB. Prefetching them removed the expected
  load time exactly (23.1 s of 53 s at 50 Mbps, `benchmarks/phase1_prefetch.py`); for a 70B-class model
  that is roughly 98% of the weight bytes.

## Spec coverage

Implemented and tested: FR 01 to FR 05 (groups, invitations, removal), FR 06 to FR 09 and FR 26
(enrollment, owner limits, battery hysteresis, warm-hold setting), FR 13 to FR 15 (per-device ledger,
pinned requests), FR 16 to FR 21 (streaming chat, private/group branches, fork rules, single writer),
FR 22 to FR 25 (API, credentials and scopes, fair bounded queue, explicit rejections), FR 31
(filter-and-score placement with explanations), spec 7.1 lifecycle, 7.2 latency budget estimate,
spec 10 device-loss handling.

Prototype deviations to close before Phase 2 sign-off:

- Agent control transport is HTTP/JSON with a device token, not gRPC over mutual TLS (the inference data path
  to remote workers is already mutual TLS). Certificates are pinned with no rotation or expiry handling yet.
- Stored content is not yet encrypted with the group data key; no-history mode, retention purge jobs,
  and backups are not implemented.
- The client-disconnect grace window, deadline-aware rejection (FR 32), validated profiles (FR 33),
  conversation cache, and multiple placements (FR 34) are not implemented.
- Warm-hold unloading and protect-my-computer mode (FR 26, FR 27) are stored as policy but not acted on.
- Token counts are approximate; a real tokenizer comes with the Phase 1 backend.
- Windows power probing returns "unknown", which pauses battery contribution by default.
- No browser chat UI or Tauri desktop app yet.

## OpenAI-compatible subset

`GET /v1/models` and `POST /v1/chat/completions` with an application credential (`Authorization: Bearer`).
Supported: text `messages`, `max_tokens` / `max_completion_tokens`, `temperature`, `seed`, `stream`.
Rejected with `UNSUPPORTED_FIELD`: tools, functions, response formats, logprobs, `n`, audio, and non-text
content. Extensions: `difa_visibility` (`private` default, or `group` with a share-scoped key and
`difa_branch_id`) and `difa_client_request_id` for idempotency. Streaming errors after headers are sent
arrive as a terminal `data: {"error": ...}` event with no `[DONE]` marker.
