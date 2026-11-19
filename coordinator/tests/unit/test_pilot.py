"""Operator helper: the bench's per-token fit and its check against the spec 7.2 prediction target."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("pilot", Path(__file__).resolve().parents[3] / "scripts" / "pilot.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def runs(fixed_s, ms_per_token, lengths):
    return [{"completion_tokens": n, "elapsed_s": fixed_s + n * ms_per_token / 1000} for n in lengths]


def test_fit_recovers_per_token_time_and_fixed_cost():
    out = pilot.prediction_check(runs(0.4, 50, [12, 20, 60, 64, 12, 20]), {"per_token_ms": 60, "rtt_source": "tunnel"})
    assert out["fit_ms_per_token"] == 50.0 and out["fit_fixed_s"] == 0.4
    assert out["prediction_error_pct"] == 20.0 and out["within_30pct"] and out["rtt_source"] == "tunnel"


def test_round_4_prediction_fails_the_target():
    out = pilot.prediction_check(runs(0.33, 51.2, [60, 12, 64, 20]), {"per_token_ms": 71.7})
    assert not out["within_30pct"] and out["prediction_error_pct"] == 40.0


def test_fit_needs_varied_lengths():
    assert "note" in pilot.prediction_check(runs(0.4, 50, [64, 64, 64]), {"per_token_ms": 50})


def test_pilot_trusts_only_the_coordinator_control_certificate(tmp_path, monkeypatch):
    import ssl

    from idlemesh_coord.transport import CoordinatorIdentity
    control = CoordinatorIdentity(str(tmp_path), "coordinator-control", "DIFA coordinator control")
    monkeypatch.setenv("IDLEMESH_CONTROL_CERT", str(control.cert_path))
    assert pilot.verify("http://127.0.0.1:8765") is True
    ctx = pilot.verify("https://127.0.0.1:8765")
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and not ctx.check_hostname
    assert ctx.cert_store_stats()["x509"] == 1  # only the coordinator's own certificate, no system CAs
