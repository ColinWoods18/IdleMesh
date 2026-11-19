# Inference backend options: llama.cpp RPC vs. a custom pipeline engine

Status: **direction agreed by Colin, approved by Michael (2026-11-19)** (see [changes.md](../../changes.md), decisions D-04 and D-09). Written 2026-11-18.

- **Phases 1–2:** llama.cpp RPC with request-level recovery.
- **Custom engine:** path B, a stage server built on a small patch to llama.cpp, detailed in [section 5](#5-path-b-in-detail-the-chosen-custom-engine-route).

This note explains what llama.cpp's RPC mode can and cannot do for IdleMesh. It then outlines what a
custom pipeline engine would involve, and recommends a staged path.

Numbers marked *measured* come from the inherited DIFA Phase 1 rounds
([results](../../experiments/llamacpp-feasibility/results/phase1-findings.md)). They used llama.cpp
v0.9.0 and Qwen2.5-0.5B, on a Mac Studio, a Windows laptop and an Ubuntu VM. Anything marked *verify*
is not established by this repository and must be checked against current upstream code first.

---

## 1. How llama.cpp RPC actually works

```
             ┌──────────────── head node ────────────────┐
 request ──▶ │ llama-server: tokenizer, sampler, graph,  │
             │ scheduler, layers N..end + output head    │
             └───┬──────────────▲──────┬──────────▲──────┘
      graph+acts │    acts back │      │          │
                 ▼              │      ▼          │
           rpc-server A (layers 0..i)  rpc-server B (layers i..j)
           holds weights + KV cache    holds weights + KV cache
```

- **Hub and spoke, not a chain.** One `llama-server` process (the head) owns the model, builds the
  compute graph and drives every remote `rpc-server`. Workers never talk to each other. Each remote
  stage is a "remote GPU" that the head calls in turn.
- **Each token pays every round trip.** A decode step visits the stages in order: send the subgraph
  and inputs, wait, read the outputs back.
  - *Measured:* about 1.02 round trips per remote stage per token.
  - *Measured:* about **0.62 MB of traffic per remote stage per token** on a 0.5B model. That is
    mostly graph serialisation. The hidden state alone would be about 2 KB.
- **The KV cache lives on the workers.** The attention cache for layers i..j sits on the node that
  holds those layers.
- **The worker set is fixed when the model loads.** The `--rpc` endpoint list is a `llama-server`
  start argument. Adding, removing or replacing a worker means restarting `llama-server` and loading
  the model again.
- **Losing a worker kills the head.** *Measured:* when a worker left, `llama-server` aborted with
  "Remote RPC server crashed or returned malformed response". It does not reconnect, retry or fail
  over.
- **No security of its own.** There is no authentication, no encryption, no memory cap, and the
  worker deserialises whatever it is sent. Upstream says never to expose it on an open network.
  - *Built:* the inherited mutual-TLS tunnel keeps the worker on loopback, so only the pinned
    coordinator can reach it.
  - *Built:* the agent's memory-cap watchdog stops the worker if it exceeds the owner's limit.
- **Same build everywhere.** The RPC protocol changes between releases, so every node must run the
  same llama.cpp version (pinned at v0.9.0 here).
- **Batching should help a lot.** `llama-server -np N` serves N sequences with continuous batching.
  One decode step then advances all N sequences for roughly the same network round trips, so on a
  network-bound split, throughput should grow almost linearly with N until compute or memory runs out.
  The cost is N times the KV memory. *Verify* over RPC and measure: it is the most important number
  for a student queue.
- **Pipeline bubbles.** With one head, only one stage works at a time during a decode step, so with
  three stages each node is idle about two thirds of the time. *Verify* whether llama.cpp's multi-copy
  pipeline scheduling overlaps work across RPC backends. It was not observed here.

### What this means for IdleMesh's goals

| Plan item | With llama.cpp RPC |
|---|---|
| Cross-backend pipelines (C3) | **Works now.** Metal, Windows CPU and Linux CPU ran together, and greedy output matched a single device. |
| Availability-aware placement (C1) | **Works.** Placement happens at load time, which is where availability prediction helps most. |
| Graceful departure (C2) | **Only between requests.** A draining node can let the current request finish, but its layers then need a reload elsewhere. Handing off mid-request is impossible. |
| Hard-failure recovery | **Request level only.** Running requests fail and the deployment must be reloaded. Recovery means re-running the request (prompt plus any text already streamed) on a standby or reloaded deployment. |
| Replicas of critical stages | **Not inside one deployment.** Each deployment has one copy of each layer, so replication means whole standby deployments. |
| Whole-model replicas (small models on one node) | **Works, and needs no RPC.** Plain `llama-server` on a single node. |

---

## 2. What a custom pipeline engine would look like

This is the approach of Petals and similar systems, applied to IdleMesh.

```
 router/client ──acts──▶ stage A ──acts──▶ stage B ──acts──▶ stage C (head)
   keeps token history   layers 0..i      layers i..j       layers j..end, logits
   and per-stage inputs  own KV cache     own KV cache      ──▶ token back to router
        ▲                  replica A'       replica B'
        └───────────────── on failure: pick a replica, replay that stage's cached inputs
```

- **Stage servers.** Each node runs a server that loads only its layers, keeps a KV cache per session
  for those layers, takes hidden states in and passes hidden states on.
- **Only activations travel.** That is roughly `d_model × 2 bytes` per token: about 8 KB for an 8B
  model and 16 KB for a 70B model. That is one to two orders of magnitude less than the measured RPC
  traffic.
- **A chain, not a hub.** Each token crosses each link once (A→B→C), instead of a round trip from
  the head to every stage.
- **Mid-request recovery.** The router keeps the inputs it sent to each stage. If a stage dies, it
  picks a replica holding the same layers and replays those inputs to rebuild the KV cache, and
  generation continues. Petals works this way.
- **Graceful handoff with KV migration (C2).** A departing node can send its KV cache to the
  replacement instead of making it replay.
  - Rough size for a 70B-class model (80 layers, 8 KV heads × 128 dimensions, fp16): about 320 KB
    per token over all layers.
  - A stage of 20 layers holding a 2,000-token session has about 160 MB of cache, around 1.3 s at
    1 Gbit/s.
  - Few seconds of notice would be enough, which is exactly the C2 hypothesis.
- **Changing membership.** Nodes join and leave without restarting the others. Each stage can have
  several replicas, and requests can be routed per stage.
- **True pipelining.** Different students' requests can occupy different stages at once, so nodes
  are not idle while they wait their turn.

### Ways to build one

| Path | What it means | Main costs |
|---|---|---|
| **A. Fork or extend Petals** (PyTorch and Hugging Face, MIT) | Fastest to a working prototype. Mid-request fault tolerance already exists. | GGUF and llama.cpp quantization are lost. CPU and Apple Silicon support is weaker than CUDA. Every workstation needs a heavy Python and PyTorch install. *Verify* that Petals is still maintained. |
| **B. A stage server built on ggml or llama.cpp** (MIT) | Keeps GGUF, the quantizations and all backends (CUDA, Metal, Vulkan, CPU). llama.cpp already accepts embeddings as input (`batch.embd`). | llama.cpp builds whole-model graphs per architecture, so running a layer range and returning hidden states needs a maintained patch or fork for each architecture family, plus ongoing rebase work. Forks that already do layer-range pipelining over llama.cpp may exist (*verify* their status and license). |
| **C. Adopt another engine** (Parallax, distributed-llama and others) | Possibly the least code. | You inherit their limits. *Verify* licenses: EXO is GPL-3.0 and so is excluded by the plan's licence rule. Parallax and distributed-llama need checking. |

---

## 3. Pros and cons

| | **llama.cpp RPC (current)** | **Custom pipeline engine** |
|---|---|---|
| Works today | ✅ On real Mac, Windows and Linux machines, with tests, tunnel, prefetch and a latency model | ❌ Months of work before parity |
| Model coverage | ✅ Every architecture and quantization llama.cpp supports | ⚠️ One architecture family at a time |
| Backends | ✅ CUDA, Metal, Vulkan, CPU from one GGUF file | Path B ✅, Path A ⚠️ |
| Network traffic per token | ❌ About 0.6 MB per stage (measured, small model) | ✅ About 8–16 KB of activations |
| Round trips per token | ❌ About 1 per remote stage, through the head | ✅ One pass along the chain |
| Recovery from failure | ❌ Head aborts. Reload, then re-run the request. | ✅ Replay to a replica and continue mid-request |
| Graceful departure (C2) | ⚠️ Between requests only | ✅ KV migration or replay with a few seconds' notice |
| Membership changes | ❌ Reload on every change | ✅ Join and leave live |
| Utilisation across stages | ❌ Stages wait their turn (verify) | ✅ Pipelined across requests |
| Security surface | ⚠️ Unauthenticated protocol, wrapped by our tunnel | ✅ Designed in, but you own all of it |
| Upkeep | ✅ Follow upstream, pin a version | ❌ Patches to rebase, per-architecture numerics to validate |
| Research novelty | Placement, prediction, measurement | Adds a stronger C2 result: handoff vs. replay vs. reload |

---

## 4. Recommendation: staged, decided by data

1. **Phases 1–2 (the service and the dataset): stay on llama.cpp and make recovery work at the
   request level.**
   - **Most student traffic should go to whole-model replicas on single nodes.** An 8B–14B model at
     Q4 fits one 16 GB workstation. There is no RPC on this path, a failure means re-running the
     request on another replica, and a drain means routing new requests elsewhere.
   - **Use split deployments only for models too big for any one node.** Keep a warm standby
     deployment. Use prefetch so reloads are fast. When a node is lost, re-run the request with the
     already-streamed text as a prefix.
   - **Choose nodes for split deployments with the availability predictor (C1).** Recovery is
     expensive on this path, which makes the predictor more valuable.
   - **Measure `-np` batching over RPC early.** It decides how many students one split deployment can
     serve.
2. **Phase 3 (research): build a minimal stage-server prototype.** Use one architecture family
   (Llama or Qwen), with **path B (chosen, D-09; see section 5)**, and treat the RPC system as the baseline. This gives the paper
   a clean C2 experiment: **request-level re-run (RPC) vs. stage replay vs. KV migration**, with
   notice windows of 0, 2, 5 and 10 s.
3. **Gate:** after Phase 2, if split deployments show a large share of failed or slow requests, or
   the traffic per token makes cross-building splits unusable, promote the custom engine to a
   production path.

This keeps the student service on a path that works today. It also keeps the larger research
contribution available, instead of spending the first months rebuilding an inference engine.

---

## 5. Path B in detail: the chosen custom-engine route

Decision D-09 (2026-11-18): if and when IdleMesh builds its own pipeline engine, it will **use
llama.cpp as a library with a small source patch**, not write an engine from scratch.

Rebuilding llama.cpp with different build flags is not enough. The source needs a patch that lets
it run only part of a model.

### 5.1 Reused from llama.cpp unchanged

| Need | llama.cpp feature |
|---|---|
| Model files | GGUF loading and every quantization format |
| Fast maths on every node type | ggml kernels and scheduler for CUDA, Metal, Vulkan and CPU (the costliest part to write) |
| Head-node text handling | Tokenizer, chat templates, sampling (`common/` and the llama-server code) |
| Several students at once | `llama_batch` with per-conversation sequence IDs and continuous batching |
| Hidden states as input | `batch.embd` already takes embeddings in place of tokens |
| Moving a conversation's memory (C2) | Per-sequence state save and restore (`llama_state_seq_get_data` / `llama_state_seq_set_data`). *Verify* that it can save only a layer range. |

### 5.2 What we add

```
                ┌──────────────── CI (D-07): build once per OS / backend, sign everything ─────────────┐
 llama.cpp ──▶  │ pinned upstream tag + idlemesh layer-range patch ──▶ libllama ──▶ idlemesh-stage   │ ──▶ installer
                └───────────────────────────────────────────────────────────────────────────────────┘

 router/head (coordinator side)                      node (supervised by idlemesh-agent, behind mTLS tunnel)
 ┌──────────────────────────────┐   hidden states   ┌──────────────────────────────────────┐
 │ tokenize · chat template     │ ────────────────▶ │ idlemesh-stage: layers [a, b)        │ ──▶ next stage …
 │ keep per-stage inputs        │ ◀──── logits ──── │ per-session KV cache · state export  │
 │ sample · stream to student   │                   └──────────────────────────────────────┘
 │ on loss: replay to replica   │
 └──────────────────────────────┘
```

1. **Layer-range patch** (`layer_start`, `layer_end` in model and context parameters):
   - load only the weights for `[a, b)`, and size the KV cache for those layers only;
   - loop only over `[a, b)` in the graph builder;
   - when `a > 0`, skip the token-embedding lookup and take the hidden state from `batch.embd`;
   - when `b < n_layer`, skip the output norm and head, and return the hidden state;
   - **patch only the architecture families needed first** (Llama and Qwen). In llama.cpp each
     architecture has its own graph-builder loop.
2. **`idlemesh-stage`**: a small C++ binary built on the patched library. It takes `(session, positions,
   hidden states)`, runs its layers, and returns hidden states (or logits on the last stage). It
   exports and imports a session's KV state for handoff.
   - The agent supervises it and puts it behind the mTLS tunnel, exactly as it does `rpc-server` today,
     so `worker.rs`, `tunnel.rs` and `prefetch.rs` carry over.
   - Running it as a separate process instead of linking it into the Rust agent keeps the agent small,
     and lets one agent binary serve CPU, CUDA, Vulkan and Metal builds of the stage.
3. **Router or head:** tokenizes and applies chat templates, using llama.cpp code. It sends hidden
   states down the chain, samples, streams to the student, and keeps each stage's inputs.
4. **Fault handling:**
   - replay a lost stage's inputs on a replica to rebuild its KV cache;
   - or, given departure notice, migrate the KV state using the save and restore functions.

   These are the C2 experiment arms: re-run vs. replay vs. migration, with 0, 2, 5 and 10 s of notice.

### 5.3 Rough effort (one person, research-grade)

| Piece | Estimate |
|---|---|
| Layer-range patch for 1–2 architecture families, plus an output-equivalence test against a single device | A few weeks |
| `idlemesh-stage` and the router | A few weeks |
| Replay to a replica, and KV migration | A few weeks |
| **Total** | **About 2–3 months**, against much longer for an engine written from scratch |

### 5.4 Risks and mitigations

| Risk | Mitigation |
|---|---|
| llama.cpp internals change often | Keep the patch small and isolated, pin the tag, and **propose the layer-range feature upstream** so the fork can be dropped |
| Correctness for each architecture | Greedy-output equivalence test: the chained run must match a single device, as in the inherited acceptance rule |
| Someone may have built this already | Review prima.cpp and other llama.cpp pipeline forks first (*verify* status and licence) |
| Installer impact (D-07) | None. CI already builds and signs llama.cpp, and `idlemesh-stage` is one more binary in the same package. |

### 5.5 Where it lives

- `engine/llama.cpp-patch/`: the patch series against the pinned upstream tag, plus a build script.
- `engine/stage/`: the `idlemesh-stage` source.
- `coordinator/idlemesh_coord/router/`: router and head logic.
- The `experiments/` harness compares the stage engine against the llama.cpp RPC baseline.
