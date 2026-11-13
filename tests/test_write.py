import asyncio
import importlib

import pytest

from moodle_mcp import api


@pytest.fixture
def write(monkeypatch, tmp_path):
    monkeypatch.setenv("MOODLE_ALLOW_WRITE", "1")
    monkeypatch.setenv("MOODLE_MCP_STATE", str(tmp_path))
    from moodle_mcp import write as w
    w = importlib.reload(w)
    sent = []
    monkeypatch.setattr(api, "me", lambda: 7)
    monkeypatch.setattr(api, "call", lambda fn, **k: {
        "core_user_get_users_by_field": [{"id": 42, "fullname": "Ada Lovelace"}],
        "mod_forum_get_discussion_post": {"post": {"subject": "Exam date?", "author": {"fullname": "Prof X"}}},
    }[fn])
    monkeypatch.setattr(api, "write_call", lambda fn, **k: sent.append((fn, k)) or ([{"msgid": 1}] if "instant" in fn else {"postid": 9}))
    w.sent = sent
    return w


def run(coro):
    return asyncio.run(coro)


def test_write_call_needs_switch(monkeypatch):
    monkeypatch.delenv("MOODLE_ALLOW_WRITE", raising=False)
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: pytest.fail("must not reach the network"))
    with pytest.raises(api.MoodleError):
        api.write_call("core_message_send_instant_messages", messages=[])
    monkeypatch.setenv("MOODLE_ALLOW_WRITE", "1")
    with pytest.raises(api.MoodleError):
        api.write_call("mod_assign_submit_for_grading", assignmentid=1)  # never in WRITE


def test_nothing_sent_when_user_declines(write, monkeypatch):
    async def no(*a):
        return "The user did not confirm, so nothing was sent."
    monkeypatch.setattr(write, "confirm", no)
    assert "did not confirm" in run(write.moodle_send_message("hi", user_id=42))
    assert "did not confirm" in run(write.moodle_forum_reply(5, "thanks"))
    assert write.sent == []


def test_nothing_sent_without_any_confirmation_channel(write, monkeypatch):
    async def no_dialog(*a):
        return None
    monkeypatch.setattr(write, "_desktop_confirm", no_dialog)
    assert run(write.moodle_send_message("hi", user_id=42, ctx=None)).startswith("Not sent")
    assert write.sent == []


def test_sends_after_confirmation_and_rate_limits(write, monkeypatch):
    async def yes(*a):
        return None
    monkeypatch.setattr(write, "confirm", yes)
    monkeypatch.setattr(write, "MAX_PER_HOUR", 2)
    assert run(write.moodle_send_message("Hello <b>\nthere", user_id=42)) == "Sent to Ada Lovelace."
    assert write.sent[0][1]["messages"][0]["text"] == "Hello &lt;b&gt;<br>there"
    assert "Posted" in run(write.moodle_forum_reply(5, "thanks"))
    assert write.sent[1][1]["subject"] == "Re: Exam date?"
    assert "Limit reached" in run(write.moodle_send_message("again", user_id=42))
    assert len(write.sent) == 2


def test_write_tools_only_when_enabled(monkeypatch, tmp_path):
    from moodle_mcp import remote, tools, write
    monkeypatch.delenv("MOODLE_ALLOW_WRITE", raising=False)
    importlib.reload(write)
    names = {t.name for t in asyncio.run(tools.local_server().list_tools())}
    assert "moodle_send_message" not in names
    monkeypatch.setenv("MOODLE_ALLOW_WRITE", "1")
    importlib.reload(write)
    ts = {t.name: t for t in asyncio.run(tools.local_server().list_tools())}
    assert {"moodle_send_message", "moodle_forum_reply"} <= set(ts)
    assert "ctx" not in ts["moodle_send_message"].input_schema["properties"]
    assert not ts["moodle_send_message"].annotations.read_only_hint
    remote_names = {t.name for t in asyncio.run(remote.build("https://x.test", set()).list_tools())}
    assert "moodle_send_message" not in remote_names and "moodle_forum_reply" not in remote_names
