# Running the agent as a service (S4.1): design

Status: **design only.** Nothing here is implemented or installed yet. Starting automatically at boot comes
with the signed installers (S10). Until then, test machines run the agent by hand, and Colin starts and stops
it (decision of 2026-11-20).

This document records the architecture so that the code written now fits it later. That code covers
enrollment from a config file, crash reports, OS memory limits and the governor.

## Why it is not just "run the agent as a service"

A service runs **outside the user's session**. That causes problems on every OS:

- **Windows:** `GetLastInputInfo` and the session lock state describe the caller's own session. A service runs
  in session 0, so it would always see "no input" and mark every machine idle. That is the wrong failure:
  work would land on a machine someone is using (D-17).
- **macOS:** `HIDIdleTime` is system-wide and works from a daemon. The screen-lock state
  (`CGSessionCopyCurrentDictionary`) only exists inside a GUI session, so a daemon gets "unknown" for it.
- **Linux:** logind's `IdleHint` and `LockedHint` are readable from a system service, and so are terminal
  atimes. No helper is needed.

## Architecture

```
            ┌──────────────────────── node ────────────────────────┐
            │  idlemesh-agent (service, runs at boot)              │
coordinator ◄─┤  heartbeats, telemetry buffer, governor, worker,     │
  (HTTPS)   │  crash reports, diagnostics, OS memory limits        │
            │        ▲ local IPC: idle reading every second        │
            │        │ (named pipe / Unix socket, OS-authenticated)│
            │  idlemesh-session-helper (one per signed-in user)    │
            │  reads input idle time and lock state, nothing else  │
            └──────────────────────────────────────────────────────┘
```

**The service** runs everything the agent does today, with its state, logs, telemetry buffer and crash reports
in a machine-wide directory owned by the service account:

| OS | Service | Data directory | Config file (S4.2) |
|---|---|---|---|
| Windows | Windows service `IdleMesh Agent`, LocalService account | `%ProgramData%\IdleMesh\` | `%ProgramData%\IdleMesh\node.yaml` |
| macOS | LaunchDaemon `edu.fairlab.idlemesh.agent` | `/Library/Application Support/IdleMesh/` | `.../node.yaml` |
| Linux | systemd unit `idlemesh-agent.service`, dedicated `idlemesh` user | `/var/lib/idlemesh/` | `/etc/idlemesh/node.yaml` |

The service starts with `idlemesh-agent run --config <node.yaml>`. On first start it enrolls from the file,
which is already implemented (S4.2). The installer writes that file from the wizard's answers.

**The session helper** is needed on Windows and macOS only. It is the same binary in another mode
(`idlemesh-agent session-helper`). It starts with each user session: a Run key or scheduled task at logon on
Windows, a LaunchAgent on macOS. Every second it sends the service one reading:
`seconds_since_input`, `session_locked` and `lid_closed`. That is the existing `idle::Reading`, with
nothing else in it.

- **IPC:** a named pipe on Windows, ACL'd to the service plus the interactive users. A Unix socket on macOS, with
  peer credentials checked.
- **Several sessions** (fast user switching): the service takes the most active one. The machine is idle only
  if every session is idle or locked.
- **No helper connected** (nobody signed in): the machine is idle since the last session ended. That matches
  the Linux headless fallback already implemented.
- **A helper that stops reporting:** after 5 s its session counts as **active**, never idle (D-17).

## What changes elsewhere when this lands

- **Memory limits:** on Linux the unit adds `MemoryMax=` (cgroup v2) for the worker's slice, on top of the
  `RLIMIT_DATA` the agent already sets (S4.8). Windows keeps the Job Object, which a service may create. macOS
  still relies on the watchdog.
- **Priority:** on Linux, a service can be given `CAP_SYS_NICE` so the governor can raise worker priority again
  after a yield. Today an ordinary user can only lower it (S4.9).
- **Logs:** logs stay as JSON files under the data directory. Windows additionally gets an Event Log source for
  start, stop and crash, so IT can see the service in its usual tools.
- **Updates (S10):** the service is replaced by the updater with a stop, swap and start sequence, and rolled
  back if the new version does not heartbeat within N minutes.
- **Consent:** collected by the installer wizard and written to `node.yaml`. It can be changed later with
  `idlemesh-agent consent` or a tray app, which is still a placeholder.

## Open questions

- Windows: LocalService versus a virtual service account, and what campus IT prefers.
- Can the helper be signed and started in a way that endpoint protection on lab machines does not block?
- Does a lab image with no interactive logins (kiosk or render nodes) need the helper at all? Probably not:
  the Linux-style fallback covers it.
