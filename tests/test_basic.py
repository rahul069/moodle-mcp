import asyncio
import base64
import hashlib

import pytest

from moodle_mcp import api, cli, config, tools

SITE = "https://moodle.example.edu"
TOKEN = "0123456789abcdef0123456789abcdef"


def launch_link(passport, site=SITE, token=TOKEN):
    sig = hashlib.md5((site + passport).encode()).hexdigest()
    return "moodlemobile://token=" + base64.b64encode(f"{sig}:::{token}:::private".encode()).decode()


def test_parse_plain_token():
    assert api.parse_token(f"  {TOKEN}\n") == TOKEN


def test_parse_launch_link_checks_signature():
    assert api.parse_token(launch_link("abc"), SITE, "abc") == TOKEN
    with pytest.raises(api.MoodleError):
        api.parse_token(launch_link("other"), SITE, "abc")


def test_parse_rejects_garbage():
    with pytest.raises(api.MoodleError):
        api.parse_token("hello")


@pytest.mark.parametrize("raw,expected", [
    ("moodle.example.edu", SITE),
    ("https://moodle.example.edu/my/", SITE),
    ("https://moodle.example.edu/login/index.php", SITE),
    ("https://lms.example.edu/moodle/course/view.php?id=4", "https://lms.example.edu/moodle"),
])
def test_normalise_url(raw, expected):
    assert cli.normalise_url(raw) == expected


def test_only_read_only_functions(monkeypatch):
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: pytest.fail("must not reach the network"))
    with pytest.raises(api.MoodleError, match="not a read-only"):
        api.call("mod_assign_save_submission", url=SITE, token=TOKEN)
    assert not any(w in f for f in api.READ_ONLY for w in ("save", "submit", "send", "delete", "update_", "mark"))


def test_errors_hide_token():
    msg = api.safe(RuntimeError(f"GET {SITE}/file.php?token={TOKEN}&x=1 failed"))
    assert TOKEN not in msg and "token=***" in msg


def test_env_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MOODLE_URL", SITE + "/")
    f = tmp_path / "moodle.env"
    f.write_text(f"# comment\nMOODLE_TOKEN={TOKEN}\n")
    monkeypatch.setenv("MOODLE_TOKEN_FILE", str(f))
    monkeypatch.delenv("MOODLE_TOKEN", raising=False)
    assert config.site_url() == SITE
    assert config.token() == TOKEN


def test_find_course(monkeypatch):
    monkeypatch.setattr(api, "courses", lambda: [{"id": 1, "fullname": "Linear Algebra", "shortname": "LA"},
                                                 {"id": 2, "fullname": "Algorithms", "shortname": "ALG"}])
    assert api.find_course("2")["id"] == 2
    assert api.find_course("linear")["id"] == 1
    with pytest.raises(api.MoodleError, match="several"):
        api.find_course("al")


def test_tools_are_read_only():
    ts = asyncio.run(tools.local_server().list_tools())
    assert len(ts) == len(tools.TOOLS) == 12
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in ts)
