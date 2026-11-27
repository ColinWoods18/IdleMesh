"""Filter-and-score placement pipeline with a per-token latency budget (spec FR 31, Section 7.2).

Filters remove ineligible devices with a recorded reason. Scorers rank the rest. The planner then
uses the fewest, best-scored devices whose per-device ledgers satisfy W + K + R + X <= B, assigning
contiguous layer ranges. Every decision returns a content-free explanation that can be replayed.
"""
from dataclasses import asdict, dataclass, field

PIPELINE_VERSION = "placement-pipeline/1"
GB = 10**9


@dataclass(frozen=True)
class DeviceProfile:
    device_id: str
    name: str
    os: str
    arch: str
    backend_digest: str
    formats: tuple[str, ...]
    budget_bytes: int            # min(owner cap, measured allocatable) before headroom
    mem_bandwidth_gbps: float    # measured memory bandwidth, GB/s
    rtt_ms: float                # measured round trip from the coordinator
    power: str = "ac"            # "ac", "battery", or "unknown"
    allow_battery: bool = False
    ready: bool = True
    authorized: bool = True
    paused: bool = False
    stability: float = 1.0       # fraction of recent heartbeats received
    endpoint: str | None = None  # inference worker address reported by the agent, if it runs one
    link_mbps: float | None = None   # bandwidth of the link to the coordinator (the head), from its tunnel test
    link_source: str | None = None   # "tunnel_test" when link_mbps was timed through the worker tunnel (S5.2)
    bandwidth_source: str = "reported"   # "decode_probe" when derived from timed decoding (FR 06)
    not_ready_reason: str | None = None
    node_state: str | None = None    # idle, active, sleeping, draining or serving, as the agent last reported (S2.5)
    rtt_source: str = "heartbeat"        # "tunnel" when timed through the worker tunnel by the coordinator
    # S6.3: the predicted chance that the node stays usable for the predictor's horizon (None: no predictor), and
    # the governor's current level (D-12, D-20), which sets the worker's share of the machine.
    availability: float | None = None
    availability_basis: str | None = None   # "hour", "node" or "fleet" (predictor.model.Estimate.basis)
    contribution_level: float | None = None
    contribution_reason: str | None = None

    @property
    def effective_bandwidth_gbps(self) -> float:
        """Measured bandwidth scaled by the contribution level: a node at level 0.5 runs its worker on about half
        the threads (D-20), so it decodes about half as fast. Memory is not scaled (D-20)."""
        level = 1.0 if self.contribution_level is None else self.contribution_level
        return self.mem_bandwidth_gbps * level


@dataclass(frozen=True)
class ModelSpec:
    name: str
    revision: str
    fmt: str
    model_class: str             # "dense" or "moe"
    total_params_b: float
    active_params_b: float
    layers: int
    bytes_per_layer: int
    kv_bytes_per_token_per_layer: int
    runtime_overhead_bytes: int  # R per participating device
    max_context_tokens: int
    synthetic: bool = True       # fixture, not a real model
    head_bytes: int = 0          # output layer; llama.cpp places it on the last device in execution order

    def kv_bytes_per_layer(self, sequences: int = 1) -> int:
        return self.kv_bytes_per_token_per_layer * self.max_context_tokens * sequences


@dataclass(frozen=True)
class BackendCost:
    """Fixed per-token costs of a backend's transport, fitted from measurements (spec 7.2).

    Defaults: llama.cpp v0.9.0 RPC, fitted on loopback calibration topologies and checked on held-out
    topologies within 13 percent (experiments/llamacpp-feasibility/results/phase1-latency-20261105T225814Z.json). The per-token
    traffic figure was measured for Qwen2.5-0.5B and grows with the model's graph; remeasure per model.
    """
    name: str = "llama.cpp-v0.9.0-rpc"
    first_remote_ms: float = 3.58
    extra_remote_ms: float = 0.74
    round_trips_per_token: float = 1.02
    bytes_per_token_per_remote: float = 0.62e6


LLAMACPP_RPC_V090 = BackendCost()


@dataclass(frozen=True)
class PlacementPolicy:
    backend_digest: str
    headroom_fraction: float = 0.10   # X: transfers, duplication, and uncertainty
    active_sequences: int = 1          # G
    require_all: bool = False          # Test A cooperation topology
    local_device: str | None = None    # device name the coordinator runs locally; others are remote
    backend_cost: BackendCost = LLAMACPP_RPC_V090
    weights: dict = field(default_factory=lambda: {"bandwidth": 0.35, "latency": 0.25, "availability": 0.2,
                                                   "power": 0.1, "stability": 0.1})


def _f_authorized(d, m, p): return None if d.authorized else "device authorization revoked or expired"
def _f_ready(d, m, p):
    return None if d.ready else (d.not_ready_reason or "device not ready (no valid heartbeat)")
def _f_paused(d, m, p): return "paused by owner" if d.paused else None
def _f_power(d, m, p):
    if d.power == "battery" and not d.allow_battery:
        return "on battery and owner allows plugged-in contribution only"
    if d.power == "unknown" and not d.allow_battery:
        return "power status unknown; battery contribution paused by default"
    return None
def _f_contribution(d, m, p):
    if d.contribution_level is not None and d.contribution_level <= 0:
        return f"contribution level 0 ({d.contribution_reason or 'governor'})"
    return None
def _f_digest(d, m, p):
    return None if d.backend_digest == p.backend_digest else f"backend digest {d.backend_digest} differs from {p.backend_digest}"
def _f_format(d, m, p): return None if m.fmt in d.formats else f"model format {m.fmt} unsupported"
def _f_budget(d, m, p):
    return None if _layer_capacity(d, m, p) >= 1 else "budget too small for one layer after overhead and headroom"


FILTERS = [("authorized", _f_authorized), ("ready", _f_ready), ("paused", _f_paused), ("power", _f_power),
           ("contribution", _f_contribution), ("backend_digest", _f_digest), ("format", _f_format), ("budget", _f_budget)]


def _usable_bytes(d: DeviceProfile, m: ModelSpec, p: PlacementPolicy) -> int:
    return int(d.budget_bytes * (1 - p.headroom_fraction)) - m.runtime_overhead_bytes


def _per_layer_bytes(m: ModelSpec, p: PlacementPolicy) -> int:
    return m.bytes_per_layer + m.kv_bytes_per_layer(p.active_sequences)


def _layer_capacity(d: DeviceProfile, m: ModelSpec, p: PlacementPolicy, head: bool = False) -> int:
    usable = _usable_bytes(d, m, p) - (m.head_bytes if head else 0)
    return max(0, usable // _per_layer_bytes(m, p))


def stage_network_ms(rtt_ms: float, link_mbps: float | None, cost: "BackendCost") -> float:
    """Per-token network time of one remote stage: its round trips, plus its per-token traffic over the link to
    the head when that link's bandwidth is known (S5.3)."""
    traffic = cost.bytes_per_token_per_remote * 8 / (link_mbps * 1e6) * 1000 if link_mbps else 0.0
    return cost.round_trips_per_token * rtt_ms + traffic


def _score(d: DeviceProfile, peers: list[DeviceProfile], p: "PlacementPolicy") -> dict:
    w = p.weights
    max_bw = max(x.effective_bandwidth_gbps for x in peers) or 1.0
    parts = {
        "bandwidth": d.effective_bandwidth_gbps / max_bw,  # capacity weighted by contribution level (S6.3)
        # A slow link costs every token, as a long round trip does (S5.3).
        "latency": 1.0 / (1.0 + stage_network_ms(d.rtt_ms, d.link_mbps, p.backend_cost) / 50.0),
        # Likely to stay idle for the predictor's horizon (S6.3); without a predictor every node counts as 1.
        "availability": 1.0 if d.availability is None else d.availability,
        "power": {"ac": 1.0, "battery": 0.3}.get(d.power, 0.0),
        "stability": d.stability,
    }
    parts = {k: round(v, 4) for k, v in parts.items()}
    return {"total": round(sum(w.get(k, 0.0) * v for k, v in parts.items()), 4), "components": parts,
            "inputs": {"contribution_level": d.contribution_level, "contribution_reason": d.contribution_reason,
                       "effective_bandwidth_gbps": round(d.effective_bandwidth_gbps, 2),
                       "availability_basis": d.availability_basis}}


def plan(devices: list[DeviceProfile], model: ModelSpec, policy: PlacementPolicy) -> dict:
    explanation: dict = {
        "pipeline_version": PIPELINE_VERSION,
        "model": {"name": model.name, "revision": model.revision, "class": model.model_class,
                  "layers": model.layers, "weights_bytes": model.layers * model.bytes_per_layer,
                  "synthetic": model.synthetic},
        "policy": asdict(policy),
        "filtered_out": [],
        "scores": [],
        "single_device_fit": [],
        "feasible": False,
    }
    eligible = []
    for d in devices:
        reason = next(((n, r) for n, f in FILTERS if (r := f(d, model, policy))), None)
        if reason:
            explanation["filtered_out"].append({"device_id": d.device_id, "filter": reason[0], "reason": reason[1]})
        else:
            eligible.append(d)

    scored = sorted(((d, _score(d, eligible, policy)) for d in eligible),
                    key=lambda x: (-x[1]["total"], x[0].device_id))
    explanation["scores"] = [{"device_id": d.device_id, **s} for d, s in scored]
    explanation["single_device_fit"] = [d.device_id for d, _ in scored
                                        if _layer_capacity(d, model, policy, head=True) >= model.layers]
    if not scored:
        explanation["reason"] = "no eligible devices"
        return explanation

    ranked = [d for d, _ in scored]
    candidate_sets = [ranked] if policy.require_all else [ranked[:k] for k in range(1, len(ranked) + 1)]
    chosen = assignment = None
    for subset in candidate_sets:
        assignment = _assign(subset, model, policy)
        if assignment:
            chosen = subset
            break
    if not chosen:
        capacity = sum(_layer_capacity(d, model, policy, head=(i == 0)) for i, d in enumerate(ranked))
        explanation["reason"] = (f"insufficient combined capacity: {capacity} of {model.layers} layers fit "
                                 f"across {len(ranked)} eligible devices")
        return explanation

    # Execution order puts the best-ranked device last so it also holds the output head, which the
    # llama.cpp backend assigns to the final device (observed in Phase 1, 2026-11-05).
    stages, start = [], 0
    per_layer = _per_layer_bytes(model, policy)
    order = list(zip(chosen, assignment))[::-1]
    for i, (d, n) in enumerate(order):
        holds_head = i == len(order) - 1
        weights = n * model.bytes_per_layer + (model.head_bytes if holds_head else 0)
        kv = n * model.kv_bytes_per_layer(policy.active_sequences)
        need = weights + kv + model.runtime_overhead_bytes
        stages.append({
            "device_id": d.device_id, "name": d.name, "layers": [start, start + n], "holds_output_head": holds_head,
            "endpoint": d.endpoint, "bandwidth_gbps": d.mem_bandwidth_gbps, "bandwidth_source": d.bandwidth_source,
            "weights_bytes": weights, "kv_bytes": kv, "runtime_bytes": model.runtime_overhead_bytes,
            "headroom_bytes": d.budget_bytes - int(d.budget_bytes * (1 - policy.headroom_fraction)),
            "budget_bytes": d.budget_bytes, "margin_bytes": _usable_bytes(d, model, policy) + model.runtime_overhead_bytes - need,
            "contribution_level": d.contribution_level, "effective_bandwidth_gbps": round(d.effective_bandwidth_gbps, 2),
            "availability": d.availability, "availability_basis": d.availability_basis,
            "est_compute_ms": round(weights / (d.effective_bandwidth_gbps * GB) * 1000, 2),
            "rtt_ms": d.rtt_ms, "rtt_source": d.rtt_source, "link_mbps": d.link_mbps, "link_source": d.link_source,
        })
        start += n
    assert start == model.layers and all(s["margin_bytes"] >= 0 for s in stages) and per_layer > 0

    local = policy.local_device or stages[-1]["name"]
    explanation.update({
        "feasible": True,
        "stages": stages,
        "reservation_bytes": {s["device_id"]: s["kv_bytes"] for s in stages},
        "latency_budget": latency_budget(stages, local, policy.backend_cost),
        "capacity_basis": ("fits a single eligible device budget" if explanation["single_device_fit"]
                           else "exceeds every eligible device budget (owner-budget capacity, Test C)"),
    })
    return explanation


def latency_budget(stages: list[dict], local: str, cost: BackendCost) -> dict:
    """Per-token latency (spec 7.2): compute from weights and effective bandwidth, plus the backend's fixed
    transport overhead, plus round trips and per-token traffic for every remote stage. Sets each stage's
    `est_network_ms` from its `rtt_ms` and optional `link_mbps`."""
    remote = [s for s in stages if s["name"] != local]
    for s in stages:
        s["est_network_ms"] = 0.0
    for s in remote:
        s["est_network_ms"] = round(stage_network_ms(s["rtt_ms"], s.get("link_mbps"), cost), 2)
    compute_ms = sum(s["est_compute_ms"] for s in stages)
    network_ms = sum(s["est_network_ms"] for s in stages)
    overhead_ms = (cost.first_remote_ms + cost.extra_remote_ms * (len(remote) - 1)) if remote else 0.0
    token_ms = compute_ms + network_ms + overhead_ms
    sources = sorted({s.get("rtt_source", "heartbeat") for s in remote})
    unmeasured = [s["device_id"] for s in remote if not s.get("link_mbps")]
    return {
        "compute_ms": round(compute_ms, 2), "network_ms": round(network_ms, 2),
        "backend_overhead_ms": round(overhead_ms, 2), "remote_stages": len(remote),
        "per_token_ms": round(token_ms, 2), "tokens_per_s": round(1000 / token_ms, 2) if token_ms else None,
        "dominant_stage": max(stages, key=lambda s: s["est_compute_ms"] + s["est_network_ms"])["device_id"],
        "rtt_source": "+".join(sources) if sources else None,
        "links_unmeasured": unmeasured,
        "backend_cost": asdict(cost),
        "basis": ("weights/effective bandwidth + fitted backend overhead + round trips and per-token "
                  "traffic per remote stage; link bandwidth term omitted where unmeasured"),
    }


def reestimate_with_path_rtt(explanation: dict, measured: dict[str, dict], cost: BackendCost | None = None,
                             links: dict[str, dict] | None = None) -> dict:
    """Replace heartbeat RTTs with round trips timed through the worker tunnel, fill link bandwidths timed by the
    tunnel's bandwidth test (`links`, S5.3), and recompute the budget.

    Heartbeat RTT includes HTTP and coordinator request handling, so it overstates the network path a
    token takes (Phase 1 Round 4: 15-18 ms by heartbeat against 9 ms by ping to the same VM). The
    prediction made at planning time is kept as `planned_latency_budget` for comparison.
    """
    if not measured and not links:
        return explanation
    old = explanation["latency_budget"]
    cost = cost or BackendCost(**old["backend_cost"])
    explanation.setdefault("planned_latency_budget", {k: v for k, v in old.items() if k != "backend_cost"})
    for s in explanation["stages"]:
        m = measured.get(s["device_id"])
        if m:
            s.setdefault("planned_rtt_ms", s["rtt_ms"])
            s["rtt_ms"], s["rtt_source"], s["rtt_measurement"] = m["mean_ms"], "tunnel", m
        b = (links or {}).get(s["device_id"])
        if b:
            s.setdefault("planned_link_mbps", s.get("link_mbps"))
            s["link_mbps"], s["link_source"], s["link_measurement"] = b["mbps"], "tunnel_test", b
    local = explanation["policy"].get("local_device") or explanation["stages"][-1]["name"]
    explanation["latency_budget"] = latency_budget(explanation["stages"], local, cost)
    return explanation


def _assign(subset: list[DeviceProfile], model: ModelSpec, policy: PlacementPolicy) -> list[int] | None:
    caps = [_layer_capacity(d, model, policy, head=(i == 0)) for i, d in enumerate(subset)]
    if sum(caps) < model.layers or any(c < 1 for c in caps):
        return None
    remaining = model.layers - len(subset)  # every chosen device gets at least one layer
    counts = [1] * len(subset)
    for i, cap in enumerate(caps):
        take = min(cap - 1, remaining)
        counts[i] += take
        remaining -= take
    return counts if remaining == 0 else None
