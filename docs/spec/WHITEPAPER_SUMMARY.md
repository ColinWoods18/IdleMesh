# Design whitepaper v0.3: summary

A condensed, paraphrased guide to *Distributed Inference For All: Design Whitepaper*, version 0.3
(November 5 2026), the companion to the [product specification](SPEC_SUMMARY.md). The full `.docx` is
kept outside this repository. If this summary and the original disagree, the original wins.

The whitepaper gives the *reasoning*. The spec turns it into requirements, tests and phases. Equations
and hypotheses (H1–H7) below are the ones the code and the findings refer to.

## 1. Motivation

- **The gap:** people own computers that are useful on their own but cannot host a wanted model at an
  acceptable precision and context length. Memory, not compute, is often the limit. Some depend on
  battery power or unreliable electricity.
- **Prior work:** Petals showed that collaborative inference over the internet is possible. Its results
  do not predict how this particular mix of devices performs.
- **The design space:**
  - llm-d serves replicated models across data-centre accelerators;
  - Strata runs one large mixture-of-experts (MoE) model on one desktop;
  - DIFA sits between them: consumer devices, ordinary networks, a trusted group. It borrows llm-d's
    control-plane patterns and Strata's single-device techniques.
- **Four different goals, never merged:**
  - making inference *possible*: the primary goal;
  - serving more people;
  - making a response faster;
  - saving energy.

  The last three each need their own measurements.

## 2. Problem and boundaries

- **The question:** can a trusted group of mixed devices reliably run a model configuration beyond each
  participant's safe standalone capacity, in a way non-technical people can operate, with acceptable
  service for several members?
- **Unit of comparison:** a *model configuration*: exact weights and revision, format, tokenizer and
  template, context, output limit and concurrency. A model family may fit on one machine at lower
  precision; reports must say so.

### Ways to distribute inference

| Approach | Use here |
|---|---|
| Request distribution (whole model per machine) | Adds throughput, but cannot meet the larger-than-one-device goal |
| **Model partitioning (layers across devices)** | **The core mechanism.** The preferred shape is large contiguous slices, to minimise network boundaries. |
| Tensor parallelism | Only on fast interconnects; to be evaluated over WAN |
| Prefill/decode disaggregation | Moves the attention cache between workers; out of v1 scope over the internet |
| Expert parallelism | Exchanges inside every layer; not suited to WAN |

### Dense and mixture-of-experts models

- A dense model reads every weight for every token.
- An MoE model stores many experts but activates few per token. Memory follows total parameters, while
  compute and bandwidth follow active parameters.
- That helps a single device (Strata caches hot experts) more than a group. Over WAN, split by
  contiguous layers, with each device holding all experts of its layers.
- Catalog entries record the model class, and capacity claims state which class was tested.

## 3. System design

- **Groups:**
  - Independent, invitation-only, each with its own identity, members, devices, coordinator, model,
    history and queue. No marketplace or central directory.
  - Membership allows use; contributing a device is a separate step with owner-set limits that
    administrators cannot raise.
- **Coordinator:** one per group in v1, initially the Mac. It is simple, and it is a single point of
  failure; recovery is a tested restart or manual migration.
- **Layers:**
  - the gateway authenticates and enforces visibility;
  - the coordinator handles membership, revisions, queue, reservations and failures;
  - agents enforce owner policy and supervise runtimes;
  - the runtime adapter isolates the backend;
  - workers compute and release state.

  The control protocol is kept separate from the backend's tensor transport, so the product layers
  survive a change of backend.
- **Request path:** resolve group, branch, visibility, revision and limits; validate size and quota;
  reserve memory; enter a bounded fair queue; run on the pinned placement; stream only to authorised
  recipients; release everything at the end.
- **Placement:**
  - It uses a filter-and-score pipeline, after llm-d. Filters remove ineligible devices; scorers rank
    placements by measured stage time, network cost, stability and power.
  - Decisions are versioned and explained without content.
  - Once latency is calibrated, admission sheds requests that cannot meet their deadline.
  - Setup offers only validated topology profiles.
- **Concurrency:** many users with one generation at a time is genuine multi-user service. Parallel
  decoding waits for memory and fairness tests.
- **Privacy:**
  - Shared mode is visible to the group; private mode is visible to the requester in the application.
  - Switching creates a new branch. Private-to-shared starts without hidden history, because a shared
    answer could reveal a private fact.
  - Neither mode protects plaintext from someone who administers an execution host. Setup must say so
    plainly.
- **Conversation cache:**
  - One branch's attention state is kept for a few minutes, for the same requester and revision,
    inside its reservation.
  - It is never shared across users or branches, and is evicted on switch, revocation, deployment
    change or memory pressure.
  - Cross-user prefix sharing stays off.
- **Energy and availability:**
  - Laptops contribute on mains by default, with battery thresholds, warm-hold unloading and a
    protect-my-computer drain.
  - Agents enforce policy even when the coordinator is unreachable; a lease bounds unsupervised work.
  - Losing a required partition fails the generation. Enrolling several devices does not imply
    redundancy.

## 4. Capacity and performance model

### 4.1 Per-device feasibility

For each device *i* with usable budget *B_i* (after owner limits, OS headroom and backend ceiling):

```text
W_i + K_i + R_i + X_i <= B_i
```

| Term | Meaning |
|---|---|
| W_i | Assigned weights |
| K_i | Reserved attention (KV) cache |
| R_i | Runtime workspace |
| X_i | Transfers, duplicated data and uncertainty |

The inequality must hold on *every* device; a positive group total proves nothing.

An illustrative KV-cache estimate (calibrate it against the allocator):

```text
K_i ≈ 2 × L_i × S × T × H_kv × D_head × b
```

| Symbol | Meaning |
|---|---|
| L_i | Local layers |
| S | Sequences |
| T | Tokens kept per sequence |
| H_kv | Key/value heads |
| D_head | Head width |
| b | Bytes per element |

For scale: 70B weights need about 140 GB at 2 bytes each, or about 35 GB at a nominal 4 bits, before
overhead. Budgets count resident memory only. Offload modes (streaming weights, cold experts) are
reported separately, with their own measured speed.

### 4.2 Added capacity against artificial limits

- **Why Test B is hard:** the Mac's 128 GB makes the actual-capacity test demanding. Planning budgets
  are Mac 90–110 GB, HP 6–10 GB and VM 4–5 GB, so a qualifying configuration can exceed the Mac by only
  about 10–15 GB. That makes the result sensitive to small changes in OS memory use.
- **Hence Test C:** the spec adopts the owner-budget result as the pilot claim. A large-memory
  contributor is bought at the Phase 1 gate only if distributed execution works and beats the strongest
  single-device baseline.
- **Strongest baseline:** each device tries the configuration with every supported offload mode. The
  distributed result counts if no single device can run it, or if a single device is too slow for the
  interactive target while the distributed placement meets it.
- **Publish constrained-budget and actual-capacity results separately.**

### 4.3 Latency and network cost

```text
T_token ≈ Σ compute + Σ communication + synchronisation
compute_i ≈ W_i / BW_i      (decode is memory-bandwidth bound)
```

- **Communication** is message latency plus bytes over effective throughput, counted per *dependent
  exchange*. An RPC backend may exchange more messages than an ideal pipeline.
- **Report separately:** loading, time to first token and decode rate.
- **Worked example (nominal figures):**
  - 40 GB on the Mac at about 800 GB/s takes about 0.05 s per token;
  - 8 GB on the HP at 51 GB/s takes at least 0.16 s;
  - 4 GB on the VM at 10–20 GB/s takes 0.2–0.4 s;
  - two or three internet exchanges at 20–80 ms add 0.04–0.24 s;
  - total: about 0.45–0.85 s per token, or **1–2 tokens/s, dominated by the smallest devices**.
- **A slow device** can make a configuration possible while slowing every token. The planner uses such
  devices only when capacity requires them.
- **Speculative decoding** (a draft model on a fast device; the placement verifies several tokens in
  one pass; lossless) could save round trips over WAN. It is an optional Phase 3 experiment.
- **Record each benchmark's connection type** (direct or relayed). A faster network does not fix slow
  devices or poor partitioning.

### 4.4 Energy

- **Method:** measure incremental energy against matched idle periods. A useful metric is incremental
  joules per completed output token. Failed requests count.
- **Sources:** prefer external meters. Report battery discharge separately. VM figures are estimates
  and must not be mixed with metered values.
- **Expectations:** the project may help a battery-limited user without reducing the group's total
  energy use.

## 5. Tools and technologies

- **llama.cpp** is the first portability experiment. Its RPC mode is documented as fragile and insecure
  and must not become a production WAN substrate just by putting it behind a VPN.
- **Petals and exo** are reference points; exo does not establish native Windows support.
- **llm-d and Strata** are references, not candidates. llm-d needs Kubernetes and fast interconnects;
  Strata is young, single-machine and self-reported.
- **What the backend decision must produce:** pinned version, model format, operations, tested resource
  limits, cancellation, protocol compatibility and a security maintenance plan. If no backend
  qualifies, a hardened or custom engine needs its own budget. A friendly installer cannot make an
  unsuitable engine production-ready.
- **Stack:** the same as spec 6.3, plus placement in the coordinator and OpenTelemetry-compatible
  content-free traces.
- **API:** an OpenAI-style subset alongside a native asynchronous API, with group-bound credentials.
- **Overlay:** pilot access policies must be explicitly restrictive. Group enrollment is distinct from
  joining the overlay, and a local-only path stays required.

## 6. Methodology

### 6.1 Hypotheses

| ID | Hypothesis |
|---|---|
| H1 | A configuration runs distributed while exceeding every device's safe standalone capacity (fixed workload, measured allocations) |
| H2 | A Windows CPU worker, a Mac accelerator worker and a remote Linux CPU worker contribute to the same correct inference (partition map, computation on each) |
| H3 | Five members use chat and the API without lost work, disclosure, or uncontrolled memory growth (serialised generation) |
| H4 | Non-technical users install, join, contribute, pause and understand visibility unaided |
| H5 | Local resource and battery controls hold during network loss or coordinator failure (energy savings are not assumed) |
| H6 | Growth to 10, 25 and 50 devices keeps correctness, isolation, owner controls and service bounds |
| H7 | Each capacity configuration either cannot run on any single device, or meets the interactive target where the strongest offloaded single device does not |

### 6.2 Experimental stages

1. **Characterise** devices and networks: versions, limits, available memory, storage, power,
   privileges, and pairwise links. Hash configurations and manifests.
2. **Baselines and correctness** on a small model:
   - topologies: each device alone, Mac+HP, Mac+VM, all three;
   - compare tokens or logits within a tolerance defined *before* seeing results;
   - add semantic tasks to separate numerical noise from broken execution.
3. **Capacity:**
   - run the controlled-budget and actual-capacity cases separately;
   - check that standalone failures are capacity failures, not unsupported operations;
   - run the strongest single-device baselines;
   - compare traces with the latency-budget estimate.
4. **Multi-user:** 1, 3 and 5 members, chat and API, private and shared.
5. **Faults and networks:**
   - kill workers mid-prefill and mid-decode, drop clients, restart the coordinator, revoke members,
     pause the laptop, fill the disk;
   - test a real WAN path plus controlled latency, bandwidth and loss.
6. **Users:** 5–8 non-technical participants per round, plus a developer API session.

### 6.3 Metrics

- **What to report:** first-token latency, queue wait, decode rate, end-to-end time, cold start, success
  and timeout rates, peak memory, bytes moved, limit violations, loss detection, cancellation delay,
  and energy.
- **Latency budget:** predicted against traced per-token time **per stage**.
- **Conversation cache:** second-turn first token with and without the cache.
- **Sample sizes:** at least 100 warm requests per main condition (p50 and p95), at least 10 per fault,
  at least 3 per exploratory placement. Keep raw content-free records, including failures.
- **Small-model targets:** p95 first token within 30 s and median decode of at least 2 tokens/s on the
  validated mixed topology. 4 of 5 users complete each core task, and 5 of 5 understand visibility.

### 6.4 Security and privacy validation

- **Threat scenarios:** leaked invitations, stolen API keys, guessed IDs, wrong-group certificates,
  malicious model output, stale device authorisation, diagnostics containing private text, and a member
  reaching a worker endpoint.
- **Canary tests:** unique synthetic strings in private prompts must appear on no other member's
  surface.
- **Accessibility:** WCAG 2.2 AA for core tasks.

### 6.5 Staged scalability

- **Stages:** SC 03, 10, 25 and 50, reporting the tuple (E, R, A, U, G, Q) as in the spec.
- **Connected is not computing:** connected-device scale and active-execution scale are different
  limits. 50 connected devices with 10 computing is not 50-device execution.
- **Evidence classes:** real hardware for hardware claims, shared-host VMs counted once, emulated agents
  for the control plane only.
- **Stable load:** at about 70% of capacity, at least 99% of admitted requests finish within deadline.

## 7. Phases and gates

- **Seven phases**, as in the spec: alignment, feasibility, groups and devices, inference service,
  privacy and API, installation and hardening, pilot.
- **Backend feasibility comes first.** A fake runtime and UI prototypes may proceed early but are not
  evidence that distribution works. An insecure research demo is not a release candidate.
- **If the devices cannot show the capacity goal:** add resources or revise scope openly. Never lower
  precision, add artificial limits, or redefine success without saying so.

## 8. Limitations

- **The reference group** is small and lopsided: the Mac supplies most memory, the VM may bottleneck
  latency, and the laptop's availability varies. No 10, 25 or 50-device result exists yet.
- **Private mode** relies on trusted execution hosts. Confidentiality against contributors needs another
  threat model.
- **The single coordinator** limits availability.
- **Later candidates:** redundant partitions, failover, multiple deployments, batching, and
  energy-aware scheduling.
- **The capacity window** is narrow, and single-device engines keep improving. Either may push the claim
  to an owner-budget result or require more hardware. Recheck llm-d and Strata at each gate.

## References

The original cites R1–R22. They match the spec's list except:

- R17 is the Petals security and privacy wiki;
- R18–R22 are llm-d, Strata and the OSDI 2026 paper, numbered one lower than in the spec.

All sources were reviewed November 5 2026.
