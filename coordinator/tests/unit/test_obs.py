"""Structured logging and redaction rules (S1.6, S1.9)."""
import io
import json
import logging

from idlemesh_coord import obs


def _capture(level=logging.DEBUG):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(obs.JsonFormatter())
    lg = logging.getLogger("test.obs")
    lg.handlers[:] = [h]
    lg.setLevel(level)
    lg.propagate = False
    return lg, buf


def test_lines_are_json_with_ids_code_and_fields():
    lg, buf = _capture()
    with obs.bound(trace_id="t1", request_id="req_1", node_id="dev_1", deployment_id="dep_1"):
        obs.log(lg, logging.WARNING, "worker lost", error_code="WORKER_LOST", attempt=2)
    line = json.loads(buf.getvalue())
    assert line["level"] == "warning" and line["component"] == "coordinator" and line["message"] == "worker lost"
    assert (line["trace_id"], line["request_id"], line["node_id"], line["deployment_id"]) == \
        ("t1", "req_1", "dev_1", "dep_1")
    assert line["error_code"] == "WORKER_LOST" and line["fields"] == {"attempt": 2}
    assert line["ts"].endswith("+00:00") and line["version"]


def test_ids_do_not_leak_outside_their_block():
    with obs.bound(request_id="req_x"):
        assert obs.request_id.get() == "req_x"
    assert obs.request_id.get() is None


def test_exceptions_are_logged_with_their_traceback():
    lg, buf = _capture()
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("failed")
    assert "ValueError: boom" in json.loads(buf.getvalue())["exception"]


SECRETS = {
    "pem": "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n-----END PRIVATE KEY-----",
    "bearer": "Authorization: Bearer s3cr3t-T0ken_value.abc",
    "api_key": "used key difa_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "json_secret": '{"device_token": "tok-123-abc", "passphrase": "correct horse"}',
    "text_secret": "enroll code=K7Q2-PLMX retry",
    "content": '{"role": "user", "content": "my private question"}',
}


def test_redact_removes_every_secret_form():
    for name, raw in SECRETS.items():
        out = obs.redact(raw)
        for needle in ("MIGHAgEA", "s3cr3t-T0ken", "AbCdEfGhIjKl", "tok-123-abc", "correct horse", "K7Q2-PLMX",
                       "my private question"):
            assert needle not in out, (name, out)


def test_redact_hides_user_names_in_home_paths():
    for raw in (r"C:\Users\alice\.cache\idlemesh", "/home/alice/.cache", "/Users/alice/Library",
                json.dumps(r"C:\Users\alice\x")):
        assert "alice" not in obs.redact(raw), raw


def test_redact_keeps_ordinary_text():
    raw = "heartbeat ok: paused=false power=ac cap=4.00GB rtt=3.1ms error_code=WORKER_LOST"
    assert obs.redact(raw) == raw


def test_secret_and_content_fields_are_dropped_from_structured_fields():
    lg, buf = _capture()
    obs.log(lg, logging.INFO, "event", token="tok-abc-123", prompt="what is my grade", tokens_out=12,
            nested={"content": "private reply", "ok": True})
    text = buf.getvalue()
    assert "tok-abc-123" not in text and "what is my grade" not in text and "private reply" not in text
    fields = json.loads(text)["fields"]
    assert fields["tokens_out"] == 12 and fields["nested"]["ok"] is True


def test_rotating_file_is_size_capped(tmp_path):
    log_file = tmp_path / "logs" / "coordinator.log"
    obs.setup_logging("info", str(log_file), max_bytes=2000, backups=2, stream=io.StringIO())
    try:
        lg = logging.getLogger("idlemesh_coord.test")
        for i in range(200):
            lg.info("line %d with some padding to fill the file quickly", i)
        files = sorted(p.name for p in log_file.parent.iterdir())
        assert files == ["coordinator.log", "coordinator.log.1", "coordinator.log.2"]
        assert all(p.stat().st_size <= 2200 for p in log_file.parent.iterdir())
        for line in log_file.read_text(encoding="utf-8").splitlines():
            json.loads(line)
    finally:
        for h in list(logging.getLogger().handlers):
            h.close()
            logging.getLogger().removeHandler(h)
