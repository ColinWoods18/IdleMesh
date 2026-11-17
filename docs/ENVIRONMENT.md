# Environment: machines, paths, versions, and gotchas

What a new machine or a new contributor needs to know that the setup guides in
[docs/testing/](testing/) do not already say. Addresses below are placeholders (MAC_IP, HP_IP, VM_IP),
apart from the two subnets that already appear in committed results.

## Test machines

| Machine | Role | Hardware and OS | Network in the Phase 1 rounds |
|---|---|---|---|
| Mac Studio | Coordinator, Metal stage, and the developer machine | M2 Ultra, 128 GB, macOS 15.6.1. Metal `recommendedMaxWorkingSetSize` 103,079 MB. | Wi-Fi in Rounds 4, 5 and 7; Ethernet in Round 6 |
| HP EliteBook | CPU stage | Windows 11 Enterprise, **workplace-managed**. About 10.2 GB available in the spec's snapshot. | Wi-Fi; 10.10.150.0 subnet |
| Ubuntu VM | CPU stage | Ubuntu, 8 GB (the spec's planning value). Its host and network mode are not recorded; the guide recommends bridged. | Wi-Fi in Rounds 4–6, Ethernet in Round 7; 10.10.132.0 subnet |

- **The HP and the VM are on different subnets from each other**, so traffic between them and the Mac is
  routed. That, more than Wi-Fi, sets the VM's round trip (about 9–10 ms, wired or not).
- **The Mac's own subnet is not recorded.**
- Record each run's connection types with `pilot bench --network "Mac=... HP=... VM=..."`.

## Versions

| Component | Version |
|---|---|
| llama.cpp | tag **v0.9.0**, commit `cd959bae99ff4bed22bef2604f45241c09bbb8c8` (`llama-server --version` prints `0.9.0-dev (build 1, commit cd959ba)`). It must be the same on all machines. |
| llama.cpp RPC protocol | 8.0.1. Framing is documented in `services/coordinator/difa/transport.py`. |
| Rust | 1.99.1 (installed with rustup; added to `~/.zshrc` on the Mac) |
| Python | 3.10.10, venv at `~/.venvs/difa` |
| CMake | 4.5.0 on the Mac (3.20 or later needed) |
| Test model | `qwen2.5-0.5b-instruct-q8_0.gguf`, Apache-2.0, SHA-256 `ca59ca7f13d0e15a8cfa77bd17e65d24f6844b554a7b6c12e07a5f89ff76844e` |

## Where things live on the Mac (all outside the repository)

| Path | Contents | Keep private? |
|---|---|---|
| `…/iCloud Drive/code/difa` | Main checkout (this repository), used for development | — |
| `…/iCloud Drive/code/*.docx` | Spec and whitepaper v0.2 and v0.3. **Not in git**; summaries are in [docs/spec/](spec/). | — |
| `…/iCloud Drive/code/.claude/` | Claude Code project settings: the ECC plugin from `github.com/affaan-m/ECC`, and `launch.json` | — |
| `~/Documents/difa` | **Second checkout, used to run the coordinator for the three-machine tests** | — |
| `~/.venvs/difa` | Python venv. Its editable install points at `~/Documents/difa`. | — |
| `~/.cache/difa-phase1/llama.cpp` | llama.cpp build (`build/bin/llama-server`, `llama-bench`, `ggml-rpc-server`) | — |
| `~/.cache/difa-phase1/models` | Test model, plus the `.difa-tensors.json` hash sidecar | — |
| `~/.cache/difa-phase1/adapter-logs` | llama-server log per deployment (`dep_*.log`), with per-request timings | — |
| `~/.cache/difa-target` | Cargo target directory (`debug/` and `release/` agent builds) | — |
| `~/.local/share/difa` | `dev.sqlite3`, `coordinator-control.{crt,key}`, `coordinator-tunnel.{crt,key}`, `mac-agent.json`, `pilot-session.json` | **Yes: keys, tokens, sessions. Never commit.** |

On the HP and the VM, the checkouts, builds, `agent.json` and cache live under `%USERPROFILE%\difa` and
`~/difa` respectively, as in their guides.

## Ports

| Port | Use |
|---|---|
| 8765 | Coordinator control API (HTTPS) |
| 50100 | Agent mTLS tunnel. The only inbound port on the HP and the VM, restricted to MAC_IP. |
| 50052 | Default worker port, on loopback only |
| 8790, 8795, 50052–50084, 60082–60084 | Benchmark scripts on the Mac (see each script) |

## Gotchas

- **There are two checkouts on the Mac.** The coordinator for the hardware tests runs from
  `~/Documents/difa`, and the venv's editable install also points there. After changes land on
  `main`, `git pull` there and restart the coordinator. Round 7 ran with that checkout one commit
  behind, so the heartbeat fix was missing.
- **Results written by `pilot bench`** land in the checkout that ran it (`~/Documents/difa`). Commit them
  from there, or copy them across. An untracked copy blocks a later `git pull` of the same file.
- **For development in the iCloud checkout,** pytest uses its own `services/coordinator` through the
  `pythonpath` setting. Ad-hoc `python -c "import difa"` uses `~/Documents/difa` unless you set
  `PYTHONPATH=services/coordinator`.
- **The integration tests use the agent's *debug* build** (`~/.cache/difa-target/debug/difa-agent`).
  Run `cargo build` as well as `cargo build --release` after agent changes; otherwise the tests run a
  stale agent.
- **Re-enroll after the HTTPS change (`40e80cc`).** Every device enrolled before it must delete its
  `agent.json` and enroll again with `https://MAC_IP:8765 --coordinator-fingerprint …`. On the Mac,
  delete `pilot-session.json`, which saved an `http://` address, and run `pilot setup` again.
- **A coordinator restart with `--fresh` forgets every device.** Agents then print "device
  authorization revoked" and must enroll again. The control certificate, and so the fingerprint,
  survives `--fresh`.
- **Windows reports power as "unknown",** which the agent treats like battery. Enroll the HP with
  `--allow-battery` and keep it plugged in.
- **`code/.claude/launch.json`** still starts the coordinator with `--fresh` and points the preview at
  `http://127.0.0.1:8765`. The dev server now serves HTTPS by default, so the preview needs
  `--insecure-http` or an `https://` URL.
- **The ECC GateGuard hook,** from the ECC plugin, asks for facts before the first edit of a file, the
  first Bash command of a session, and destructive commands. Answer and retry. Setting
  `ECC_GATEGUARD=off` disables it.
- **llama.cpp details:**
  - the CMake target is `ggml-rpc-server`, not `rpc-server`;
  - `--rpc` must come before `-dev`;
  - RPC buffers in the log are named `RPC0[host:port]`, so key them by endpoint;
  - the worker serves one client at a time, which is why the tunnel RTT probe runs before load.
- **Keep secrets out of the repository:**
  - `*.sqlite3` and `difa-agent-state.json` are git-ignored;
  - certificates, keys, `agent.json` and `pilot-session.json` live outside the checkout. Keep them there.
