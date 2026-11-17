# Decision log

Decisions made while building DIFA, with the reasoning and the alternatives considered. Numbers in
brackets refer to spec v0.3 (FR = functional requirement, DEV = development backlog item, AT =
acceptance test). Measurements behind each decision are in
[phase1-findings.md](../benchmarks/results/phase1-findings.md).

## Working rules for contributors and AI assistants

These are the project owner's standing instructions.

- **Commits:** commit or push only when the owner asks. Commit messages carry no attribution or
  co-author lines.
- **Downloads:** ask before downloading anything, and state the file, source and size. Model downloads
  included.
- **Prompts:** use synthetic prompts only with llama.cpp RPC. It is a research backend with no
  authentication or encryption of its own (spec 6.2).
- **The HP laptop** is a workplace machine. Never work around its organisation's policy (installs,
  services, firewall). If policy forbids a step, use another machine for that role.
- **Never commit secrets or private data:** device state (`agent.json`), session files, keys and
  certificates, databases, model weights, or personal details.

## Specification

| Date | Decision | Why | Alternatives considered |
|---|---|---|---|
| 2026-11-05 | Revise the spec and whitepaper to v0.3 after a gap review of v0.2, drawing on llm-d and Strata. Additions: strongest single-device baseline, model-class-aware planning (FR 28), filter-and-score placement (FR 31), deadline admission (FR 32), validated profiles (FR 33), multiple placements (FR 34), per-device latency budget, conversation cache. Gaps and their fixes are in spec Appendix A. | v0.2 had no way to show distribution beat the best single device, and no planner model | — |
| 2026-11-05 | FR numbering: FR 29–35 renumbered to FR 28–34 to close a gap | Consistent references | — |
| 2026-11-05 | **The pilot capacity claim is Test C (owner-budget capacity)** with the existing three devices: Mac capped at 64 GB, HP and VM at their measured budgets. A large-memory contributor (64 GB or more) is approved or declined at the Phase 1 gate. Test B (actual capacity) stays required before release. | On the reference hardware the Test B window above the Mac is only about 10–15 GB, so small changes in memory pressure move a model in or out of single-device range (spec "Test C pilot budgets"). | Buy a big-memory device first and run Test B. Rejected for the pilot: it is a hardware cost before feasibility is shown. |

## Prototype (Phase 0)

| Date | Decision | Why |
|---|---|---|
| 2026-11-05 | Coordinator in Python (FastAPI, Pydantic v2, SQLite WAL, argon2), device agent in Rust (one static binary per OS) | Not recorded at the time. Likely: fast iteration on the API, and a small native agent for Windows, macOS and Linux. |
| 2026-11-05 | A fake runtime behind the adapter interface, so access, queue and lifecycle logic is tested without inference | DEV 04 |
| 2026-11-05 | Private-to-shared forks get a neutral title ("Shared conversation") unless the user names one | The first version copied the private title, which leaked private content |
| 2026-11-05 | Builds, the venv, models and the database live outside the iCloud-synced checkout | iCloud would sync gigabytes of build output |

## Phase 1: inference backend and placement

| Date | Decision | Why | Alternatives |
|---|---|---|---|
| 2026-11-05 | Use llama.cpp **v0.9.0** with RPC as the *research* backend; the same tag on every machine | Runs on all three OSes and splits layers across devices. The RPC protocol changes between releases. | The release backend is undecided (DEV 08) |
| 2026-11-05 | llama-server binds only to 127.0.0.1, with a random `--api-key` | Its defaults have no API key and allow all CORS origins | — |
| 2026-11-05 | The planner runs the strongest device last, so it holds the output head | llama.cpp puts the output layer on the final device in execution order | — |
| 2026-11-05 | Every worker must hold weights; a stage with 0 MiB fails as `PARTITION_MISMATCH` | A split gave a connected worker zero layers, so it did no work | — |
| 2026-11-05 | The agent enforces the owner's memory cap with a 250 ms watchdog that kills the worker, and the coordinator drains | llama.cpp's RPC worker has no memory cap of its own | OS limits (cgroups, Job Objects): deferred to Phase 2 |
| 2026-11-05 | Latency model: per-stage weights over effective bandwidth, plus fitted fixed overhead per remote hop (3.58 ms, then 0.74 ms each), plus k = 1.02 round trips per remote stage per token, plus traffic over link | Within 13% on held-out loopback topologies | Bandwidth-only model: −53% to −56% error, rejected |
| 2026-11-05 | Device speed comes from a timed decode probe (llama-bench) per backend | A CPU memory-copy probe cannot stand in for Metal | — |
| 2026-11-09 | Worker traffic goes through a **mutual-TLS tunnel** run by the agent. The worker stays on loopback; both ends pin certificates exchanged at enrollment, by exact bytes, with no CA. Interim answer to DEV 08. | llama.cpp RPC is unencrypted and unauthenticated | SSH tunnels, or Tailscale with restricted rules. Rejected as the default because they need per-device setup outside DIFA. |
| 2026-11-09 | Exact `-ts` split points, plus a check that each device's loaded bytes match the plan within 2% | llama.cpp splits `n_layer + 1` slots, so raw layer counts move boundaries | — |
| 2026-11-09 | Partition prefetch (FR 30) uses llama.cpp's RPC weight cache. Devices fetch their byte ranges with HTTP Range requests, verify the SHA-256, and store them under llama.cpp's FNV-1a names. Download needs the owner's `--accept-license`. | Removes weight streaming from load time (load went from 53.0 s to 30.0 s at 50 Mbps) | — |
| 2026-11-16 | The latency model's RTT input is **timed through each worker tunnel** before loading: 16 back-to-back llama.cpp RPC round trips, using the mean. The heartbeat-based prediction is kept as `planned_latency_budget`. | Heartbeat RTT included HTTP and coordinator handling, so predictions were 53–58% too slow on real links | ICMP ping (often blocked on Windows) |
| 2026-11-16 | On a drain, llama-server is killed without its RPC cleanup, and the log notes `difa: drain` | Its cleanup aborts once a remote worker has gone, which hid real crashes in the logs | — |

## Phase 1: control channel security

| Date | Decision | Why | Alternatives |
|---|---|---|---|
| 2026-11-16 | Agent control traffic (enroll, heartbeats, coordinator weight downloads) goes over **HTTPS with a self-signed coordinator certificate pinned by SHA-256**. The fingerprint is returned with each enrollment code, and the agent checks it during the TLS handshake before sending the code. At least 16 hex digits (64 bits) are accepted at enrollment; the full 64 are pinned afterwards. Plain HTTP is allowed only on loopback. | The device token and enrollment code crossed the LAN in clear text. A home network has no CA. | A public CA (needs a domain), or trust on first use (open to a MITM at enrollment). gRPC over mTLS remains the Phase 2 target. |
| 2026-11-16 | Separate certificates for the control server and the tunnel client | Different roles; can be rotated separately | One shared key |
| 2026-11-16 | Control connections stay open for 30 s (`CONTROL_KEEP_ALIVE_S`). Heartbeat replies carry `Server-Timing`, and the agent reports the median of its last five network-only samples. | uvicorn's 5 s keep-alive equalled the heartbeat interval, so every heartbeat re-handshaked TLS (83–191 ms RTT) | — |
