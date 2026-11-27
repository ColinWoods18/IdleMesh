# Telemetry dataset schema (S6.5)

This describes the research dataset that `scripts/export_dataset.py` writes (format `idlemesh-dataset/1`). The
coordinator's own tables are in `coordinator/idlemesh_coord/store.py`. The export is a filtered and renamed
copy of three of them.

```bash
python scripts/export_dataset.py --db <coordinator.sqlite3> --out dataset/ --seed <private seed> \
    [--from 2026-11-01] [--to 2026-12-01]
```

## What is in it, and what is not

| Included | Left out |
|---|---|
| Idle samples and state changes from nodes whose owner's **current** consent has `idle_data` | Nodes without that consent, even if older samples are stored |
| Network figures from nodes whose current consent has `diagnostics` | Revoked devices |
| Nodes as `node_01`, `node_02`, ...; the order comes from the seed | Device IDs, device and owner names, the coordinator's node keys, org and site IDs, receive times |
| Times in UTC, to the second | Anything typed or shown on a machine: the agent never collects it (D-11) |

Keep the seed private. With it and the coordinator's salt, the aliases can be linked back to devices. Without
them they cannot. `manifest.json` stores only the seed's SHA-256.

The same database, seed and range give byte-identical files. Check a copy against the SHA-256 values in the
manifest.

## Files

### `manifest.json`

| Field | Meaning |
|---|---|
| `format` | `idlemesh-dataset/1` |
| `schema_version` | the coordinator database schema the export was read from |
| `from`, `to` | the range of `ts`: from is included, to is not (UTC) |
| `seed_sha256` | SHA-256 of the seed |
| `nodes` | how many aliases were assigned (unrevoked devices) |
| `files` | for each CSV, its row count and SHA-256 |

### `idle_samples.csv`: one row per sample (once a minute by default, `telemetry.interval_s`)

| Column | Type | Meaning |
|---|---|---|
| `node` | string | node alias |
| `ts` | UTC time | when the agent took the sample |
| `seconds_since_input` | integer or empty | seconds since the last keyboard or mouse input; empty when the OS would not say |
| `session_locked` | 0, 1 or empty | the user's session was locked |
| `cpu_util` | 0 to 1 | whole-machine CPU use |
| `gpu_util` | 0 to 1 or empty | NVIDIA GPU use; empty without one (S3.5) |
| `mem_free_gb` | number | free memory, GB |
| `on_battery` | 0 or 1 | running on battery |
| `battery_pct` | 0 to 100 or empty | battery charge |
| `state` | `idle`, `active`, `sleeping`, `draining`, `serving` | the agent's node state (S2.5). `idle` and `serving` count as available for the predictor (S6.1) |
| `contribution_level` | 0 to 1 or empty | the contribution governor's level (S4.9, D-12) |
| `level_reason` | `idle_ramp`, `time_window`, `policy`, `user_activity`, `local_load`, `power`, or empty | why the governor chose that level |

A gap in the samples means the agent was not running or the machine was asleep. After a sleep, the agent sends
a `sleeping` state change at the start of the gap (below).

### `node_events.csv`: state changes and lid events

| Column | Type | Meaning |
|---|---|---|
| `node` | string | node alias |
| `ts` | UTC time | when it happened |
| `kind` | `state` or `lid` | a node-state change, or a laptop lid opening or closing (S3.1) |
| `from_state` | state or empty | the state before (`state` rows) |
| `to_state` | state, or `closed`/`open` for `lid` rows | the state after |

### `net_edges.csv`: network figures

| Column | Type | Meaning |
|---|---|---|
| `node` | string | node alias of the measuring node |
| `peer` | alias, `coordinator`, or `other` | the far end. `other` is a node not in this export (revoked) |
| `ts` | UTC time | when it was measured |
| `method` | `heartbeat`, `tunnel_test`, `tcp`, `tunnel`, `tunnel_bulk`, `iperf3_lab` | how it was measured: heartbeat RTT on change (S5.1), the bandwidth test through the worker tunnel (S5.2), or a figure an agent reported |
| `rtt_ms_p50`, `rtt_ms_p95` | ms or empty | median and 95th-percentile round trip |
| `bandwidth_mbps` | Mbit/s or empty | the slower direction of a bandwidth test |
| `samples` | integer or empty | how many round trips were behind the RTT figures |

## Hour of the week

The predictor (S6.1) and the Grafana heatmap use the coordinator's local time. The export uses UTC, so
convert it to the campus time zone first. Monday 00:00 is hour 0.
