"""A real agent finds llama-bench beside its worker, downloads the probe model from the coordinator, verifies it,
and reuses it on the next start (S5.6). Needs the llama.cpp build and the reference model (see live.py).

The benchmark itself waits until the governor would let the node contribute, which depends on whether someone is
using this machine, so it is not asserted here; the bench call is the unchanged `probe::decode_probe`."""
import subprocess
import time

import pytest
from live import AGENT, EXE, HAVE_STACK, MODEL_FILE, RPC_SERVER, coordinator, enroll_agent, free_port, \
    owner_client, stop_agents, until

from idlemesh_coord.catalog import MODEL_FILES, PROBE_MODEL
from idlemesh_coord.runtime.fake import FakeRuntime

BENCH = RPC_SERVER.parent / f"llama-bench{EXE}"
pytestmark = pytest.mark.skipif(not (HAVE_STACK and BENCH.exists()),
                                reason="needs the agent build, llama.cpp with llama-bench, and the reference model")


class _Runtime(FakeRuntime):
    """The fake runtime, able to serve the real reference model file."""

    def model_path(self, name):
        assert name == PROBE_MODEL
        return MODEL_FILE


def _run(state, log):
    return subprocess.Popen([str(AGENT), "run", "--state", str(state), "--worker-bin", str(RPC_SERVER),
                             "--worker-port", str(free_port())], stdout=open(log, "w"), stderr=subprocess.STDOUT)


def test_agent_fetches_verifies_and_reuses_the_probe_model(tmp_path):
    meta = MODEL_FILES[PROBE_MODEL]
    with coordinator(tmp_path, _Runtime()) as (base, app):
        c, _ = owner_client(base)
        state = enroll_agent(c, base, tmp_path, "HP EliteBook", "hp-elitebook")
        cached = tmp_path / "difa-cache" / "probe" / meta["file"]
        marker = cached.with_name(meta["file"] + ".sha256")

        log = tmp_path / "agent-1.log"
        agent = _run(state, log)
        try:
            until(lambda: marker.exists(), 300, "the probe model to download and verify")
        finally:
            stop_agents([agent])
        text = log.read_text(errors="replace")
        assert "downloading the decode-probe model" in text
        assert "PROBE_FAILED" not in text
        assert cached.stat().st_size == MODEL_FILE.stat().st_size
        assert marker.read_text().strip() == meta["sha256"]

        # Second start: the verified copy is reused, nothing is downloaded again.
        log2 = tmp_path / "agent-2.log"
        agent = _run(state, log2)
        try:
            until(lambda: "heartbeat" in log2.read_text(errors="replace").lower()
                  or log2.stat().st_size > 2000, 60, "the agent's first heartbeat")
            time.sleep(8)  # several heartbeats, each carrying the offer while no probe result exists
        finally:
            stop_agents([agent])
        assert "downloading the decode-probe model" not in log2.read_text(errors="replace")
