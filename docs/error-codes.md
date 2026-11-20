# Error codes

<!-- Generated from contracts/error-codes.json by scripts/gen_error_codes.py. Do not edit by hand. -->

Every error that IdleMesh returns or logs carries one of these stable codes (D-11). The same codes
are used by the coordinator (API responses and JSON logs) and the agent (JSON logs and reports), so
one code can be followed across components and counted in metrics.

- **HTTP** is the status the coordinator's API returns with that code; `—` means the code appears
  only in logs, events or agent reports.
- **Retry** says whether trying again later can succeed without anything changing.
- To add a code: edit `contracts/error-codes.json`, then run `python scripts/gen_error_codes.py`.
  Tests fail if code uses a code that is not in the catalog, or if this file is out of date.
  Never reuse or change the meaning of a released code; retire it instead.

## Request handling

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `BODY_TOO_LARGE` | 413 | no | The request body exceeds the size limit (1 MiB). |
| `HTTP_ERROR` | — | no | A generic HTTP error with no more specific code. |
| `IDEMPOTENCY_CONFLICT` | 409 | no | A client_request_id was reused with different content. |
| `INTERNAL` | 500 | yes | An unexpected internal error. The response carries a request_id that finds the full log entry. |
| `INVALID_FIELDS` | 400 | no | The request body has missing, unknown or invalid fields. |
| `INVALID_JSON` | 400 | no | The request body is not valid JSON. |
| `METHOD_NOT_ALLOWED` | 405 | no | The HTTP method is not supported on this path. |
| `NOT_FOUND` | 404 | no | The resource does not exist or is not visible to this caller. |
| `UNSUPPORTED_FIELD` | 400 | no | The request uses an OpenAI field or content type that IdleMesh does not support yet. |

## Sign-in and permissions

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `BOOTSTRAP_LOCAL_ONLY` | 403 | no | Groups can only be created from the coordinator computer itself. |
| `CSRF` | 403 | no | A browser request is missing a valid CSRF token. |
| `FORBIDDEN` | 403 | no | The caller is authenticated but not allowed to do this. |
| `INVALID_LOGIN` | 401 | no | The group, login or passphrase is incorrect. |
| `INVALID_RECOVERY` | 401 | no | The account recovery details are incorrect. |
| `INVITATION_INVALID` | 404 | no | The invitation is invalid, expired or already used. (DIFA groups; replaced in sprint 9.) |
| `LOGIN_TAKEN` | 409 | no | That login is already used in this group. (DIFA groups; replaced in sprint 9.) |
| `OWNER_REMOVAL` | 409 | no | The group owner cannot be removed before ownership is transferred. (DIFA groups; replaced in sprint 9.) |
| `RATE_LIMITED` | 429 | yes | Too many sign-in attempts; try again later. |
| `UNAUTHENTICATED` | 401 | no | No valid session, credential or device token. |

## Nodes and the agent

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `AGENT_PANIC` | — | yes | Agent: the agent crashed; a crash report was saved and is uploaded on its next start. |
| `CONFIG_INVALID` | — | no | A command-line option or configuration value is invalid. |
| `CONTROL_UNREACHABLE` | — | yes | Agent: the coordinator could not be reached, or its certificate did not match the pinned fingerprint. |
| `CRASH_REPORT_UPLOAD_FAILED` | — | yes | Agent: a saved crash report could not be uploaded; it stays on disk and is retried. |
| `DEVICE_REVOKED` | 403 | no | The node is no longer authorized; the agent stops all local work. |
| `DIAGNOSTICS_FAILED` | — | yes | Agent: a requested diagnostic bundle could not be built or uploaded. |
| `DIAGNOSTICS_NO_CONSENT` | 409 | no | The device's owner has not consented to diagnostics, so crash reports, diagnostic bundles and remote log levels are refused. |
| `ENROLLMENT_INVALID` | 404 | no | The enrollment code is invalid, expired or already used. |
| `ENROLL_FAILED` | — | yes | Agent: enrollment did not complete (the coordinator refused or the reply was invalid). |
| `HEARTBEAT_REJECTED` | — | yes | Agent: the coordinator answered a heartbeat with an unexpected error status. |
| `INVALID_DEVICE_KEY` | 400 | no | The device certificate sent at enrollment could not be parsed. |
| `LEASE_EXPIRED` | — | yes | Agent: no heartbeat succeeded within the lease, so assigned work was released. |
| `PROBE_FAILED` | — | yes | Agent: a hardware or decode probe failed; the node reports without that measurement. |
| `STATE_UNREADABLE` | — | no | Agent: the state file is missing or corrupt; enroll the node again. |
| `STATE_WRITE_FAILED` | — | no | Agent: the state file could not be written or restricted to its owner. |
| `TELEMETRY_BUFFER_FULL` | — | no | Agent: the telemetry buffer reached its size limit and dropped its oldest records; the count is reported. |
| `TELEMETRY_BUFFER_IO` | — | yes | Agent: the on-disk telemetry buffer could not be read or written. |
| `TELEMETRY_NO_CONSENT` | 403 | no | The device's owner has not consented to idle-data collection, so the coordinator refuses its telemetry. |
| `TELEMETRY_REJECTED` | — | no | Agent: the coordinator refused a telemetry batch as invalid; the batch was dropped and counted as lost. |
| `TELEMETRY_UPLOAD_FAILED` | — | yes | Agent: buffered telemetry could not be delivered; it stays on disk and is retried. |

## Workers and the tunnel

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `ENDPOINT_REFUSED` | — | no | A non-loopback llama.cpp worker would be used without the tunnel, so it was refused. |
| `MEMORY_CAP_EXCEEDED` | — | no | Agent: the worker used more memory than the owner's cap and was killed. It stays stopped until the cap is raised. |
| `NO_ENDPOINT` | — | no | No llama.cpp endpoint is configured for a node in the placement. |
| `OS_MEMORY_LIMIT_FAILED` | — | no | Agent: the operating-system memory limit for the worker could not be applied; the 250 ms watchdog still enforces the cap. |
| `TUNNEL_FAILED` | 503 | yes | Coordinator: the tunnel to a node's worker could not be opened or verified. |
| `TUNNEL_HANDSHAKE_FAILED` | — | no | Agent: a connection to the worker tunnel failed mutual-TLS verification and was refused. |
| `TUNNEL_HANDSHAKE_TIMEOUT` | — | yes | Agent: a tunnel connection did not finish its TLS handshake in time. |
| `TUNNEL_WORKER_UNREACHABLE` | — | yes | Agent: the tunnel accepted a connection but the local worker was not listening. |
| `WORKER_CRASHED` | — | yes | Agent: the worker exited on its own; a crash report with its exit status and last output was saved. |
| `WORKER_EXITED` | — | yes | Agent: the llama.cpp worker process exited on its own. |
| `WORKER_LOST` | 503 | yes | A worker or the inference server stopped responding during a request. |
| `WORKER_START_FAILED` | — | yes | Agent: the llama.cpp worker process could not be started. |

## Models

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `LICENSE_NOT_ACCEPTED` | 409 | no | A node's owner has not accepted the model's licence, so it refused to download it (D-10). |
| `MODEL_HASH_MISMATCH` | — | no | The model file failed SHA-256 verification. |
| `MODEL_MISSING` | — | no | The model file is not downloaded on the coordinator. |
| `MODEL_UNAVAILABLE` | 503 | yes | The model's deployment is not loaded or not ready yet. |
| `UNKNOWN_MODEL` | 400 | no | The model is not in the catalog. |
| `UNSUPPORTED_MODEL` | — | no | The model has no pinned model file for this backend. |

## Deployments, placement and prefetch

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `DEPLOYMENT_BUSY` | 409 | yes | The deployment cannot change while requests are queued or running. |
| `LOAD_FAILED` | — | yes | The inference server exited while loading the model. |
| `LOAD_TIMEOUT` | — | yes | The inference server did not become ready in time. |
| `NO_PREFETCH` | 404 | no | This node has no partition to download. |
| `PARTITION_MISMATCH` | — | yes | Weights on one or more nodes do not match the placement plan. |
| `PLACEMENT_INFEASIBLE` | 409 | no | No set of available nodes can hold the model under their caps and constraints. |
| `PLACEMENT_UNSUPPORTED` | — | no | The planned split cannot be expressed to llama.cpp. |
| `PREFETCH_ERROR` | — | yes | The coordinator hit an internal error while coordinating prefetch. |
| `PREFETCH_FAILED` | — | yes | A node failed to download its share of the model. |
| `PREFETCH_UNSUPPORTED` | 409 | no | This backend or model cannot prefetch partitions to nodes. |
| `RANGE_NOT_ASSIGNED` | 403 | no | The requested byte range is not part of this node's partition. |
| `RANGE_REQUIRED` | 416 | no | A model-file download must request exactly one assigned byte range. |

## Queue and generation

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `ACTIVE_DEADLINE` | — | yes | Generation ran past its deadline and was stopped. |
| `BACKEND_ERROR` | 503 | yes | The inference server returned an unexpected error status. |
| `CANCELLED` | — | no | The request was cancelled by the caller. |
| `CAPACITY` | — | yes | Not enough reserved memory is available to start the request right now. |
| `CONTEXT_TOO_LONG` | 422 | no | The conversation is longer than the model's context limit. |
| `COORDINATOR_RESTART` | — | yes | The coordinator restarted while the request was queued or running, so the request was ended. |
| `DEADLINE` | 504 | yes | The request did not finish within its overall deadline. |
| `QUEUE_FULL` | 429 | yes | The caller or the deployment has too many queued requests; retry later. |
| `WAIT_DEADLINE` | — | yes | The request waited in the queue too long and was not started. |

## Conversations

| Code | HTTP | Retry | Meaning |
|---|---|---|---|
| `INVALID_SELECTION` | 409 | no | Messages selected for a fork do not belong to the source conversation. |
| `PREVIEW_REQUIRED` | 409 | no | The fork preview must be confirmed before messages are copied. |
| `SINGLE_WRITER` | 403 | no | Shared conversations have one writer; fork to continue. |
| `VISIBILITY_CONFLICT` | 409 | no | The request's visibility does not match the conversation's. |
