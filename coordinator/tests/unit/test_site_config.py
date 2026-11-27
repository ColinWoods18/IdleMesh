"""Site configuration file (S1.12)."""
import io
import json
import logging
from pathlib import Path

import pytest

from idlemesh_coord import obs, site_config
from idlemesh_coord.config import Settings

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "example.site.yaml"


def test_example_file_loads_and_validates():
    site = site_config.load(EXAMPLE)
    assert (site.site.org_id, site.site.site_id) == ("example-university", "main-campus")
    assert site.telemetry.tier == "operational" and site.idle.yield_level == 0.02
    assert [w.name for w in site.contribution.windows] == ["overnight", "daytime"]
    assert site.log_file() == str(EXAMPLE.parent / "logs" / "coordinator.log")


def test_settings_take_the_governor_section():
    s = Settings.from_site(site_config.load(EXAMPLE))
    assert (s.ramp_s, s.yield_level, s.max_cpu_fraction) == (1800, 0.02, 0.75)
    assert [w["name"] for w in s.contribution_windows] == ["overnight", "daytime"]
    assert s.contribution_windows[0]["start"] == "20:00" and s.contribution_windows[1]["max_level"] == 0.5


def test_settings_take_the_predictor_section():
    s = Settings.from_site(site_config.load(EXAMPLE))
    assert (s.predictor_window_days, s.predictor_horizons_min, s.predictor_default_horizon_min) == (28, (30, 60, 120), 60)
    assert (s.predictor_prior_strength, s.predictor_every_s) == (10, 3600)


def test_settings_take_queue_limits_and_site_identity():
    site = site_config.load(EXAMPLE)
    s = Settings.from_site(site, db_path="x.sqlite3")
    assert (s.org_id, s.site_id, s.per_member_queue, s.group_queue, s.active_generations) == \
        ("example-university", "main-campus", 2, 10, 1)
    assert s.db_path == "x.sqlite3"


def _write(tmp_path, text):
    p = tmp_path / "site.yaml"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("text, problem", [
    ("site: {org_id: u, site_id: s, name: n}\nqueu: {per_user: 2}\n", "queu"),          # typo in a section
    ("site: {org_id: u, site_id: s, name: n}\nqueue: {per_usr: 2}\n", "per_usr"),       # typo in a key
    ("site: {org_id: U Of X, site_id: s, name: n}\n", "org_id"),                         # bad identifier
    ("site: {org_id: u, site_id: s, name: n}\nfleet: {min_active_nodes: 9, max_active_nodes: 2}\n", "min_active"),
    ("site: {org_id: u, site_id: s, name: n}\ncontribution: {windows: [{name: w, days: [moon], start: '20:00', "
     "end: '07:00', max_level: 1}]}\n", "days"),
    ("site: {org_id: u, site_id: s, name: n}\ncontribution: {windows: [{name: w, days: [mon], start: '8pm', "
     "end: '07:00', max_level: 1}]}\n", "HH:MM"),
    ("- just\n- a list\n", "mapping"),
    ("queue: {per_user: 2}\n", "site"),                                                  # site section required
])
def test_mistakes_fail_loudly_with_the_key_named(tmp_path, text, problem):
    with pytest.raises(site_config.SiteConfigError, match=problem):
        site_config.load(_write(tmp_path, text))


def test_app_factory_applies_the_site_file(tmp_path, monkeypatch):
    from idlemesh_coord.api.app import app_from_env

    log_file = tmp_path / "logs" / "coordinator.log"
    cfg = _write(tmp_path, "site: {org_id: test-u, site_id: lab-1, name: Test lab}\n"
                           f"logging: {{level: info, file: {json.dumps(str(log_file))}}}\n"
                           "queue: {per_user: 3, total: 40}\n")
    monkeypatch.setenv(site_config.ENV, str(cfg))
    monkeypatch.setenv("IDLEMESH_DB", str(tmp_path / "db.sqlite3"))
    try:
        app = app_from_env()
        assert app.state.site.site.site_id == "lab-1"
        assert (app.state.settings.per_member_queue, app.state.settings.group_queue) == (3, 40)
        line = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
        assert line["message"] == "coordinator starting"
        assert (line["org_id"], line["site_id"]) == ("test-u", "lab-1")
    finally:
        obs._SITE.clear()
        root = logging.getLogger()
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)
        obs.setup_logging("warning", stream=io.StringIO())
        for h in list(root.handlers):
            root.removeHandler(h)
