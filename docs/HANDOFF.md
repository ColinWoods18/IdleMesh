# DIFA handoff: where the project stands and how to resume

Start here. This file holds project state that is not recorded elsewhere in the repository. Detail lives
in the files it cites:

| Topic | File |
|---|---|
| Measured results | [benchmarks/results/phase1-findings.md](../benchmarks/results/phase1-findings.md) |
| Running the test | [docs/testing/](testing/) |
| Why things are the way they are | [DECISIONS.md](DECISIONS.md) |
| Machines, paths, versions, gotchas | [ENVIRONMENT.md](ENVIRONMENT.md) |
| What the product must do (spec and whitepaper v0.3, summarised) | [spec/SPEC_SUMMARY.md](spec/SPEC_SUMMARY.md), [spec/WHITEPAPER_SUMMARY.md](spec/WHITEPAPER_SUMMARY.md) |

State as of 2026-11-17, commit `fb22446`.

## Where things stand

- **Spec and whitepaper:**
  - Version 0.3 of the DIFA Product Specification and Design Whitepaper (Distributed Inference For All)
    is current.
  - Both are `.docx` files kept **outside this repository**, in the parent folder of the checkout. See
    ENVIRONMENT.md.
  - Condensed Markdown summaries are in [docs/spec/](spec/). They are enough to resume work; use the
    originals when exact wording matters.
  - v0.3 came out of a gap review of v0.2, drawing on llm-d and Strata.
  - Spec section numbers (FR, AT, DEV, SC) used across the code refer to v0.3.
- **Phase 0 prototype** is done (`76de660`):
  - coordinator, device agent, contracts and fake runtime.
  - Not done:
    - Phase 0 user testing with 3–5 non-technical people;
    - the browser chat UI.
- **Phase 1 (inference and network feasibility)** is in progress, with seven measurement rounds:

| Round | Date | Commit | What it established |
|---|---|---|---|
| 1 | 2026-11-05 | `ccf4698` | llama.cpp v0.9.0 RPC works on one Mac. Output head sits on the last device. |
| 2 | 2026-11-05 | `7eeb4bb` | Agent-enforced memory caps; fitted latency model; network delay relay |
| 3 | 2026-11-09 | `0857bc7` | Mutual-TLS worker tunnel; partition prefetch; exact layer splits |
| 4 | 2026-11-16 | `52a1625`, `ad716f9` | First run on the real HP and VM. Heartbeat RTT made the prediction miss. |
| 5 | 2026-11-16 | `bbc5d3d`, `1dd84d6` | Tunnel-timed RTT. Prediction within 30% on Wi-Fi. |
| 6 | 2026-11-16 | `40e80cc`, `2a9dea8` | HTTPS control channel works on all three OSes. Prediction within 7% with the Mac wired. |
| 7 | 2026-11-17 | `fb22446` | Prediction 19–26% too fast with the Mac on Wi-Fi. Heartbeat fix not in that run. |

## Phase 1 gate (spec section 13, "Phase 1 Inference and network feasibility")

| Gate item | Status | Evidence or blocker |
|---|---|---|
| Valid cross-platform model execution | Met | Rounds 4–7: macOS Metal, Windows CPU, Linux CPU |
| Actual remote computation | Met | Partition map per device in every round; mTLS transport |
| Bounded resource behaviour (owner caps) | Met in tests; not on real devices | `tests/integration/test_agent_worker.py`; runbook checks 4a–4f never recorded on hardware |
| Latency estimate agrees with traces within 30% (spec section 11) | Met | Rounds 5–7, −6% to −22% by bench fit. It is consistently too fast with the Mac on Wi-Fi. |
| Test C owner-budget result that beats the strongest single-device baseline | **Not started** | Needs the large model download (about 58 GB), which needs owner approval |
| Approved security path (DEV 08) | **Open** | mTLS tunnel and pinned HTTPS are interim. Rotation, independent review and a backend decision are outstanding. |
| Recorded hardware decision (large-memory contributor) | **Open** | Depends on the Test C result |
| User review of measured waits (3–5 users) | **Not started** | |

## Not yet verified on the physical machines

- **Heartbeat RTT fix** (`48cac9a`): the Round 7 coordinator ran from a checkout still at `2a9dea8`. The
  next run should show heartbeat RTT near the tunnel RTT (about 10–20 ms), not 80–190 ms.
- **Clean drain stop** (`bbc5d3d`): llama-server is now stopped without its RPC cleanup when a worker
  leaves first. So far only the integration test exercises this; no drain has happened on hardware.
- **Owner-control checks 4a–4f** in `docs/testing/three_machine_test.md`: their outcomes were never
  recorded.

## Backlog, in recommended order

1. **Re-run the three-machine test on current `main`** with drains 4a–4f recorded. It is cheap and closes
   the three unverified items above. Pull the second checkout first (see ENVIRONMENT.md).
2. **Measure link throughput per stage and fill `link_mbps`.** Rounds 5 and 7 (Mac on Wi-Fi) predicted
   19–22% too fast, against −6% with the Mac wired. The model drops the per-token traffic term because
   `link_mbps` is never measured. Possible sources:
   - the tunnel forwarder's `bytes_to_device` count over the load time;
   - a short bulk transfer during the RTT probe.

   Also re-measure `bytes_per_token_per_remote` (0.62 MB, from loopback) for a one-layer CPU stage.
3. **Test C (the main remaining gate item).** It needs the owner's approval to download.
   - **Candidate model:** a 70B model at about 6-bit (Q6_K), roughly 58 GB.
   - **Fit:** it must exceed the Mac's usable budget under its 64 GB owner cap (minus 10% headroom),
     and fit all three budgets combined. It only just fits.
   - **Expected speed:** 1–2 tokens/s over a wide-area path.
   - **Label:** the Mac could run it alone without its cap, so the result is labelled an owner-budget
     result, not the main capacity claim.
   - **Baseline:** also measure the strongest single-device baseline (DEV 12).
4. **Backend security decision (DEV 08).** llama.cpp RPC is a research backend only. The options at the
   gate are:
   - a maintained secure backend;
   - an owned, hardened adapter with its own budget;
   - a reduced scope.
5. **Security follow-ups:**
   - certificate rotation and expiry for the control and tunnel certificates;
   - restrict the agent state file's permissions on Windows;
   - Windows power status probing (it reads "unknown", so the HP needs `--allow-battery`);
   - OS-level memory limits for the worker (cgroups, Job Objects). The 250 ms watchdog can overshoot.
6. **Remaining spec features:**
   - deadline-aware admission (FR 32);
   - validated profiles (FR 33);
   - multiple placements (FR 34);
   - conversation cache;
   - encryption at rest with the group data key;
   - no-history mode and retention jobs;
   - warm-hold and protect-my-computer (FR 26, FR 27);
   - prefetch disk-space checks.
7. **Phase 0 leftovers:** user testing with mock-ups, and a browser chat UI.

## Decisions waiting on the owner

- Approve the Test C model download (about 58 GB) and choose the exact model and revision.
- DEV 08 backend path, and the Phase 1 hardware decision, after Test C.

## How to resume

1. Clone `https://github.com/TheCodeheadMT/I4A.git`. Keep the checkout out of synced folders, or keep
   builds and data outside it, as the guides do.
2. Install and build as in [docs/testing/mac_test_setup.md](testing/mac_test_setup.md):
   - Python 3.10+ venv, `pip install -e ".[dev]"`;
   - llama.cpp **v0.9.0** with Metal and RPC;
   - the agent with `cargo build` and `cargo build --release`;
   - the test model, checking its SHA-256.
3. Verify:
   - `pytest tests -q`: 79 tests. The integration tests need the agent's **debug** build, the
     llama.cpp build and the model; otherwise they are skipped.
   - `cargo test` and `cargo clippy --all-targets` in `agents/device`: 24 tests, clippy clean.
4. Run the three-machine test with
   [docs/testing/three_machine_test.md](testing/three_machine_test.md):
   - restart the coordinator with `--fresh`;
   - run `pilot setup`;
   - enroll each device over `https://` with `--coordinator-fingerprint`;
   - `pilot deploy`;
   - `pilot bench --n 20 --max-tokens 64 --network "..."`.
5. Add each round's result JSON to `benchmarks/results/` and a section to `phase1-findings.md`.
