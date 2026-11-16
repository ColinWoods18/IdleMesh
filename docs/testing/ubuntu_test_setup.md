# Ubuntu test setup (VM contributing device)

The Ubuntu VM contributes CPU and memory to the test. It runs:

- **`difa-agent`**, which enrolls the VM, enforces your memory cap, downloads its share of the model, and
  runs the encrypted tunnel;
- **`ggml-rpc-server`** (llama.cpp's worker), which the agent starts, stops, and keeps on the VM's own
  loopback address.

Set up the Mac first ([mac_test_setup.md](mac_test_setup.md)) so the coordinator is running before you
enroll. The whole test sequence is in [three_machine_test.md](three_machine_test.md). These steps also
work on a physical Ubuntu or Debian machine.

## What you need

| Item | Recommended | Check with |
|---|---|---|
| Ubuntu | 22.04 or 24.04, x86_64 or arm64 | `lsb_release -a` |
| vCPUs | 4 or more | `nproc` |
| Memory | 8 GB or more | `free -g` |
| Free disk | about 6 GB | `df -h ~` |
| Network | the Mac must be able to reach the VM's IP address (see below) | |

### VM networking: the Mac must be able to reach the VM

The coordinator connects **to** the VM's tunnel port, so the VM needs an address the Mac can reach:

- **Bridged networking (recommended).** The VM gets its own address on your LAN. In VirtualBox, VMware,
  Parallels, or UTM, set the VM's network adapter to *Bridged*.
- **VM running on the Mac itself.** Shared or NAT networking in UTM, Parallels, or VMware Fusion usually
  works too, because the host can reach the VM's private address (for example `192.168.64.x`).
- **NAT on another host.** If the VM sits behind NAT on another computer (for example on the HP), the
  Mac can't reach it. Switch to bridged networking. If you can't, add a port forward on that host from
  port 50100 to the VM's port 50100, and later use the host's address in `--tunnel-advertise`.

## 1. Install supporting software

```bash
sudo apt update && sudo apt install -y build-essential cmake git curl pkg-config tmux netcat-openbsd
```

Install Rust with the official installer (`-y` accepts the defaults and adds Cargo to your profile):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
```

```bash
source "$HOME/.cargo/env" && cargo --version && cmake --version && git --version
```

All three print a version. CMake must be 3.20 or later; Ubuntu 22.04 and 24.04 both ship newer.

## 2. Get the code

```bash
mkdir -p ~/difa && git clone https://github.com/TheCodeheadMT/I4A.git ~/difa/I4A
```

```bash
git clone --depth 1 --branch v0.9.0 https://github.com/ggml-org/llama.cpp.git ~/difa/llama.cpp
```

**The llama.cpp tag must be v0.9.0 on every machine.** A different version fails at load time.

## 3. Build the llama.cpp worker (CPU and RPC)

```bash
cd ~/difa/llama.cpp && cmake -B build -DGGML_RPC=ON -DLLAMA_CURL=OFF && cmake --build build -j"$(nproc)" --target ggml-rpc-server llama-bench
```

Check:

```bash
~/difa/llama.cpp/build/bin/ggml-rpc-server --help | head -5
```

It prints the worker's options, including `-c, --cache`.

## 4. Build the device agent

```bash
cd ~/difa/I4A/agents/device && cargo build --release && ./target/release/difa-agent --version
```

It prints `difa-agent 0.1.0`.

## 5. Network: addresses, reachability, firewall

Find the VM's address:

```bash
hostname -I
```

Write the LAN-facing one down as **VM_IP**. Then check that the Mac's coordinator is reachable,
replacing `MAC_IP`:

```bash
curl -sk -o /dev/null -w "%{http_code}\n" https://MAC_IP:8765/docs
```

It prints `200`. (`-k` skips certificate checks for this reachability test only; the agent checks the
coordinator's fingerprint itself.) Anything else means:
- the coordinator isn't running;
- the Mac's firewall blocked Python; or
- the VM's network mode doesn't reach the LAN.

If the Ubuntu firewall is active (`sudo ufw status` shows `Status: active`), allow the Mac, and only the
Mac, to reach the tunnel port:

```bash
sudo ufw allow from MAC_IP to any port 50100 proto tcp
```

Only port 50100 (the mutual-TLS tunnel) is opened. The llama.cpp worker listens on 127.0.0.1 and is
never reachable from the network.

## 6. Enroll the VM

On the **Mac**, get a one-time code (valid for 10 minutes). It also prints the coordinator fingerprint:

```bash
~/.venvs/difa/bin/python scripts/pilot.py code
```

On the **VM**, replacing `MAC_IP`, `FINGERPRINT` (the 16 digits after `--coordinator-fingerprint`) and
`CODE`. Keep `--memory-cap-gb` at least 2 GB below the VM's
memory.

```bash
~/difa/I4A/agents/device/target/release/difa-agent enroll --coordinator https://MAC_IP:8765 --coordinator-fingerprint FINGERPRINT --code CODE --name "Ubuntu VM" --memory-cap-gb 6 --backend-digest llama.cpp-v0.9.0 --state ~/difa/agent.json
```

Expected output: `enrolled device dev_... as 'Ubuntu VM'; state saved to /home/.../difa/agent.json`. The
state file holds the VM's device key and token, and the agent restricts it to your user (mode 600).

A VM has no battery, so it reports mains power and needs no `--allow-battery`.

## 7. Run the agent

Run the agent inside `tmux` so it survives an SSH disconnect. Detach with Ctrl-B then D; reattach with
`tmux attach -t difa`.

```bash
tmux new -s difa
```

Inside tmux, replacing `VM_IP`:

```bash
~/difa/I4A/agents/device/target/release/difa-agent run --state ~/difa/agent.json --worker-bin ~/difa/llama.cpp/build/bin/ggml-rpc-server --worker-threads "$(nproc)" --tunnel-listen 0.0.0.0:50100 --tunnel-advertise VM_IP:50100 --accept-license apache-2.0 --cache-dir ~/difa/cache
```

What the options do:
- `--tunnel-listen` and `--tunnel-advertise` set up the encrypted tunnel and give the coordinator the
  address to reach it on. With NAT and a port forward, use the forwarding host's address here.
- `--accept-license apache-2.0` is your consent, as the device owner, to download the test model's
  weights.
- `--cache-dir` is where this device keeps its share of the model.

Expected output:

```
tunnel: mutual TLS on 0.0.0.0:50100 (advertised VM_IP:50100); worker on 127.0.0.1:50052
heartbeat ok: paused=false power=ac cap=6.00GB rtt=...ms
worker started on 127.0.0.1:50052
```

## 8. Check from the Mac

```bash
~/.venvs/difa/bin/python scripts/pilot.py devices
```

The VM shows `ready` with `worker=running`. Check that the Mac can reach the tunnel port:

```bash
nc -vz VM_IP 50100
```

It prints `succeeded`. Because `nc` has no certificate, the VM agent logs `tunnel: rejected ...`. That is
the tunnel refusing a stranger, as it should. If `nc` can't connect, revisit the networking section and
the firewall rule.

## Stopping and cleaning up

- **Stop:** press Ctrl-C in the agent's tmux window. It prints `agent stopping: worker halted`, and no
  worker is left running.
- **Remove the firewall rule** if you added it:

  ```bash
  sudo ufw delete allow from MAC_IP to any port 50100 proto tcp
  ```

- **Remove the device's key, token and cached model share:**

  ```bash
  rm -rf ~/difa/agent.json ~/difa/cache
  ```

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `cannot reach coordinator` | Run the `curl ... /docs` check. Check the VM's network mode and the Mac's firewall. |
| `certificate does not match the fingerprint (the code was not sent)` | The fingerprint was mistyped, or something other than your coordinator answered at that address. Check the digits against `pilot.py code` on the Mac. The code is still valid. |
| `refusing plain HTTP to a coordinator on the network` | Use `https://MAC_IP:8765` with `--coordinator-fingerprint`. An `agent.json` enrolled with `http://` must be deleted and enrolled again. |
| `enrollment failed (404 ...)` | The code expired (10 minutes) or was already used. Get a new one with `pilot.py code`. |
| `nc` from the Mac times out | The VM is behind NAT, or ufw is blocking. Use bridged networking or a port forward, and check `sudo ufw status`. |
| Deployment fails with `TUNNEL_FAILED` | `--tunnel-advertise` has an address the Mac can't reach, or the firewall rule uses the wrong MAC_IP. |
| `worker killed: memory cap exceeded` | The cap is below what the worker needs. Raise it from the Mac with `pilot.py policy "Ubuntu VM" --cap-gb 6`. It can't go above the cap you enrolled with. |
| Build fails with `cc: not found` | `build-essential` is missing. Rerun step 1. |
