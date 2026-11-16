# Windows test setup (HP EliteBook contributing device)

The HP contributes CPU and memory to the test. It runs:

- **`difa-agent`**, which enrolls the laptop, enforces your memory cap, downloads its share of the model,
  and runs the encrypted tunnel;
- **`ggml-rpc-server`** (llama.cpp's worker), which the agent starts, stops, and keeps on the laptop's
  own loopback address.

Set up the Mac first ([mac_test_setup.md](mac_test_setup.md)) so the coordinator is running before you
enroll. The whole test sequence is in [three_machine_test.md](three_machine_test.md).

> **Workplace laptop.** Before you start, check that your workplace's policy allows installing developer
> tools, running a self-built program, and opening an inbound firewall port. If it does not, don't work
> around it. Use another personal machine for this role and follow the Ubuntu guide or this one there.

All commands are for **PowerShell** (Start > type "PowerShell"). Steps marked *(Administrator)* need
"Run as administrator".

## What you need

| Item | Version | Check with |
|---|---|---|
| Windows 10 22H2 or Windows 11, x64 | | `winver` |
| winget (App Installer) | any recent | `winget --version` |
| Git | any recent | `git --version` |
| CMake | 3.20 or later | `cmake --version` |
| Visual Studio 2022 Build Tools, C++ workload | 17.x | `& "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -products * -latest -property displayName` |
| Rust (rustup), MSVC toolchain | stable | `cargo --version` |
| Free disk | about 4 GB | |
| Memory | 8 GB or more | |

## 1. Install supporting software

Install each tool with winget. Each line asks you to accept the publisher's terms.

```powershell
winget install --id Git.Git -e
```

```powershell
winget install --id Kitware.CMake -e
```

The C++ build tools are a large download (about 2 to 3 GB):

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

```powershell
winget install --id Rustlang.Rustup -e
```

**Close PowerShell and open a new window** so the new tools are on your PATH. Then select the MSVC Rust
toolchain:

```powershell
rustup default stable-x86_64-pc-windows-msvc
```

Run the checks from the table. All of them should print a version.

## 2. Make a working folder outside OneDrive

`Documents` is often synced by OneDrive, and builds there are slow and fill your OneDrive. Use a plain
folder in your profile:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\difa" | Out-Null; Set-Location "$env:USERPROFILE\difa"
```

## 3. Get the code

```powershell
git clone https://github.com/TheCodeheadMT/I4A.git "$env:USERPROFILE\difa\I4A"
```

```powershell
git clone --depth 1 --branch v0.9.0 https://github.com/ggml-org/llama.cpp.git "$env:USERPROFILE\difa\llama.cpp"
```

**The llama.cpp tag must be v0.9.0 on every machine.** A different version fails at load time.

## 4. Build the llama.cpp worker (CPU and RPC)

```powershell
Set-Location "$env:USERPROFILE\difa\llama.cpp"; cmake -B build -DGGML_RPC=ON -DLLAMA_CURL=OFF
```

```powershell
cmake --build build --config Release -j --target ggml-rpc-server llama-bench
```

This takes about 5 to 15 minutes. Check:

```powershell
& "$env:USERPROFILE\difa\llama.cpp\build\bin\Release\ggml-rpc-server.exe" --help
```

It prints the worker's options, including `-c, --cache`.

## 5. Build the device agent

```powershell
Set-Location "$env:USERPROFILE\difa\I4A\agents\device"; $env:CARGO_TARGET_DIR = "$env:USERPROFILE\difa\target"; cargo build --release
```

Check:

```powershell
& "$env:USERPROFILE\difa\target\release\difa-agent.exe" --version
```

It prints `difa-agent 0.1.0`.

## 6. Network: addresses, reachability, firewall

Find the HP's LAN address (use the adapter that is on the same network as the Mac, usually Wi-Fi or
Ethernet):

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet' } | Select-Object InterfaceAlias, IPAddress
```

Write it down as **HP_IP**. Then check that the Mac's coordinator is reachable, replacing `MAC_IP`:

```powershell
Test-NetConnection MAC_IP -Port 8765
```

`TcpTestSucceeded : True` means it works. If it is `False`:
- the coordinator isn't running;
- the Mac's firewall blocked Python; or
- a VPN is sending your LAN traffic elsewhere. If your workplace allows it, disconnect the VPN for the
  test.

Make sure Windows treats this network as **Private**: Settings > Network & internet > (your connection) >
Network profile type > Private.

Allow the Mac, and only the Mac, to reach the tunnel port *(Administrator)*:

```powershell
New-NetFirewallRule -DisplayName "DIFA worker tunnel" -Direction Inbound -Protocol TCP -LocalPort 50100 -RemoteAddress MAC_IP -Action Allow -Profile Private,Domain
```

Only port 50100 (the mutual-TLS tunnel) is opened. The llama.cpp worker listens on 127.0.0.1 and is
never reachable from the network.

## 7. Keep the laptop awake and plugged in

For the test, set Settings > System > Power & battery > Screen and sleep > "When plugged in, put my
device to sleep after" to **Never**, and keep the charger connected. You can restore it afterwards.

## 8. Enroll the HP

On the **Mac**, get a one-time code (valid for 10 minutes). It also prints the coordinator fingerprint:

```bash
~/.venvs/difa/bin/python scripts/pilot.py code
```

On the **HP**, replacing `MAC_IP`, `FINGERPRINT` (the 16 digits after `--coordinator-fingerprint`) and
`CODE`:

```powershell
& "$env:USERPROFILE\difa\target\release\difa-agent.exe" enroll --coordinator https://MAC_IP:8765 --coordinator-fingerprint FINGERPRINT --code CODE --name "HP EliteBook" --memory-cap-gb 8 --allow-battery --backend-digest llama.cpp-v0.9.0 --state "$env:USERPROFILE\difa\agent.json"
```

What the options do:
- `--memory-cap-gb 8` is the most memory the HP may contribute. The agent kills the worker if it goes
  over. The test model needs well under 1 GB, so any value from 2 to 8 works.
- `--coordinator-fingerprint` is checked before the code is sent: the agent talks only to a coordinator
  presenting that certificate, and pins it for all later traffic.
- `--allow-battery` is **needed on Windows for now**. The agent can't read Windows power status yet,
  reports it as "unknown", and treats unknown like battery, which would pause contribution. Keep the
  charger connected instead.

Expected output: `enrolled device dev_... as 'HP EliteBook'; state saved to ...agent.json`.

> `agent.json` holds the laptop's device key and token. On Windows the agent doesn't yet restrict the
> file's permissions, so keep it in your profile folder and delete it after the test.

## 9. Run the agent

Find the number of physical cores, and use it for `--worker-threads`:

```powershell
(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum
```

Then start the agent, replacing `HP_IP` and the thread count. Leave this window open for the whole test.

```powershell
& "$env:USERPROFILE\difa\target\release\difa-agent.exe" run --state "$env:USERPROFILE\difa\agent.json" --worker-bin "$env:USERPROFILE\difa\llama.cpp\build\bin\Release\ggml-rpc-server.exe" --worker-threads 4 --tunnel-listen 0.0.0.0:50100 --tunnel-advertise HP_IP:50100 --accept-license apache-2.0 --cache-dir "$env:USERPROFILE\difa\cache"
```

What the options do:
- `--tunnel-listen` and `--tunnel-advertise` set up the encrypted tunnel and give the coordinator the
  address to reach it on.
- `--accept-license apache-2.0` is your consent, as the device owner, to download the test model's
  weights.
- `--cache-dir` is where this device keeps its share of the model.

If Windows Security asks whether `difa-agent.exe` may communicate on networks, tick **Private networks
only** and choose Allow.

Expected output:

```
tunnel: mutual TLS on 0.0.0.0:50100 (advertised HP_IP:50100); worker on 127.0.0.1:50052
heartbeat ok: paused=false power=unknown cap=8.00GB rtt=...ms
worker started on 127.0.0.1:50052
```

## 10. Check from the Mac

```bash
~/.venvs/difa/bin/python scripts/pilot.py devices
```

The HP shows `ready` with `worker=running`. You can also check that the tunnel port is reachable:

```bash
nc -vz HP_IP 50100
```

It prints `succeeded`. Because `nc` has no certificate, the HP agent logs `tunnel: rejected ...` for it.
That is the tunnel refusing a stranger, as it should.

## Stopping and cleaning up

- **Stop:** press Ctrl-C in the agent window. It prints `agent stopping: worker halted`, and no worker is
  left running.
- **Remove the firewall rule** after the test *(Administrator)*:

  ```powershell
  Remove-NetFirewallRule -DisplayName "DIFA worker tunnel"
  ```

- **Remove the device's key, token and cached model share:**

  ```powershell
  Remove-Item "$env:USERPROFILE\difa\agent.json", "$env:USERPROFILE\difa\cache" -Recurse
  ```

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `cannot reach coordinator` | Check `Test-NetConnection MAC_IP -Port 8765`. Check the Mac's firewall and any VPN. |
| `certificate does not match the fingerprint (the code was not sent)` | The fingerprint was mistyped, or something other than your coordinator answered at that address. Check the digits against `pilot.py code` on the Mac. The code is still valid. |
| `refusing plain HTTP to a coordinator on the network` | Use `https://MAC_IP:8765` with `--coordinator-fingerprint`. An `agent.json` enrolled with `http://` must be deleted and enrolled again. |
| `enrollment failed (404 ...)` | The code expired (10 minutes) or was already used. Get a new one with `pilot.py code`. |
| `heartbeat ok: paused=true` | You enrolled without `--allow-battery`. Delete `agent.json`, get a new code, and enroll again with it. |
| `tunnel cannot listen on 0.0.0.0:50100` | Another program uses the port. Pick another port, and update both the agent flags and the firewall rule. |
| Deployment fails with `TUNNEL_FAILED` | The firewall rule is missing or uses the wrong MAC_IP, or `--tunnel-advertise` has the wrong HP_IP. |
| `worker failed to start` | The `--worker-bin` path is wrong. Check it with the `--help` command from step 4. |
| `cmake` can't find a compiler | The C++ workload isn't installed. Rerun the Build Tools install in step 1. |
