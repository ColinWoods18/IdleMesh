"""The scheduler's event channel, the fake runtime's contract, and Settings defaults and overrides."""
import asyncio

import pytest

from idlemesh_coord import scheduler
from idlemesh_coord.config import Settings
from idlemesh_coord.runtime.base import Cancelled, Generation, RuntimeFailure
from idlemesh_coord.runtime.fake import FakeRuntime


def run(coro):
    return asyncio.run(coro)


# --- Channel ---------------------------------------------------------------------------------
def test_channel_numbers_events_and_marks_terminal():
    async def go():
        ch = scheduler.Channel("req_1")
        ch.emit("queued", {"position": 1})
        ch.emit("token", {"text": "hi"})
        ch.emit("completed", terminal=True)
        return ch, [e async for e in ch.iterate()]
    ch, events = run(go())
    assert ch.done
    assert [(e["sequence"], e["type"]) for e in events] == [(1, "queued"), (2, "token"), (3, "completed")]
    assert all(e["request_id"] == "req_1" for e in events) and events[2]["payload"] == {}


def test_channel_resumes_after_a_given_sequence():
    async def go():
        ch = scheduler.Channel("r")
        for i in range(5):
            ch.emit("token", {"i": i})
        ch.emit("completed", terminal=True)
        return [e["payload"].get("i") async for e in ch.iterate(after=3)]
    assert run(go()) == [3, 4, None]


def test_channel_wakes_a_waiting_subscriber():
    async def go():
        ch = scheduler.Channel("r")
        got = []

        async def reader():
            async for e in ch.iterate():
                got.append(e["type"])

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.01)
        ch.emit("token")
        await asyncio.sleep(0.01)
        assert got == ["token"]
        ch.emit("completed", terminal=True)
        await asyncio.wait_for(task, 1)
        return got
    assert run(go()) == ["token", "completed"]


def test_wait_for_new_times_out_then_sees_an_event():
    async def go():
        ch = scheduler.Channel("r")
        quiet = await ch.wait_for_new(0.02)
        asyncio.get_running_loop().call_later(0.01, ch.emit, "token")
        return quiet, await ch.wait_for_new(1)
    assert run(go()) == (False, True)


def test_channel_is_bounded_but_always_keeps_the_terminal_event(monkeypatch):
    monkeypatch.setattr(scheduler, "MAX_EVENTS", 3)
    ch = scheduler.Channel("r")
    for _ in range(10):
        ch.emit("token")
    ch.emit("failed", {"code": "X"}, terminal=True)
    assert len(ch.events) == 4 and ch.events[-1]["type"] == "failed"


# --- FakeRuntime -----------------------------------------------------------------------------
PLACEMENT = {"model": "fixture-small", "stages": [{"device_id": "a", "name": "Mac", "layers": [0, 12]},
                                                  {"device_id": "b", "name": "HP", "layers": [12, 24]}]}
MSGS = [{"role": "user", "content": "Hello there"}]


async def _collect(rt, max_tokens=64, cancel=None):
    result = Generation()
    parts = [p async for p in rt.generate_stream("d1", MSGS, max_tokens, None, cancel or asyncio.Event(), result)]
    return "".join(parts), result


def test_fake_runtime_streams_a_deterministic_answer_naming_the_placement():
    async def go():
        rt = FakeRuntime(token_delay_s=0)
        loaded = await rt.load_deployment("d1", PLACEMENT)
        text, res = await _collect(rt)
        again, _ = await _collect(rt)
        return loaded, text, res, again
    loaded, text, res, again = run(go())
    assert loaded["partition_map"] == [{"device_id": "a", "layers": [0, 12]}, {"device_id": "b", "layers": [12, 24]}]
    assert "Mac layers 0-11, HP layers 12-23" in text and text == again
    assert res.finish_reason == "stop" and res.completion_tokens == len(text.split(" "))


def test_fake_runtime_honours_max_tokens():
    async def go():
        rt = FakeRuntime(token_delay_s=0)
        await rt.load_deployment("d1", PLACEMENT)
        return await _collect(rt, max_tokens=3)
    text, res = run(go())
    assert res.finish_reason == "length" and res.completion_tokens == 3 and len(text.split(" ")) == 3


def test_fake_runtime_failure_cancel_and_unloaded_paths():
    async def go():
        rt = FakeRuntime(token_delay_s=0)
        with pytest.raises(RuntimeFailure) as e:
            await _collect(rt)
        assert e.value.code == "MODEL_UNAVAILABLE"
        await rt.load_deployment("d1", PLACEMENT)
        rt.fail_after_tokens = 2
        with pytest.raises(RuntimeFailure) as e:
            await _collect(rt)
        assert e.value.code == "WORKER_LOST"
        rt.fail_after_tokens = None
        stop = asyncio.Event()
        stop.set()
        with pytest.raises(Cancelled):
            await _collect(rt, cancel=stop)
        assert (await rt.health())["deployments"] == ["d1"]
        await rt.unload_deployment("d1")
        assert (await rt.health())["deployments"] == []
    run(go())


# --- Settings --------------------------------------------------------------------------------
def test_settings_defaults_match_the_spec_baseline(monkeypatch):
    for var in ("IDLEMESH_WAIT_DEADLINE_S", "IDLEMESH_ACTIVE_DEADLINE_S", "IDLEMESH_COOKIE_SECURE"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert (s.per_member_queue, s.group_queue, s.active_generations) == (2, 10, 1)
    assert s.wait_deadline_s == 600 and s.active_deadline_s == 300
    assert s.heartbeat_unassign_s > s.heartbeat_interval_s
    assert s.min_passphrase_len >= 15 and s.cookie_secure is False
    assert s.pseudonymize_nodes is True and s.telemetry_retention_days == 90


def test_settings_read_overrides_from_the_environment(monkeypatch):
    monkeypatch.setenv("IDLEMESH_WAIT_DEADLINE_S", "5")
    monkeypatch.setenv("IDLEMESH_COOKIE_SECURE", "1")
    monkeypatch.setenv("IDLEMESH_DB", "elsewhere.sqlite3")
    s = Settings()
    assert s.wait_deadline_s == 5.0 and s.cookie_secure is True and s.db_path == "elsewhere.sqlite3"
