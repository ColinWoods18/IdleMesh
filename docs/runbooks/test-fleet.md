# Run the test fleet (S4.6, S4.7)

The development machines (the Windows dev PC, the Linux server and the MacBook) act as a small test fleet. They
report to one coordinator on the dev PC. Everything here is started by hand and runs only while testing.
Nothing starts at boot (D-21).

## The network, and why there are SSH tunnels

| From → to | Works? |
|---|---|
| Dev PC (Wi-Fi) → Linux server, MacBook | ✅ SSH, and the worker tunnels (port 50100) |
| Linux server → dev PC | ❌ the campus network drops connections from the server network to Wi-Fi clients |

Agents must reach the coordinator, so each remote machine reaches it through an SSH reverse tunnel opened
**from the dev PC**. The agent then uses `https://127.0.0.1:PORT` on its own machine. It still pins the
coordinator's certificate fingerprint, and everything travels inside SSH. Worker traffic goes the other way
(coordinator → agent, port 50100, mutual TLS), which the network allows.

```bash
# On the dev PC; reconnects by itself, stop it with Ctrl-C
while true; do
  ssh -o ServerAliveInterval=15 -o ExitOnForwardFailure=yes -N -R 8766:127.0.0.1:8766 -R 8767:127.0.0.1:8767 linux
  sleep 3
done
```

`ssh macbook` and `ssh linux` are set up in `~/.ssh/config` on the dev PC, with key login. A Windows Firewall
rule for ports 8765–8766 from the two machines exists but is not what makes it work. Remove it when testing is
done: `Remove-NetFirewallRule -DisplayName "IdleMesh coordinator (dev)"`.

## Enroll a machine from a config file (S4.2)

```yaml
# node.yaml, next to where the state file should live
coordinator: https://127.0.0.1:8766
coordinator_fingerprint: <first 16+ hex digits from the coordinator's startup line>
enrollment_code: <python scripts/pilot.py code>
name: Linux server (env-colin)
memory_cap_gb: 4
consent: {idle_data: true, diagnostics: true}
```

```bash
idlemesh-agent run --config node.yaml      # enrolls on first start, then runs
```

On the shared Linux server, run the agent at low priority (`nice -n 10`), and build with `-j 4`. GPU work belongs
to other projects there.

## llama.cpp v0.9.0 (S4.7)

- **Build (CPU, RPC):**
  `cmake -B build -DGGML_RPC=ON -DLLAMA_CURL=OFF -DGGML_CUDA=OFF -DLLAMA_USE_PREBUILT_UI=OFF`, then
  `cmake --build build --config Release --target ggml-rpc-server llama-server llama-bench`.
  - `LLAMA_USE_PREBUILT_UI=OFF` stops the build from downloading the web UI, which failed here.
  - If a download already failed, delete `build/tools/ui/dist`.
- **CMake:** the Linux server has CMake 3.31.6 in `~/.local/opt` (no root). On Windows it comes with the
  Visual Studio Build Tools.
- **Coordinator in llama.cpp mode, with the dev PC's own stage as a loopback worker** (no Metal needed):

  ```bash
  IDLEMESH_LLAMACPP_BIN=~/.cache/difa-phase1/llama.cpp/build/bin/Release IDLEMESH_LLAMACPP_LOCAL_DEVICE=none \
    python scripts/dev_server.py --llamacpp --model-source coordinator --host 0.0.0.0 --port 8767
  ```

- **Agents** are enrolled with `--backend-digest llama.cpp-v0.9.0`:
  - dev PC: `--worker-bin ...\ggml-rpc-server.exe --worker-port 50052`, on loopback with no tunnel;
  - Linux: add `--tunnel-listen 0.0.0.0:50100 --tunnel-advertise 10.100.10.19:50100`.
- **Then** follow [the legacy runbook](../legacy-difa/testing/three_machine_test.md) from step 1, using
  `pilot devices`, `deploy`, `ask`, `bench` and the 4a–4f checks.

## Results so far

| Run | Machines | Ready after | Measured | Predicted | Drain checks |
|---|---|---|---|---|---|
| 2026-11-20 | Dev PC + Linux server | 13 s | 40.9 ms/token | 26.8 ms/token (−34.5%) | 4a–4f pass |
| 2026-11-20 | Dev PC + Linux server + MacBook (Metal, head) | 32 s | 51.9 ms/token | 29.7 ms/token (−42.8%) | 4a–4f pass, Mac as the target |

Results files: `experiments/llamacpp-feasibility/results/pilot-3machine-20261120T190618Z.json` and
`pilot-3machine-20261120T212314Z.json`.

Two lessons from the checks:

- An agent sees a policy change at its next heartbeat. Wait for its `worker stopped` line before resuming.
- Before `pilot deploy`, wait until `pilot devices` shows `worker=running` for every machine. A deployment
  started earlier leaves out the machine that is still starting.

On the Mac, the agent is built from a `git archive` copy of `main` in `~/idlemesh-main`. llama.cpp is built with
the default Metal backend (`-DGGML_RPC=ON -DLLAMA_CURL=OFF -DLLAMA_USE_PREBUILT_UI=OFF`), and CMake comes from
`uv tool install cmake`.
