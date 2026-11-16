# Three-machine test runbook (Phase 1, Test A on real devices)

This test runs one model split across three real machines over your network. It checks that:

- devices enroll, report capacity, and stay under their owners' limits;
- the coordinator plans and loads a deployment that uses **every** device;
- model traffic to the remote devices goes only through the mutual-TLS tunnel;
- requests complete, and speed lines up with the planner's prediction;
- pausing, stopping, or capping a device drains the deployment safely, and it can be loaded again.

It is a cooperation test (spec 3.1, Test A). The test model (0.5B) fits on any one device; capacity
(Tests B and C) needs the larger model later. **Use synthetic prompts only**: llama.cpp RPC is a
research backend (spec 6.2).

## The machines

| Machine | Role | Runs | Inbound port | Guide |
|---|---|---|---|---|
| Mac Studio | Coordinator, and the Metal stage | coordinator, `llama-server`, agent (no worker) | 8765 (HTTPS, from HP and VM) | [mac_test_setup.md](mac_test_setup.md) |
| HP EliteBook (Windows) | CPU stage | agent, `ggml-rpc-server` on loopback | 50100 (mTLS, from the Mac only) | [windows_test_setup.md](windows_test_setup.md) |
| Ubuntu VM | CPU stage | agent, `ggml-rpc-server` on loopback | 50100 (mTLS, from the Mac only) | [ubuntu_test_setup.md](ubuntu_test_setup.md) |

```
      HTTPS 8765 (pinned certificate): enroll, heartbeats, weight download
  HP agent  ───────────────────────────────►  Mac coordinator  ◄──────────────  VM agent
  HP worker ◄═══ mTLS 50100 (model traffic) ═══  llama-server  ═══ mTLS 50100 ═══► VM worker
                                              + Metal stage
```

All three must use **llama.cpp v0.9.0** and the same DIFA commit (`git pull` on each).

## Order of work

1. **Mac:** sections 1 to 9 of the Mac guide: install, build, download the model, start the coordinator,
   and create the group.
2. **HP and VM**, in any order or in parallel: install, build, and network checks, up to the "Enroll"
   section of each guide.
3. **Enroll and start agents.** For each device, run `pilot.py code` on the Mac, then enroll on the device
   within 10 minutes with `https://MAC_IP:8765` and the `--coordinator-fingerprint` it prints, then start
   its agent. **Devices enrolled before the HTTPS change must enroll again:** delete their `agent.json`
   first. Do the Mac first (sections 10 and 11), then the HP, then the
   VM.
4. Continue below.

The commands below run **on the Mac**, from the checkout folder, unless they say otherwise. To keep them
short, define this alias in your Mac terminal:

```bash
alias pilot="~/.venvs/difa/bin/python scripts/pilot.py"
```

## Step 1. All three devices ready

```bash
pilot devices
```

Expected (budgets depend on your caps):

```
Mac Studio       ready        worker=-        budget=  64.0 GB  power=ac       macos/aarch64
HP EliteBook     ready        worker=running  budget=   8.0 GB  power=unknown  windows/x86_64
Ubuntu VM        ready        worker=running  budget=   6.0 GB  power=ac       linux/x86_64
```

If you enrolled a device twice, its old entry shows `unavailable`. The planner ignores it.

## Step 2. Deploy across all three

```bash
pilot deploy
```

This plans a placement that must use every ready device, sends each remote device its download
manifest, waits until each reports its share verified, then loads. Expected output, roughly:

```
deployment dep_...: preparing
  Ubuntu VM        layers   0-0   head=no  ...  endpoint=VM_IP:50100   rtt=... ms
  HP EliteBook     layers   1-1   head=no  ...  endpoint=HP_IP:50100   rtt=... ms
  Mac Studio       layers   2-23  head=yes ...  endpoint=-             rtt=... ms
  predicted: ... ms/token (... tokens/s), network ... ms, slowest stage ...
preparing | Ubuntu VM: ready 100% | HP EliteBook: ready 100%
ready
ready after ... s
as loaded:
  Ubuntu VM        ...  rtt=... ms (tunnel), planned with ... ms
  HP EliteBook     ...  rtt=... ms (tunnel), planned with ... ms
  Mac Studio       ...  rtt=... ms (heartbeat)
  planned:   ... ms/token (... tokens/s) from heartbeat round trips
  predicted: ... ms/token (... tokens/s), ... (round trips timed through the tunnels at load)
```

The first prediction uses the round trip each agent reports with its heartbeat. That figure includes HTTP
and the coordinator's request handling, so it reads high. Just before loading, the coordinator times 16
llama.cpp RPC round trips through each tunnel, then predicts again. The `as loaded` prediction is the
one to compare with the bench. The coordinator reuses those round trips for 15 minutes, so a second
`pilot deploy` in that window plans with them from the start (`rtt=... ms (tunnel)` in the first
listing).

Then confirm the remote stages run through the tunnel:

```bash
pilot status
```

Each remote stage shows `transport=mtls`.

**What to expect from the plan:**
- The planner runs the fastest device (the Mac) last, so it holds the output head.
- Slower devices get as few layers as possible, often one each, because `require_all` forces them in.
- With this small model, every tensor on the HP and the VM is under llama.cpp's 10 MiB cache threshold.
  So their prefetch has nothing to download and shows 100% at once. Their layers (about 15 MiB each)
  travel over the tunnel at load. Prefetch only pays off with larger models.

## Step 3. Requests and timing

One request:

```bash
pilot ask "Name three planets."
```

Then a timed run of 20 requests. The results are saved to `benchmarks/results/pilot-3machine-<time>.json`.

Use `--network` to record how each machine is connected; it is saved with the results.

```bash
pilot bench --n 20 --max-tokens 64 --network "Mac=ethernet HP=wifi VM=wifi"
```

The summary at the end checks the prediction for you:

```
  "fit_ms_per_token": ...,        <- measured decode time per token (slope across requests)
  "fit_fixed_s": ...,             <- prompt processing and polling per request
  "predicted_ms_per_token": ...,  <- the "as loaded" prediction
  "rtt_source": "heartbeat+tunnel",
  "prediction_error_pct": ...,
  "within_30pct": true            <- spec 7.2 target
```

**Record:**
- the `planned` and `predicted` ms/token from step 2;
- the bench summary above;
- how each machine is connected (Wi-Fi, Ethernet, VPN), and whether they share a subnet.

From Phase 1 on one Mac, expect speed to be set mostly by round trips: about one round trip per token
for each remote device, plus about 0.62 MB per token sent to each remote device. On a home network
(1 to 5 ms) expect tens of tokens per second. Over Wi-Fi or a VPN, expect less.

The bench's time includes prompt processing and polling, so it reads a little slower than pure decode
speed.

## Step 4. Owner controls and failure handling

Run each check, then load again with `pilot deploy` before the next.

| # | Action | Expected |
|---|---|---|
| 4a | Pause the VM: `pilot policy "Ubuntu VM" --pause` | Within a few seconds the VM agent prints `worker stopped`, and `pilot status` shows `unavailable`. `pilot ask "hi"` returns `503 MODEL_UNAVAILABLE`. |
| 4b | Resume: `pilot policy "Ubuntu VM" --resume`, then `pilot deploy` | The worker restarts and the deployment becomes `ready`. |
| 4c | Stop the HP agent with Ctrl-C | The agent prints `agent stopping: worker halted`. Within about 15 seconds the deployment becomes `unavailable`. Restart the agent with the same `run` command, then `pilot deploy`. |
| 4d | Lower the HP's cap below what its worker holds: `pilot policy "HP EliteBook" --cap-gb 0.01` | The HP agent prints `worker killed: memory cap exceeded`, and the deployment drains. The coordinator's adapter log for that deployment ends with `difa: drain (...)`, not a crash trace. |
| 4e | Raise it back: `pilot policy "HP EliteBook" --cap-gb 8`, then `pilot deploy` | The worker restarts and the deployment is `ready` again. |
| 4f | Stranger check: run `nc -vz HP_IP 50100` and `nc -vz VM_IP 50100` | The ports are reachable, but the agents log `tunnel: rejected`. Nothing reaches the worker without the coordinator's certificate. |

## Step 5. Optional: compare without the tunnel's protection

Don't do this unless the network is isolated. It exists to measure the tunnel's cost on real links.

The agents can expose the worker directly: `--worker-host <device IP> --insecure-plaintext-worker`,
with no `--tunnel-*` flags. The coordinator adapter refuses non-loopback plaintext workers, so this
comparison isn't supported end to end yet. Measured on one Mac, the tunnel costs about 1 ms per token.

## What to send back

- the `benchmarks/results/pilot-3machine-*.json` files from step 3;
- the full output of `pilot deploy` and `pilot status` from step 2;
- the agent logs, if anything failed. To capture them, start the agents with output saved to a file:
  - on Ubuntu, append `2>&1 | tee ~/difa/agent.log` to the `run` command;
  - on Windows, append `*>&1 | Tee-Object -FilePath "$env:USERPROFILE\difa\agent.log"`;
- the coordinator terminal's output, if a deployment failed;
- the network setup: Wi-Fi or Ethernet for each machine, the VM's host and network mode, and any VPN.

## Common errors

| Error from `pilot deploy` | Meaning and fix |
|---|---|
| `PLACEMENT_INFEASIBLE` | A device isn't ready, is paused, or is incompatible. Run `pilot devices`. `incompatible` means a different `--backend-digest`, so enroll again with `llama.cpp-v0.9.0`. |
| `NO_ENDPOINT` | A remote device's worker isn't running. Check its agent window. |
| `TUNNEL_FAILED: cannot reach the worker tunnel` | Check the firewall rule on that device, and that `--tunnel-advertise` uses an address the Mac can reach (`nc -vz`). |
| `TUNNEL_FAILED: TLS handshake ... failed` | The device pinned a different coordinator certificate, for example after `~/.local/share/difa/coordinator-tunnel.*` was deleted. Delete the device's `agent.json` and enroll again. |
| `LICENSE_NOT_ACCEPTED` | Add `--accept-license apache-2.0` to that device's `run` command. |
| `LOAD_FAILED` | Usually a llama.cpp version mismatch, or a worker crash. Check that every machine built tag v0.9.0, and look at the coordinator's adapter log in `~/.cache/difa-phase1/adapter-logs/`. |
| `rtt measurement failed` after a stage in `as loaded` | The tunnel was reachable but the worker did not answer RPC commands in time, often because it was still busy with a previous client. The deployment still loads, using the heartbeat round trip for that stage. Run `pilot deploy` again. |
| `PARTITION_MISMATCH` | The weights llama.cpp loaded on a device differ from the plan. Send the adapter log. This is a finding, not a setup error. |
| `Session expired` | Run `pilot login`. |
| `pilot` fails with a certificate or connection error | The saved session points at `http://`. Delete `~/.local/share/difa/pilot-session.json` and run `pilot setup` against the `--fresh` coordinator. |
| A device agent prints `device authorization revoked` and exits | The coordinator was restarted with `--fresh`, which forgets every device. Delete that device's `agent.json` and enroll again. |

## Ending the test

Stop each agent with Ctrl-C, then stop the coordinator with Ctrl-C. Remove the firewall rules and the
device state files as described at the end of each device guide. A `--fresh` coordinator start
invalidates all enrollments, so every device must enroll again next time.
