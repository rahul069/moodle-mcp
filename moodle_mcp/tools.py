"""The MCP tools. Every tool only reads; errors come back as text so the model can explain them."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import config
from .api import call, courses, download, find_course, me, modules, safe, text, when

DOWNLOADS = Path(os.environ.get("MOODLE_DOWNLOADS", Path.home() / "Downloads" / "moodle"))
INLINE_PDF = os.environ.get("MOODLE_INLINE_TEXT", "1") != "0"
logging.getLogger("pypdf").setLevel(logging.ERROR)  # font warnings are noise here
UPDATE_WORDS = {"configuration": "changed", "fileslastmodified": "new or updated files", "discussions": "new forum posts",
                "gradeitems": "grades", "grades": "grades", "submissions": "submissions", "entries": "new entries",
                "comments": "comments", "posts": "new posts", "chapters": "chapters", "pages": "pages"}

INSTRUCTIONS = (
    "Read-only access to the user's Moodle. Start with moodle_courses; a course can be given by id or part of its "
    "name. moodle_course lists sections and materials with [module ids]; moodle_open(module_id) returns the text of "
    "pages and PDFs and saves files locally. moodle_updates, moodle_deadlines and moodle_notifications show what is "
    "new. Nothing can be submitted, posted, changed or marked as read."
)


def pdf_text(data: bytes, limit: int = 40000) -> str:
    try:
        import io

        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(data)).pages
        t = "\n".join(f"[page {i}] " + (p.extract_text() or "") for i, p in enumerate(pages, 1))
    except Exception as e:
        return f"(could not extract text: {e})"
    t = re.sub(r"[ \t]+", " ", t)
    if len(t) > limit:
        return t[:limit] + f"\n… (truncated; {len(pages)} pages)"
    return t or "(no text in this PDF; it may be scanned)"


def moodle_courses() -> str:
    """List the Moodle courses you are enrolled in, with their ids."""
    try:
        return "\n".join(f"[{c['id']}] {c['fullname']}"
                         + (f"  (last opened {when(c['lastaccess'])})" if c.get("lastaccess") else "")
                         for c in courses()) or "You are not enrolled in any course."
    except Exception as e:
        return safe(e)


def moodle_course(course: str) -> str:
    """Show a course's sections and materials (files, pages, forums, assignments, links) with their [module ids].
    `course` is the course id or part of its name."""
    try:
        c = find_course(course)
        lines = [f"{c['fullname']} (id {c['id']})", f"{config.site_url()}/course/view.php?id={c['id']}"]
        for sec in call("core_course_get_contents", courseid=c["id"]):
            mods = [m for m in sec.get("modules", []) if m.get("uservisible", True)]
            summ = text(sec.get("summary"), 300)
            if not mods and not summ:
                continue
            lines.append(f"\n## {sec.get('name') or 'Section'}")
            if summ:
                lines.append(summ)
            for m in mods:
                if m.get("modname") == "label":
                    lines.append(f"  [{m['id']}] (note) {text(m.get('description'), 200)}")
                    continue
                files = [f for f in m.get("contents", []) if f.get("type") == "file"]
                extra = ""
                if files:
                    newest = max(f.get("timemodified") or 0 for f in files)
                    extra = (f" · {len(files)} files, updated {when(newest)}" if len(files) > 1
                             else f" · {files[0]['filename']}, updated {when(newest)}")
                for d in m.get("dates", []):
                    extra += f" · {d.get('label', '').rstrip(':')} {when(d.get('timestamp'))}"
                lines.append(f"  [{m['id']}] {m.get('name')} ({m.get('modname')}){extra}")
        return "\n".join(lines)
    except Exception as e:
        return safe(e)


def moodle_open(module_id: int, course: str = "") -> str:
    """Open one item from moodle_course by its [module id]: returns the text of pages, labels, links and PDFs,
    and saves attached files under ~/Downloads/moodle/<course>/. Give `course` (id or name) to make it faster."""
    try:
        cs = [find_course(course)] if course else courses()
        for c in cs:
            mods = modules(c["id"])
            if module_id not in mods:
                continue
            m, section = mods[module_id]
            out = [f"{m.get('name')} ({m.get('modname')}) · {c['fullname']} / {section}", m.get("url", "")]
            if m.get("description"):
                out.append(text(m["description"]))
            folder = DOWNLOADS / re.sub(r"[^\w .-]+", "_", c.get("shortname") or c["fullname"])[:60]
            for f in m.get("contents", []):
                if f.get("type") == "url":
                    out.append("Link: " + f.get("fileurl", ""))
                    continue
                if f.get("type") != "file" or not f.get("fileurl"):
                    continue
                data = download(f["fileurl"])
                name = f["filename"]
                if name.lower().endswith((".html", ".htm")) and m.get("modname") == "page":
                    out.append(text(data.decode("utf-8", "replace"), 20000))
                    continue
                dest = folder / (f.get("filepath") or "/").strip("/") / re.sub(r"[\\/]+", "_", name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                out.append(f"Saved: {dest} ({len(data) // 1024} KB)")
                if INLINE_PDF and dest.suffix.lower() == ".pdf":
                    out.append(f"--- text of {name} ---\n" + pdf_text(data))
            return "\n".join(x for x in out if x)
        return f"No module {module_id} found" + (f" in {course}" if course else " in your courses") + "."
    except Exception as e:
        return safe(e)


def moodle_updates(days: int = 7, course: str = "") -> str:
    """What changed in your courses in the last `days` days (new or updated files, forum posts, grades, ...).
    Leave `course` empty for all courses."""
    try:
        since = int(time.time() - days * 86400)
        cs = [find_course(course)] if course else courses()
        lines = []
        for c in cs:
            ups = call("core_course_get_updates_since", courseid=c["id"], since=since).get("instances", [])
            ups = [u for u in ups if any(x.get("name") != "completion" for x in u.get("updates", []))]
            if not ups:
                continue
            mods = modules(c["id"])
            lines.append(f"\n## {c['fullname']} (id {c['id']})")
            for u in ups:
                what = sorted({UPDATE_WORDS.get(x["name"], x["name"]) for x in u["updates"] if x.get("name") != "completion"})
                m = mods.get(u.get("id"), ({"name": f"item {u.get('id')}", "modname": u.get("contextlevel", "")}, ""))[0]
                lines.append(f"  [{u.get('id')}] {m.get('name')} ({m.get('modname')}): {', '.join(what)}")
        return f"Changes in the last {days} day(s):" + "\n".join(lines) if lines else f"No changes in the last {days} day(s)."
    except Exception as e:
        return safe(e)


def moodle_deadlines(days: int = 60) -> str:
    """Upcoming deadlines and calendar events (assignments, quizzes, course events) for the next `days` days."""
    try:
        now = int(time.time())
        evs = call("core_calendar_get_calendar_events",
                   events={"courseids": [c["id"] for c in courses()]},
                   options={"userevents": 1, "siteevents": 1, "timestart": now - 3600, "timeend": now + days * 86400}
                   ).get("events", [])
        names = {c["id"]: c.get("shortname") or c["fullname"] for c in courses()}
        seen, lines = set(), []
        for e in sorted(evs, key=lambda e: e["timestart"]):
            key = (e.get("name"), e["timestart"])
            if key in seen:
                continue
            seen.add(key)
            cname = names.get(e.get("courseid"), "")
            lines.append(f"{when(e['timestart'])} · {cname + ': ' if cname else ''}{text(e.get('name'))}"
                         + (f" ({e['modulename']})" if e.get("modulename") else ""))
        return "\n".join(lines) or f"Nothing in the next {days} day(s)."
    except Exception as e:
        return safe(e)


def moodle_notifications(limit: int = 20) -> str:
    """Your latest Moodle notifications (announcements, forum posts, feedback, grades), newest first."""
    try:
        ns = call("message_popup_get_popup_notifications", useridto=me(), newestfirst=1,
                  limit=max(1, min(limit, 50))).get("notifications", [])
        return "\n\n".join(f"{when(n['timecreated'])}{'' if n.get('read') else ' (unread)'} · {text(n.get('subject'))}\n"
                           f"{text(n.get('fullmessagehtml') or n.get('fullmessage') or n.get('smallmessage'), 600)}"
                           for n in ns) or "No notifications."
    except Exception as e:
        return safe(e)


def moodle_forum(course: str, forum_id: int = 0, limit: int = 15) -> str:
    """List a course's forums (no forum_id), or the latest discussions in one forum (forum_id from that list)."""
    try:
        c = find_course(course)
        if not forum_id:
            fs = call("mod_forum_get_forums_by_courses", courseids=[c["id"]])
            return "\n".join(f"forum_id {f['id']}: {f['name']} ({f.get('numdiscussions', '?')} discussions)" for f in fs) \
                or "This course has no forums."
        ds = call("mod_forum_get_forum_discussions", forumid=forum_id, sortorder=1, perpage=max(1, min(limit, 50))
                  ).get("discussions", [])
        return "\n\n".join(f"discussion_id {d['discussion']} · {when(d.get('timemodified'))} · {d.get('userfullname', '')}\n"
                           f"{text(d.get('name'))}\n{text(d.get('message'), 400)}" for d in ds) or "No discussions."
    except Exception as e:
        return safe(e)


def moodle_discussion(discussion_id: int) -> str:
    """Read all posts of one forum discussion (discussion_id from moodle_forum)."""
    try:
        ps = call("mod_forum_get_discussion_posts", discussionid=discussion_id, sortby="created", sortdirection="ASC")
        return "\n\n".join(f"{when(p.get('timecreated'))} · {(p.get('author') or {}).get('fullname', '')}\n"
                           f"{text(p.get('subject'))}\n{text(p.get('message'), 4000)}"
                           for p in ps.get("posts", [])) or "No posts."
    except Exception as e:
        return safe(e)


def moodle_assignments(course: str = "", include_past: bool = False) -> str:
    """Assignments with due dates and your submission and grading status. Leave `course` empty for all courses."""
    try:
        ids = [find_course(course)["id"]] if course else [c["id"] for c in courses()]
        now, lines = time.time(), []
        for c in call("mod_assign_get_assignments", courseids=ids).get("courses", []):
            for a in sorted(c.get("assignments", []), key=lambda a: a.get("duedate") or 0):
                due = a.get("duedate") or 0
                if due and due < now - 86400 and not include_past:
                    continue
                status = ""
                try:
                    st = call("mod_assign_get_submission_status", assignid=a["id"])
                    status = ((st.get("lastattempt") or {}).get("submission") or {}).get("status", "")
                    if (st.get("feedback") or {}).get("gradefordisplay"):
                        status += " · grade: " + text(st["feedback"]["gradefordisplay"])
                except RuntimeError:
                    pass
                lines.append(f"{when(due) if due else 'no due date'} · {c.get('shortname')}: {a['name']} "
                             f"[module {a.get('cmid')}]" + (f" · {status}" if status else ""))
        return "\n".join(lines) or "No open assignments."
    except Exception as e:
        return safe(e)


def moodle_grades(course: str) -> str:
    """Your grades in one course (id or part of its name)."""
    try:
        c = find_course(course)
        g = call("gradereport_user_get_grade_items", courseid=c["id"], userid=me())
        lines = [c["fullname"]]
        for ug in g.get("usergrades", []):
            for it in ug.get("gradeitems", []):
                if it.get("gradeformatted") not in (None, "", "-") or it.get("itemtype") == "course":
                    name = it.get("itemname") or ("Course total" if it.get("itemtype") == "course" else "?")
                    lines.append(f"  {name}: {it.get('gradeformatted') or '-'}"
                                 + (f" / {it['grademax']}" if it.get("grademax") else ""))
        return "\n".join(lines) if len(lines) > 1 else "No grades yet."
    except Exception as e:
        return safe(e)


def moodle_messages(limit: int = 15) -> str:
    """Your Moodle private chats and group conversations (newest first): who, unread count and the last message.
    Open one with moodle_conversation(conversation_id). Nothing is marked as read."""
    try:
        cs = call("core_message_get_conversations", userid=me(), limitfrom=0, limitnum=max(1, min(limit, 50))
                  ).get("conversations", [])
        out = []
        for c in cs:
            who = c.get("name") or ", ".join(m.get("fullname", "") for m in c.get("members", []) if m.get("id") != me()) \
                or "(you)"
            last = (c.get("messages") or [{}])[0]
            unread = f" · {c['unreadcount']} unread" if c.get("unreadcount") else ""
            out.append(f"conversation_id {c['id']} · {who}{unread}\n  {when(last.get('timecreated'))}: "
                       f"{text(last.get('text'), 200) or '(no messages)'}")
        return "\n".join(out) or "No Moodle conversations."
    except Exception as e:
        return safe(e)


def moodle_conversation(conversation_id: int, limit: int = 30) -> str:
    """Read the latest messages of one Moodle conversation (conversation_id from moodle_messages), oldest first.
    Nothing is marked as read."""
    try:
        r = call("core_message_get_conversation_messages", currentuserid=me(), convid=conversation_id,
                 limitfrom=0, limitnum=max(1, min(limit, 100)), newest=1)
        names = {m["id"]: m.get("fullname", "") for m in r.get("members", [])}
        msgs = sorted(r.get("messages", []), key=lambda m: m.get("timecreated", 0))
        return "\n\n".join(f"{when(m.get('timecreated'))} · "
                           f"{'You' if m.get('useridfrom') == me() else names.get(m.get('useridfrom'), '?')}\n"
                           f"{text(m.get('text'), 3000)}" for m in msgs) or "No messages."
    except Exception as e:
        return safe(e)


TOOLS = [moodle_courses, moodle_course, moodle_open, moodle_updates, moodle_deadlines, moodle_notifications,
         moodle_forum, moodle_discussion, moodle_assignments, moodle_grades, moodle_messages, moodle_conversation]

READ_ONLY_HINT = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def register(server: MCPServer) -> MCPServer:
    for fn in TOOLS:
        server.add_tool(fn, annotations=READ_ONLY_HINT)
    return server


def local_server() -> MCPServer:
    return register(MCPServer("moodle", instructions=INSTRUCTIONS))
