"""Thin, read-only client for the Moodle Web Service REST API (the one the Moodle mobile app uses).

Only the functions in READ_ONLY can be called, so nothing can be submitted, posted, marked as read
or changed in Moodle through this package.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import re
import time

import requests

from . import __version__, config

USER_AGENT = f"moodle-mcp/{__version__} (read-only; +https://github.com/rahul069/moodle-mcp)"

READ_ONLY = frozenset({
    "core_webservice_get_site_info", "core_enrol_get_users_courses", "core_course_get_contents",
    "core_course_get_updates_since", "core_calendar_get_calendar_events",
    "message_popup_get_popup_notifications",
    "mod_forum_get_forums_by_courses", "mod_forum_get_forum_discussions", "mod_forum_get_discussion_posts",
    "core_message_get_conversations", "core_message_get_conversation_messages",
    "mod_assign_get_assignments", "mod_assign_get_submission_status", "gradereport_user_get_grade_items",
    "core_user_get_users_by_field", "mod_forum_get_discussion_post",
})

# Only used by the optional write tools (write.py), which are off unless MOODLE_ALLOW_WRITE=1.
WRITE = frozenset({
    "core_message_send_instant_messages", "core_message_send_messages_to_conversation", "mod_forum_add_discussion_post",
})


class MoodleError(RuntimeError):
    pass


def _flatten(params: dict) -> dict:
    flat: dict = {}

    def add(prefix, v):
        if isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                add(f"{prefix}[{i}]", x)
        elif isinstance(v, dict):
            for k, x in v.items():
                add(f"{prefix}[{k}]", x)
        else:
            flat[prefix] = v

    for k, v in params.items():
        add(k, v)
    return flat


def call(fn: str, *, url: str | None = None, token: str | None = None, **params):
    if fn not in READ_ONLY:
        raise MoodleError(f"not a read-only Moodle function: {fn}")
    return _post(fn, url, token, params)


def write_call(fn: str, **params):
    """For write.py only: the few functions that send something, and only when writing is switched on."""
    import os
    if fn not in WRITE or os.environ.get("MOODLE_ALLOW_WRITE") != "1":
        raise MoodleError(f"writing is not allowed: {fn}")
    return _post(fn, None, None, params)


def _post(fn: str, url: str | None, token: str | None, params: dict):
    data = {"wstoken": token or config.token(), "wsfunction": fn, "moodlewsrestformat": "json", **_flatten(params)}
    r = requests.post((url or config.site_url()) + "/webservice/rest/server.php", data=data, timeout=60,
                      headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    out = r.json()
    if isinstance(out, dict) and out.get("exception"):
        raise MoodleError(f"Moodle {fn}: {out.get('message') or out.get('errorcode')}")
    return out


def download(file_url: str) -> bytes:
    r = requests.get(file_url, params={"token": config.token()}, timeout=120, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.content


# ------------------------------------------------------------------ sign-in helpers (used by `moodle-mcp setup`)

def public_config(url: str) -> dict:
    """What the site tells the mobile app before login: name, real URL, and how users log in."""
    r = requests.post(url.rstrip("/") + "/lib/ajax/service-nologin.php",
                      json=[{"index": 0, "methodname": "tool_mobile_get_public_config", "args": {}}],
                      timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    out = r.json()[0]
    if out.get("error"):
        raise MoodleError((out.get("exception") or {}).get("message") or "the site refused the request")
    return out["data"]


def password_login(url: str, username: str, password: str) -> str:
    """Token for sites that log in with a Moodle username and password (not SSO)."""
    r = requests.post(url.rstrip("/") + "/login/token.php",
                      data={"username": username, "password": password, "service": "moodle_mobile_app"},
                      timeout=30, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    out = r.json()
    if not out.get("token"):
        raise MoodleError(out.get("error") or "Moodle did not return a token")
    return out["token"]


def browser_login_url(url: str, passport: str) -> str:
    return (url.rstrip("/") + "/admin/tool/mobile/launch.php?service=moodle_mobile_app"
            f"&passport={passport}&urlscheme=moodlemobile")


def parse_token(raw: str, url: str | None = None, passport: str | None = None) -> str:
    """Accept a bare 32-character token, or the moodlemobile://token=... link from the browser sign-in."""
    raw = raw.strip()
    m = re.search(r"token=([A-Za-z0-9+/=_-]+)", raw)
    value = m.group(1) if m else raw
    if re.fullmatch(r"[0-9a-f]{32}", value):
        return value
    try:
        parts = base64.b64decode(value + "=" * (-len(value) % 4)).decode().split(":::")
    except Exception:
        parts = []
    if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{32}", parts[1]):
        if url and passport:  # the signature is md5(site url + passport); a mismatch means another sign-in
            expected = {hashlib.md5((u + passport).encode()).hexdigest() for u in (url.rstrip("/"), url.rstrip("/") + "/")}
            if parts[0] not in expected:
                raise MoodleError("That link belongs to a different sign-in attempt. Open the new sign-in link and try again.")
        return parts[1]
    raise MoodleError("That doesn't look like a Moodle token. Copy the whole moodlemobile://token=... text.")


# ------------------------------------------------------------------ cached lookups and formatting

_cache: dict = {}


def reset_cache() -> None:
    _cache.clear()


def me() -> int:
    if "me" not in _cache:
        _cache["me"] = int(call("core_webservice_get_site_info")["userid"])
    return _cache["me"]


def courses() -> list[dict]:
    if "courses" not in _cache or time.time() - _cache["courses"][0] > 600:
        _cache["courses"] = (time.time(), call("core_enrol_get_users_courses", userid=me()))
    return _cache["courses"][1]


def find_course(course: str | int) -> dict:
    cs = courses()
    s = str(course).strip()
    if s.isdigit():
        for c in cs:
            if c["id"] == int(s):
                return c
    hits = [c for c in cs if s.lower() in (c.get("fullname", "") + " " + c.get("shortname", "")).lower()]
    if len(hits) == 1:
        return hits[0]
    listing = "; ".join(f"{c['id']} {c['fullname']}" for c in (hits or cs))
    if not hits:
        raise MoodleError(f"No course matches '{course}'. Courses: {listing}")
    raise MoodleError(f"'{course}' matches several courses: {listing}")


def modules(course_id: int) -> dict[int, tuple[dict, str]]:
    out = {}
    for sec in call("core_course_get_contents", courseid=course_id):
        for m in sec.get("modules", []):
            out[m["id"]] = (m, sec.get("name", ""))
    return out


def text(h: str | None, limit: int = 0) -> str:
    t = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", h or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:limit] + " …" if limit and len(t) > limit else t


def when(ts) -> str:
    return dt.datetime.fromtimestamp(int(ts)).strftime("%a %d.%m.%Y %H:%M") if ts else "-"


def safe(e: Exception) -> str:
    """Error text for the model, with any token removed."""
    return re.sub(r"((?:ws)?token=)[^&\s'\"]+", r"\1***", f"Error: {e}")
