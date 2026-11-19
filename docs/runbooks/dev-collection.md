# Leave a dev machine collecting idle data (S2.7)

The Phase 1 exit needs at least two weeks of idle data, and the clock starts when a machine begins
collecting. This runbook sets up the Windows dev PC to collect continuously. The same steps work on the
Mac and the Linux server once their idle monitors land (S3.1, S3.2); until then they report CPU,
memory and power, but never claim to be idle.

Commands are for Git Bash on Windows. Use `~/.venvs/idlemesh/bin/python` on macOS and Linux.

## 1. Start the coordinator

Use HTTPS (the default), so the same setup later works for the other machines on the network:

```bash
PY=~/.venvs/idlemesh/Scripts/python
$PY scripts/dev_server.py --site-config config/example.site.yaml
```

It prints the control address and the certificate fingerprint. The database, the coordinator's keys and
the telemetry salt live in `~/.local/share/difa/`, outside OneDrive.

## 2. Enroll this machine, with consent to idle data

In a second terminal:

```bash
$PY scripts/pilot.py setup          # once per fresh database: creates the group and signs in
$PY scripts/pilot.py code           # one-time enrollment code, valid for 10 minutes
```

```bash
AGENT=~/.cache/idlemesh-target/debug/idlemesh-agent.exe   # or a release build
NODE=~/.local/share/idlemesh-node
mkdir -p $NODE
$AGENT enroll --coordinator https://127.0.0.1:8765 --coordinator-fingerprint <FINGERPRINT> \
  --code <CODE> --name "Dev PC" --memory-cap-gb 8 --state $NODE/state.json --consent-idle-data
```

`--consent-idle-data` records the owner's consent on the device (policy version v0). Without it the
agent collects nothing and the coordinator refuses its telemetry. To change it later:
`$AGENT consent --state $NODE/state.json --idle-data false`, then restart the agent.

## 3. Run the agent

```bash
$AGENT run --state $NODE/state.json
```

Run it **in your own signed-in session**, not as a service or a scheduled task that runs whether or
not you are logged on. Windows reports keyboard and mouse input only to the session it happened in, so
an agent in another session would always see the machine as idle. It runs as a service from S4.1, with a
small helper in the user's session.

To keep it running across sign-ins until then, put a shortcut to the command in the Startup folder
(`Win+R`, `shell:startup`).

## 4. Check it

```bash
$AGENT idle --watch                 # seconds since input reset when you touch the mouse; Ctrl-C to stop
$PY scripts/idle_timeline.py --hours 12
```

The timeline shows one row per hour: `I` idle, `a` active, `z` asleep, blank for no data. Samples taken
while the coordinator was down arrive when it is back (the agent buffers up to 20 MB, about 50 days at
one sample a minute); a drop count other than 0 means the buffer overflowed.

Logs: the agent writes `$NODE/logs/idlemesh-agent.log`; telemetry problems carry the codes
`TELEMETRY_UPLOAD_FAILED`, `TELEMETRY_BUFFER_FULL` and `TELEMETRY_NO_CONSENT` (see
[error-codes.md](../error-codes.md)).
