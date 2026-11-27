# Changes from the inherited DIFA repository

This file records how this repository moves from **DIFA** (Distributed Inference For All, the prototype
Michael Todd handed off) to **IdleMesh** (working name), the project defined in
[PROJECT_PLAN.md](PROJECT_PLAN.md). It explains what was kept, moved, renamed, changed or dropped,
and why.

- **Baseline:** commit `1dbc8eb` (2026-11-17), the last DIFA commit. Use
  `git checkout 1dbc8eb` to see the repository exactly as handed off. The original repository is also
  kept separately by Michael.
- **Approval:** changes are reworked in this fork. Each change gets Michael's explicit approval
  before it moves forward. The **Status** column records this.
- **Format:** newest entries first. Decisions have IDs (D-xx) that the plan and other docs refer to.
  To add an entry, copy the template at the end.

---

## 2026-11-27 — Sprint 5: network graph, clusters by subnet, decode probe on every worker

Status: **on branch `sprint-5`.** Story-by-story status is in
[Sprints.md](Sprints.md#sprint-5--network-probes-and-the-network-graph).

### What changed

| Story | Change |
|---|---|
| S5.4 | New coordinator package `graph/`. `network.py` combines measurements into links (for each direction, the newest value of each field; then the higher RTT and lower bandwidth of the two directions), applies the threshold rule, finds connected components and gives clusters stable IDs (a cluster keeps the ID most of its members had; larger clusters choose first). `service.py` recomputes clusters over enrolled, unrevoked devices every `clusters.recompute_every_s` and logs each node that changes cluster. Edges to the coordinator are stored but do not join nodes. |
| S5.4 | Agents can upload `net_edge` telemetry records (`dst` = peer device ID or `coordinator`, `method`, RTT p50/p95, bandwidth, sample count). They are stored in `net_edges` under pseudonymous node keys, deduplicated on retry, pruned with the other telemetry, and need the owner's **diagnostics** consent. `NetEdge` added to the telemetry proto draft. |
| S5.4 | `GET /api/v1/groups/{id}/network` (group administrators): devices with their cluster, measured links with `joins` (whether the link passes the threshold), and clusters. Node keys never leave the coordinator. |
| S5.4 | Site config section `clusters` (`max_rtt_ms`, `min_mbps`, `max_age_h`, `recompute_every_s`). |
| S5.1 | The agent reports `network.subnet` in its capabilities: the network address and prefix of the interface it uses to reach the coordinator (never its own address), rechecked every minute and logged when it changes. The coordinator records the subnet in `node_networks` and the heartbeat-timed RTT to itself in `net_edges` (method `heartbeat`), **only on change**: the first figure, a new subnet, a change of more than 50% and 1 ms, or the daily refresh (`probes.rtt_refresh_h`). A steady desktop costs about one row a day. Needs diagnostics consent. |
| S5.1 | Clusters never span subnets (D-24). Within a subnet, pairs with no direct measurement get a **relayed** link: the sum of both nodes' RTT to the coordinator, an upper bound on their direct path. The admin network view shows each device's subnet and coordinator RTT, and each link's `source` (`measured` or `relayed`). |
| S5.1 | Site config `probes`: `rtt_every_s` (never used) replaced by `rtt_refresh_h` (24) and `rtt_change_pct` (50); `bandwidth_every_h` default 24 → 168. `clusters.max_age_h` default 48 → 72 (three refreshes). |
| S5.6 | Agent `decode.rs`: llama-bench is found beside `--worker-bin` when `--decode-probe-bench` is not given. Without `--decode-probe-model`, the agent takes the reference model the coordinator offers (`decode_probe_model` in the heartbeat reply, sent only to a worker with no probe result yet), downloads it from `GET /api/v1/devices/{id}/probe-model` into `<cache>/probe/`, verifies size and SHA-256, and reuses it on later starts. The benchmark waits until the governor level is above 0 for a reason other than user activity, and no deployment is assigned (D-23). New error code `PROBE_MODEL_UNAVAILABLE`. |

Database schema version 5: `net_edges` and `node_clusters`. New coordinator dependency: networkx (BSD-3-Clause).
No new error codes for S5.4 (a failed recompute is logged as `INTERNAL` and never stops the liveness monitor).

### Decisions

| ID | Decision | Why | Status |
|---|---|---|---|
| D-22 | **Clusterer v0: a networkx threshold graph, split into connected components, with first-guess thresholds.** A link joins two nodes when its median RTT is ≤ 5 ms and, once measured, its bandwidth is ≥ 100 Mbps; measurements older than 48 h are ignored. All four values are site settings. | PROJECT_PLAN names networkx, and a later clusterer can switch to community detection on the same graph. networkx is coordinator-only, so nodes are unaffected (D-07). The thresholds are a guess for a campus LAN, to be tuned against the building layout in the S5 demo. | Proposed |
| D-24 | **One dedicated coordinator per network, and clusters follow networks.** Each network (subnet) has its own coordinator machine; nodes on that subnet form its clusters and never join a cluster on another subnet. Coordinators reach the coordinators of other networks to exchange work, for example to hand a request to another network's cluster and return the result when the work there has finished (federation, S8.10). | Colin, 2026-11-27. Within a network the paths are short and stable, so a node's RTT to its own coordinator is a good local figure, and the subnet is a boundary every node can report without new trust between nodes. Work crosses networks only between coordinators. | Colin |
| D-25 | **Network figures are recorded on change, not scanned.** RTT comes from heartbeats the agent already sends, and is stored only on change or once a day. Node-to-node peer probes come later, off by default, run on events (enrollment, a network change, an admin request) and only between likely neighbours on one subnet; their figures replace relayed ones. | Colin, 2026-11-27: most nodes are desktops fixed in one place, so a constant scan wastes node resources (D-12) for figures that rarely change. Relayed figures need no open port or peer trust on nodes (D-07). | Colin |
| D-23 | **Every worker runs the decode probe, with nothing to configure.** The agent uses llama-bench beside its worker binary, and the coordinator offers the catalog reference model (Qwen2.5-0.5B Q8, Apache-2.0) in the heartbeat reply. The agent downloads it once over the pinned channel, checks its SHA-256 and keeps it in its cache. The benchmark runs on its own thread, only while the governor would let the node contribute and no deployment is assigned; this now applies to the command-line flags too. | S4.7: CPU workers were planned from the memory-copy figure (34.1 GB/s on the dev PC against 27.6 GB/s measured by decoding). One-click nodes (D-07) cannot be asked to find a model file. The probe model is chosen by the operator for measurement, not offered to users, so it falls under D-10's exception for models we choose. A benchmark must not compete with the owner (D-12). | Proposed |

### How it was verified

- Unit tests (`test_graph.py`): direction combining, newest value per field, the threshold rule, components through
  a third node, ID stability through joins, leaves, merges and splits, and recompute against the store (stale,
  coordinator and revoked-node edges left out).
- API tests (`test_network_graph.py`): uploads become clusters in the admin view, keys are pseudonymous, retries
  are ignored, diagnostics consent is required, bad records are refused, and only admins see the view.
- No real measurements yet: those come from the agent prober (S5.1).
- S5.6: Rust unit tests (exact-length copy with SHA-256, too many or too few bytes refused and cleaned up, offered
  names cannot leave the probe directory, a verified copy is reused, llama-bench found beside the worker). API tests
  (offer only to an unprobed worker, fetch needs that device's token, no offer without the model file). Live test
  with a real agent and llama.cpp on the dev PC: llama-bench found beside the worker, the 530 MB model downloaded
  and verified, and not downloaded again on the next start.
- **What the probe changes, from the S4.7 results.** llama-bench on the dev PC (4 threads) decodes the reference
  model at 52.6 tokens/s, or 27.6 GB/s, against the 34.1 GB/s memory-copy figure the planner used.
  - Two-machine run: the dev PC's stage (509 MB) becomes 18.4 ms instead of 14.9, so the prediction moves from
    −34.5% to about −26%, inside ±30%.
  - Three-machine run: the CPU workers held one layer each, so the probe changes almost nothing (about 0.1 ms).
    The missing ~22 ms/token is mostly the per-token traffic over three remote links (0.62 MB per stage per
    token), which placement leaves out while `link_mbps` is unmeasured. That needs S5.2 and S5.3; S5.6 alone
    cannot meet its ±30% acceptance on this run.

### Follow-ups

- S5.1: unit tests for relayed links (same subnet only, direct figures win), the subnet rule (no cluster spans
  subnets; no known subnet, no cluster), relayed clusters from the store, and change-only recording (first figure,
  small and large changes, a network change, the daily refresh, two heartbeats in one clock tick). API tests: five
  devices on two subnets give two clusters from one row each; a host address or missing prefix is refused; no
  figures are recorded without diagnostics consent. Rust: subnet masking (IPv4 and IPv6) and the loopback case.
  On the dev PC the agent reports `10.0.0.0/24`.
- **Placement must check the head's own links.** With llama.cpp RPC the head talks to every worker
  (hub and spoke), so a cluster can hold two nodes whose direct link is slow, joined through a third. S5.3
  should use the head-to-worker link from `net_edges`, not cluster membership alone.
- S5.5: Grafana edges and clusters view.
- Peer probes (D-25), and the first cross-machine check of relayed clusters, when the Mac and Linux machines are
  available again.
- Federation between per-network coordinators (D-24) needs a design: see S8.10.
- S5.6 acceptance (the three-machine run within ±30%) is re-checked after S5.2 and S5.3, on all three machines.
- The probe runs once per agent start. A re-probe after a llama.cpp upgrade comes with the update channel (S10).

---

## 2026-11-20 — Sprint 4: config enrollment, crash reports, diagnostics, OS limits, governor

Status: **on branch `sprint-4` (PR #6, stacked on Sprint 3).** Story-by-story status is in
[Sprints.md](Sprints.md#sprint-4--agent-as-a-service-crash-reports-diagnostics-test-fleet-live).

### What changed

| Story | Change |
|---|---|
| S4.1 | Design only: [docs/design/agent-service.md](docs/design/agent-service.md). The service runs everything; a session helper on Windows and macOS reports input idle time and lock state, which a service cannot see. Nothing is installed or auto-started (D-21). |
| S4.2 | `idlemesh-agent run --config node.yaml` enrolls from the file on first start (coordinator, fingerprint, one-time code, name, cap, consent), then runs. Unknown keys are refused. |
| S4.3 | Crash reports. A panic hook saves message, location, backtrace, version, build hash (from git at build time), OS and a redacted config. A worker that exits on its own is saved with its exit status and last output lines. Reports go to `POST /api/v1/devices/{id}/crash-reports` when the coordinator is reachable, are deduplicated, and are logged on the coordinator as `AGENT_PANIC` or `WORKER_CRASHED`. Worker restarts after a crash back off (1 s doubling to 60 s). |
| S4.4 | Diagnostic bundles. An owner or admin asks with `POST .../devices/{id}/diagnostics`; the next heartbeat reply carries the request. The agent uploads logs (up to about 500 KB), versions, capabilities, health, worker and telemetry status, the governor level, pending crash reports and a redacted config. Bundles are kept 14 days. |
| S4.5 | `POST .../devices/{id}/log-level {level, minutes}` opens a window of up to 4 hours. The agent changes its log level at run time and reverts on its own when the window ends, even if the coordinator is unreachable then. `minutes: 0` closes it. |
| S4.6 | Test fleet: the dev PC and the Linux server report to a coordinator on the dev PC ([docs/runbooks/test-fleet.md](docs/runbooks/test-fleet.md)). |
| S4.7 | Legacy three-machine test re-run on current code with llama.cpp v0.9.0, on two machines so far (see "How it was verified"). |
| S4.8 | The worker starts under an OS memory limit: a Job Object on Windows (which also kills the worker if the agent dies) and `RLIMIT_DATA` on Linux. macOS has no per-process limit, so the 250 ms watchdog remains its only guard. |
| S4.9 | Contribution governor v0 (`governor.rs`). It sets the level and reason every second, from the idle ramp, the site's time windows (local time, past midnight handled), the owner's `max_level` (new policy field; the coordinator can only lower it), and pause or battery state. The level goes in heartbeats (`contribution`) and idle samples (`contribution_level`, `level_reason`), and changes are logged when the reason changes or the level moves by 0.05. |

New error codes: `DIAGNOSTICS_NO_CONSENT`, `AGENT_PANIC`, `CRASH_REPORT_UPLOAD_FAILED`, `DIAGNOSTICS_FAILED`,
`WORKER_CRASHED`, `OS_MEMORY_LIMIT_FAILED`. Database schema version 4: `crash_reports`, `diagnostic_bundles`,
`devices.log_override` and the governor columns on `idle_samples`. New agent dependencies: `chrono`, `serde_yaml`
and `libc` (all MIT or Apache-2.0; [docs/licenses.md](docs/licenses.md)).

### Decisions

| ID | Decision | Why | Status |
|---|---|---|---|
| D-20 | **Governor v0 applies the level in three ways.** CPU priority changes at once. Worker threads change at the next worker start. The memory offered to the planner follows the ceiling (time window, owner limit), not the momentary yield. On Linux and macOS an ordinary user can lower priority but not raise it again, so it recovers at the next worker start. | D-12: yield compute at once and give memory back at handoff, not on a mouse move. Raising priority needs privileges a service will have (S4.1 design). | Proposed |
| D-21 | **No auto-start or service install until the installers (S10).** Test machines run the agent by hand. The service architecture is written down so the code fits it. | Colin, 2026-11-20: full control of test machines for now; installs that start at boot come much later. | Colin |

### How it was verified

- **Tests:** 70 Rust unit tests plus an integration test that the OS limit refuses an allocation past the cap
  (Windows and Linux). Python: 209 tests, including end to end with the real agent: config enrollment, a forced
  panic reported with its backtrace, a crashing worker reported with exit code and output, a bundle fetched, and
  a debug window opened and closed.
- **On hardware, with llama.cpp v0.9.0** (dev PC: loopback CPU worker and `llama-server`; Linux server: CPU worker
  behind the mTLS tunnel):
  - The deployment over both machines was ready in 13 s.
  - 10 of 10 requests completed: 40.9 ms/token measured against 26.8 predicted.
  - Drain checks 4a–4f all passed. On 4c, killing the agent also killed its worker through the Job Object.
  - The stranger in 4f was refused with `TUNNEL_HANDSHAKE_FAILED`.
  - Demo: a SIGKILLed worker on the Linux server was diagnosed from the coordinator alone, through its crash
    report (signal 9, last output) and a fresh bundle (restart counted, worker running again).

### Known gaps

- The MacBook is not yet in the fleet or the llama.cpp run (it was asleep), so S3.1, S4.6 and S4.7 still need it.
- **Speed prediction error −34.5%** (target ±30%): the CPU workers had no decode probe, so planning used the
  memory-copy bandwidth, which overstates CPU decode speed. Run the decode probe on every worker (S5 or S6).
- The governor's yield on real user input has been unit-tested but not yet watched on hardware.
- Builds outside a git checkout record `build_hash: unknown`.
- The Windows Job Object is attached just after the worker starts, so there is a moment of a few microseconds
  before the limit applies.

---

## 2026-11-20 — Sprint 3: macOS and Linux idle, hardware profiler, node health, Grafana

Status: **on branch `sprint-3` (PR #5).** Story-by-story status is in
[Sprints.md](Sprints.md#sprint-3--macos-and-linux-idle-gpu-profiler-grafana).

### What changed

| Story | Change |
|---|---|
| S3.1 | macOS idle via IOKit `HIDIdleTime` (system-wide), the console session's lock state (CoreGraphics), and the lid (`AppleClamshellState`), all read through the C APIs with no helper processes. Lid changes are sent as `kind: lid` records. |
| S3.2 | Linux idle via logind `IdleHint`, `IdleSinceHint` and `LockedHint` for a local graphical session. Headless fallback: the most recent input on any login terminal (`/dev/pts/*`, user-owned `/dev/tty*`), or the last login or logout from wtmp, or boot. Unknown never counts as idle (D-17). |
| S3.3, S3.4 | Hardware profile: CPU model, cores and threads, RAM, unified memory, and GPUs with VRAM and driver. NVIDIA comes from `nvidia-smi`, Macs from `system_profiler`, Windows from CIM, and Linux from sysfs, which skips virtual displays. Backends detected: CUDA, Metal, Vulkan and CPU. The profile is sent in heartbeats and enrollment and shown in the device list. |
| S3.5 | `kind: health` records (NodeHealthSample) and `kind: errors` (per-code counts of the agent's warnings and errors, codes only). New tables `node_health` and `error_events`; the coordinator also counts its own error codes once a minute. Each record kind needs its own consent: idle data (`idle`, `state`, `lid`) or diagnostics (`health`, `errors`) (D-19). |
| S3.6 | Read-only metrics API (`/api/v1/metrics/{nodes,idle-heatmap,states,health,errors,crashes}`) for **metrics keys**: API keys with the `metrics` scope, which only administrators can create and which work on those paths only. [deploy/grafana/](deploy/grafana/) holds Grafana 13 in Docker with the Infinity data source, verifying the coordinator's own certificate, and a dashboard generated by `build_dashboard.py`. `pilot.py metrics-key` makes the key. |

Database schema version 3 (`gpu_util` on idle samples, `node_health`, `error_events`).

### Decisions

| ID | Decision | Why | Status |
|---|---|---|---|
| D-18 | **Dashboards read a read-only metrics API, never the database.** Grafana authenticates with an administrator's metrics key, which works only on `/api/v1/metrics/`, and verifies the coordinator's certificate. Pseudonymous node keys (D-16) are mapped to device names inside the coordinator, for the key owner's group only. | A shared SQLite file across Docker's file sharing is unsafe while the coordinator writes it. An API keeps the database private and lets Grafana run anywhere. A key that cannot change anything limits the damage if it leaks. | Proposed |
| D-19 | **Consent is per record kind.** Idle data covers idle samples, state changes and lid events. Diagnostics covers health samples, error counts, crash reports, bundles and remote log levels. | Matches S2.6: what a person does at the machine is a different question from how the node itself is doing. | Proposed |

### Fixes found on the way

- Metrics queries missed rows stamped at the exact end of the range. Windows' clock ticks every ~15.6 ms, so a
  row written and queried in the same tick was dropped. Ranges now include their end.
- API keys authenticate on every endpoint, and most check only the member's role. A metrics key is therefore
  refused outside `/api/v1/metrics/` by the authentication layer itself, and a test checks that it cannot deploy,
  list members or chat.

### Known gaps

- The campus network does not let the server network open connections to Wi-Fi clients. Test agents reach a
  coordinator on the dev laptop through SSH reverse tunnels ([docs/runbooks/test-fleet.md](docs/runbooks/test-fleet.md)).
  A site coordinator belongs on a reachable server.
- Hour-of-week figures use the coordinator's local time zone.
- Grafana updates its bundled plugins at start. The Infinity plugin is pinned.

---

## 2026-11-19 — Sprint 2: Windows idle monitor and telemetry pipeline

Status: **on branch `sprint-2`.** Story-by-story status is in
[Sprints.md](Sprints.md#sprint-2--windows-idle-monitor-and-telemetry-pipeline).

### What changed

| Story | Change |
|---|---|
| S2.1 | Agent `idle` module: seconds since the last keyboard or mouse input (`GetLastInputInfo`), session lock state (Terminal Services session flags) and CPU use. `idlemesh-agent idle --watch` prints them once a second for a manual check. macOS and Linux report "unknown" until S3. |
| S2.2 | Windows power status from `GetSystemPowerStatus`: AC, battery with percentage, or a desktop with no battery. It was "unknown" before, which kept Windows laptops paused unless battery use was allowed. |
| S2.3 | Idle samples (`kind: idle`) and state changes (`kind: state`) uploaded in batches to `POST /api/v1/devices/{id}/telemetry`, with `schema_version`, stored in new SQLite tables `idle_samples`, `node_events` and `telemetry_drops`, stamped with `org_id`/`site_id` and keyed by a pseudonymous node key. Retention from the site config, applied hourly. The heartbeat reply now tells agents the site's sample interval and idle threshold. `GET /api/v1/groups/{gid}/devices/{id}/idle` returns a device's timeline to its owner or an admin. |
| S2.4 | Agent on-disk buffer (`telemetry/` next to the state file): records stay until the coordinator accepts them, are sent oldest first, survive restarts, and are capped (20 MB by default, `--telemetry-buffer-mb`); when full the oldest are dropped and the count is reported with the next batch. Ingest is idempotent, so retries never duplicate. |
| S2.5 | Node states idle, active, sleeping, draining and serving, decided each second; every change is logged and sent as an event. Sleep is detected afterwards as a gap in the wall clock and recorded for the length of the gap. The devices list shows each node's state. |
| S2.6 | Consent record v0 on the device (`--consent-idle-data`, `--consent-diagnostics` at enrollment; `idlemesh-agent consent` to change it), sent at enrollment and with every heartbeat and stored on the coordinator. The agent collects nothing without consent; the coordinator refuses telemetry without it (`TELEMETRY_NO_CONSENT`). |
| S2.7 | [docs/runbooks/dev-collection.md](docs/runbooks/dev-collection.md) and `scripts/idle_timeline.py`, which prints each node's timeline from the database. |

New error codes: `TELEMETRY_NO_CONSENT`, `TELEMETRY_UPLOAD_FAILED`, `TELEMETRY_REJECTED`,
`TELEMETRY_BUFFER_FULL`, `TELEMETRY_BUFFER_IO`. Database schema version 2 (existing databases are
migrated on start). The `proto/` telemetry draft now matches what is sent (`session_locked`,
`battery_pct`, `NodeStateChange`).

**Tests:** Rust 45 passed, clippy clean. Python: 9 coordinator telemetry tests, 2 for the timeline
script, and an end-to-end test where a real agent keeps sampling through a coordinator outage and every
buffered sample arrives afterwards with no gap.

**Test suite additions (pytest, also in VS Code's Testing panel):**
- Unit tests for modules that were only covered through the API: `telemetry` (node keys, salt file,
  consent, ingest, deduplication across time zones, timeline, retention), `security`, `tokens`, `store`
  (schema, rollback, foreign keys, branch messages), `partition` (tensor hashes, side-car cache,
  manifests), the scheduler's event `Channel`, `FakeRuntime` and `Settings`. 130 unit tests in all.
- Smoke tests, `pytest -m smoke` (about 3 s): the main path (sign-in, device enrolment with consent,
  heartbeat, telemetry, deployment, streamed chat, OpenAI-compatible call) run in-process and against
  the coordinator started as a real uvicorn process with a throwaway database. With
  `IDLEMESH_SMOKE_URL` (and `IDLEMESH_SMOKE_CA` for its self-signed certificate) the same walk checks
  a coordinator already running elsewhere. CI runs the smoke tests first, before the agent build.
- **Fix found by the new tests:** `Passwords.verify` accepted an empty stored hash when given the
  dummy passphrase written in the source (the timing-equalisation path). No code path stores an empty
  hash, so it was not reachable, but an empty hash now never verifies.
- Full suite on the dev PC: 171 passed, 8 skipped (7 need the agent or llama.cpp build, 1 needs a
  remote URL).

### Decisions

| ID | Decision | Why | Status |
|---|---|---|---|
| D-15 | **No collection without consent.** The owner's consent lives on the device; the agent does not record idle data without it, and the coordinator refuses telemetry from a device whose last reported consent lacks it. Devices enrolled before S2.6 count as not consented. | Consent must hold even if one side is wrong or out of date. Matches D-11 and the privacy review still to come. | Colin |
| D-16 | **Telemetry is stored under a pseudonymous node key**, an HMAC-SHA256 of the device ID with a per-site salt kept next to the database (`pseudonymize_nodes`, on by default). The API and scripts map keys back to device names on the coordinator; exported data cannot be. | PROJECT_PLAN §6 asks for pseudonymization at collection; this keeps research exports safe without losing per-device views. | Colin |
| D-17 | **Unknown never counts as idle.** A node is idle only when the OS reports no input for `idle_after_s` or a locked session. The agent must run in the user's own session to see input; as a service (S4.1) it needs a helper in that session. | A wrong "idle" sends work to a machine someone is using; a wrong "active" only loses capacity. | Colin |

### Known gaps
- Lock state needs Windows 8 or later (Windows 7 reports it inverted); campus machines are newer.
- The telemetry upload runs in the agent's main loop, limited to five batches per heartbeat, so a long
  backfill catches up over several heartbeats rather than delaying the memory-cap watchdog.
- `gpu_util`, contribution level and NodeHealthSample come in S3 and S4.9.

---

## 2026-11-19 — Sprint 1: foundations, logging and CI

Status: **merged to `main` (PR #1), awaiting Michael's review.** Story-by-story status is in [Sprints.md](Sprints.md#sprint-1--foundations-logging-and-approval).

### What changed

| Story | Change |
|---|---|
| S1.1 | Michael's approval recorded in every entry below (2026-11-19). |
| S1.2 | The restructure is committed (`0a025e4`) with history kept: `git log --follow agent/src/tunnel.rs` reaches DIFA's commits. |
| S1.3 | **Security fix.** On Windows the coordinator's private keys *and* the agent's state file (device token, tunnel private key) were readable by other accounts, because 0600 does nothing there. Both now remove inherited access and grant only the owner, using Windows' built-in `icacls`, before any secret is written. On macOS and Linux the files are created 0600 instead of being chmodded after the write. New: `coordinator/idlemesh_coord/fsperm.py`, `agent/src/secure_fs.rs`. |
| S1.4 | Integration tests find `.exe` builds and honour `CARGO_TARGET_DIR`, `IDLEMESH_AGENT_BIN` and `IDLEMESH_LLAMACPP_BIN`. A tunnel test that was flaky on Windows now accepts a connection abort as the refusal signal. |
| S1.5 | `.github/workflows/ci.yml`: agent build, `cargo test`, `clippy -D warnings`, `pytest`, and stale-file and licence checks on Windows, macOS and Ubuntu. `.gitattributes` keeps LF line endings on every OS. |
| S1.6 | Agent logs through `tracing`: JSON lines in a size-capped rotating file next to the state file, and the same readable messages on the console as before. New options `--log-level` (or `IDLEMESH_LOG`), `--log-dir`, `--log-max-mb`, `--log-keep`. rpc-server output, previously discarded, is captured, and its last lines are reported when it exits on its own. |
| S1.7 | Coordinator logs are JSON lines (stderr, plus a rotating file from the site config) with `trace_id`, `request_id`, `node_id`, `deployment_id`, `org_id` and `site_id`. Requests log admission, start (with their nodes) and outcome. Unexpected exceptions return a catalogued `INTERNAL` error carrying the trace ID. `scripts/trace.py` prints everything about one request across the coordinator and agent logs. |
| S1.8 | `contracts/error-codes.json` is the single catalog (71 codes); `docs/error-codes.md` is generated from it. Tests fail on an uncatalogued code, in Python and in Rust. The check found `COORDINATOR_RESTART`, which DIFA used but never documented. |
| S1.9 | Every log line in both components passes through the same redaction rules: private keys, bearer tokens, API keys, secret fields, prompt and reply fields, and user names in home paths. llama-server runs verbose and can echo prompts, so only its warning, error and load-summary lines reach the JSON log; its full output stays in the local per-deployment file. An end-to-end test checks that prompts, passphrases, tokens, API keys and cookies never appear in any log. |
| S1.10 | `docs/licenses.md`, generated by `scripts/licenses.py`: 196 Rust crates and 23 Python packages, all permissive, with what each licence asks of us. Hand-written rows cover llama.cpp, models, the CUDA runtime and the observability stack (Grafana, Loki and Tempo are AGPL: run unmodified for our own campus, never ship). CI fails if a non-permissive dependency appears. |
| S1.11 | Draft `proto/idlemesh/v1/control.proto` and `telemetry.proto`, with review questions in `proto/README.md`. |
| S1.12 | `config/example.site.yaml` and `site_config.py`: strict validation, queue limits and logging applied now, the other sections (telemetry, idle and yield, contribution windows, probes, fleet) validated and stored for their sprints. `dev_server.py --site-config`. |
| S1.13 | `docs/long-lead.md`: a tracker and ready-to-send first messages for IT, the privacy office, SSO, code signing and tech transfer, plus a test-fleet inventory table for the 8 laptops. |

**Tests:** Python 102 passed, 7 skipped (they need a llama.cpp build and a model), 0 failed, up from
70/8/1. Rust 32 passed, clippy clean.

### Decisions

| ID | Decision | Why | Status |
|---|---|---|---|
| D-13 | **Logging and correlation design.**<ul><li>One JSON object per line in both components, in the LogEvent shape (PROJECT_PLAN §6).</li><li>`trace_id` per HTTP exchange, sent by agents as `X-Trace-Id`.</li><li>`request_id` for inference requests; heartbeat replies carry the node's active request IDs so agent and worker lines can name them.</li><li>Error codes from one JSON catalog in `contracts/`.</li><li>Redaction applied to the finished line, as a safety net behind never logging content in the first place.</li></ul> | Makes remote diagnosis possible from the first sprint (D-11). With llama.cpp RPC the agent never sees requests, so the heartbeat reply is the cheapest way to tag node-side logs with them. | Proposed |
| D-14 | **Site configuration is YAML**, validated strictly; adds PyYAML (MIT) as a coordinator dependency. | Admins edit it by hand; comments explain each setting; strict validation catches typos. | Proposed |

### Known gaps
- CI: the Windows job failed at first (owner-only ACL checks on GitHub's runners), so `main` was
  merged with CI on Windows only. macOS and Ubuntu, which had passed, are back in the matrix (PR #2),
  and CI is required before merging to `main`.
- S1.11: the proto drafts are accepted as the starting shape without a separate review (Colin has
  explicit permission). S1.13: long-lead items 3–7 are deferred so engineering can move ahead. Dev machines on all three OSes (Windows PC, macOS laptop, Linux server)
  are available to reproduce failures; the 8 laptops are for later sprints.
- Worker-output capture is covered by unit tests and the end-to-end trace test without a worker. It
  needs checking on a laptop with a llama.cpp build (sprint 4, test fleet).
- The error-code catalog file is read from the repository; packaging it with the coordinator comes with
  the installers (sprint 10).

---

## 2026-11-18 — Clearer account of what is reused from DIFA

Status: **documentation only, approved by Michael (2026-11-19).** No code changed.

### What changed
- [PROJECT_PLAN.md](PROJECT_PLAN.md) has a new **§1a "What we reuse from DIFA"**. It covers:
  - a split by size: ≈ 5,000 source lines, of which ≈ 30% are used as is, ≈ 61% used and extended, and ≈ 9% replaced later;
  - a capability table marking each item as working today, a foundation to extend, replaced, or new;
  - the measured findings the project relies on.
- The infographic has a matching **section 2, "What IdleMesh gets from DIFA"**, and a header stat. Later sections are renumbered 3–9.

### Why
Michael and other readers should be able to see at a glance which parts of IdleMesh come from his work and which are new. Until now that was spread across the component table (Restructure entry, §6) and the **[~]** marks in the phase checklists.

### How the numbers were counted
Line counts per file (`wc -l`) for `coordinator/idlemesh_coord/` and `agent/src/`, not counting tests. Each file was assigned to the verdict in the Restructure entry's §6. `api/app.py` is split by route: the group, sign-in, invitation, credential and fork routes count as replaced; the device, deployment, queue, request and conversation routes count as extended. The figures are approximate.

---

## 2026-11-18 — Elastic contribution: yield, don't leave

Status: **requirement from Colin, approved by Michael (2026-11-19).** This change is planning only.

### What changed

- **New decision D-12.** Contribution is elastic, not on or off.
  - **Per node:** a *contribution governor* in the agent sets a level from 0.0 to 1.0, from the
    idle-duration ramp, time windows (for example overnight), admin and owner settings, and live load
    and power.
  - **Fleet-wide:** the coordinator runs anywhere from a minimal fraction of workstations up to every
    idle one, according to demand and settings.
  - **On user activity:** a node **yields instead of leaving**. Within about 1 s it drops to a minimal
    level: lowest CPU priority, fewest threads, and GPU duty-cycled to about 2–5%. It keeps working
    slowly or reaches a handoff point, and only then frees memory.
- **[PROJECT_PLAN.md](PROJECT_PLAN.md):**
  - new §2a (governor, fleet scaling, yield flow, what can shrink);
  - C2 extended;
  - Phase 2 items for yield, the governor and fleet scaling;
  - `IdleSample` gains `contribution_level`, `level_reason` and the `yielding` state;
  - E2 compares hard leave with yield, and a new E6 covers the elastic fleet.
- **[Sprints.md](Sprints.md):**
  - S4.9 governor v0;
  - S6.3 placement weighted by level;
  - S7.8 worker throttle control;
  - S8.2 yield, don't leave, and S8.9 fleet scaling;
  - S17 KV migration during the yield window.

### Why

- On workstations, most "node fell off" events are a user coming back. Yielding turns them from hard
  failures into slow nodes that are still reachable, which stretches the handoff window from seconds
  to minutes. That is a large fault-tolerance gain, and a stronger C2 result for the paper.
- Scaling with demand and time windows uses only as many machines as needed, which saves energy and
  reduces the impact on users.

### Constraints recorded

- **CPU and GPU compute** shrink almost immediately: priority, threads, duty-cycling.
- **CPU-node RAM** shrinks partly by itself, because memory-mapped weights can be dropped by the OS.
- **GPU VRAM** cannot shrink while the node is still computing, so it is freed at handoff.
- **Split deployments:** a throttled stage slows the whole chain, so split stages hand off quickly,
  while a single-node replica can finish slowly.
- **Backstop:** the memory-pressure kill stays in place.
- **Patch needed:** GPU duty-cycling needs a small control hook in the worker (S7.8). Check upstream
  llama.cpp options first.

### Decisions

| ID | Decision | Status |
|---|---|---|
| D-12 | Elastic contribution: per-node governor levels (idle ramp, time windows, settings, load and power), fleet scaling between a minimum and maximum number of active nodes with warm standbys, and yield-don't-leave on user activity (minimal level within about 1 s, finish or hand off, then free memory; memory-pressure kill as the backstop). | Requirement from Colin; approved by Michael (2026-11-19) |

---

## 2026-11-18 — Observability, diagnostics and remote support become a core requirement

Status: **requirement from Colin, approved by Michael (2026-11-19).** This change is planning only.

### What changed

- **New decision D-11.** IdleMesh collects detailed error logs and telemetry from every component, so
  that:
  - nodes can be diagnosed and fixed remotely;
  - fixes and patches can be rolled out safely;
  - the tool's performance is benchmarked and tracked continuously.
- **[PROJECT_PLAN.md](PROJECT_PLAN.md):**
  - a new objective 6;
  - a new **§6a** covering what is collected, the remote support loop (detect, diagnose, fix, roll
    out, verify), privacy rules and tiers, the stack, and a licensing note;
  - new data records: `LogEvent`, `ErrorEvent`, `CrashReport`, `NodeHealthSample`, `DiagnosticBundle`
    and `CanaryResult`, plus a much richer `InferenceEvent`;
  - an "Observability" row in the tech stack;
  - two new architecture hooks.
- **[Sprints.md](Sprints.md):**
  - observability is now part of the definition of done;
  - there is an observability column in the roadmap, and stories in every sprint from 1 to 10:
    - S1: structured logs, error catalog, redaction tests;
    - S2: telemetry pipeline with offline buffering;
    - S3: node health;
    - S4: crash reports, diagnostic bundles, remote log level;
    - S5: network metrics;
    - S6: predictor accuracy;
    - S7: per-request performance events;
    - S8: recovery metrics, benchmark canaries, alerts;
    - S9: user-facing error IDs;
    - S10: version inventory, staged rollout with rollback, remote config;
  - the support dashboard is in S14, and an opt-in vendor support channel in Phase 4.

### Why

- Nodes are spread across buildings, and a sold product would run at customer sites. Remote diagnosis
  and safe remote fixes are the only workable way to support it.
- Request-level metrics and scheduled canaries give continuous benchmarks of the tool's performance.
  They also feed experiments E1–E5.
- Adding structured logging and error codes before new feature code is far cheaper than retrofitting
  them later, so the work starts in sprint 1.

### Decisions

| ID | Decision | Status |
|---|---|---|
| D-11 | Observability is a first-class requirement:<ul><li>structured JSON logs with request, node and deployment IDs, and a shared error-code catalog;</li><li>crash reports, node health, per-request performance events, recovery metrics and scheduled benchmark canaries;</li><li>remote diagnostic bundles and a temporary debug log level;</li><li>version inventory, staged signed rollouts with automatic rollback, and remote configuration.</li></ul>**Never collects prompt or response content.** Consent covers diagnostics, telemetry has retention limits and tiers, and for a sold product it stays on the customer's coordinator unless they opt in to vendor support. Shipped components use permissive licences only (Grafana, Loki and Tempo are AGPL). | Requirement from Colin; approved by Michael (2026-11-19) |

---

## 2026-11-18 — Sprint plan added

Status: **approved by Michael (2026-11-19).**

### What changed

- New [Sprints.md](Sprints.md) breaks PROJECT_PLAN.md into **numbered sprints with no calendar
  dates**. A sprint closes when its goal is met; pace is set as we go (Colin's preference):
  - **sprints 1–10** in detail, each with a goal, stories (`S<sprint>.<n>`), acceptance criteria and
    a demo;
  - **sprints 11–21+** as placeholders, to be detailed about two sprints ahead;
  - a long-lead tracker (approvals, IT, privacy, SSO, signing certificates, IP), a risk table, and a
    sprint log.
- PROJECT_PLAN.md links to it.

### Changes to plan details made while sequencing

- The idle-data clock starts in sprint 2 (Windows dev machine) and runs continuously from then on,
  so the Phase 1 exit (sprint 6) has plenty of data.
- **iperf3 is replaced by a built-in bandwidth test** through the tunnel (S5.2), so nodes need no
  extra software (D-07).
- The agent runs as a background service from sprint 4. The full installer comes in sprint 10.
- Pre-work for the stage engine (D-09) is scheduled for sprints 15–17.

---

## 2026-11-18 — Models are supplied by the customer (licensing posture)

Status: **agreed by Colin, approved by Michael (2026-11-19).** This change is planning only.

### What changed

- **New decision D-10.** IdleMesh ships no models. The people who buy or run it add models themselves
  through the Ollama-style interface, the same way they would in Ollama. When a model is added, its
  licence is shown and must be accepted, and the acceptance is recorded.
- **[PROJECT_PLAN.md](PROJECT_PLAN.md):**
  - the Phase 2 model-manager item now describes customer-supplied models, licence acceptance, and
    which sources are allowed;
  - Phase 4 gains the commercial-readiness items listed below.

### Why

- With customer-supplied models, the product works like Ollama and llama.cpp: a tool, with the
  customer as the party using each model.
- Model licences (Llama's community licence, Gemma's terms, non-commercial Mistral models, and
  others) are the biggest licensing risk in a sellable version. This keeps that risk with the party
  who chooses the model.
- Reusing llama.cpp (MIT) is low risk on its own. The obligation is to keep its copyright and licence
  notices in what we ship.

### Decisions

| ID | Decision | Status |
|---|---|---|
| D-10 | Customers supply their own models (Ollama-style pull, Hugging Face or local GGUF). No bundled, default or preselected models; demos use only permissively licensed models. Licence shown and accepted on add, with an audit record. The EULA makes customers responsible for model licences and acceptable-use policies. Hosted offerings and the university pilot are exceptions, where we or FAIRlab choose the models and must check their licences. | Agreed by Colin; approved by Michael (2026-11-19) |

### Follow-ups added to Phase 4

- University IP policy check, before any commercial step.
- A written assignment or licence for Michael's code (the agent is currently marked `UNLICENSED`).
- `docs/licenses.md` dependency audit, and third-party notices in every installer.
- A customer EULA.
- A patent clearance search.
- A model licence check for the university pilot.
- Check the Ollama registry's terms for pulls by a third-party product.

*This is general information, not legal advice. Review it with the university's tech transfer office
and a lawyer before any sale.*

---

## 2026-11-18 — Custom engine route: a stage server built on patched llama.cpp

Status: **direction agreed by Colin, approved by Michael (2026-11-19).** This change is documentation and
planning only; no engine code exists yet.

### What changed

- **New decision D-09.** If IdleMesh builds its own pipeline engine, it uses **llama.cpp as a library
  with a small layer-range patch** (path B), not an engine written from scratch. Each node runs an
  `idlemesh-stage` binary that executes only layers `[a, b)` and passes hidden states along a chain. A
  router keeps each stage's inputs so it can replay to a replica, and can move a session's KV state
  when a node is about to leave.
- **D-04 updated.** Its direction is now agreed by Colin: llama.cpp RPC for Phases 1–2, and the D-09
  stage engine as the Phase 3 prototype, promoted to production only if Phase 2 data calls for it.
- **[docs/research/backend-options.md](docs/research/backend-options.md):** new section 5 covering
  what is reused from llama.cpp, what is added, the build and runtime diagram, a rough effort estimate
  (about 2–3 months for one person, research grade), risks, and where the code will live.
- **[PROJECT_PLAN.md](PROJECT_PLAN.md):**
  - a "Stage engine" row in the tech stack;
  - an `engine/` folder in the repository layout (`llama.cpp-patch/`, `stage/`);
  - a Phase 3 checklist for the stage-engine prototype;
  - C2 and next step 5 now point to D-04 and D-09.
- **Infographic page:** D-04's status, the new D-09, and a path B build-and-run diagram.

### Why

- llama.cpp already provides the most expensive parts for every node type:
  - GGUF loading and quantized kernels for CUDA, Metal, Vulkan and CPU;
  - the tokenizer, chat templates and sampling;
  - batching;
  - per-sequence KV state save and restore.
- What it lacks is running a layer range and returning hidden states. That is a small, contained
  patch, and it could be proposed upstream to avoid keeping a fork.
- It fits the one-click installer rule (D-07): CI already builds and signs llama.cpp, so
  `idlemesh-stage` is one more signed binary in the same package.

### Decisions

| ID | Decision | Status |
|---|---|---|
| D-09 | The custom engine is built on patched llama.cpp: a layer-range patch, the `idlemesh-stage` binary supervised by the agent behind the mTLS tunnel, and a router in the coordinator. Patch the Llama and Qwen families first, add a greedy-output equivalence test, and propose the feature upstream. Review prior art (prima.cpp and similar) before writing code. | Agreed by Colin; approved by Michael (2026-11-19) |

### Follow-ups

- Check that the llama.cpp state API can export a single layer range.
- Review the status and licences of the prior-art forks.
- Measure `-np` batching over RPC (Phases 1–2), which is still needed whatever happens with the engine.

---

## 2026-11-18 — Restructure the repository for IdleMesh

Status: **approved by Michael (2026-11-19).**

### 1. Change of direction

| | DIFA (inherited) | IdleMesh (going forward) |
|---|---|---|
| Who it serves | Small invitation-only groups of trusted people pooling home computers | **Anyone at the university.** Students open a chat or use the API; requests join a shared fair queue and are served immediately when nobody is waiting. |
| Where compute comes from | Members' own devices, contributed under owner limits | **Idle university workstations** across buildings (opt-in), used only while idle |
| Network | Home LAN and the internet | Campus LAN, within and across buildings |
| Main goal | Run one model too large for any single member's device | Run a fair, reliable LLM service on spare capacity; **availability-aware placement**, **graceful departure**, **cross-backend pipelines** (paper contributions C1–C3) |
| Identity | Group-local accounts, invitations, recovery codes | University SSO, `org_id` / `site_id`, roles |
| Outputs | Product spec v0.3 and gate evidence | Working service, idle and network dataset, paper |

**Why:** PROJECT_PLAN.md is the project going forward. DIFA's inference and agent core does almost
exactly what IdleMesh needs, so it is kept and extended rather than rewritten. Its group product
layer is not needed.

### 2. Decisions

| ID | Decision | Why | Status |
|---|---|---|---|
| D-01 | **Keep the Rust agent.** The plan's Go agent is dropped. | Rust gives the same single cross-platform binary. The existing agent (about 1,700 lines, 24 tests) already enrolls, heartbeats, supervises `rpc-server` under a memory cap, runs the mTLS tunnel and prefetches shards, on macOS, Windows and Linux. A Go rewrite would cost weeks and add nothing. | Agreed with Colin; approved by Michael (2026-11-19) |
| D-02 | **Evolve this repository in place** instead of starting a new one. | Keeps git history, the provenance of the measurements and the test suite. | Agreed with Colin; approved by Michael (2026-11-19) |
| D-03 | **Rename code identifiers to IdleMesh. Leave on-disk and on-the-wire names unchanged for now.** | Names inside the code follow the plan. Changing certificate names, cache file names or API fields would break compatibility for no benefit and can be done later in one pass. "IdleMesh" is still a placeholder. | Approved by Michael (2026-11-19) |
| D-04 | **Inference backend path: llama.cpp RPC with request-level recovery for Phases 1–2. A custom stage-server engine is a Phase 3 research prototype.** | llama.cpp RPC works today on every backend. It cannot hand off mid-request (see [docs/research/backend-options.md](docs/research/backend-options.md)). | **Updated in the D-09 entry above:** direction agreed by Colin, with the engine built on patched llama.cpp; approved by Michael (2026-11-19) |
| D-05 | **Two serving paths.** Models that fit one node run as whole-model replicas on single nodes, which is the default for student traffic. Only larger models are layer-split. | Single-node replicas avoid RPC's network cost and make failures cheap: re-run on another node. | Approved by Michael (2026-11-19) |
| D-06 | **Keep the control channel as HTTPS/JSON with a pinned coordinator certificate for now. gRPC + mTLS stays the target.** | Already works on all three operating systems. Moving to gRPC is planned work, not a blocker. | Approved by Michael (2026-11-19) |
| D-08 | **Two install paths from one installer file: manual per workstation (always supported) and an optional silent push** (Intune, SCCM, Jamf or a script). Plus an antivirus plan: signing, published hashes, an IT allowlist document, and testing against the campus antivirus product. See PROJECT_PLAN §3. | Pushing from a server may be blocked or flagged by antivirus or endpoint tools. Onboarding must never depend on it. | Requirement from Colin; approved by Michael (2026-11-19) |
| D-07 | **One-click, self-contained node installer.** A node needs only "install and wait": the installer bundles the prebuilt agent and the pinned prebuilt llama.cpp binaries, and installs nothing else. Rust and C++ toolchains are needed only on build machines (CI). See PROJECT_PLAN §3, "Node install requirement". | Campus-wide rollout to non-technical machine owners and IT-managed fleets. **This replaces DIFA's test procedure**, where every device built llama.cpp from source with cmake and a compiler (docs/legacy-difa/testing/). | Requirement from Colin; approved by Michael (2026-11-19) |

### 3. Files moved

All moves used `git mv`, so `git log --follow` keeps each file's history.

| Before (DIFA) | After (IdleMesh) |
|---|---|
| `services/coordinator/difa/` | `coordinator/idlemesh_coord/` |
| `tests/` | `coordinator/tests/` |
| `agents/device/` | `agent/` |
| `benchmarks/*.py` | `experiments/llamacpp-feasibility/*.py` |
| `benchmarks/results/` | `experiments/llamacpp-feasibility/results/` |
| `docs/HANDOFF.md`, `docs/DECISIONS.md`, `docs/ENVIRONMENT.md`, `docs/spec/`, `docs/testing/` | `docs/legacy-difa/` (frozen; see its README) |
| `contracts/`, `scripts/`, `pyproject.toml` | unchanged location |

### 4. Renames (D-03)

| What | Before | After |
|---|---|---|
| Python package | `difa` | `idlemesh_coord` |
| Python distribution | `difa-coordinator` | `idlemesh-coordinator` |
| Rust crate and binary | `difa-agent` | `idlemesh-agent` |
| Agent state file default | `difa-agent-state.json` | `idlemesh-agent-state.json` |
| Environment variables | `DIFA_*` (for example `DIFA_DB`, `DIFA_DATA_DIR`) | `IDLEMESH_*` |
| Session cookie | `difa_session` | `idlemesh_session` |
| Default database file | `difa.sqlite3` | `idlemesh.sqlite3` |
| Cargo target dir used by tests and scripts | `~/.cache/difa-target` | `~/.cache/idlemesh-target` |
| CLI description | "DIFA device agent" | "IdleMesh node agent" |

**Kept on purpose** (on-disk or on-the-wire names; to rename together later):

- TLS names: `difa-coordinator` (certificate SAN and the agent's server name), `difa-device`
- Agent cache files: `difa-cache/`, `difa-index.json`; coordinator sidecar `.difa-tensors.json`
- API extension fields `difa_visibility`, `difa_branch_id`, `difa_client_request_id`, and the `difa_`
  prefix on issued API keys
- Adapter protocol string `difa-adapter/0`, and the `difa: drain` marker in llama-server logs (an
  integration test checks for it)
- Local directories `~/.local/share/difa` (coordinator data) and `~/.cache/difa-phase1` (llama.cpp
  build and models), which existing guides and machines use

Path constants were updated to match: the experiment scripts' `ROOT`, `sys.path` and results paths;
`scripts/dev_server.py`, `export_openapi.py` and `pilot.py`; the test paths to results and scripts;
`pyproject.toml` (`where`, `pythonpath`, `testpaths`); and `.gitignore`.

### 5. Fixes

- `scripts/export_openapi.py` crashed on Windows while deleting its temporary SQLite file, which was
  still open. It now uses `TemporaryDirectory(ignore_cleanup_errors=True)`. The exported contract is
  unchanged.

### 6. What happens to each inherited component

| Component | Verdict | Notes |
|---|---|---|
| `gguf.py`, `partition.py` (GGUF parsing, exact split points, shard manifests) | **Keep** | Model-agnostic; checked against llama.cpp's own rules |
| `runtime/llamacpp.py` | **Keep, extend** | Add whole-model single-node deployments (D-05) and `-np` slots |
| `transport.py`, `agent/src/tunnel.rs`, `agent/src/control.rs` | **Keep** | Secure path around llama.cpp RPC; add certificate rotation |
| `domain/placement.py` (filter-and-score, latency model) | **Keep, extend** | Add the availability term (C1), the link-throughput term and replica placement |
| `domain/fairqueue.py`, `scheduler.py`, `domain/lifecycle.py`, `domain/ledger.py` | **Keep, scale up** | Queue for the student service. Today: 1 active generation, 2 queued per user, 10 in total, one deployment per group. Needs many deployments, batching and per-user quotas. |
| `api/compat.py` (OpenAI `/v1` subset) | **Keep, extend** | Add the Ollama `/api/generate` and `/api/chat` endpoints |
| `agent/src/worker.rs`, `policy.rs` | **Keep, extend** | Becomes the drain and `DRAIN` handler; add OS memory limits (Job Objects, cgroups) |
| `agent/src/probe.rs` | **Extend** | Becomes the hardware profiler: GPU and VRAM, backend detection, Windows power |
| `agent/src/prefetch.rs` | **Keep** | Model manager shard cache |
| `api/app.py` groups, invitations, recovery codes, branch forking, `domain/access.py` | **Replace later** | With SSO, `org_id` / `site_id` and roles. Kept for now because tests and the queue path use them. |
| Private-by-default chat history, retention, audit without prompt text | **Keep the ideas** | Matter for student data (FERPA) |
| `runtime/fake.py` | **Keep** | Useful for testing the queue and API without inference |
| `catalog.py` (synthetic DIFA fixtures) | **Replace** | With a real model catalog (Ollama library or GGUF) |
| `experiments/llamacpp-feasibility/` | **Keep as evidence** | Baseline for E3 and E4; `netem_relay.py` simulates cross-building links |
| `docs/legacy-difa/` | **Frozen** | Reference only. DIFA's spec gates (Test C, the 58 GB download, user reviews) no longer apply. |
| `scripts/pilot.py`, `smoke.py`, `dev_server.py` | **Keep for now** | Rework after the group model is replaced |

### 7. Known issues carried over

- `coordinator/tests/unit/test_transport.py::test_identity_is_created_once_with_a_private_key` **fails
  on Windows.** The coordinator's tunnel private key is not restricted to its owner there, because
  `chmod 0600` has no effect on Windows. This is a real security gap for a Windows coordinator, not
  only a test problem. It failed the same way before this change.
- The integration tests look for the agent binary without `.exe`, so they are skipped on Windows even
  when it is built.
- Windows power status reads "unknown", so Windows nodes need `--allow-battery` to contribute.
- From the DIFA handoff, never verified on hardware: the heartbeat RTT fix (`48cac9a`), the clean
  drain stop, and the owner-control checks 4a–4f.

### 8. How this change was verified

- `pytest`, run in a fresh virtual environment with Python 3.12 on Windows: **70 passed, 8 skipped,
  1 failed**, both before and after the restructure. The failure is the known Windows issue above.
  The skipped tests need the llama.cpp build, the model and an agent build.
- `scripts/export_openapi.py` regenerates `contracts/http/openapi.json` with no diff.
- The experiment scripts and helper scripts parse cleanly. They were not run, because they need
  llama.cpp and a model.
- **Rust agent, after the rename:** `cargo test` gives **24 passed**, `cargo clippy --all-targets` is clean, and the debug and release builds succeed (the release `idlemesh-agent.exe` is 5.3 MB). This was on Windows with rustup stable MSVC (cargo 1.100.0) and VS 2022 Build Tools, both installed on 2026-11-18.
  - Windows 11 **Smart App Control** first blocked Cargo's build scripts (os error 4551). Colin turned it off on this dev machine.
  - The same policy would block an unsigned agent on campus machines, so D-07 requires signing every bundled binary.
- `Cargo.lock` changes only in the renamed package entry, which moved to its new alphabetical position.
- The agent's help text no longer mentions "the DIFA app". The tunnel certificate's common name, "DIFA device …", was kept as a wire identifier (D-03). The crate
  rename changes only `Cargo.toml`, `Cargo.lock`, the CLI name and text, and default file names, so
  the risk is low. Run `cargo test` and `cargo clippy --all-targets` in `agent/` before committing.

### 9. Not done yet (next entries)

- Add `proto/` (gRPC contracts), `config/example.site.yaml` and `docs/architecture.md`.
- D-07 groundwork: run the agent as a service (it is a foreground CLI today); enroll from installer settings instead of `idlemesh-agent enroll` on a command line; locate the bundled llama.cpp binaries automatically instead of `--worker-bin`; add `crt-static` to the Windows build; set up CI release builds.
- Idle detection in the agent (Windows first), `IdleSample` telemetry, Grafana.
- Confirm D-04 and D-09 with Michael (done on Colin's side; see the entry above), and measure `-np` batching over RPC.
- Remove the DIFA-only product code once SSO and roles exist.
- Rename the remaining `difa` wire and disk identifiers in one pass, after the project name is final.

---

## Template

```markdown
## YYYY-MM-DD — <short title>

Status: proposed | approved by Michael (date) | done

### What changed
### Why
### Decisions (D-xx)
### How it was verified
### Follow-ups
```
