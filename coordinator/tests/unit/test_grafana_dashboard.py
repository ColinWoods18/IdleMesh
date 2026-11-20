"""The committed Grafana dashboard (S3.6) is current and only queries endpoints the coordinator serves."""
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
GRAFANA = ROOT / "deploy" / "grafana"


def test_dashboard_is_generated_from_the_builder():
    r = subprocess.run([sys.executable, str(GRAFANA / "build_dashboard.py"), "--check"], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_panel_queries_a_real_metrics_route(app):
    routes = {r.path for r in app.routes}
    dash = json.loads((GRAFANA / "dashboards" / "idlemesh.json").read_text(encoding="utf-8"))
    assert dash["panels"]
    for p in dash["panels"]:
        for t in p["targets"]:
            assert t["datasource"]["uid"] == "idlemesh"
            assert t["url"].startswith("/api/v1/metrics/") and t["url"] in routes, (p["title"], t["url"])


def test_provisioned_data_source_trusts_only_the_coordinator_certificate():
    ds = yaml.safe_load((GRAFANA / "provisioning" / "datasources" / "idlemesh.yaml").read_text())["datasources"][0]
    assert ds["uid"] == "idlemesh"
    assert ds["jsonData"]["tlsAuthWithCACert"] is True and not ds["jsonData"].get("tlsSkipVerify")
    assert ds["secureJsonData"]["bearerToken"] == "${IDLEMESH_METRICS_KEY}"  # from .env, never committed
    compose = yaml.safe_load((GRAFANA / "docker-compose.yml").read_text())
    svc = compose["services"]["grafana"]
    assert svc["ports"] == ["127.0.0.1:3000:3000"]  # not exposed to the network
    assert ":" in svc["image"] and not svc["image"].endswith(":latest")
