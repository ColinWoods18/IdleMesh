# Grafana for IdleMesh (S3.6)

One dashboard, **IdleMesh overview**, with:

- the node list (hardware profile and backends);
- idle share by hour of the week;
- node states over time;
- node health: CPU, GPU, memory, temperature, cache disk and tunnel errors;
- error rates by error code from all nodes and the coordinator.

Grafana runs in Docker and reads the coordinator's read-only metrics API (`/api/v1/metrics/...`). It never opens
the database.

## How it fits together

- **Key:** Grafana authenticates with an administrator's **metrics key**. This kind of API key works on
  `/api/v1/metrics/` and nowhere else, so even a leaked key cannot change anything.
- **TLS:** Grafana verifies the coordinator's own self-signed certificate. The file is mounted read-only, and
  the name `difa-coordinator` points at the Docker host.
- **Privacy:** telemetry is stored under pseudonymous node keys (D-16). The coordinator maps them back to device
  names for the key owner's group only.

## Run it

1. Start the coordinator with HTTPS (the default):
   `python scripts/dev_server.py --site-config config/example.site.yaml`.
2. Create a metrics key as a group administrator (signed in with `scripts/pilot.py`):

   ```bash
   python scripts/pilot.py metrics-key      # prints the key once
   ```

3. Create `deploy/grafana/.env`. It is ignored by git, so the key stays out of the repository:

   ```ini
   IDLEMESH_METRICS_KEY=difa_...
   IDLEMESH_CONTROL_CERT=C:\Users\you\.local\share\difa\coordinator-control.crt
   # IDLEMESH_URL=https://difa-coordinator:8765   (the default)
   ```

4. Start Grafana from this folder, then open http://127.0.0.1:3000. The first login is admin/admin, and Grafana
   asks you to change the password.

   ```bash
   docker compose --env-file .env up -d
   ```

Stop it with `docker compose down`. Add `-v` to also delete Grafana's own settings.

## Change the dashboard

Edit `build_dashboard.py`, then run `python deploy/grafana/build_dashboard.py`. A test fails if the committed
`dashboards/idlemesh.json` is out of date or queries an endpoint the coordinator does not have.

## Known limits

- Hour-of-week figures use the coordinator's local time zone.
- Error counts are counts of log events that carry an error code. They are not request counts, and one failure
  can log more than one event.
- Grafana updates its bundled plugins at startup. The Infinity plugin is pinned (4.1.5).
