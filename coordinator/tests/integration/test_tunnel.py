"""Remote workers reached only over mutual TLS (DEV 08 interim; spec 6.2 and 6.4).

Real agents run real llama.cpp rpc-server workers. One agent serves its worker through the mTLS tunnel;
the coordinator must route that stage through a loopback forwarder, generate correctly, and the tunnel
must refuse clients that do not hold the pinned coordinator certificate. Synthetic prompts only.
"""
import json
import socket
import ssl
import subprocess

import pytest
from live import (AGENT, BIN, RPC_SERVER, HAVE_STACK, MODELS, all_ready, coordinator, enroll_agent, free_port, mac_heartbeat,
                  owner_client, start_agent, stop_agents, until)

from idlemesh_coord.runtime.fake import FakeRuntime
from idlemesh_coord.runtime.llamacpp import LlamaCppRuntime
from idlemesh_coord.transport import CoordinatorIdentity

pytestmark = pytest.mark.skipif(not HAVE_STACK, reason="agent binary, llama.cpp build, or model not present")


def test_agent_refuses_to_expose_the_plaintext_worker(tmp_path):
    with coordinator(tmp_path, FakeRuntime(0)) as (base, _):
        c, _ = owner_client(base)
        state = enroll_agent(c, base, tmp_path, "HP EliteBook", "hp-elitebook")
        saved = json.loads(state.read_text())
        assert saved["tunnel_cert_pem"].startswith("-----BEGIN CERTIFICATE-----")
        assert saved["coordinator_cert_pem"].startswith("-----BEGIN CERTIFICATE-----")
        out = subprocess.run([str(AGENT), "run", "--state", str(state), "--worker-bin", str(RPC_SERVER),
                              "--worker-host", "0.0.0.0", "--beats", "1"], capture_output=True, text=True, timeout=30)
        assert out.returncode != 0 and "refusing to expose the unencrypted llama.cpp worker" in out.stderr


def _intruder_attempt(port: int, identity: CoordinatorIdentity | None, device_cert: str) -> bool:
    """True if a client got any bytes through the tunnel (it must not)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.load_verify_locations(cadata=device_cert)
    if identity:
        ctx.load_cert_chain(identity.cert_path, identity.key_path)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with ctx.wrap_socket(raw) as s:
                s.sendall(b"\x00" * 16)  # TLS 1.3 reports a rejected client certificate on first I/O
                return bool(s.recv(16))
    except (ssl.SSLError, ConnectionError, OSError):
        return False


def test_deployment_runs_through_the_tunnel_and_refuses_strangers(tmp_path):
    rt = LlamaCppRuntime(str(BIN), str(MODELS), "Mac Studio", {}, log_dir=str(tmp_path))
    agents, logs = [], {}
    with coordinator(tmp_path, rt) as (base, app):
        c, gid = owner_client(base)
        with mac_heartbeat(c, base):
            try:
                hp_state = enroll_agent(c, base, tmp_path, "HP EliteBook", "hp-elitebook")
                vm_state = enroll_agent(c, base, tmp_path, "Ubuntu VM", "ubuntu-vm")
                tport = free_port()
                logs["hp"], logs["vm"] = tmp_path / "hp.log", tmp_path / "vm.log"
                agents.append(start_agent(hp_state, logs["hp"], "--worker-port", str(free_port()),
                                          "--worker-threads", "4", "--tunnel-listen", f"127.0.0.1:{tport}"))
                agents.append(start_agent(vm_state, logs["vm"], "--worker-port", str(free_port()),
                                          "--worker-threads", "2"))
                until(lambda: all_ready(c, gid, 3), 30, "three ready devices")

                r = c.post(f"/api/v1/groups/{gid}/deployment", json={"model": "qwen2.5-0.5b-q8", "require_all": True})
                assert r.status_code == 200, r.text
                body = r.json()
                stages = {s["name"]: s for s in body["explanation"]["stages"]}
                hp, vm = stages["HP EliteBook"], stages["Ubuntu VM"]
                assert hp["transport"] == "mtls" and hp["endpoint"] == f"127.0.0.1:{tport}"
                assert hp["rpc_endpoint"].startswith("127.0.0.1:") and hp["rpc_endpoint"] != hp["endpoint"]
                assert vm["transport"] == "plaintext" and "rpc_endpoint" not in vm
                # The HP's round trip was timed through its tunnel with real RPC commands before loading.
                assert hp["rtt_source"] == "tunnel" and hp["rtt_measurement"]["samples"] == 16, hp
                assert hp["rtt_ms"] == hp["rtt_measurement"]["mean_ms"] and "planned_rtt_ms" in hp
                assert vm["rtt_source"] == "heartbeat" and "rtt_measurement" not in vm
                expl = body["explanation"]
                assert expl["planned_latency_budget"]["rtt_source"] == "heartbeat"
                assert expl["latency_budget"]["rtt_source"] == "heartbeat+tunnel"
                assert all(p["model_mib"] > 0 for p in body["loaded"]["partition_map"])

                req = c.post("/api/v1/requests", json={"model": "group-default", "visibility": "private",
                                                       "max_output_tokens": 16,
                                                       "messages": [{"role": "user", "content": "Count to five."}]})
                status = until(lambda: (s := c.get(req.json()["status_url"]).json())["status"] in
                               ("completed", "failed") and s, 90, "answer")
                assert status["status"] == "completed", status
                assert app.state.tunnels.connections(body["deployment_id"]) >= 1

                # Anything without the pinned coordinator certificate is refused before reaching the worker.
                device_cert = json.loads(hp_state.read_text())["tunnel_cert_pem"]
                stranger = CoordinatorIdentity(str(tmp_path / "stranger"))
                assert not _intruder_attempt(tport, stranger, device_cert)
                assert not _intruder_attempt(tport, None, device_cert)
                until(lambda: logs["hp"].read_text().count("tunnel: rejected") >= 2, 10, "rejections logged")
            finally:
                stop_agents(agents)
    for name, log in logs.items():
        assert "agent stopping: worker halted" in log.read_text(), name
