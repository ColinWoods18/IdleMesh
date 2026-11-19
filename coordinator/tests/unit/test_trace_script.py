"""scripts/trace.py joins coordinator and agent logs for one request."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("trace_script", ROOT / "scripts" / "trace.py")
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)


def _write(path, lines):
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    return str(path)


def test_follows_a_request_from_coordinator_to_agent_and_worker(tmp_path):
    coord = _write(tmp_path / "coord.log", [
        {"ts": "2026-11-19T10:00:00.000+00:00", "component": "coordinator", "message": "request admitted",
         "request_id": "req_1", "deployment_id": "dep_1", "trace_id": "t-admit"},
        {"ts": "2026-11-19T10:00:00.001+00:00", "component": "coordinator", "message": "POST /api/v1/requests 202",
         "trace_id": "t-admit"},
        {"ts": "2026-11-19T10:00:04.000+00:00", "component": "coordinator", "message": "request completed",
         "request_id": "req_1", "deployment_id": "dep_1"},
        {"ts": "2026-11-19T10:00:02.000+00:00", "component": "coordinator", "message": "other request",
         "request_id": "req_2", "deployment_id": "dep_1"},
    ])
    agent = _write(tmp_path / "agent.log", [
        {"ts": "2026-11-19T10:00:01.000+00:00", "component": "agent", "node_id": "dev_a", "message": "serving requests",
         "request_ids": ["req_1"], "deployment_id": "dep_1"},
        {"ts": "2026-11-19T10:00:02.500+00:00", "component": "agent", "node_id": "dev_a", "message": "rpc line",
         "deployment_id": "dep_1"},
        {"ts": "2026-11-19T11:00:00.000+00:00", "component": "agent", "node_id": "dev_a", "message": "much later",
         "deployment_id": "dep_1"},
    ])
    lines = trace.read([coord, agent])
    plain = [x["message"] for x in trace.select(lines, "req_1", None)]
    assert plain == ["request admitted", "POST /api/v1/requests 202", "serving requests", "request completed"]
    ctx = [x["message"] for x in trace.select(lines, "req_1", 5.0)]
    assert "rpc line" in ctx and "other request" in ctx and "much later" not in ctx
    assert trace.select(lines, "req_missing", 5.0) == []
    assert "agent@dev_a" in trace.show(trace.select(lines, "req_1", None)[2])
