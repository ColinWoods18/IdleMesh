# S5.2 and S5.3 on one machine: a capped loopback link (2026-11-27)

**What ran.** The Windows dev PC (CPU only, llama.cpp v0.9.0, Qwen2.5-0.5B Q8). One agent ran its worker
(4 threads) behind the mutual-TLS tunnel. The coordinator reached the tunnel through
`netem_relay.py --rtt-ms 2 --mbps <cap>`, and placement put the whole model on that worker. The deployment had
no stored link figure, so the coordinator timed the link at load (4 MiB each way). Six requests alternated 16 and
48 output tokens. `pilot.prediction_check` fitted the per-token slope. The harness is not committed: it wraps
`coordinator/tests/integration/live.py` the same way `test_tunnel.py` does.

| Relay cap | Tested link | Tunnel RTT | Planned, link left out | Predicted with the link | Measured slope | Error |
|---|---|---|---|---|---|---|
| 60 Mbps | 59.6 Mbps | 29.6 ms | 19.0 ms/token | 127.3 ms/token | 165.6 ms/token | −23.1% |
| 150 Mbps | 148.3 Mbps | 29.1 ms | 19.0 ms/token | 77.1 ms/token | 150.5 ms/token | −48.8% |

**What it shows.**

- The bandwidth test reads a capped link correctly: within 1% of 60 and 150 Mbps. `test_bandwidth_agent.py`
  checks 50 and 200 Mbps within 15% on every run.
- Without the link term, placement predicts 19 ms/token on a link where tokens take 150 to 166 ms. With the
  tested figure, the prediction moves most of the way: −23% at 60 Mbps.

**What it does not show.** The relay is not a faithful link on Windows. `--rtt-ms 2` gave a 29 ms tunnel RTT,
because asyncio timers on Windows fire at about 15.6 ms resolution, and the measured slope barely changes
between the two caps (166 against 150 ms). Most of the remaining gap is relay delay on each round trip, not
bandwidth. These numbers do not settle the S5.3 or S5.6 acceptance: check them on the real machines
(docs/runbooks/sprint5-hardware-checks.md).
