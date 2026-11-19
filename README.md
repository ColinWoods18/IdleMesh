# IdleMesh (working name)

A university-wide local LLM service that runs on idle workstations. Opted-in Windows, macOS and Linux
machines (NVIDIA, Apple Silicon or CPU-only) across campus become an availability-aware inference
cluster. Students open a chat or call an Ollama/OpenAI-compatible API. Requests join a fair queue
shared with everyone else and run immediately when nobody is waiting.

- **Plan:** [PROJECT_PLAN.md](PROJECT_PLAN.md): objectives, architecture, phases, experiments.
- **What changed from the inherited code:** [changes.md](changes.md).
- **Backend decision (llama.cpp RPC vs. custom pipeline engine):** [docs/research/backend-options.md](docs/research/backend-options.md).
- **History:** this repository evolved from the DIFA prototype by Michael Todd. Its frozen handoff
  docs are in [docs/legacy-difa/](docs/legacy-difa/).

## Status

The code is the inherited DIFA prototype, restructured and renamed. What works today:

- A Python coordinator (FastAPI) with fair-queue scheduling, streaming requests, and an
  OpenAI-compatible `/v1/chat/completions`.
- A Rust node agent: enrollment, heartbeats over HTTPS with a pinned certificate, memory caps,
  `rpc-server` supervision, a mutual-TLS worker tunnel, and shard prefetch.
- A llama.cpp RPC adapter. It was tested across macOS (Metal), Windows (CPU) and Linux (CPU) in seven
  measurement rounds ([results](experiments/llamacpp-feasibility/results/phase1-findings.md)).

Not built yet: idle detection, GPU profiling, the network graph and clustering, the availability
predictor, telemetry and Grafana, Ollama endpoints, SSO, the web chat UI, and gRPC. See the plan.

> **llama.cpp RPC is research-grade.** It has no authentication or encryption. Workers must stay on
> loopback behind the agent's mTLS tunnel (`--tunnel-listen`), and the agent refuses to expose a
> plaintext worker on a network address.

## Layout

| Path | What it is |
|---|---|
| `coordinator/idlemesh_coord/` | Coordinator: API gateway, scheduler and fair queue, placement and latency model, llama.cpp adapter, GGUF tools, tunnel |
| `coordinator/tests/` | Unit and integration tests (pytest) |
| `agent/` | Rust node agent (`idlemesh-agent`) |
| `contracts/http/openapi.json` | Exported HTTP contract (`scripts/export_openapi.py`) |
| `experiments/llamacpp-feasibility/` | Inherited benchmarks, network delay relay, and results |
| `scripts/` | Dev server, pilot operator helper, smoke test |
| `docs/` | Research notes and legacy DIFA docs |

## Develop

Python 3.10+ and Rust stable. Keep the virtual environment, Cargo build output and models **outside**
synced folders such as OneDrive or iCloud.

```bash
python -m venv ~/.venvs/idlemesh
~/.venvs/idlemesh/bin/python -m pip install -e ".[dev]"   # Windows: ~/.venvs/idlemesh/Scripts/python
~/.venvs/idlemesh/bin/python -m pytest -q
```

```bash
cd agent && CARGO_TARGET_DIR=~/.cache/idlemesh-target cargo test
```

Start a local coordinator with the fake runtime on loopback:

```bash
~/.venvs/idlemesh/bin/python scripts/dev_server.py --fresh --insecure-http
```

Interactive API docs are at http://127.0.0.1:8765/docs. For real inference with llama.cpp across
machines, follow the inherited runbooks in [docs/legacy-difa/testing/](docs/legacy-difa/testing/),
using the path and name mapping in [docs/legacy-difa/README.md](docs/legacy-difa/README.md).

Known issue on Windows: one unit test fails because the coordinator's private key file is not
restricted to its owner there. See [changes.md](changes.md#7-known-issues-carried-over).
