"""store.py: schema creation, transactions, group boundaries, branches and messages."""
import sqlite3
from datetime import timedelta

import pytest

from idlemesh_coord.store import SCHEMA_VERSION, Store, iso, now, parse


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "s.sqlite3"))


def _group(store, gid="g1"):
    with store.tx() as c:
        c.execute("INSERT INTO groups (id, display_name, fingerprint, created_at) VALUES (?,?,?,?)",
                  (gid, "G", "FP", iso(now())))
        c.execute("INSERT INTO members (id, group_id, login, display_name, pass_hash, recovery_verifier, role,"
                  " status, joined_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (f"m_{gid}", gid, "alice", "Alice", "h", "v", "owner", "active", iso(now())))


def test_new_database_is_at_the_current_schema_version(store):
    assert store.one("SELECT version FROM schema_version")[0] == SCHEMA_VERSION
    tables = {r["name"] for r in store.all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"groups", "members", "devices", "requests", "idle_samples", "node_events", "audit"} <= tables


def test_reopening_keeps_data_and_does_not_duplicate_the_version_row(tmp_path):
    path = str(tmp_path / "s.sqlite3")
    _group(Store(path))
    again = Store(path)
    assert len(again.all("SELECT * FROM schema_version")) == 1
    assert again.one("SELECT id FROM groups")["id"] == "g1"


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.tx() as c:
            c.execute("INSERT INTO groups (id, display_name, fingerprint, created_at) VALUES ('x','X','F','t')")
            raise RuntimeError("boom")
    assert store.one("SELECT * FROM groups WHERE id = 'x'") is None


def test_foreign_keys_are_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        with store.tx() as c:
            c.execute("INSERT INTO members (id, group_id, login, display_name, pass_hash, recovery_verifier, role,"
                      " status, joined_at) VALUES ('m','no-such-group','l','L','h','v','member','active','t')")


def test_login_is_unique_within_a_group_only(store):
    _group(store, "g1")
    _group(store, "g2")  # same login "alice" in another group is fine
    with pytest.raises(sqlite3.IntegrityError):
        with store.tx() as c:
            c.execute("INSERT INTO members (id, group_id, login, display_name, pass_hash, recovery_verifier, role,"
                      " status, joined_at) VALUES ('m2','g1','alice','A2','h','v','member','active','t')")


def test_messages_are_numbered_in_order_per_branch(store):
    _group(store)
    with store.tx() as c:
        store.create_branch(c, "b1", "g1", "m_g1", "private", "T", 30)
        store.create_branch(c, "b2", "g1", "m_g1", "private", "T", 30)
        store.add_message(c, "x1", "b1", "m_g1", "user", "one")
        store.add_message(c, "y1", "b2", "m_g1", "user", "other branch")
        store.add_message(c, "x2", "b1", None, "assistant", "two", "incomplete")
    rows = store.branch_messages("b1")
    assert [(r["seq"], r["content"], r["completion_state"]) for r in rows] == \
        [(1, "one", "complete"), (2, "two", "incomplete")]
    assert store.branch_messages("b2")[0]["seq"] == 1


def test_branch_retention_deadline_follows_the_policy(store):
    _group(store)
    with store.tx() as c:
        store.create_branch(c, "b1", "g1", "m_g1", "shared", "T", 7)
    b = store.one("SELECT created_at, retention_deadline FROM branches WHERE id = 'b1'")
    assert parse(b["retention_deadline"]) - parse(b["created_at"]) == timedelta(days=7)


def test_audit_rows_record_operation_and_result(store):
    with store.tx() as c:
        store.audit(c, "g1", "m1", "member.remove", "m2", result="denied")
    a = store.one("SELECT * FROM audit")
    assert (a["operation"], a["object_id"], a["result"]) == ("member.remove", "m2", "denied")


def test_time_helpers_are_utc_and_round_trip():
    t = now()
    assert t.tzinfo is not None
    s = iso(t)
    assert s.endswith("+00:00") and parse(s).replace(microsecond=0) == t.replace(microsecond=0)


def test_dumps_is_canonical():
    assert Store.dumps({"b": 1, "a": [2]}) == Store.dumps({"a": [2], "b": 1}) == '{"a": [2], "b": 1}'
