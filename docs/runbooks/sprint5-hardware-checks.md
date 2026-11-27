# Sprint 5 checks that need the real machines

Everything in Sprint 5 that could run on the Windows dev PC alone is built and tested there (see
[changes.md](../../changes.md), "Sprint 5"). The checks below need two or more machines, or Wi-Fi, so they were
left for when the MacBook, the Linux server and the 8 laptops are available again. Do them in order: each one
uses what the previous one set up.

Setup is the same as for Sprint 4: one coordinator on the dev PC, SSH reverse tunnels for the agents' control
traffic, and worker tunnels from the coordinator to each agent on port 50100
([test-fleet.md](test-fleet.md)). The bandwidth test runs over the worker tunnels. Those go **coordinator →
agent** directly, not through SSH, so the test measures the real LAN or Wi-Fi path.

## 0. Before you start

- **Agents on every machine must be at this commit or later.** An older agent forwards the test's first byte to
  its worker, which closes the connection. The coordinator then logs `NET_PROBE_FAILED` with "has no built-in
  bandwidth test; update the agent".
- **Consent:** the owner's `diagnostics` consent must be on (it is in the `node.yaml` in test-fleet.md), or the
  sweep skips the node and `pilot.py network` says so.
- **Governor:** like the decode probe, the bandwidth test waits until the governor allows contribution for a
  reason other than user activity. Step away from (or lock) each machine for `idle_after_s` (600 s by default).
  The Linux server is headless and contributes from the start.

## 1. S5.2: the bandwidth test on real links

1. Start the coordinator and the agents (test-fleet.md). Wait until `python scripts/pilot.py devices` shows every
   worker running.
2. Within a minute or two of each node becoming idle, the sweep tests its link (reason `first`). Watch:
   `python scripts/pilot.py network` shows `link=NN Mbps` per node, `due=-` afterwards, and a reason if a node is
   blocked.
3. To test a node now (skips the off-hours rule, nothing else):
   `python scripts/pilot.py bandwidth-test "Linux server (env-colin)"`.
4. **What to record**, in the table under "Results" below: each machine, how it is connected (Ethernet or Wi-Fi,
   which access point), `up_mbps`, `down_mbps` and `mbps`. Repeat each 3 times, a few minutes apart, to see the
   spread on Wi-Fi.
5. **Sanity check (optional):** time an `scp` of a 200 MB file from the dev PC to the same machine. The tunnel
   figure should be the same order of magnitude. Do not install iperf3 on nodes (D-07). If a one-off lab
   comparison is wanted, ask Colin first; the `iperf3_lab` method exists for uploading such a figure.

**Pass:** every node gets a figure, Wi-Fi and Ethernet differ as expected, and nothing shows in
`NET_PROBE_FAILED` except while an agent is stopped.

## 2. S5.3 and S5.6: the prediction with measured links (the acceptance runs)

This re-runs the Sprint 4 three-machine test (S4.7) with link figures. It is the acceptance for both S5.3 ("the
Wi-Fi under-prediction from Rounds 5 and 7 re-checked") and S5.6 ("the three-machine run's speed prediction
within ±30%").

1. Complete step 1, so every remote stage has a stored figure. (If not, the coordinator times the link at load,
   4 MiB each way. That also works, and the stage then shows `link_source: tunnel_test` all the same.)
2. `python scripts/pilot.py deploy`. In the stage lines, check that every remote stage shows `link=NN Mbps` and
   `bw=... (decode_probe)`. The explanation's `latency_budget.links_unmeasured` must be empty.
3. `python scripts/pilot.py bench --n 10 --network "PC=wifi Linux=ethernet Mac=wifi"` (say how each machine is
   connected).
4. Compare `prediction_error_pct` with the S4.7 results: −42.8% on three machines and −34.5% on two (files
   `experiments/llamacpp-feasibility/results/pilot-3machine-20261120T*.json`).
5. Repeat with the two-machine layout (`--allow-subset` and one machine paused) and with the Mac as head on
   Wi-Fi (Rounds 5 and 7 in the legacy runbook).

**Pass:** each run within ±30%. Then mark S5.3 and S5.6 ✅ in Sprints.md, with the numbers.

**If it still under-predicts:** look at which term is short.

- The traffic term is `0.62 MB × 8 / link_mbps` per remote stage per token, fitted for Qwen2.5-0.5B
  (`BackendCost.bytes_per_token_per_remote`). For another model, measure it again. The tunnel forwarders count
  the bytes sent to each deployment's workers (`TunnelManager.bytes_to_devices`), but nothing records that count
  yet: read it before and after a timed request.
- If the traffic term is right but tokens are still slower, the link may be latency-bound, not bandwidth-bound.
  Compare the tunnel RTT (`rtt_source: tunnel`) with `round_trips_per_token` (1.02, fitted on loopback). The
  loopback check on 2026-11-27 (`experiments/llamacpp-feasibility/results/s5-link-loopback-20261127.md`) hints
  at more round trips per token over a slow path. Do not refit from that run: its relay is not faithful on
  Windows.

## 3. S5.5: the Grafana network view

1. Start Grafana ([deploy/grafana/README.md](../../deploy/grafana/README.md)). Open "IdleMesh overview" and
   scroll to the **Network (S5)** row.
2. **Network map** (Grafana's node graph panel): the coordinator in grey, each node coloured by its cluster,
   each node joined to the coordinator (RTT, and Mbps once tested), and cluster links between nodes.
   - This panel was built but **never rendered**, because Grafana was not run in Sprint 5. If it is empty, the
     node graph has not recognised the two frames. They come from the queries with refIds `nodes` and `edges`,
     and fields `id`/`title`/`mainstat` and `id`/`source`/`target`. Check the frame names in the panel
     inspector. Fix the panel in `deploy/grafana/build_dashboard.py`, never in the UI.
3. **Nodes and links** table, **Link bandwidth**, **RTT to the coordinator** and **Probe health** panels: check
   they match `pilot.py network`.
4. Stop one agent mid-test, or block port 50100 for a minute. `NET_PROBE_FAILED` should appear in Probe health
   within a minute (the coordinator flushes its error counts each minute).

## 4. S5.1 and S5.5 on real networks: clusters and degradation

1. **Clusters follow subnets (D-24):** machines on the same subnet within 5 ms (relayed: the sum of both RTTs
   to the coordinator) form one cluster; machines on another subnet never join it. Record the subnets
   `pilot.py network` shows.
2. **The Sprint 5 demo:** compare the network map with where the machines physically are. Tune
   `clusters.max_rtt_ms` in the site file if the clusters do not match rooms or buildings (D-22 thresholds are a
   first guess).
3. **Link degradation:** with a laptop on Wi-Fi, run `bandwidth-test`, then move it far from the access point
   (same network) and run it again. Below half the first figure, `LINK_DEGRADED` appears in Probe health and in
   the coordinator log with `metric=bandwidth_mbps`. A heartbeat RTT jump on the same network logs it with
   `metric=rtt_ms`.
4. **Network change:** move a laptop to another network (for example Ethernet → Wi-Fi on another subnet). The
   agent logs the new subnet within a minute; the coordinator tests the link again at once (reason
   `network_changed`), even in working hours.

## 5. The 8 laptops (later sprints)

- They bring batteries, sleep and Wi-Fi roaming. The test never runs on battery unless the owner allows battery
  contribution (the governor reports `power`).
- The weekly refresh runs in off-hours (`probes.off_hours_start`/`off_hours_end`, coordinator local time,
  weekends all day). Check on a laptop left on overnight that it is refreshed then and not during the day.
- Record their OS and GPU mix in [docs/long-lead.md](../long-lead.md) first.

## Not built yet: peer probes (D-25)

Node-to-node tests were left out of Sprint 5 on purpose:

- **Placement does not need them yet.** With llama.cpp RPC the head talks to every worker (hub and spoke). Every
  token crosses node ↔ head links only, and S5.2 measures exactly those. Clusters within a subnet use relayed
  figures, an upper bound.
- **They need two nodes that can reach each other**, and the campus network blocks server → Wi-Fi connections
  (test-fleet.md). That is the very case a test would have to cover.
- **They need trust between nodes:** today each agent's tunnel accepts only the coordinator's certificate.

When they are needed (stage engine S15–17, or clusters that must separate rooms on one subnet):

1. The coordinator gives each agent the pinned certificates of its likely neighbours (same subnet, a few at a
   time), in the heartbeat reply, only when a test is wanted (enrollment, network change, admin request).
2. The agent's tunnel accepts those pins for the bandwidth test only, never for the worker stream.
3. The agent dials the neighbour's tunnel, runs the same protocol (`agent/src/bulk.rs`) and the RPC
   DEVICE_COUNT timing, and uploads `net_edge` records (`method: tunnel_bulk`). The clusterer then prefers them
   over relayed figures, and `clusters.min_mbps` starts to apply.
4. Off by default, rate-limited, governor-gated like the coordinator's test.

## Results

| Date | Machine | Connection | up Mbps | down Mbps | mbps | Notes |
|---|---|---|---|---|---|---|
| 2026-11-27 | dev PC loopback, relay capped at 60 / 150 Mbps | loopback | — | — | 59.6 / 148.3 | the relay check, not a real link |

## When done

Remove the dev PC's temporary firewall rule (test-fleet.md):
`Remove-NetFirewallRule -DisplayName "IdleMesh coordinator (dev)"`.
