# Distributed Idle-Compute Inference — Project Plan

> Working name: **IdleMesh** (placeholder)
> Goal: Turn idle, heterogeneous workstations (Windows, macOS, Linux; NVIDIA / Apple Silicon / CPU-only) across multiple buildings into an availability-aware, fault-tolerant LLM inference cluster with an Ollama-compatible interface.
> **Primary use: a university-wide local LLM service.** Any student opens a chat (or uses the API); the request joins a fair queue shared with all other users and is served immediately when nobody is waiting.
> Approach: Engineering/research first → working proof of concept + dataset → paper → (optional) commercialization.
> Sprint-by-sprint delivery plan: [Sprints.md](Sprints.md).
> Starting point: this repository evolves the DIFA prototype handed off by Michael Todd (forked at `1dbc8eb`). What was kept, changed or dropped is logged in [changes.md](changes.md).

---

## 1. Objectives

1. **Map & profile** every opted-in workstation: hardware, backend capability, network latency/bandwidth, idle patterns.
2. **Cluster & place** model layers across nodes using a network graph + availability prediction.
3. **Serve inference** across mixed hardware/OSes with graceful departure and fault recovery.
4. **Expose** an Ollama/OpenAI-compatible API so users can plug in models.
5. **Serve students** through a web chat and the API: university sign-in, a fair queue (round-robin across users, FIFO within a user), visible queue position, streaming replies, cancellation, and per-user limits.
6. **Observe and support remotely (D-11):** detailed structured logs, error codes, crash reports, node health and per-request performance telemetry from every component. Admins can diagnose and fix nodes remotely (diagnostic bundles, remote config, staged signed updates), and the tool's performance is benchmarked continuously. See §6a.
7. **Produce** a research dataset and paper.

### Research contributions (paper)
| # | Contribution | Hypothesis |
|---|---|---|
| C1 | Availability-aware placement | Predicting idle windows reduces failed requests and tail latency vs. reactive recovery (Petals-style). |
| C2 | Graceful departure protocol, extended by **yield, don't leave** (D-12, §2a) | A few seconds of departure notice converts most hard failures into near-zero-cost handoffs. Throttling a returning user's node to minimal compute, instead of dropping it, stretches that notice window to minutes with no noticeable impact on the user. (With llama.cpp RPC this is request-level only. Mid-request handoff comes from the Phase 3 stage engine, built on patched llama.cpp (D-09) — see [docs/research/backend-options.md](docs/research/backend-options.md).) |
| C3 | Cross-backend pipelines | Mixed CUDA/Metal/Vulkan/CPU pipelines are viable; throughput-proportional partitioning beats memory-proportional; numerical drift is measurable but small. |

---

## 1a. What we reuse from DIFA (Michael Todd's code)

IdleMesh starts from Michael's DIFA prototype, not from zero. This section is the single place that says what his code gives the project. File-level verdicts and renames are in [changes.md](changes.md) (Restructure entry, §6). The phase checklists below mark inherited work with **[~]**.

**By size:** about 5,000 lines of coordinator and agent source, not counting tests. Approximate split, by file:

| Share | Lines | What |
|---|---|---|
| **Used as is** | ≈ 1,500 (30%) | GGUF parsing, split points, mTLS tunnel, pinned HTTPS control, prefetch, request lifecycle, memory ledger, fake runtime, secrets hashing, error envelope |
| **Used and extended** | ≈ 3,050 (61%) | Placement, fair queue and scheduler, llama.cpp adapter, OpenAI API, agent worker/policy/probe/CLI, storage, device and request routes |
| **Replaced later** | ≈ 450 (9%) | Groups, invitations, recovery codes, group access rules, sample model catalog, conversation forking |

Also inherited: 70 passing tests (about 1,700 lines), four experiment scripts plus a network emulator (`netem_relay.py`), seven rounds of measured results, and the operator scripts (`pilot.py`, `smoke.py`, `dev_server.py`).

**By capability:**

| IdleMesh needs | What DIFA provides | Status |
|---|---|---|
| Enrolling and controlling nodes | One-time-code enrollment; 5 s heartbeats over HTTPS with a pinned coordinator certificate | ✅ Working today (gRPC later, D-06) |
| Secure link to workers | Mutual-TLS tunnel with pinned certificates around llama.cpp RPC | ✅ Working today |
| Getting model files onto nodes | GGUF parsing, exact split points, byte-range prefetch with SHA-256 into llama.cpp's cache | ✅ Working today |
| Running models | llama.cpp adapter; supervised `rpc-server` workers for split models | 🔧 Foundation: add single-node replicas (D-05) and `-np` batching |
| Placement (C1) | Filter-and-score placement; latency model within 6–22% on real links | 🔧 Foundation: add predicted availability and link speed |
| Student fair queue | Round-robin across users, FIFO within a user, cancellation, deadlines, lifecycle, all-or-nothing memory ledger | 🔧 Foundation: scale from 1 active generation to campus size |
| Chat API | OpenAI `/v1/models`, `/v1/chat/completions` with streaming | 🔧 Foundation: add Ollama endpoints |
| Protecting the owner's machine | Memory-cap watchdog (250 ms), battery hysteresis, leases, drain | 🔧 Foundation: add the D-12 governor and OS-level limits |
| Hardware profile | OS, architecture, memory, power, per-backend decode benchmark | 🔧 Foundation: add GPU/VRAM detection and Windows power |
| Network measurement | Round-trip time measured from heartbeats | 🔧 Foundation: add the network graph and bandwidth tests |
| Student privacy | Private-by-default history, retention limits, no prompt text in logs or audit | 🔧 Design carries over; confirm with the privacy office |
| Sign-in and roles | Group accounts, invitations, recovery codes | 🔁 Replaced by university SSO, sites and roles |
| Idle detection and prediction (C1) | — | 🆕 New |
| Yield, don't leave (D-12) | — (builds on the memory watchdog) | 🆕 New |
| Logs and telemetry (D-11) | — (DIFA has an audit trail only) | 🆕 New |
| One-click installer and service (D-07) | — (DIFA's agent is set up from the command line) | 🆕 New |
| Chat web page and admin dashboard | — (DIFA is API only) | 🆕 New |
| Stage engine (D-09) | — | 🆕 New, on patched llama.cpp |

**Measured findings we rely on** (from `experiments/llamacpp-feasibility/results/`):
- **Latency model constants:** 3.58 ms for the first stage, 0.74 ms for each extra stage, about 1.02 round trips per token per stage, about 0.62 MB per token per stage. They let placement predict speed before anything runs.
- **llama.cpp's exact split rules:** n_layer + 1 split slots, with the output layer on the last device.
- **llama.cpp RPC is hub-and-spoke**, its worker set is fixed at load time, and it can't recover mid-reply. This finding drove D-04 and D-09.
- **A three-machine pilot across Windows, macOS and Linux** proved that the control channel and tunnel work on all three.
- **Round 7:** the model under-predicts a Mac on Wi-Fi. This is still open and feeds into link-aware placement and E4.

Ownership of this code still needs a written agreement with Michael (Phase 4). The agent is currently marked `UNLICENSED`.

---

## 2. Architecture Overview

```
                         ┌──────────────────────────┐
   Users / Apps  ───────▶│  API Gateway (Ollama/    │
                         │  OpenAI-compatible)      │
                         └────────────┬─────────────┘
                                      │
                         ┌────────────▼─────────────┐
                         │       COORDINATOR        │
                         │  - Node registry         │
                         │  - Network graph         │
                         │  - Clusterer             │
                         │  - Availability predictor│
                         │  - Placement engine      │
                         │  - Router / orchestrator │
                         └───┬──────────┬───────────┘
              control (gRPC/mTLS)       │ telemetry
                 ┌───────────┘          ▼
                 │              ┌───────────────┐
                 │              │ Telemetry DB  │◀── Dashboard
                 │              └───────────────┘
   ┌─────────────▼───┐   ┌─────────────────┐   ┌─────────────────┐
   │ Node Agent (Win)│   │ Node Agent (Mac)│   │ Node Agent (Lin)│
   │ - profiler      │   │ - profiler      │   │ - profiler      │
   │ - idle monitor  │   │ - idle monitor  │   │ - idle monitor  │
   │ - net prober    │   │ - net prober    │   │ - net prober    │
   │ - worker (CUDA) │   │ - worker (Metal)│   │ - worker (CPU)  │
   └────────┬────────┘   └────────┬────────┘   └────────┬────────┘
            └──── activations (hidden states) over LAN ─┘
```

**Key design principle:** unify the *protocol*, not the platform. Each node runs its native backend. No VMs.

> **Correction from measured data:** with llama.cpp RPC the data path is *hub-and-spoke*, not a chain. One head `llama-server` drives every remote `rpc-server`, at about one round trip per remote stage per token and about 0.6 MB of traffic per remote stage per token (measured on a 0.5B model; mostly graph serialisation, not hidden states). Pure hidden-state exchange between stages would need a custom stage-server engine. See [docs/research/backend-options.md](docs/research/backend-options.md).
>
> **Two serving paths:** (1) models that fit one node run as **whole-model replicas** on single idle nodes; this is the default for student traffic. (2) Models too large for any one node are **layer-split** across a cluster.

### 2a. Elastic contribution: yield, don't leave (D-12)

Contribution is **not on/off**. Each node has a contribution level that rises and falls with policy and with what the machine's user is doing. The fleet as a whole scales from "a minimal fraction of workstations" to "everything idle" according to settings and demand.

**Scaling up (per node).** The agent's *contribution governor* sets a level from:
- **Idle duration:** a ramp, for example 5 min idle → light, 30 min → medium, 60 min+ → full.
- **Time windows:** for example, overnight or weekends allow much higher levels.
- **Admin and owner settings:** the maximum CPU, GPU and memory share, which never exceed the owner's local caps.
- **Live load and power:** the machine's own CPU and GPU load, and whether it's on battery.

**Scaling up (fleet).** The coordinator activates only as many nodes as demand needs, between a configured minimum and maximum:
- it adds nodes as the queue grows and releases them as it shrinks;
- it scales up overnight;
- it keeps a few warm standbys for recovery.

**Yield on user activity (soft departure).** When someone starts using a node, it doesn't vanish:

```
 user input / local load rises
   → within ~1 s: governor drops to the minimal level
       CPU: lowest priority + fewest threads
       GPU: duty-cycled pauses between decode steps (target ≈2–5% utilisation)
       no new work accepted; heartbeat reports "yielding"
   → coordinator picks the cheapest safe exit:
       replica path: let it finish slowly, or re-run elsewhere if faster
       split path:   hand off quickly (the slowest stage throttles the whole chain)
       stage engine: migrate KV state to a replica (D-09)
   → at the handoff point: unload weights, free memory (VRAM included)
   → hard backstop: if the user's memory pressure crosses a threshold, kill
     the worker now (existing cap watchdog) and recover like a hard failure
```

**What can and can't shrink while still working:**
- **CPU and GPU compute** shrink almost immediately.
- **CPU-node RAM** shrinks partly by itself: weights are memory-mapped, so the OS can drop those pages under pressure. That costs speed but not correctness.
- **GPU VRAM** is released only at handoff.

**Why it helps fault tolerance:** most "node falls off" events on workstations are a user coming back. Yielding turns those from hard failures into slow nodes that are still reachable, so the departure-notice window grows from seconds to as long as the throttled node can keep going without bothering the user. This refines C2.

---

## 3. Recommended Tech Stack

| Component | Choice | Rationale |
|---|---|---|
| Node agent | **Rust** (changed from Go, decision D-01) | Single static binary for Win/macOS/Linux, like Go. The inherited agent already enrolls, heartbeats, supervises `rpc-server` under a memory cap, runs the mTLS worker tunnel and prefetches weight shards, and has been tested on all three OSes. |
| Coordinator | **Python (FastAPI)** | Fast iteration on scheduling/ML; `networkx`, `numpy`, `scikit-learn`. Port hot paths later if needed. |
| Control plane | **gRPC + mTLS** (target); **HTTPS/JSON with a pinned coordinator certificate** (current, inherited) | Typed contracts, streaming heartbeats, secure by default. Rust side via `tonic`. |
| Worker data path | **mTLS tunnel** around llama.cpp RPC (inherited) | llama.cpp RPC has no authentication or encryption; workers stay on loopback. |
| Chat UI | **React + TypeScript** web chat served by the gateway | Students need no install. |
| Identity | **University SSO** (OIDC/SAML via campus IdP) | Replaces DIFA's group-local accounts; needs IT engagement. |
| Inference backend | **llama.cpp** (`rpc-server`, GGUF) | MIT; runs on CUDA, Metal, Vulkan, CPU; same model file everywhere. |
| Stage engine (Phase 3, D-09) | **Patched llama.cpp library + `idlemesh-stage`** | Path B: a small layer-range patch lets each node run only layers [a, b) and pass hidden states along a chain. It reuses llama.cpp's kernels, GGUF, tokenizer, batching and per-sequence state save/restore. See [backend-options §5](docs/research/backend-options.md#5-path-b-in-detail-the-chosen-custom-engine-route). |
| Model source | **Ollama library / GGUF** | Plug-and-play models; Ollama-compatible API on gateway. |
| Telemetry DB | **SQLite** (Phase 1) → **PostgreSQL + TimescaleDB** (pilot) | Start simple; time-series at scale. (Check TimescaleDB's licence before commercial bundling; see §6a.) |
| Observability (D-11) | **Structured JSON logs + OpenTelemetry-compatible metrics and traces**: Rust `tracing`, Python logging/OTel; crash reports; diagnostic bundles | Remote diagnosis and fixes, continuous benchmarks, research data. Permissive licences in shipped components. |
| Network probing | **Built-in agent probes** (RTT and bulk transfer through the tunnel); iperf3 only as a lab cross-check | Bandwidth + latency matrix with no extra software on nodes (D-07). |
| Dashboard | **React + TypeScript** (or Grafana for early phases) | Grafana gives instant visibility before custom UI. |
| Packaging | MSI (Win), signed .pkg (macOS), .deb/.rpm + systemd (Linux) | Native, IT-friendly installs. **One-click and self-contained** (D-07): each installer bundles the prebuilt agent and the pinned llama.cpp binaries. Nothing else is downloaded or installed on the node. |
| Build & release | CI builds per OS/arch (e.g. GitHub Actions with Windows, macOS and Linux runners) | Only build machines need Rust and C/C++ toolchains; nodes never do. |

### Node install requirement (D-07)

A new node computer must only need to **run the installer, approve it, and wait**. Specifically:

- **No prerequisites to install:** no Rust, compilers, Visual Studio, Python, Docker, WSL, CUDA toolkit, package managers or terminal steps.
- **Self-contained:** the installer contains the agent (Windows build statically linked against the C runtime with `crt-static`, so the VC++ redistributable isn't needed) and **prebuilt llama.cpp binaries** pinned to the exact version every node uses.
  - The installer picks the CPU, CUDA, Vulkan or Metal build for the node's hardware.
  - CUDA runtime libraries ship inside the package. Only the NVIDIA driver already on the machine is assumed.
- **Runs as a background service** (Windows service, launchd daemon, systemd unit). It starts at boot and needs no logged-in terminal.
- **Two supported install paths, using the same installer file** (D-08):
  1. **Manual, one workstation at a time** (always available, the baseline). Someone downloads or copies the installer to the machine and double-clicks it. A short wizard then:
     - shows the consent and resource-limit choices;
     - asks for the coordinator address and an enrollment code. The admin dashboard generates the code and shows it as text or a QR code, with the coordinator fingerprint built in.

     It then enrolls the machine and starts the service. No IT tooling is involved.
  2. **Pushed to many machines** (optional). The same MSI or `.pkg` runs silently, with the coordinator address and enrollment token passed as installer properties or a config file. This works with campus tools (Intune, SCCM, Jamf) or a script.
- **Antivirus and endpoint security:**
  - Pushes and agent behaviour can be flagged by antivirus or EDR tools. The behaviours most likely to look suspicious: a background service opening a listening port, downloading multi-GB files, starting child processes (`rpc-server`, `llama-server`), and, later, auto-updating itself.
  - Mitigations:
    - sign everything;
    - keep binary hashes stable and published per release;
    - give campus IT a short allowlist document (signer, install paths, the single tunnel port, the coordinator address);
    - test each release against the campus antivirus product before a rollout;
    - deliver auto-updates as signed full installers, not in-place binary patching.
  - Because the manual path doesn't depend on any push tool, a blocked push never blocks onboarding.
- **Updates:** a node installed manually must still update without a visit. The agent fetches a signed installer from the coordinator and checks it before running it. An admin can also re-run the newer installer by hand.
- **Signed:** an Authenticode-signed MSI; on macOS, a signed and notarized `.pkg`.
  - **Sign every executable inside the package too**, not just the installer: the agent and each bundled llama.cpp binary and DLL.
  - Windows 11 **Smart App Control** and IT application-control policies (WDAC, AppLocker) block unsigned or unknown programs. This was observed on the dev machine on 2026-11-18. Upstream llama.cpp release binaries are not signed by us, so build them in CI and sign them with the project certificate.
- **Models are not part of the installer.** They download later into the node's cache, managed by the agent (prefetch) and bounded by its disk cap.
- **Clean uninstall** removes the service, binaries and model cache.

**License rule:** permissive dependencies only (MIT/Apache-2.0/BSD) in core. Avoid GPL (e.g., EXO) in shipped components; use GPL tools only as research baselines.

---

## 4. Repository Structure

Target layout. ✅ = exists now (mostly inherited from DIFA, see [changes.md](changes.md)); everything else is still to be created.

```
IdleMesh/                       # github.com/ColinWoods18/IdleMesh (fork of TheCodeheadMT/I4A); "IdleMesh" is still a working name
├── README.md                   ✅ rewritten for IdleMesh
├── PROJECT_PLAN.md             ✅
├── changes.md                  ✅ log of changes from the inherited DIFA repo
├── Sprints.md                  ✅ numbered sprint plan
├── .github/workflows/ci.yml    ✅ CI: Windows, macOS, Ubuntu (S1.5)
├── LICENSE                     # decide in Phase 4; keep deps permissive
├── docs/
│   ├── architecture.md
│   ├── telemetry-schema.md
│   ├── privacy-consent.md
│   ├── error-codes.md          ✅ generated from contracts/error-codes.json (S1.8)
│   ├── licenses.md             ✅ generated dependency licence inventory (S1.10)
│   ├── long-lead.md            ✅ contacts and first messages for long-lead items (S1.13)
│   ├── research/
│   │   ├── backend-options.md  ✅ llama.cpp RPC vs. custom pipeline engine
│   │   ├── experiments.md
│   │   └── related-work.md
│   └── legacy-difa/            ✅ frozen DIFA handoff, decisions, spec summaries, runbooks
├── proto/idlemesh/v1/          ✅ draft contracts, review only (S1.11): control.proto, telemetry.proto
├── contracts/
│   ├── http/openapi.json       ✅ exported HTTP contract (inherited)
│   └── error-codes.json        ✅ error-code catalog shared by coordinator and agent (S1.8)
├── agent/                      ✅ Rust crate `idlemesh-agent`
│   ├── src/
│   │   ├── main.rs             ✅ CLI: enroll / run
│   │   ├── control.rs          ✅ pinned-certificate HTTPS control client
│   │   ├── probe.rs            ✅ OS, memory, power, decode probe → grows into profiler
│   │   ├── policy.rs           ✅ owner caps, battery hysteresis, leases
│   │   ├── worker.rs           ✅ rpc-server lifecycle + memory watchdog
│   │   ├── tunnel.rs           ✅ mTLS worker tunnel
│   │   ├── prefetch.rs         ✅ shard download into llama.cpp cache
│   │   ├── logging.rs          ✅ JSON log file, rotation, redaction, correlation IDs (S1.6)
│   │   ├── secure_fs.rs        ✅ owner-only secret files on every OS (S1.3)
│   │   ├── idle/               # per-OS idle detection (new)
│   │   ├── netprobe/           # peer RTT/bandwidth (new)
│   │   └── consent/            # (new)
│   └── packaging/
├── engine/                     # Phase 3 stage engine (D-09)
│   ├── llama.cpp-patch/        # layer-range patch series against the pinned tag + build script
│   └── stage/                  # idlemesh-stage: runs layers [a, b), KV state export/import
├── coordinator/                ✅ Python
│   ├── idlemesh_coord/         ✅ (inherited modules; see changes.md for keep/adapt/replace)
│   │   ├── domain/placement.py ✅ filter-and-score + latency model → placement engine
│   │   ├── domain/fairqueue.py ✅ fair queue → student queue
│   │   ├── scheduler.py        ✅ admission, lifecycle, cancellation
│   │   ├── runtime/llamacpp.py ✅ llama.cpp adapter
│   │   ├── gguf.py, partition.py ✅ GGUF parsing, exact splits, shard manifests
│   │   ├── transport.py        ✅ coordinator side of the mTLS tunnel
│   │   ├── api/                ✅ gateway (OpenAI subset now; add Ollama endpoints)
│   │   ├── obs.py              ✅ JSON logs, correlation IDs, redaction (S1.7, S1.9)
│   │   ├── site_config.py      ✅ site configuration loader (S1.12)
│   │   ├── registry/           # (new)
│   │   ├── graph/              # latency/bandwidth graph + clustering (new)
│   │   ├── predictor/          # availability forecasting (new)
│   │   └── router/             # (new)
│   └── tests/                  ✅
├── chat/                       # student web chat (new)
├── dashboard/
├── experiments/
│   ├── llamacpp-feasibility/   ✅ inherited benchmarks, netem relay, 7 rounds of results
│   ├── baselines/              # petals, exo, parallax, llama.cpp-rpc
│   └── analysis/
├── scripts/                    ✅ dev server, trace.py, gen_error_codes.py, licenses.py, pilot helper, smoke test
├── deploy/                     # config templates, docker-compose for coordinator
└── config/
    └── example.site.yaml       ✅ (S1.12)
```

---

## 5. Phase Plan

### Phase 1 — Nodes, Profiler, Coordinator
**Goal:** Live map of all nodes and clusters; real idle data accumulating.

Legend: **[~]** = partly provided by inherited DIFA code (what exists is noted in *italics*).

**Agent**
- [ ] Opt-in install flow; consent recorded locally + sent to coordinator
- [~] Registration + heartbeat (gRPC, mTLS, every 5–10 s) — *exists: one-time-code enrollment and 5 s heartbeats over HTTPS with a pinned coordinator certificate; gRPC still to do*
- [~] Hardware profiler: CPU, RAM, GPU model/VRAM, unified memory, OS — *exists: OS, arch, available memory, power (macOS/Linux; Windows reads "unknown"); no GPU/VRAM yet*
- [ ] Backend detection: CUDA / Metal / Vulkan / CPU
- [~] Micro-benchmark: tokens/sec per layer on a reference GGUF model — *exists: llama-bench decode probe per backend, fed into placement*
- [ ] Idle monitor (per OS):
  - Windows: `GetLastInputInfo`
  - macOS: IOKit `HIDIdleTime`
  - Linux: logind `IdleHint` / X11 XScreenSaver / Wayland idle protocol
- [~] Also track: CPU/GPU load, logged-in session, power/battery state, lid/sleep — *exists: power/battery with hysteresis*
- [ ] Net prober: RTT to peers; scheduled bandwidth tests (rate-limited, off-hours preferred). Use a built-in bulk transfer through the tunnel rather than iperf3, so nodes need no extra software (D-07). *Lesson inherited: heartbeat RTT overstates the path 3–10×; time RTT on the real data path. With llama.cpp RPC only head↔worker edges matter, so all-pairs probing is only needed to choose heads.*

**Coordinator**
- [~] Node registry + telemetry ingestion — *exists: device registry with capabilities and heartbeats in SQLite; no time-series telemetry*
- [ ] Network graph (nodes = machines; edges = RTT + bandwidth)
- [ ] Clusterer v0: threshold graph on bandwidth/RTT → connected components / community detection
- [x] Availability predictor v0: per-node hour-of-week idle probability histogram — *S6.1; real-data evaluation waits for S6.6*
- [ ] Grafana view of nodes, clusters, idle heatmaps

**Exit criteria:** ≥ 5 nodes across ≥ 2 OSes reporting; graph + clusters auto-update; ≥ 2 weeks of idle data.

### Phase 2 — Inference, Fault Tolerance, Orchestration
**Goal:** A model split across heterogeneous nodes that survives forced departures.

- [~] Worker: agent manages `llama.cpp rpc-server` lifecycle on demand — *exists: supervised worker with memory-cap watchdog, lease, drain; add whole-model `llama-server` workers for the replica path*
- [~] Gateway: Ollama/OpenAI-compatible endpoints (`/api/generate`, `/api/chat`, `/v1/chat/completions`) — *exists: `/v1/models` and `/v1/chat/completions` (streaming); Ollama endpoints to add*
- [~] Model manager: pull GGUF from Ollama library; distribute shards to assigned nodes; local cache — *exists: per-node byte-range prefetch with SHA-256 into llama.cpp's weight cache*
  - **Customer-supplied models (D-10):** admins add models themselves through the Ollama-style interface (pull by name, or a GGUF file they provide). The product ships and preselects no models.
  - When a model is added, show its licence (Ollama licence layer / GGUF metadata), require the admin to accept it, and record who accepted which licence and when, per model revision. This extends the agent's existing `--accept-license` check.
  - Check the Ollama registry's terms for third-party pulls; support Hugging Face and local files as other sources.
- [ ] Router: small models → full replicas on single nodes; large models → layer-split across a cluster
- [~] Placement engine v1: layers ∝ measured throughput × predicted availability — *exists: filter-and-score placement + latency model within 6–22% on real links; availability term and contribution-weighted capacity added in S6.3.* Replication = whole standby deployments (llama.cpp RPC has one copy per layer).
- [ ] **Graceful departure (C2), as yield-don't-leave (D-12):** user activity → the governor throttles the node to its minimal level within about 1 s and reports `yielding` → no new work → the coordinator lets it finish slowly or hands off at the next safe point → release memory. The memory-pressure kill remains the backstop. With llama.cpp RPC the handoff is between requests; see D-04.
- [ ] **Contribution governor (D-12):** per-node levels from the idle-duration ramp, time windows (overnight or weekends), admin and owner settings, and live load and power. Applied through CPU priority and threads, GPU duty-cycling, and memory caps.
- [ ] **Fleet scaling (D-12):** a minimum and maximum number of active nodes, scaled with queue demand and time windows, with warm standbys for recovery.
- [ ] **Hard-failure recovery (request level):** on node loss, re-run the request on a replica / standby deployment, continuing from text already streamed. (Stage-level replay that rebuilds the KV cache needs a custom stage-server engine — Phase 3 research, D-04.)
- [ ] Background re-planning; migrations only between requests

**Student service (queue + chat)**
- [~] Fair queue: round-robin across users, FIFO within a user, queue position, cancellation, wait/run deadlines — *exists (`fairqueue.py`, `scheduler.py`) but sized for a small group (1 active generation, 2 per user, 10 total); scale up and dispatch across many deployments*
- [ ] Concurrency: `llama-server -np N` continuous batching per deployment; measure throughput vs. N over RPC
- [ ] University SSO; per-user quotas and rate limits; API keys for programmatic use (*credential scopes exist*)
- [ ] Web chat UI with queue position and streaming
- [ ] Replace DIFA groups/invitations with `org_id`/`site_id` + roles (student, staff, admin)

**Exit criteria:** 70B-class model running across ≥ 3 heterogeneous nodes; scripted kill/drain tests pass with measured recovery times.

### Phase 3 — Dashboard, Pilot, Paper
- [ ] Admin dashboard: node map, cluster health, idle analytics, model catalog, request metrics
- [ ] **Stage engine prototype (path B, D-09):**
  - [ ] Review prior art first (prima.cpp and other llama.cpp pipeline forks): status, licence, reuse
  - [ ] Layer-range patch for llama.cpp (Llama and Qwen families) + a greedy-output equivalence test against a single device
  - [ ] `idlemesh-stage` binary, supervised by the agent behind the mTLS tunnel; built and signed in CI (D-07)
  - [ ] Router: tokenize, chain hidden states, sample, keep per-stage inputs
  - [ ] Recovery arms for E2/C2: request re-run (RPC baseline) vs. replay to a replica vs. KV-state migration
  - [ ] Propose the layer-range feature upstream to llama.cpp
  - [ ] Gate: promote to production if Phase 2 split deployments show high failure or latency
- [ ] Pilot rollout: 3–5 nodes → one building → all buildings
- [ ] Experiment harness + baselines (Petals, EXO, Parallax, plain llama.cpp RPC)
- [ ] Data analysis + paper draft → workshop submission (MLSys / NSDI workshop)

### Phase 4 — Foundations & Commercialization (post-PoC)
- [ ] Confirm IP ownership with employer (university IP policy), **before any commercial step**
- [ ] Written assignment or licence for Michael Todd's inherited code (the agent is currently marked `UNLICENSED`)
- [ ] Choose project license; dependency license audit in `docs/licenses.md`, including llama.cpp build options (CUDA runtime, BLAS) and the third-party notices shipped in every installer
- [ ] Customer licence agreement (EULA) that makes customers responsible for the models they add and those models' licences and acceptable-use policies (D-10)
- [ ] Hosted (SaaS) offering, if ever: re-check model licences, because the operator then becomes us
- [ ] Patent clearance search on the router and handoff design before a commercial launch
- [ ] University pilot: FAIRlab acts as the customer, so verify the licence of every model offered to students
- [ ] Formal privacy/consent policy; IRB if university-involved
- [ ] Packaging, pricing model, design-partner pilots

---

## 6. Data Model (v1 draft)

All records carry `org_id`, `site_id`, `schema_version`. Node/user identifiers are **pseudonymized** at collection (salted hash); raw hostnames stay local unless admin-enabled.

```yaml
Node:
  node_id: uuid
  org_id: str
  site_id: str
  building: str
  os: [windows, macos, linux]
  cpu: {model, cores, threads}
  ram_gb: float
  gpus: [{vendor, model, vram_gb, backend: [cuda, metal, vulkan, cpu]}]
  unified_memory: bool
  benchmark: {ref_model, tokens_per_sec, layer_ms}
  consent: {granted: bool, timestamp, policy_version}

IdleSample:            # every 30–60 s
  node_id, ts,
  seconds_since_input: int
  session_active: bool
  cpu_util: float
  gpu_util: float
  mem_free_gb: float
  on_battery: bool
  state: [idle, active, sleeping, draining, serving, yielding]
  contribution_level: float      # 0.0-1.0 from the governor (D-12)
  level_reason: [idle_ramp, time_window, policy, user_activity, local_load, power]

NetEdge:               # periodic
  src_node, dst_node, ts,
  rtt_ms_p50, rtt_ms_p95,
  bandwidth_mbps,
  method: [tcp, tunnel_bulk, iperf3_lab]

InferenceEvent:
  request_id, model, model_revision, route: [replica, split, stage_engine], ts_start, ts_end,
  nodes: [node_id], stages: int,
  queue_wait_ms, ttft_ms,                     # time to first token
  tokens_in, tokens_out, tps,
  stage_ms: [{node_id, compute_ms, network_ms}],
  predicted_ms_per_token, actual_ms_per_token,
  outcome: [ok, recovered, failed, cancelled, expired],
  error_code: str | null,
  recovery_ms: int | null,
  departure_type: [none, graceful, hard] | null,
  versions: {coordinator, agent, llama_cpp}
  # never: prompt or response text

# --- Observability records (D-11) ---
LogEvent:              # structured JSON log line (agent, coordinator, worker capture)
  ts, level, component, node_id?, deployment_id?, request_id?,
  error_code?, message, fields: {}, version

ErrorEvent:            # a counted occurrence of a catalogued error
  ts, error_code, component, node_id?, request_id?, count, context: {}

CrashReport:
  crash_id, ts, component: [agent, worker, stage, coordinator],
  node_id, version, build_hash, os, backtrace, exit_code?,
  log_tail (redacted), config_hash

NodeHealthSample:      # every 30-60 s alongside IdleSample
  node_id, ts, cpu_util, gpu_util, mem_used_gb, gpu_mem_used_gb, temp_c?,
  cache_disk_free_gb, agent_uptime_s, worker_restarts, cap_kills,
  tunnel_errors, telemetry_buffer_bytes, telemetry_dropped

DiagnosticBundle:      # on admin request, with node consent
  bundle_id, node_id, requested_by, ts, contents: [logs, versions, probes,
  health, redacted_config], size_bytes, retention_until

CanaryResult:          # scheduled benchmark (golden prompts)
  ts, model, route, nodes, tps, ttft_ms, greedy_match: bool, regression: bool
```

---

## 6a. Observability, Diagnostics & Remote Support (D-11)

**Why it matters:**
- **Remote fixes:** a fleet of workstations across buildings can't be debugged by walking up to each
  machine. Every failure has to be diagnosable, and every fix deliverable, from the coordinator.
- **Performance tracking:** the same data gives continuous benchmarks of the tool's performance.
- **Research data:** it is also part of the research dataset (E1–E5).

**What gets collected**

| Signal | Source | Purpose |
|---|---|---|
| Structured logs (JSON, levels, request/node/deployment IDs) | Agent, coordinator, captured llama.cpp worker output | Follow one request across all components; root-cause analysis |
| Error codes and counts | Everywhere, from one catalog (`docs/error-codes.md`) | Error rates by code, node and version; user-facing reference IDs |
| Crash reports | Agent panic hook; worker and stage exit status and log tail | Fix crashes we never saw happen |
| Node health | Agent | Spot failing or overloaded machines before users notice |
| Request performance (`InferenceEvent`) | Coordinator and router | p50/p95/p99 latency, throughput, queue wait, prediction accuracy |
| Recovery and drain events | Coordinator and agent | Reliability metrics; the C2/E2 dataset |
| Benchmark canaries | Scheduled golden prompts | Catch performance or correctness regressions after updates |
| Version inventory | Agent heartbeats | Know which nodes run what; target fixes |

**Remote support loop**

```
 detect (alert / error spike / crash report / user reference ID)
   → diagnose (traces, logs, diagnostic bundle, temporary debug log level on one node)
   → fix (remote config change, or new signed build)
   → roll out (canary group → everyone; automatic halt and rollback on rising errors)
   → verify (error rate, canaries, crash-free rate back to normal)
```

**Rules**

- **Never collect content.** No prompt or response text, API keys, tokens, passwords, or usernames in
  file paths. Redaction is enforced by tests (S1.9).
- **Consent and transparency.** The node consent screen covers diagnostics. Users and admins can see
  what is collected. Telemetry has its own retention limits, set with the privacy office.
- **Tiers.**
  - Operational (always on): health, errors, request metrics.
  - Detailed diagnostics (on request, time-limited): debug logs and diagnostic bundles.
  - Research export (anonymised, opt-in by site).
- **Resilience.** Agents buffer telemetry on disk while offline (bounded, drops counted). Telemetry
  must never slow inference, so it is sent asynchronously and rate-limited.
- **Stays with the customer by default.** For a sold product, telemetry lives on the customer's own
  coordinator. Any "send to vendor for support" channel is opt-in, visible to the customer, and sends
  only what they approve (Phase 4).
- **Versioned schemas** (`schema_version` on every record), so data stays readable across releases.

**Stack**
- OpenTelemetry-compatible structured logs, metrics and traces: Rust `tracing`, Python logging and
  OpenTelemetry, both Apache-2.0 or MIT.
- Stored on the coordinator: SQLite in Phase 1, then a time-series store.
- Viewed in Grafana at first, then the admin and support dashboard (S14).

**Licensing note**
- Grafana, Loki and Tempo are **AGPL-3.0**, and TimescaleDB's advanced features use the Timescale
  License. Using them unmodified for our own pilot is fine.
- For a sold product, ship only permissively licensed components (Prometheus and OpenTelemetry are
  Apache-2.0), or offer AGPL tools as separate optional installs. Track this in `docs/licenses.md`.

---

## 7. Key Interfaces (gRPC sketch)

```proto
service Coordinator {
  rpc Register(NodeInfo) returns (RegisterAck);
  rpc Heartbeat(stream NodeStatus) returns (stream Command); // Command: ASSIGN, DRAIN, PROBE, UPDATE, STOP
  rpc ReportNetEdges(NetEdgeBatch) returns (Ack);
  rpc ReportIdle(IdleBatch) returns (Ack);
}

service Agent {
  rpc StartWorker(WorkerSpec) returns (WorkerStatus);  // model, layer range, backend
  rpc StopWorker(WorkerId) returns (Ack);
  rpc RunProbe(ProbeSpec) returns (ProbeResult);
}
```

---

## 8. Architecture Hooks (bake in now, cheap; expensive later)

- [ ] `org_id` / `site_id` on every record and API → future multi-tenancy
- [ ] Pseudonymization + consent records + versioned telemetry schema
- [ ] **Observability from the first line of new code (D-11):** structured logs with request, node and deployment IDs, an error-code catalog, redaction tests, and no content in telemetry. Retrofitting this later is far more expensive.
- [ ] Version inventory, staged rollout with automatic rollback, and remote configuration, so fixes reach every node without a visit
- [ ] Code-signed agent; secure auto-update channel; remote kill switch
- [ ] mTLS everywhere; auth/RBAC stub on coordinator + dashboard
- [ ] All tunables in `config/*.yaml` (idle thresholds, probe schedules, replication factor)
- [ ] Clean API boundaries between agent / coordinator / inference / dashboard
- [ ] Permissive-license-only dependencies; track in `docs/licenses.md`
- [~] Resource caps on agent (CPU/GPU %, bandwidth, disk for model cache) so it never degrades a user's machine — *exists: memory cap (250 ms watchdog); OS-level limits (Job Objects/cgroups) still needed*
- [ ] Student data: prompts and chat history are likely subject to FERPA and campus data policy. Private-by-default history, retention limits and a no-history mode (DIFA's privacy model) should carry over; confirm with the university's privacy office and IT before a pilot.
- [ ] Acceptable-use policy and a model-catalog review process for student-facing models

---

## 9. Experiment Plan (Paper)

| Experiment | Variable | Metrics |
|---|---|---|
| E1 Availability-aware placement (C1) | predictor on/off; predictor type | failed-request rate, p95/p99 latency, re-placements/hour |
| E2 Graceful departure (C2) | notice window 0 / 2 / 5 / 10 s; **hard leave vs. yield** (throttle to the minimal level, D-12); yield level 2 / 5 / 10% | recovery time, lost tokens, user-perceived interruption for the student waiting **and** for the workstation's user (input latency, foreground app frame time) |
| E6 Elastic fleet (D-12) | fixed fleet vs. demand-scaled fleet; daytime vs. overnight windows | queue wait, failed-request rate, active node-hours, energy |
| E3 Cross-backend pipelines (C3) | homogeneous vs. mixed; memory- vs. throughput-proportional splits | tokens/sec, stage idle time, output drift (KL divergence / perplexity delta vs. single-backend) |
| E4 Network topology | intra- vs. cross-building clusters | tokens/sec vs. RTT/bandwidth |
| E5 Cost & energy | IdleMesh vs. cloud GPU baseline | $/1M tokens, kWh/1M tokens |

**Baselines:** Petals, EXO, Parallax, llama.cpp RPC (static split).
**Models:** 8B, 32B, 70B-class GGUF (Q4/Q8); optional MoE stretch goal.

### Related work to cite
- Petals (Borzunov et al., ACL 2023 demo) — collaborative inference over volunteer GPUs
- HexGen / HexGen-2 (Jiang et al., 2024/2025) — heterogeneous GPU scheduling, disaggregated inference
- Helix (ASPLOS 2025) — max-flow placement on heterogeneous GPUs
- Parallax (Gradient/HKUST, 2025) — decentralized heterogeneous serving, two-phase scheduler
- BloomBee (2026) — communication-optimized internet-scale inference
- EXO — P2P heterogeneous device clusters
- Industry: HP Z Boost, Hivenet, Stardock Hivemind, NVIDIA PAIR

---

## 10. Immediate Next Steps

Revised 2026-11-18 now that the DIFA code is in place (see [changes.md](changes.md)):

1. ~~Create repo with structure above~~ — done by evolving this repo in place. Next: add `proto/` contracts.
2. ~~Build agent skeleton: register + heartbeat~~ — inherited. Extend the profiler: GPU/VRAM, backend detection, Windows power.
3. Implement the idle monitor in the Rust agent: Windows first (`GetLastInputInfo`), then macOS and Linux. Add `IdleSample` telemetry.
4. Add time-series telemetry to the coordinator (SQLite) and Grafana.
5. Confirm D-04 and D-09 (llama.cpp RPC now, patched-llama.cpp stage engine in Phase 3) with Michael; measure `-np` batching over RPC.
6. Deploy to 3–5 test machines (≥ 1 Mac, ≥ 1 NVIDIA PC, ≥ 1 CPU-only).
7. Start collecting idle + network data immediately — the dataset clock starts on day one.
8. Remove DIFA-only product code (groups, invitations, branch forks) once SSO and roles replace it.
