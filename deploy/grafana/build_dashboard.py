"""Build the IdleMesh Grafana dashboard (S3.6) from readable panel definitions.

    python deploy/grafana/build_dashboard.py           # write dashboards/idlemesh.json
    python deploy/grafana/build_dashboard.py --check   # exit 1 if the committed file is out of date (tests)

Every panel reads one coordinator metrics endpoint (/api/v1/metrics/...) through the Infinity data source
provisioned as "idlemesh". Edit this file rather than the dashboard in Grafana's UI.
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "idlemesh.json"
DS = {"type": "yesoreyeram-infinity-datasource", "uid": "idlemesh"}
RANGE = [{"key": "from_ms", "value": "${__from}"}, {"key": "to_ms", "value": "${__to}"}]

STATE_COLORS = {"idle": "green", "active": "blue", "sleeping": "#8f8f8f", "serving": "purple", "draining": "orange"}


def query(path: str, columns: list[tuple[str, str, str]], params=RANGE) -> dict:
    """An Infinity JSON query of one metrics endpoint. columns: (selector, title, type)."""
    return {
        "refId": "A", "datasource": DS, "type": "json", "source": "url", "parser": "backend", "format": "table",
        "url": path, "url_options": {"method": "GET", "data": "", "params": list(params)},
        "root_selector": "", "filters": [],
        "columns": [{"selector": s, "text": t, "type": ty} for s, t, ty in columns],
    }


def by_node(field: str = "node") -> dict:
    """Split rows into one series per node, labelled with the node's name."""
    return {"id": "partitionByValues", "options": {"fields": [field], "keepFields": False,
                                                   "naming": {"asLabels": True}}}


PANEL_ID = iter(range(1, 1000))


def panel(kind: str, title: str, grid: tuple[int, int, int, int], targets: list[dict], *, description: str = "",
          transformations=None, defaults=None, overrides=None, options=None) -> dict:
    x, y, w, h = grid
    return {
        "id": next(PANEL_ID), "type": kind, "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
        "transformations": transformations or [],
        "fieldConfig": {"defaults": defaults or {}, "overrides": overrides or []},
        "options": options or {},
    }


def health_series(title: str, field: str, label: str, grid, unit: str, *, description: str, **kw) -> dict:
    defaults = {"unit": unit, "displayName": "${__field.labels.node}",
                "custom": {"lineWidth": 2, "fillOpacity": 8, "spanNulls": 180000, "showPoints": "never"}}
    defaults.update(kw)
    return panel("timeseries", title, grid,
                 [query("/api/v1/metrics/health", [("ts", "Time", "timestamp"), ("node", "node", "string"),
                                                   (field, label, "number")])],
                 description=description, transformations=[by_node()], defaults=defaults,
                 options={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}})


def row(title: str, y: int) -> dict:
    return {"id": next(PANEL_ID), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": [], "targets": []}


def network_panels(y: int) -> list[dict]:
    """The network view (S5.5): the cluster map, each node's link, figures over time and probe health."""
    graph_q = [dict(query("/api/v1/metrics/network-graph", [
        ("id", "id", "string"), ("title", "title", "string"), ("subtitle", "subtitle", "string"),
        ("mainstat", "mainstat", "string"), ("secondarystat", "secondarystat", "string"),
        ("color", "color", "string")], params=[{"key": "part", "value": "nodes"}]), refId="nodes"),
        dict(query("/api/v1/metrics/network-graph", [
            ("id", "id", "string"), ("source", "source", "string"), ("target", "target", "string"),
            ("mainstat", "mainstat", "string"), ("secondarystat", "secondarystat", "string")],
            params=[{"key": "part", "value": "edges"}]), refId="edges")]
    return [
        row("Network (S5)", y),
        panel("nodeGraph", "Network map", (0, y + 1, 14, 12), graph_q,
              description="Nodes coloured by cluster (D-22, D-24; clusters never span subnets), each joined to the "
                          "coordinator by its own link (RTT, and bandwidth once tested, S5.2), and to other nodes by "
                          "links that join a cluster. Compare it with the building layout (Sprint 5 demo).",
              options={"nodes": {"mainStatUnit": "", "secondaryStatUnit": ""}, "edges": {}}),
        panel("table", "Nodes and links", (14, y + 1, 10, 12), [query("/api/v1/metrics/network-nodes", [
            ("name", "Node", "string"), ("subnet", "Subnet", "string"), ("cluster", "Cluster", "string"),
            ("cluster_size", "Size", "number"), ("coordinator_rtt_ms", "RTT (ms)", "number"),
            ("link_mbps", "Link (Mbps)", "number"), ("link_measured_at", "Tested", "timestamp")], params=[])],
            description="Each node's subnet and cluster, its RTT to the coordinator from heartbeats (S5.1) and its "
                        "link bandwidth from the latest tunnel test (S5.2). An empty Link cell means never tested: "
                        "placement then leaves that link's traffic out (S5.3)."),
        panel("timeseries", "Link bandwidth to the coordinator", (0, y + 13, 8, 8), [query(
            "/api/v1/metrics/network-links", [("ts", "Time", "timestamp"), ("node", "node", "string"),
                                              ("bandwidth_mbps", "Mbps", "number")],
            params=RANGE + [{"key": "method", "value": "tunnel_test"}])],
            description="Tunnel bandwidth tests (S5.2): the slower of the two directions. Weekly in off-hours, and "
                        "at once on a node's first worker start or a network change.",
            transformations=[by_node()],
            defaults={"unit": "Mbits", "min": 0, "displayName": "${__field.labels.node}",
                      "custom": {"drawStyle": "points", "pointSize": 6, "showPoints": "always"}},
            options={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}),
        panel("timeseries", "RTT to the coordinator", (8, y + 13, 8, 8), [query(
            "/api/v1/metrics/network-links", [("ts", "Time", "timestamp"), ("node", "node", "string"),
                                              ("rtt_ms_p50", "RTT", "number")],
            params=RANGE + [{"key": "method", "value": "heartbeat"}])],
            description="Heartbeat RTT, recorded only when it changes or once a day (S5.1, D-25), so points are "
                        "sparse on a steady network and step at a change.",
            transformations=[by_node()],
            defaults={"unit": "ms", "min": 0, "displayName": "${__field.labels.node}",
                      "custom": {"lineInterpolation": "stepAfter", "showPoints": "always", "pointSize": 5,
                                 "spanNulls": True}},
            options={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}),
        panel("timeseries", "Probe health", (16, y + 13, 8, 8), [query(
            "/api/v1/metrics/errors", [("ts", "Time", "timestamp"), ("code", "code", "string"),
                                       ("count", "count", "number")],
            params=RANGE + [{"key": "codes", "value": "network"}])],
            description="Failed and refused bandwidth tests, degraded links and tunnel faults, by error code "
                        "(docs/error-codes.md). LINK_DEGRADED: bandwidth below half the last test, or RTT up by "
                        "more than probes.rtt_change_pct.",
            transformations=[{"id": "groupBy", "options": {"fields": {
                "Time": {"aggregations": [], "operation": "groupby"}, "code": {"aggregations": [], "operation": "groupby"},
                "count": {"aggregations": ["sum"], "operation": "aggregate"}}}},
                by_node("code")],
            defaults={"displayName": "${__field.labels.code}", "unit": "short", "min": 0, "decimals": 0,
                      "custom": {"drawStyle": "bars", "fillOpacity": 70, "stacking": {"mode": "normal"}}},
            options={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}),
    ]


def build() -> dict:
    hours = [f"{h:02d}" for h in range(24)]
    panels = [
        panel("table", "Nodes", (0, 0, 24, 7), [query("/api/v1/metrics/nodes", [
            ("name", "Node", "string"), ("status", "Status", "string"), ("node_state", "State", "string"),
            ("os", "OS", "string"), ("cpu", "CPU", "string"), ("cpu_threads", "Threads", "number"),
            ("ram_gb", "RAM (GB)", "number"), ("unified_memory", "Unified", "boolean"), ("gpus", "GPUs", "string"),
            ("vram_gb", "VRAM (GB)", "number"), ("backends", "Backends", "string"),
            ("idle_source", "Idle from", "string"), ("power", "Power", "string"),
            ("last_seen", "Last heartbeat", "timestamp")], params=[])],
            description="Every enrolled node with its hardware profile (S3.3) and inference backends (S3.4).",
            overrides=[{"matcher": {"id": "byName", "options": "Status"}, "properties": [
                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                {"id": "mappings", "value": [{"type": "value", "options": {
                    "online": {"color": "green", "index": 0}, "offline": {"color": "red", "index": 1},
                    "disabled": {"color": "#8f8f8f", "index": 2}}}]}]},
                {"matcher": {"id": "byName", "options": "State"}, "properties": [
                    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                    {"id": "mappings", "value": [{"type": "value", "options": {
                        k: {"color": c, "index": i} for i, (k, c) in enumerate(STATE_COLORS.items())}}]}]}]),

        panel("table", "Idle share by hour of the week", (0, 7, 12, 9), [query(
            "/api/v1/metrics/idle-heatmap", [("day", "Day", "string")] + [(h, h, "number") for h in hours])],
            description="Share of idle samples (no input for idle_after_s, or locked) by local day and hour, across "
                        "all nodes, over the dashboard's time range. Empty cells had no samples.",
            defaults={"unit": "percentunit", "min": 0, "max": 1, "decimals": 0,
                      "color": {"mode": "continuous-RdYlGr"},
                      "custom": {"cellOptions": {"type": "color-background", "mode": "gradient"}, "width": 44,
                                 "align": "center"}},
            overrides=[{"matcher": {"id": "byName", "options": "Day"}, "properties": [
                {"id": "custom.cellOptions", "value": {"type": "auto"}}, {"id": "custom.width", "value": 60}]}],
            options={"showHeader": True, "cellHeight": "sm"}),

        panel("state-timeline", "Node states", (12, 7, 12, 9), [query(
            "/api/v1/metrics/states", [("ts", "Time", "timestamp"), ("node", "node", "string"),
                                       ("state", "state", "string")])],
            description="Each node's state from its idle samples (S2.5). Gaps are sleep or no data.",
            transformations=[by_node()],
            defaults={"displayName": "${__field.labels.node}", "color": {"mode": "thresholds"},
                      "mappings": [{"type": "value", "options": {
                          k: {"color": c, "index": i, "text": k} for i, (k, c) in enumerate(STATE_COLORS.items())}}],
                      "custom": {"fillOpacity": 80, "lineWidth": 0}},
            options={"mergeValues": True, "showValue": "never", "rowHeight": 0.8,
                     "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}}),

        health_series("CPU use", "cpu_util", "CPU", (0, 16, 8, 8), "percentunit", min=0, max=1,
                      description="Whole-machine CPU use per node (S3.5)."),
        health_series("GPU use", "gpu_util", "GPU", (8, 16, 8, 8), "percentunit", min=0, max=1,
                      description="Mean NVIDIA GPU use per node. Nodes without an NVIDIA GPU report none."),
        health_series("Memory used", "mem_used_gb", "Memory", (16, 16, 8, 8), "decgbytes", min=0,
                      description="System RAM in use per node."),
        health_series("Temperature", "temp_c", "Temperature", (0, 24, 8, 8), "celsius",
                      description="Hottest CPU or GPU sensor the OS exposes; absent where it exposes none."),
        health_series("Model cache disk free", "cache_disk_free_gb", "Free", (8, 24, 8, 8), "decgbytes", min=0,
                      description="Free space on the disk holding each node's model cache."),
        health_series("Tunnel errors (since agent start)", "tunnel_errors", "Tunnel errors", (16, 24, 8, 8),
                      "short", min=0, decimals=0,
                      description="Counters count from the agent's start; a drop means the agent restarted "
                                  "(see agent uptime), never a silent reset."),

        panel("timeseries", "Errors by code", (0, 32, 16, 9), [query(
            "/api/v1/metrics/errors", [("ts", "Time", "timestamp"), ("code", "code", "string"),
                                       ("count", "count", "number")])],
            description="Warnings and errors per error code (contracts/error-codes.json) from all nodes and the "
                        "coordinator, in 5-minute buckets.",
            transformations=[{"id": "groupBy", "options": {"fields": {
                "Time": {"aggregations": [], "operation": "groupby"}, "code": {"aggregations": [], "operation": "groupby"},
                "count": {"aggregations": ["sum"], "operation": "aggregate"}}}},
                by_node("code")],
            defaults={"displayName": "${__field.labels.code}", "unit": "short", "min": 0, "decimals": 0,
                      "custom": {"drawStyle": "bars", "fillOpacity": 70, "stacking": {"mode": "normal"},
                                 "lineWidth": 1}},
            options={"legend": {"displayMode": "table", "placement": "right", "calcs": ["sum"]},
                     "tooltip": {"mode": "multi"}}),

        panel("table", "Errors by code and source", (16, 32, 8, 9), [query(
            "/api/v1/metrics/errors", [("code", "Code", "string"), ("source", "Source", "string"),
                                       ("count", "Count", "number")])],
            description="Totals over the dashboard's time range. Look a code up in docs/error-codes.md.",
            transformations=[{"id": "groupBy", "options": {"fields": {
                "Code": {"aggregations": [], "operation": "groupby"}, "Source": {"aggregations": [], "operation": "groupby"},
                "Count": {"aggregations": ["sum"], "operation": "aggregate"}}}},
                {"id": "sortBy", "options": {"sort": [{"field": "Count (sum)", "desc": True}]}}]),

        panel("timeseries", "Contribution level", (0, 41, 12, 8), [query(
            "/api/v1/metrics/states", [("ts", "Time", "timestamp"), ("node", "node", "string"),
                                       ("contribution_level", "Level", "number")])],
            description="The contribution governor's level per node (S4.9, D-12): it ramps up with idle time, is "
                        "capped by time windows and owner settings, and drops to the yield level within about a "
                        "second of user input.",
            transformations=[by_node()],
            defaults={"unit": "percentunit", "min": 0, "max": 1, "displayName": "${__field.labels.node}",
                      "custom": {"lineWidth": 2, "fillOpacity": 10, "lineInterpolation": "stepAfter",
                                 "showPoints": "never"}},
            options={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}),

        panel("table", "Crash reports", (12, 41, 12, 8), [query(
            "/api/v1/metrics/crashes", [("ts", "Time", "timestamp"), ("node", "Node", "string"),
                                        ("kind", "Kind", "string"), ("summary", "Summary", "string"),
                                        ("version", "Version", "string"), ("build_hash", "Build", "string"),
                                        ("crash_id", "Crash ID", "string")])],
            description="Agent panics and worker crashes (S4.3), newest first. The full report (backtrace, last "
                        "worker output, redacted config) is at /api/v1/groups/{group}/devices/{device}/crash-reports/"
                        "{crash_id}."),
    ]
    panels += network_panels(49)
    return {
        "uid": "idlemesh-overview", "title": "IdleMesh overview", "tags": ["idlemesh"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1, "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"}, "panels": panels,
        "templating": {"list": []}, "annotations": {"list": []},
        "description": "Generated by deploy/grafana/build_dashboard.py (S3.6). Do not edit in the UI.",
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            sys.exit(f"{OUT} is out of date: run python deploy/grafana/build_dashboard.py")
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {OUT}")
