# proto/ — draft contracts (review only)

Status: **draft for review (S1.11).** Nothing compiles or serves these yet.

| File | Covers | Used from |
|---|---|---|
| [idlemesh/v1/control.proto](idlemesh/v1/control.proto) | Enrollment, heartbeat and lease, commands to nodes (drain and yield, probes, log level, diagnostics, config, stop) | Shapes of today's HTTPS/JSON payloads; gRPC in sprint 12 (D-06) |
| [idlemesh/v1/telemetry.proto](idlemesh/v1/telemetry.proto) | Idle and health samples, log and error events, crash reports, diagnostic bundles, per-request and canary records | Sprint 2 onwards (D-11) |

## How these are used before gRPC

The control channel stays HTTPS/JSON with a pinned certificate for now (D-06). New JSON payloads
(telemetry from sprint 2, crash reports from sprint 4) follow these messages field for field, using the
proto field names. When the transport moves to gRPC + mTLS, only the transport changes.

## Rules

- **No prompt or response text in any message** (D-11). Review every new field against this.
- Records that leave a node carry `org_id`, `site_id` and `schema_version`.
- Error codes come from [contracts/error-codes.json](../contracts/error-codes.json).
- Add fields; never renumber or reuse them. Retire with `reserved`.

## Review questions

1. Should `Heartbeat` become a bidirectional stream (immediate drain and yield commands), or stay
   request/response with commands piggybacked on the reply as now? Streams make D-12's 1-second yield
   easier to coordinate.
2. Are the D-12 fields on `NodeStatus` and `IdleSample` (`contribution_level`, `level_reason`, the
   `yielding` state) the right granularity for the governor and for experiment E2?
3. Which `LogEvent` levels should be uploaded by default (proposed: warn and above, plus everything during
   a time-limited debug window)? This depends on the privacy office's answer about telemetry.
