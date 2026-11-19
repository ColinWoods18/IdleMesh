# IdleMesh (working name)

A university-wide local LLM service that runs on idle workstations. Opted-in Windows, macOS and Linux
machines (NVIDIA, Apple Silicon or CPU-only) across campus become an availability-aware inference
cluster. Students open a chat or call an Ollama/OpenAI-compatible API. Requests join a fair queue
shared with everyone else and run immediately when nobody is waiting.

- **Plan:** [PROJECT_PLAN.md](PROJECT_PLAN.md): objectives, architecture, phases, experiments.
- **What changed from the inherited code:** [changes.md](changes.md).
- **Delivery plan:** [Sprints.md](Sprints.md). Sprint 1 (foundations, logging, CI) is complete.
- **Backend decision (llama.cpp RPC vs. custom pipeline engine):** [docs/research/backend-options.md](docs/research/backend-options.md).
- **Error codes:** [docs/error-codes.md](docs/error-codes.md). **Licences:** [docs/licenses.md](docs/licenses.md).
- **History:** this repository evolved from the DIFA prototype by Michael Todd. Its frozen handoff
  docs are in [docs/legacy-difa/](docs/legacy-difa/).

## Status

The inherited DIFA prototype, restructured and renamed, plus the sprint 1 foundations. What works today:

- A Python coordinator (FastAPI) with fair-queue scheduling, streaming requests, and an
  OpenAI-compatible `/v1/chat/completions`.
- A Rust node agent: enrollment, heartbeats over HTTPS with a pinned certificate, memory caps,
  `rpc-server` supervision, a mutual-TLS worker tunnel, and shard prefetch.
- **Observability foundations (D-11):** structured JSON logs from the coordinator and the agent, with
  IDs that follow a request across both, a shared error-code catalog, redaction of secrets and prompt
  text, size-capped log rotation, and a site configuration file.
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
| `contracts/error-codes.json` | Error-code catalog shared by coordinator and agent (`docs/error-codes.md` is generated from it) |
| `config/example.site.yaml` | Site configuration: org and site IDs, logging, telemetry, idle and contribution settings, queue limits |
| `proto/` | Draft protobuf contracts for the control channel and telemetry (review only) |
| `experiments/llamacpp-feasibility/` | Inherited benchmarks, network delay relay, and results |
| `scripts/` | Dev server, request tracing across logs, catalog and licence generators, pilot helper, smoke test |
| `docs/` | Error codes, licences, long-lead contacts, research notes, legacy DIFA docs |
| `.github/workflows/ci.yml` | CI on Windows, macOS and Ubuntu |

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
# with a site file (copy config/example.site.yaml and edit it):
~/.venvs/idlemesh/bin/python scripts/dev_server.py --fresh --insecure-http --site-config my-site.yaml
```

### Logs

- **Coordinator:** one JSON object per line on stderr, and in the rotating file named in the site config.
- **Agent:** a readable line per event on the console, and JSON lines in `logs/idlemesh-agent.log` next
  to its state file (`--log-dir`, `--log-level` or `IDLEMESH_LOG`, `--log-max-mb`, `--log-keep`).
- Each line carries `request_id`, `node_id`, `deployment_id` and `trace_id` where they apply, and an
  `error_code` from [docs/error-codes.md](docs/error-codes.md) on failures. Errors returned by the API
  include a trace ID that finds the matching log line.
- Follow one request through every log: `python scripts/trace.py REQUEST_ID coordinator.log node-*/logs/idlemesh-agent.log --context`
- Prompts, replies, passwords, tokens and keys are never logged; tests enforce it.

Interactive API docs are at http://127.0.0.1:8765/docs. For real inference with llama.cpp across
machines, follow the inherited runbooks in [docs/legacy-difa/testing/](docs/legacy-difa/testing/),
using the path and name mapping in [docs/legacy-difa/README.md](docs/legacy-difa/README.md).

After changing `contracts/error-codes.json` run `python scripts/gen_error_codes.py`; after changing the
API run `python scripts/export_openapi.py`; after changing dependencies run `python scripts/licenses.py`.
CI fails if any of these outputs is stale or a dependency's licence is not approved.
