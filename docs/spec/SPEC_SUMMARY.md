# Product specification v0.3: summary

A condensed, paraphrased guide to *Distributed Inference For All: Product Specification*, version 0.3
(November 5 2026). The full `.docx` is kept outside this repository, next to the checkout (see
[ENVIRONMENT.md](../ENVIRONMENT.md)). If this summary and the original disagree, the original wins.

Identifiers used in code and docs (GOAL, FR, AT, DEV, SC, section numbers) are the spec's own, so they
can be looked up in either file. Numbers marked "proposed" are engineering baselines, not owner-approved
commitments; changes go in [DECISIONS.md](../DECISIONS.md).

## 1. Purpose and priorities

Let invitation-only groups of trusted people pool their computers to run a language model that no single
member's device can practically host. Members may use the service without contributing a device. The
design must handle any number of mixed devices: it must not hard-code the three reference machines or a
three-worker limit.

Priority order:

1. correct distributed inference beyond one device's capacity;
2. access for several invited users, including non-contributors;
3. understandable setup, application privacy, owner control of contributions;
4. Windows, Linux and macOS, over LAN and internet;
5. usable latency and energy use, with no promise of speed-up or savings.

Security, correctness and privacy are release gates whatever their place in this list.

## 2. Confirmed goals and scope

| ID | Goal |
|---|---|
| GOAL 01 | Run a model too large for any one participating device |
| GOAL 02 | Several users; speed is secondary |
| GOAL 03 | Same LAN and over the internet |
| GOAL 04 | Contribution controls for CPU, GPU, memory, power and battery |
| GOAL 05 | Anyone can create an independent, invitation-only group; no public discovery |
| GOAL 06 | Use without contributing a device |
| GOAL 07 | Chat and an application API |
| GOAL 08 | Windows, Linux and macOS from the first release |
| GOAL 09 | Guided setup for mostly non-technical people |
| GOAL 10 | Shared and private visibility, switchable during use |

**v1 boundary:** text-only chat; one approved model per group at a time; many conversations per member;
generation may be serialised while requests queue; a device serves one active group at a time.

**Out of scope for v1:** public volunteer networks, payments, training, tool execution, retrieval,
audio and images, plugins, user-supplied code, cross-group pooling, live migration of a running
generation, guaranteed service through outages, and mobile devices as compute.

**Private mode** hides content from other members *in the application*. It is not secrecy from the
coordinator's administrator, OS administrators, or the owners of the devices doing the computing.

## 3. Reference deployment and capacity tests

| Device | Resources (as supplied) | Role |
|---|---|---|
| HP EliteBook x360 830 G8 | Windows 11 Enterprise, i5-1135G7, 32 GB (10.2 GB available in the snapshot) | CPU contributor; protect foreground use and battery |
| Mac Studio | M2 Ultra, 128 GB unified memory | Main compute and proposed coordinator |
| Remote Ubuntu VM | 8 GB, 2 cores | Internet-connected CPU worker |

The nominal 168 GB total is not a usable model size. The HP's page file is not memory, and unified
memory is counted once. Store sizes in bytes and display GiB. Before hardware work, record OS versions,
accelerator limits, available RAM, disk, power, install permissions, and pairwise network measurements.
Never change workplace policy to make setup work.

### 3.1 Three capacity results, kept separate

- **Test A (cooperation):** a small model split across all three devices, with proof that each one
  computes. Registration or copying weights is not enough.
- **Test B (actual capacity):** with model, precision, context, output and concurrency fixed, the
  workload exceeds every device's real safe standalone budget yet completes when distributed.
  - It must beat the **strongest single-device baseline**, meaning each device also tries every
    supported offload mode (streaming or memory-mapping weights, expert offload as in Strata).
  - The claim holds if the configuration cannot run on any single device, or runs too slowly there
    while the distributed placement meets the interactive target. State which basis applies.
  - Do not pick a worse quantisation to inflate size.
- **Test C (owner-budget capacity):** the workload exceeds every device's *owner-approved contribution
  budget* and completes when distributed.
  - **Decision in v0.3:** Test C is the pilot claim, worded only as "larger than any member's
    contributed budget", never "larger than any member's computer".
  - Test B is still required before release.

### 3.2 Capacity window

Planning budgets, to be replaced by measurements:

| Device | Planning budget |
|---|---|
| Mac | about 90–110 GB |
| HP | about 6–10 GB |
| VM | about 4–5 GB |
| Window above the Mac | about 10–15 GB |

That window is too narrow for a robust Test B. So the pilot runs Test C:

- the Mac is capped by its owner (proposed 64 GB);
- the HP and VM contribute their measured budgets;
- the configuration must exceed each budget and fit their sum after overhead.

At the Phase 1 gate the owner approves a large-memory contributor (proposed 64 GB or more) only if all
of these hold:

- distributed execution is correct on all three devices;
- the latency budget is credible;
- Test C meets the interactive target where the strongest single-device baseline does not.

Test B then runs in Phase 3. Raising the Mac's GPU-wired memory limit is an owner choice that must be
recorded in device policy.

## 4. Roles and authorisation

- **Roles:**
  - the owner creates the group, transfers ownership and deletes it;
  - administrators manage invitations, membership, model choice, quotas and health;
  - members chat and use the API;
  - a contributor is a member who has enrolled a device.
- **Device limits:** the device owner alone sets or raises a device's limits. Administrators may disable
  or remove a device but not raise its limits.
- **Private conversations:** nobody, administrators included, reads another member's private
  conversation through the application.
- **Authorisation checks:** every check uses authenticated membership and object ownership, never
  client-claimed roles, on every read, write, search, export, subscription and stream. Default deny.

## 5. Functional requirements (one line each)

### 5.1 Groups and invitations

- **FR 01:** create a group with no global registry. The group gets a random immutable ID and identity
  keys. The wizard warns that the coordinator must stay online.
- **FR 02:** invitations are single-use, group-bound, revocable and expiring (proposed 24 h), with at
  least 128-bit secrets. Store a verifier only. Redemption is transactional.
- **FR 03:** joining shows the group name and a verifiable fingerprint, explains the privacy boundary,
  and offers use-only or contribute. Bad invitations fail without leaking content.
- **FR 04:** removing a member revokes their sessions, keys and devices and closes streams within 30 s.
  Workers stop when their authorisation lease expires, even offline.
- **FR 05:** groups are fully separate. Ownership transfer needs confirmation from both sides. The last
  owner cannot be removed.

### 5.2 Devices and contribution

- **FR 06:** enrollment binds a device key to a member and group. The device reports OS, architecture,
  runtime, operations, memory and accelerator budgets, disk and power, plus measured memory bandwidth,
  a per-backend compute probe, and storage read speed.
- **FR 07:** owners set the memory, CPU, accelerator, disk-cache and schedule limits, and can pause
  instantly. Limits are local authority; the coordinator cannot override them.
- **FR 08:** battery devices contribute only on mains by default. Opting in to battery uses a stop
  threshold (proposed 30%) and a resume threshold (40%), with hysteresis. Unknown power status means
  paused.
- **FR 09:** plain-language states: ready, contributing, draining, paused, unavailable, incompatible,
  updating. Pause offers drain or stop now. Lowering a limit below current use triggers a drain and
  re-placement.
- **FR 10:** compatibility is checked per OS, architecture, accelerator, backend build, model format and
  protocol, with specific explanations. A failing device can still be a chat client.
- **FR 26:** owner-set warm-hold before an idle deployment unloads (proposed 30 min for desktops, 10 min
  for laptops). Chat shows the cold-start wait.
- **FR 27:** (SHOULD) protect-my-computer mode drains contribution when foreground memory pressure
  crosses a threshold, before swapping.

### 5.3 Models and distributed execution

- **FR 11:** an allowlisted catalog pins source, revision, hashes, tokenizer, template, format,
  operations, licence and context limits.
- **FR 12:** downloads are resumable, hash-verified and atomic. Workers run no model-supplied code.
  Request content and KV cache are never persistent cache.
- **FR 13:** placement counts weights, buffers, KV cache, duplicated data and headroom *per device*.
  A group-wide sum does not prove feasibility. The adapter must prove where each partition runs.
- **FR 14:** deployment states run unavailable, downloading, verifying, planning, loading, ready,
  draining, failed. A model change creates a new immutable revision. Never silently substitute a
  model or precision.
- **FR 15:** each admitted request pins revision, privacy mode, requester, reservation, deadline and
  context. A request that cannot be reserved is queued or rejected before generation.
- **FR 28:** the catalog records model class (dense or MoE), total and active parameters, layers,
  minimum partition granularity, and expert count and routing width. Size and speed are shown
  separately.
- **FR 29:** every worker in a placement runs the same backend digest, manifest and protocol. Mixed
  versions are rejected. Updates drain and reload the whole placement.
- **FR 30:** each worker downloads only its own partition where supported. The owner sees the transfer
  and disk cost first; metered connections need consent. Each contributing owner accepts the model
  licence.

### 5.4 Chat and privacy

- **FR 16:** conversations with streaming, stop, retry as a new attempt, history, deletion, and a
  visible queue or unavailable status. Model output is untrusted (sanitised Markdown, no scripts).
- **FR 17:** every request is explicitly private or group. Private is a proposed default. Group means
  all current and future members can read that branch.
- **FR 18:** switching visibility makes a new branch with fixed visibility.
  - Shared to private may copy context, after confirmation.
  - Private to shared starts empty unless the user previews and publishes selected content.
  - Hidden private context must not influence shared generation.
- **FR 19:** a mode change applies only to later submissions. In-flight responses keep their branch.
- **FR 20:** private text is absent from other members' lists, titles, search, exports, notifications,
  queue views, analytics, logs, diagnostics and admin screens. Shared visibility does not mean consent
  to training or to sending content to external services.
- **FR 21:** (SHOULD) shared branches have a single writer in v1; others fork.

### 5.5 API and fairness

- **FR 22:** the API follows the same membership, visibility and queue rules as chat. It offers model
  listing and a documented subset of OpenAI-style chat completions with streaming, with differences
  published.
- **FR 23:** credentials belong to one member and group, with scopes and an expiry (proposed 30 days).
  The secret is shown once and stored as a verifier, never placed in URLs, and revocable immediately.
  Sharing needs an explicit share scope.
- **FR 24:** a bounded per-group fair queue. Proposed: 5 connected users, 1 active generation, 2 queued
  per member, 10 group-wide. The API cannot bypass chat quotas.
- **FR 25:** reject tool, image, audio and function fields explicitly. Context limits include prompt,
  history, template and reserved output. Oversized input is rejected; private context is never
  silently truncated.

### 5.6 Placement pipeline and profiles

- **FR 31:** filter then score, adapted from llm-d's endpoint picker.
  - Filters remove devices that are unauthorised, incompatible, not ready, power-paused, short of
    budget, or on the wrong digest.
  - Scorers rank placements by stage compute time, RTT and throughput, partition count, stability and
    power.
  - Filter and scorer sets are versioned. Each decision has a content-free, reproducible explanation.
- **FR 32:** (SHOULD) once latency is calibrated, reject requests that cannot meet their deadline with an
  explained 429 or 503. Record offered demand, including rejections.
- **FR 33:** guided setup offers only validated topology profiles: a model configuration, placement
  shape, device and network classes, backend digest, and a reproducible benchmark recipe with its
  measured envelope. Anything else is experimental and excluded from support claims.
- **FR 34:** (SHOULD) with several placements, route to the one holding the requester's conversation
  cache, otherwise the least loaded. This is an SC 25 and SC 50 experiment, off by default in v1.

## 6. Architecture

### 6.1 Topology and components

- **Topology:** one coordinator per group, proposed on the Mac. The coordinator may also compute. Do not
  default the coordinator to the 8 GB VM.
- **Components:** desktop app, browser chat, gateway and coordinator (sole writer of state), device agent
  (enforces limits locally), runtime adapter (isolates the backend), worker runtime (no public API), and
  encrypted network (does not replace application permissions).
- **Measure, don't assume:** record the actual coordinator-to-worker traffic rather than assuming a direct
  worker-to-worker pipeline.

### 6.2 Backend decision

- **llama.cpp (GGUF, CPU and Metal):** the first portability experiment. Its RPC mode is documented as
  insecure and experimental, so it may be used only in a restricted research setting with synthetic
  prompts. A VPN does not fix it, and it is not the approved production backend.
- **Petals and exo:** compared for precedent; neither covers this exact device and network mix.
- **Phase 1 must choose one of:**
  - a maintained backend that meets the security contract;
  - an owned, isolated, constrained adapter with a funded hardening plan and review;
  - stop and revise scope.

  A custom engine is a separate workstream. Trust between people does not make an insecure backend
  acceptable.
- **llm-d** (Kubernetes, vLLM or SGLang in data centres) contributes routing, deadline admission and
  validated profiles. Kubernetes, prefill/decode disaggregation and expert parallelism are out of scope
  for v1 internet placements.
- **Strata** (one large MoE on one desktop through expert caching and speculative decoding) raises the
  single-device baseline and sets the bar for setup simplicity. Its figures are self-reported, and its
  canonical repository needs confirming. A different OSDI 2026 "Strata" paper on context caching
  informs later work only.

### 6.3 Proposed technology baseline

| Layer | Proposal |
|---|---|
| Desktop | Tauri 2, Rust agent, TypeScript and React |
| Group service | Python, FastAPI, Pydantic (runtime bundled) |
| Storage | SQLite on the coordinator's local disk, with migrations |
| Agent control | Versioned gRPC and Protobuf over mutual TLS |
| Internet transport | Tailscale pilot with restricted peer policies |
| LAN | Same authenticated protocols; no internet needed once enrolled |
| Public API | HTTPS, JSON, server-sent events, pinned OpenAPI |
| Tests | pytest, Rust tests, Playwright, native installer checks |

End users need no Docker, WSL, terminal, build tools or package manager. A headless Linux install that
still needs a command counts as a missed setup target.

### 6.4 Trust and networking

- **Exposure:** only the gateway is exposed to members.
- **Agent connections:** agents accept work only from their pinned coordinator.
- **Workers:** listeners bind to loopback behind an authenticated tunnel, or to a restricted interface.
  Worker ports are firewalled, LAN included.
- **Discovery:** no unauthenticated multicast enrollment. Discovery may suggest an endpoint but never
  establishes trust.
- **Overlay:** never rely on its default allow-all policy. Separate client-to-gateway access from
  coordinator-to-worker access.
- **Certificates:** they carry group and device IDs. Enrollment pins the group root; reconnects verify
  it. Credentials rotate.
- **Local-only groups** work without cloud sign-in after provisioning. Test provider outages, DNS loss,
  address changes and relay fallback.

### 6.5 Login and recovery

- **Accounts:** a group-local account (login name, display name, passphrase of at least 15 characters)
  created at invitation redemption. No email or global identity provider. Argon2id, generic failure
  messages, bounded attempts.
- **Recovery code:** shown once and stored as a verifier. Recovery rotates credentials and revokes
  sessions and keys. An administrator can re-invite someone as a new identity but cannot reset them
  into their old private-content identity.
- **Sessions:** server-side, with Secure, HttpOnly, SameSite cookies, rotated on login. 30 minutes idle,
  12 hours absolute. Sensitive actions need fresh authentication. Native clients use the OS credential
  store.
- **HTTPS:** the installer must provide a browser-trusted route. Never train users to click through
  certificate warnings.
- **Endpoints:** `POST /api/v1/auth/login`, `logout` and `recover`, and `GET /api/v1/auth/session`.

## 7. Scheduling and resources

- **Device budget:** the minimum of the owner cap, measured allocatable memory and the backend ceiling,
  less headroom. Each partition's weights, buffers, reserved KV cache, transfers and headroom must fit
  it. Unified memory goes in one ledger. Budgets are rechecked at deployment and admission.
- **First planner:** measured profiles and explicit placement, not a learned scheduler.
  - It prefers fewer network boundaries, contiguous partitions and stable powered devices.
  - It omits a slow remote worker unless capacity needs it. The all-three test is a validation
    topology, not a rule.
- **Generation:** serialised at first, reserving for the full context and output. CPU thread limits are
  not percentage caps; mark unsupported GPU caps as unavailable.
- **Scheduling:** round-robin across members, FIFO within each member. Deadlines: 10 minutes waiting,
  5 minutes generating. No ETA until calibrated.
- **Request states (7.1):** validated, queued, reserved, running, completed, cancelled, failed, expired.
  - Transitions are transactional and audited without prompt text.
  - Cancellation removes the reservation or stops the stream, with a hard kill as the fallback.
  - Partial text is labelled incomplete. Nothing restarts automatically once text has been emitted.
- **Latency budget (7.2):** decode is memory-bandwidth bound. Per-token time is the sum over stages of
  assigned weight bytes over measured bandwidth, plus dependent network exchanges times RTT, plus
  synchronisation.
  - Phase 1 must publish this estimate for each candidate placement *before* benchmarking, and compare
    it with traces.
  - A dominant stage is a placement problem, not a reason to relax targets.

## 8. Privacy and data lifecycle

- **Access rules:** private branches are readable only by their owner; shared branches by active members
  of that group. Admin status overrides neither rule. Opaque IDs are not access control.
- **Mode transitions** record the source and new branch, the copied message IDs and the preview
  confirmation. A visibility conflict through the API returns 409.
- **Retention:** history is kept 30 days by default (adjustable, with disclosure). Members may choose
  no-history for private requests, which keeps only IDs, status and usage.
- **Encryption at rest:** stored content is encrypted with a group data key in the OS credential store.
  This protects files and backups, not the running coordinator.
- **Never logged:** prompts, completions, authorisation headers, full invitation URLs, raw tensors.
  Diagnostic exports are previewed and opt-in.
- **Deletion:** content is purged within 24 hours. Backups are encrypted, off until configured, and kept
  30 days. Restores re-apply deletion tombstones. No forensic-erasure promise.
- **Workers** keep request state in memory only and release it at the end. No cross-user prompt-cache
  reuse in v1.
- **Conversation cache:** a worker may keep one branch's attention cache for the same requester for
  about 5 minutes, inside that request's reservation.
  - It is keyed by group, branch, requester and revision.
  - It is evicted on branch switch, visibility change, revocation, deployment change or memory
    pressure, and is off for no-history requests.

## 9. Data model and contracts

- **Persistent entities (9.1):** group, member identity (no mandatory email), membership, invitation,
  device, contribution policy, model manifest, deployment, conversation branch (immutable visibility),
  message (encrypted, with deletion tombstone), inference request, application credential, and audit
  event (no content).
  - Foreign keys enforce group boundaries.
  - Timestamps are stored in UTC; leases use a monotonic clock.
- **HTTP endpoints (9.2):** groups, invites, redemption, members, device enrollment and policy,
  conversations and forks, asynchronous requests with status, events and cancel, plus `GET /v1/models`
  and `POST /v1/chat/completions`.
  - Asynchronous admission returns 202.
  - Cookies with CSRF protection for browsers; bearer tokens for applications; TLS off loopback; no
    wildcard credentialed CORS.
  - Proposed limits: 1 MiB body, 4,096 context tokens including output, 512 output tokens.
  - Compatibility calls default to private.
- **Events and errors (9.3):**
  - Events carry `request_id`, a monotonic sequence and a type: queued, started, token, completed,
    cancelled or failed.
  - Errors use `error.code`, `message`, `retryable` and `request_id`, with no stack traces.
  - Status codes: 400, 401, 403, 404, 409, 413, 422, 429 (with Retry-After) and 503. After streaming
    starts, errors arrive as a terminal event.
  - Idempotency keys last 24 hours and are bound to the payload.
- **Adapter contract (9.4):**
  - Methods: `probeCapabilities`, `estimateResources`, `planPlacement`, `loadDeployment`,
    `generateStream`, `cancelRequest`, `unloadDeployment`, `health`, `collectContentFreeMetrics`.
  - Loading returns the manifest digest and the actual partition map.
  - Optional capabilities: `offloadModes`, `conversationCache`, and `speculativeDecode` (which must be
    lossless; a Phase 3 experiment).
  - Agent control covers enroll, heartbeat, applyPolicy, prepare, activate, drain and revoke.
  - Heartbeats every 5 s. Stop assigning work after 15 s of silence; authorisation expires at 30 s.

## 10. Reliability and operations

- **Lost worker:** the deployment becomes unavailable for new work at once. Active work fails clearly
  and its reservations are released. Re-plan only when enough capacity exists. A retry is a new
  attempt. Clients get a 15 s disconnect grace period.
- **Coordinator:** a single point of failure in v1. A restart marks running attempts interrupted and
  resumes only safe queued work. Manual, signed migration must be tested before recovery is called
  supported.
- **Metrics and traces:** content-free metrics, plus an OpenTelemetry-compatible trace per request (queue
  time, placement, prefill and decode per stage, network per hop). Traces are the evidence for the
  latency budget. External telemetry is off by default.
- **Releases:** signed installers and updates (macOS notarised). Updates drain first and never touch a
  busy model process. Uninstall revokes enrollment.

## 11. Acceptance targets (proposed; revisable at the Phase 1 gate)

| Measure | Target |
|---|---|
| Platforms | Windows x64, Apple Silicon macOS, Ubuntu x64 contribution proven |
| Correctness | 100-prompt small-model corpus; token or logit comparison against a baseline within documented tolerance |
| Capacity | Pilot: Test C, labelled, beating the strongest single device. Release: Test B on the expanded group. |
| Multiple users | 5 users × 2 requests; fair, 1 active generation, no loss or cross-user disclosure |
| Small-model speed | 256-token prompt, ≤128 output tokens: p95 first token ≤ 30 s; median decode ≥ 2 tok/s on the validated mixed topology |
| Large-model speed | Measured separately; target agreed after feasibility |
| UI | Queue acknowledgement ≤ 2 s at p95 |
| Device loss | Detected ≤ 15 s; connected revocation ≤ 30 s |
| Owner stop | UI ≤ 1 s; execution stops ≤ 10 s, or the worker is killed |
| Resources | No allocation above the owner's memory ceiling |
| Privacy | Zero unauthorised disclosures across the full matrix |
| Usability | 4 of 5 non-technical users finish each core task unaided; 5 of 5 understand the audience |
| Stability | 8-hour mixed-load soak with no crash, lost request or leaked reservation |
| Scalability | SC 03 → 10 → 25 → 50 with separate limits for connected, ready, computing, users and generations |
| Single-device baseline | Strongest attempt per device, including offload, with the claim basis stated |
| **Latency budget** | **Predicted and traced per-token time agree within 30%**, or the model is revised before targets are set |
| Multi-turn | Second-turn first token with cache against without; zero cross-user or cross-branch reuse |

Sample sizes: at least 100 warm requests per main scenario (report p50 and p95; cold load separately),
at least 10 repeats per fault scenario, at least 3 per exploratory placement. Publish failures.

## 12. Testing

- **12.1 Layers:** unit and property tests, contract tests against a fake runtime, integration tests with a
  real database (no mocked authorisation), end-to-end tests, native tests on all three OSes, and
  real-device distributed tests. A 10–50 device simulator counts as control-plane evidence only.
- **12.2 Acceptance scenarios:**

| AT | Scenario |
|---|---|
| 01 | Similar-named groups; no cross-group access |
| 02 | Invitation races, expiry, revocation, replay |
| 03 | Use-only member uses chat and the API |
| 04 | All three devices compute; partition evidence and traffic recorded |
| 05 | Configuration fails standalone on each device and succeeds distributed |
| 06 | LAN, mixed, forced relay, isolated LAN |
| 07 | Private canary absent from every other-user surface |
| 08 | Mode switches before, during and after streaming |
| 09 | Five users mixing chat and API: quotas, fairness, cancellation |
| 10 | Unplug, battery threshold, lower cap, pause, rejoin |
| 11 | Kill worker, coordinator or client during prefill and decode; no false success |
| 12 | Revoke a member and device mid-stream; lease expiry stops offline work |
| 13 | Corrupt download, full disk, resume, unsupported backend, rollback |
| 14 | Install, upgrade, migrate, uninstall, restore, tombstone replay |
| 15 | Non-technical user completes the core tasks |
| 16–19 | Staged scale matrices, churn and fault storms, user load against device count, stage reports |
| 20 | Every Test B configuration on each single device with all offload modes |
| 21 | Conversation-cache privacy and eviction |
| 22 | Placement decisions replay exactly; deadline rejections are explained |
| 23 | Mixed digest, manifest or protocol rejected; whole-placement update |
| 24 | Validated profile reproduced on fresh hardware |
| 25 | Warm-hold, protect-my-computer, metered consent, per-partition download |

- **12.3 Experiment matrix:**
  - placements: each device alone where it fits, Mac+HP, Mac+VM, and all three;
  - added RTT: 20, 80 and 150 ms;
  - bandwidth caps: 10, 50 and 100 Mbps;
  - packet loss: 0% and 1%;
  - a real direct path and a real relay path;
  - 1, 3 and 5 users.
- **12.4 User research:** 5–8 non-technical users per round with goal-based tasks. WCAG 2.2 AA for core
  flows. Every phase includes user validation.
- **12.5 Staged scalability:** report the tuple (E connected, R ready, A computing, U users,
  G generations, Q queue).

  | Stage | Load | Placements |
  |---|---|---|
  | SC 03 (Phase 1) | E 3; U 1/3/5; G 1; Q 10 | A 1, 2, 3; all three must compute |
  | SC 10 (Phase 3) | E 10; U 1/5; G 1; Q 10 | A 3, 5, 10 |
  | SC 25 (Phase 5) | E 25; U 5/10; G 1; Q 20 | A 3, 10, 25 |
  | SC 50 (Phase 6) | E 50; U 5/10/20; G 1; Q 40 | A 3, 10, 25, 50 |

  - **Coverage:** each stage needs all three OSes, CPU and accelerator workers, two memory bands, LAN,
    and mixed LAN/internet across two sites.
  - **Evidence classes:** real hardware, shared-host VMs (count the host once) and emulated agents
    (control plane only).
  - **Pass criteria:** at least 99% of admitted, non-cancelled requests complete within deadline at
    about 70% load; one explained outcome per request; no leaks or limit violations.

## 13. Phases and gates

| Phase | Objective | Gate |
|---|---|---|
| 0 Alignment | Backlog, decision log, threat model, contracts, fake adapter, OpenAPI skeleton; confirm the Test C budgets, reference model class, baselines and first profile | Agreement on audiences and consent; owners for critical requirements; 3–5 user walkthrough |
| **1 Inference and network feasibility** | Runtimes on all three OSes; probes; the cooperation test; real WAN and relay; backend security evaluation; SC 03; bandwidth and storage probes; **latency-budget estimate per placement**; strongest single-device baselines (AT 20); optional speculative-decoding spike | Valid cross-platform execution; actual remote computation; bounded resources; **Test C beating the strongest single-device baseline**; an approved security path; a recorded hardware decision. Users review measured waits. Otherwise stop and revise. |
| 2 Groups and device control | Identity, invitations, membership, device keys, revocation, policy, persistence, authenticated networking, UI slices | No authorisation defects; 4 of 5 users complete membership tasks |
| 3 Distributed inference service | Backend wired to lifecycle, downloads, placement, reservations, queue, streaming, recovery; **Test B on the expanded group**; SC 10; placement pipeline, conversation cache, first validated profile | Valid outputs, clear failures, measured latency, capacity evidence |
| 4 Privacy and API | Full branching, no-history, credentials, compatibility subset, diagnostics, accessibility | Zero privacy leaks; all participants understand the boundary |
| 5 Installation and hardening | Signed installers, wizard, updates, backup, security review; SC 25 | Install and privacy targets pass; no open critical or high defects |
| 6 Pilot | Two or more independent groups, at least a 7-day monitored pilot; SC 50 | Owner accepts the evidence pack |

The indicative total is 15–26 weeks with a small dedicated team. This is a planning hypothesis, to be
re-estimated after Phase 1.

## 14. Organisation and backlog

Suggested directories: `apps/desktop`, `apps/web`, `services/coordinator`, `agents/device`,
`adapters/inference`, `contracts/http`, `contracts/agent`, `tests/{unit,integration,e2e,hardware}`,
`benchmarks`, `packaging`, `docs/decisions`. Before release, a security reviewer who did not write the
isolation and authorisation code reviews it.

| DEV | First deliverable | Depends on |
|---|---|---|
| 01 | Requirements and decision register | — |
| 02 | Test-device inventory and network measurements | Device access |
| 03 | Native capability probe on all three systems | 02 |
| 04 | Fake runtime and validated HTTP and agent contracts | 01 |
| 05 | Small-model baseline and runtime builds | 02, 03 |
| 06 | Three-device inference with partition evidence | 05 |
| 07 | Actual-capacity model selection and memory ledger | 03, 06 |
| 08 | Backend security decision and cost estimate | 05, 06 |
| 09 | Create, invite, join, use-only prototype | 01, 04 |
| 10 | Privacy branching prototype and comprehension tests | 01, 09 |
| 11 | Test C pilot budgets and Phase 1 hardware criteria | 02 |
| 12 | Strongest single-device offload baselines | 05 |
| 13 | Filter-and-score placement with explanations | 04 |
| 14 | Device and network simulator for 10–50 agents | 04 |
| 15 | Validated profile format and benchmark recipe | 06 |

- **CI:** formatting, types, unit and contract tests, migrations, and synthetic integration on every
  change. Native suites and real-hardware inference run on release builds.
- **Supply chain:** keep model weights out of the repository; pin dependencies and inference commits;
  keep a bill of materials.

## 15. Risks (abridged)

| Risk | Mitigation |
|---|---|
| No secure backend for all targets | Phase 1 hard gate; keep the adapter replaceable |
| The Mac dominates memory; narrow window | Measure budgets; Test C pilot; hardware decision at the gate |
| Slow CPU worker or high RTT | Compare placements; omit unneeded workers |
| Battery or departure removes a partition | Owner controls, clear failure, explicit retry |
| Private mode misunderstood | Visible audience, comprehension gate |
| Broad VPN access to workers | Separate network roles, authenticated protocols, firewall tests |
| Enterprise laptop restrictions | Check permissions early; never bypass policy |
| Hidden GPU or unified-memory limits | Probe real allocation; shared-memory ledger |
| Single-device offload good enough | AT 20; state the claim basis |
| Multi-turn re-prefill cost | Owner-scoped conversation cache (AT 21) |
| Reference projects change quickly | Pin versions; recheck at each gate |

**Open decisions for Phases 0–1:** OS versions, HP GPU, the Mac's network location, disk and install
rights, baseline model and precision, release backend, recovery experience, retention defaults,
acceptable large-model latency, overlay provider, licensing, funding. **Added in v0.3:** reference model
class, warm-hold defaults, conversation-cache retention, and whether speculative decoding enters
Phase 3.

## 16. Release evidence

- **Release package:** signed installers, the support matrix, pinned manifests, the API contract with
  examples, the threat model and security review, requirement-to-test traceability, capacity and
  performance data, energy method, usability and privacy results, recovery guide, known limits, and the
  SC 03–50 stage reports.
- **Success:** a non-technical member gets an answer through chat, and an application gets one through
  the API, from a deployment whose required capacity exceeds every participating device, with correct
  visibility and limits.
- **What does not count:** a pilot that has passed only Test C. Neither does serving the whole model
  from the Mac, or a good-looking UI.

## 17. References

The original cites R1–R23:

- Petals (R1, R2);
- llama.cpp build, RPC and security docs (R3, R4);
- exo (R5);
- Tailscale connection types and ACLs (R6, R7);
- Tauri, FastAPI and gRPC (R8–R10);
- OWASP authorisation, password and session cheat sheets (R11, R17, R18);
- OpenAPI 3.1 (R12), SQLite (R13), pytest (R14), Playwright (R15), WCAG 2.2 (R16);
- llm-d (R19, R20);
- Strata and a review of it (R21, R22);
- the OSDI 2026 Strata context-caching paper (R23).

## Appendix A: v0.2 → v0.3 gap review

Gaps closed in v0.3:

- an offload-aware single-device baseline (3.1, AT 20);
- the capacity window quantified (3.2), with Test C adopted;
- model class in the catalog (FR 28);
- memory bandwidth in the latency model (FR 06, 7.2);
- the conversation cache (8.2, AT 21);
- explainable placement (FR 31, AT 22);
- deadline shedding (FR 32);
- validated profiles (FR 33, AT 24);
- multiple placements (FR 34);
- version-uniform placements (FR 29, AT 23);
- per-partition downloads and per-owner licence acceptance (FR 30);
- warm-hold and protect-my-computer (FR 26, FR 27);
- per-hop tracing (section 10);
- a scale simulator (12.1).
