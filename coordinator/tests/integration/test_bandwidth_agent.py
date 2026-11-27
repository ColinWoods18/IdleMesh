"""The built-in bandwidth test against a real agent's tunnel (S5.2).

The agent's `tunnel` command serves the same mutual-TLS listener as `run --tunnel-listen`. A loopback relay
caps the link (experiments/llamacpp-feasibility/netem_relay.py), so the test can check the figure against a
known bandwidth on one machine. With a real rpc-server behind the tunnel it also checks that RPC still
passes through untouched now that the agent reads each connection's first byte.
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from live import AGENT, RPC_SERVER, free_port

from idlemesh_coord.transport import CoordinatorIdentity, TunnelError, TunnelManager

RELAY = Path(__file__).resolve().parents[3] / "experiments" / "llamacpp-feasibility" / "netem_relay.py"

pytestmark = pytest.mark.skipif(not AGENT.exists(), reason="agent binary not built")


def _listening(port: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"nothing listening on {port}")


@pytest.fixture
def tunnel(tmp_path):
    coord = CoordinatorIdentity(str(tmp_path), "coord")
    dev = CoordinatorIdentity(str(tmp_path), "device")
    procs = []
    worker_port, tport = free_port(), free_port()
    if RPC_SERVER.exists():
        procs.append(subprocess.Popen([str(RPC_SERVER), "-H", "127.0.0.1", "-p", str(worker_port), "-d", "CPU"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen([str(AGENT), "tunnel", "--listen", f"127.0.0.1:{tport}", "--worker-port",
                                   str(worker_port), "--cert", str(dev.cert_path), "--key", str(dev.key_path),
                                   "--peer-cert", str(coord.cert_path)],
                                  stdout=open(tmp_path / "agent.log", "w"), stderr=subprocess.STDOUT))
    _listening(tport)
    tm = TunnelManager(coord)

    def relay(mbps: float, rtt_ms: float = 2) -> str:
        port = free_port()
        procs.append(subprocess.Popen([sys.executable, str(RELAY), "--listen", str(port), "--target", str(tport),
                                       "--rtt-ms", str(rtt_ms), "--mbps", str(mbps)], stdout=subprocess.DEVNULL))
        _listening(port)
        return f"127.0.0.1:{port}"
    try:
        yield tm, f"127.0.0.1:{tport}", dev.cert_pem, relay, tmp_path / "agent.log"
    finally:
        for p in procs:
            p.kill()
            p.wait()


@pytest.mark.parametrize("mbps", [50, 200])
def test_the_figure_matches_a_capped_link(tunnel, mbps):
    tm, _, cert, relay, _ = tunnel
    out = tm.measure_bandwidth(relay(mbps), cert, nbytes=4 << 20)
    assert abs(out["mbps"] - mbps) / mbps < 0.15, out
    assert out["mbps"] == min(out["up_mbps"], out["down_mbps"])


def test_a_stranger_cannot_run_the_test_and_the_agent_logs_what_it_served(tunnel, tmp_path):
    tm, direct, cert, _, log = tunnel
    other = TunnelManager(CoordinatorIdentity(str(tmp_path / "other"), "coord"))
    with pytest.raises(TunnelError):
        other.measure_bandwidth(direct, cert, nbytes=1 << 20)
    assert tm.measure_bandwidth(direct, cert, nbytes=2 << 20)["mbps"] > 100  # loopback
    deadline = time.monotonic() + 5
    while "served a bandwidth test" not in log.read_text() and time.monotonic() < deadline:
        time.sleep(0.1)
    text = log.read_text()
    assert "served a bandwidth test" in text and "rejected" in text  # the stranger, refused at the handshake


@pytest.mark.skipif(not RPC_SERVER.exists(), reason="llama.cpp rpc-server not built")
def test_rpc_still_passes_through_the_tunnel(tunnel):
    tm, direct, cert, _, _ = tunnel
    before = tm.measure_rtt(direct, cert, samples=8)
    tm.measure_bandwidth(direct, cert, nbytes=1 << 20)
    after = tm.measure_rtt(direct, cert, samples=8)
    assert before["samples"] == after["samples"] == 8
