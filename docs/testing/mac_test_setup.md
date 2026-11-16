# Mac test setup (coordinator and Metal device)

The Mac Studio has two roles in the three-machine test:

1. **Coordinator.** It runs the DIFA coordinator, which plans placements, holds the group's data, and runs
   `llama-server` with the Mac's own Metal stage.
2. **Contributing device.** It runs a `difa-agent` that reports the Mac's capacity. It does not need an
   inference worker: the coordinator uses the Mac's GPU directly.

Do this machine first. The HP and the Ubuntu VM both need the coordinator running before they can enroll.
The whole test sequence is in [three_machine_test.md](three_machine_test.md).

> Builds, the virtual environment, models, and the database all live **outside iCloud**
> (`~/.venvs`, `~/.cache`, `~/.local/share`) so iCloud does not sync gigabytes of build output. Only the
> source checkout lives in iCloud.

## What you need

| Item | Version | Check with |
|---|---|---|
| macOS on Apple silicon | 14 or later | `sw_vers` |
| Xcode Command Line Tools | any recent | `xcode-select -p` |
| Homebrew | any recent | `brew --version` |
| Python | 3.10 or later | `python3 --version` |
| CMake | 3.20 or later | `cmake --version` |
| Rust (rustup) | stable | `cargo --version` |
| Free disk | about 3 GB | |

## 1. Install supporting software

Skip any line whose check above already passes. This Mac already has Python 3.10, Rust, and CMake from
earlier phases.

```bash
xcode-select --install
```

Install Homebrew from https://brew.sh if `brew --version` fails, then:

```bash
brew install cmake git python@3.12
```

Install Rust with the official installer (it asks before changing your shell profile):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Open a new terminal afterwards so `cargo` is on your PATH.

## 2. Get the code

The checkout is already at `~/Library/Mobile Documents/com~apple~CloudDocs/code/difa`. If you are setting
up a different Mac, clone it anywhere:

```bash
git clone https://github.com/TheCodeheadMT/I4A.git ~/difa
```

All commands below run from the checkout folder. Change to it and update it:

```bash
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/code/difa && git pull
```

## 3. Python environment for the coordinator

```bash
python3 -m venv ~/.venvs/difa && ~/.venvs/difa/bin/python -m pip install -e ".[dev]"
```

Check: `~/.venvs/difa/bin/python -c "import difa, cryptography; print('ok')"` prints `ok`.

## 4. Build llama.cpp v0.9.0 with Metal and RPC

**All three machines must run the same llama.cpp tag (v0.9.0).** The RPC protocol changes between
releases, and a mismatch fails at load time.

```bash
git clone --depth 1 --branch v0.9.0 https://github.com/ggml-org/llama.cpp.git ~/.cache/difa-phase1/llama.cpp
```

```bash
cd ~/.cache/difa-phase1/llama.cpp && cmake -B build -DGGML_METAL=ON -DGGML_RPC=ON -DLLAMA_CURL=OFF && cmake --build build -j --target llama-server llama-bench ggml-rpc-server
```

Check: `~/.cache/difa-phase1/llama.cpp/build/bin/llama-server --version` prints
`version: 0.9.0-dev (build 1, commit cd959ba)`.

## 5. Download and verify the test model

The test uses Qwen2.5-0.5B-Instruct Q8_0 (676 MB, Apache-2.0). Only the Mac needs the full file: the
other devices download just their share from the coordinator.

```bash
mkdir -p ~/.cache/difa-phase1/models && curl -L -o ~/.cache/difa-phase1/models/qwen2.5-0.5b-instruct-q8_0.gguf https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q8_0.gguf
```

```bash
shasum -a 256 ~/.cache/difa-phase1/models/qwen2.5-0.5b-instruct-q8_0.gguf
```

The hash must be `ca59ca7f13d0e15a8cfa77bd17e65d24f6844b554a7b6c12e07a5f89ff76844e`. The coordinator
refuses to load a file with any other hash.

## 6. Build the device agent

```bash
cd agents/device && CARGO_TARGET_DIR=~/.cache/difa-target cargo build --release && cd ../..
```

The agent is at `~/.cache/difa-target/release/difa-agent`. Check: `~/.cache/difa-target/release/difa-agent --version`.

## 7. Network: address and firewall

Find the Mac's LAN address. A Mac Studio on Ethernet usually uses `en0`; on Wi-Fi, try `en1`.

```bash
ipconfig getifaddr en0
```

Write it down as **MAC_IP**; the HP and the VM use it to reach the coordinator.

If the macOS firewall is on (System Settings > Network > Firewall), the first time the coordinator starts
macOS asks whether Python may accept incoming connections. Choose **Allow**. The coordinator uses TCP
port **8765**.

> **Encrypted channels.** The coordinator serves its API over HTTPS with a self-signed certificate. Each
> device checks that certificate's fingerprint before sending its enrollment code, then accepts only that
> certificate. Model traffic to the HP and the VM is mutual TLS. This is still a prototype: run the test
> on a home or lab network you control, not on public Wi-Fi.

## 8. Start the coordinator

Use a dedicated terminal window and leave it running for the whole test. `--fresh` starts with an empty
database. Leave it off if you restart the coordinator mid-test and want to keep enrolled devices.

```bash
~/.venvs/difa/bin/python scripts/dev_server.py --fresh --llamacpp --host 0.0.0.0 --model-source coordinator
```

What the options do:
- `--llamacpp` uses the real llama.cpp backend.
- `--host 0.0.0.0` listens on all interfaces. The group can still only be created from the Mac itself.
- `--model-source coordinator` makes devices fetch their weights from this Mac rather than from Hugging
  Face.

Expected output:

```
control API: https://0.0.0.0:8765  certificate /Users/.../.local/share/difa/coordinator-control.crt
coordinator fingerprint (SHA-256): 3f2a 9c1b ... (16 groups of four)
Listening on 0.0.0.0: devices enroll with https and the fingerprint above. ...
Uvicorn running on https://0.0.0.0:8765
```

The certificate is created on first start and kept in `~/.local/share/difa/`, so the fingerprint stays
the same across restarts, including `--fresh`. `pilot.py code` also prints it with each enrollment code.

Check from the Mac:

```bash
curl -s --cacert ~/.local/share/difa/coordinator-control.crt -o /dev/null -w "%{http_code}\n" https://127.0.0.1:8765/docs
```

It prints `200`. Your browser will warn about the self-signed certificate if you open the API docs at
https://127.0.0.1:8765/docs; you don't need the docs page for this test.

## 9. Create the group and sign in

In a second terminal, from the checkout:

```bash
~/.venvs/difa/bin/python scripts/pilot.py setup
```

It connects to `https://127.0.0.1:8765` and trusts only the coordinator's own certificate. If you used an
earlier version of this guide, delete `~/.local/share/difa/pilot-session.json` first, because that saved
an `http://` address.

It asks for a new owner passphrase of 15 characters or more. **Write down the recovery code it prints;
it is shown once.** The helper saves your session in `~/.local/share/difa/pilot-session.json`. If a
later command says the session expired (after 30 idle minutes), run
`~/.venvs/difa/bin/python scripts/pilot.py login`.

## 10. Enroll the Mac as a device

Get a one-time code (valid for 10 minutes):

```bash
~/.venvs/difa/bin/python scripts/pilot.py code
```

It prints the code, then the coordinator fingerprint and the first 16 digits to pass on a device:

```
enr_....
expires ...; give it to one device's `difa-agent enroll --code ...`
coordinator fingerprint: 3f2a 9c1b ...
  on the device: --coordinator-fingerprint 3f2a9c1b........  (or the whole fingerprint)
```

Enroll, replacing `CODE` and `FINGERPRINT` with what was printed. **The name must be exactly
`Mac Studio`**: that is how the coordinator knows this device's stage runs on its own Metal GPU. If you
enrolled the Mac before this change, delete `~/.local/share/difa/mac-agent.json` first.

```bash
~/.cache/difa-target/release/difa-agent enroll --coordinator https://127.0.0.1:8765 --coordinator-fingerprint FINGERPRINT --code CODE --name "Mac Studio" --memory-cap-gb 64 --backend-digest llama.cpp-v0.9.0 --state ~/.local/share/difa/mac-agent.json
```

## 11. Run the Mac agent

Use a third terminal and leave it running. The decode probe times real Metal decoding at startup, so the
planner knows how fast this Mac is. That takes about 10 seconds.

```bash
~/.cache/difa-target/release/difa-agent run --state ~/.local/share/difa/mac-agent.json --decode-probe-bench ~/.cache/difa-phase1/llama.cpp/build/bin/llama-bench --decode-probe-model ~/.cache/difa-phase1/models/qwen2.5-0.5b-instruct-q8_0.gguf
```

Expected output: `decode probe: about 290 tokens/s on default backend`, then `heartbeat ok: paused=false
power=ac cap=64.00GB` every 5 seconds.

## 12. Check

```bash
~/.venvs/difa/bin/python scripts/pilot.py devices
```

Expected:

```
Mac Studio       ready        worker=-        budget=  64.0 GB  power=ac       macos/aarch64  dev_...
```

`worker=-` is correct for the Mac. Now set up the HP ([windows_test_setup.md](windows_test_setup.md)) and
the VM ([ubuntu_test_setup.md](ubuntu_test_setup.md)), then continue with
[three_machine_test.md](three_machine_test.md).

## Stopping

Press Ctrl-C in the agent terminal, then in the coordinator terminal. The coordinator unloads the model
and closes its tunnels on the way out.
