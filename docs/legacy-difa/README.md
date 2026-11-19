# Legacy DIFA documents (frozen)

These are the handoff documents of **DIFA** (Distributed Inference For All), the prototype this
repository grew from. They are kept as reference: the measured results, the reasons behind design
choices, and the test runbooks. They are **not** the plan going forward. That is
[PROJECT_PLAN.md](../../PROJECT_PLAN.md), and the move from DIFA is logged in
[changes.md](../../changes.md).

These files describe the repository as it was at commit `1dbc8eb` and were not edited afterwards. To
follow them, either check out that commit, or translate the paths and names below.

| These docs say | Now |
|---|---|
| `services/coordinator/difa/` | `coordinator/idlemesh_coord/` |
| `tests/` | `coordinator/tests/` |
| `agents/device/` | `agent/` |
| `benchmarks/`, `benchmarks/results/` | `experiments/llamacpp-feasibility/`, `.../results/` |
| `difa-agent` binary | `idlemesh-agent` |
| `DIFA_*` environment variables | `IDLEMESH_*` |
| `~/.cache/difa-target` | `~/.cache/idlemesh-target` |

| File | What it is |
|---|---|
| [HANDOFF.md](HANDOFF.md) | Project state on 2026-11-17, the Phase 1 gate, the DIFA backlog |
| [DECISIONS.md](DECISIONS.md) | DIFA decision log, including the reasons behind the llama.cpp findings |
| [ENVIRONMENT.md](ENVIRONMENT.md) | The three test machines, versions, ports, gotchas |
| [spec/](spec/) | Summaries of DIFA spec and whitepaper v0.3 |
| [testing/](testing/) | Setup guides for Mac, Windows and Ubuntu, and the three-machine test runbook |
