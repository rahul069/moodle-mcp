"""`moodle-mcp` command line.

    moodle-mcp                 run the MCP server over stdio (this is what MCP clients start)
    moodle-mcp setup           connect your Moodle account (guided)
    moodle-mcp test            check the saved connection
    moodle-mcp add-to-claude   register the server with Claude Code
    moodle-mcp logout          delete the saved site and token
    moodle-mcp serve ...       run as a remote connector for claude.ai (HTTPS + OAuth)
    moodle-mcp set-passphrase  set the passphrase for the remote connector's sign-in page
    moodle-mcp revoke-all      sign out every client of the remote connector
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import shutil
import subprocess
import sys
import webbrowser
from urllib.parse import urlparse

from . import __version__, api, config

REPO = "git+https://github.com/rahul069/moodle-mcp"
MOODLE_PAGES = ("/my/courses.php", "/my/index.php", "/my", "/login/index.php", "/course/view.php", "/index.php")


def normalise_url(raw: str) -> str:
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    u = urlparse(raw)
    path = u.path.rstrip("/")
    for page in MOODLE_PAGES:
        if path.endswith(page):
            path = path[: -len(page)]
            break
    return f"{u.scheme}://{u.netloc}{path}".rstrip("/")


def ask(prompt: str, default: str = "") -> str:
    try:
        value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        value = ""
    return value or default


def yes(prompt: str, default: bool = True) -> bool:
    a = ask(f"{prompt} {'[Y/n]' if default else '[y/N]'}").lower()
    return default if not a else a.startswith("y")


def server_command() -> list[str]:
    exe = shutil.which("moodle-mcp")
    if exe and "/.cache/" not in exe and "/tmp/" not in exe:
        return [exe]
    return ["uvx", "--from", REPO, "moodle-mcp"]


# ------------------------------------------------------------------ setup

BROWSER_STEPS = """
Your Moodle signs in through the browser (single sign-on), so the token comes from there:

  1. In your normal browser, log in to Moodle as usual.
  2. Open this link in the same browser:

     {link}

  3. The page tries to open the Moodle app with an address that starts with
     moodlemobile://token=...  Copy that whole address:
       Firefox: it stays in the address bar (or on the error page). Copy it.
       Chrome / Edge: if a dialog asks to open an app, cancel it. Press F12, choose
         the Network tab, reload the page, click the "launch.php" row and copy the
         "Location" value from the Response Headers.
"""


def get_token_by_browser(site: str) -> str:
    passport = secrets.token_hex(8)
    link = api.browser_login_url(site, passport)
    print(BROWSER_STEPS.format(link=link))
    if yes("Open the link in your browser now?"):
        webbrowser.open(link)
    while True:
        raw = getpass.getpass("\nPaste the moodlemobile://token=... address (or a token) here (hidden): ")
        if not raw.strip():
            sys.exit("Nothing pasted; setup cancelled.")
        try:
            return api.parse_token(raw, site, passport)
        except api.MoodleError as e:
            print(f"  {e}")


def get_token_by_password(site: str) -> str:
    print("\nYour Moodle accepts its own username and password. They are sent only to your Moodle,")
    print("once, to get a token; the password is not saved.")
    while True:
        user = ask("Moodle username")
        pw = getpass.getpass("Moodle password (hidden): ")
        try:
            return api.password_login(site, user, pw)
        except api.MoodleError as e:
            print(f"  Moodle said: {e}")
            if not yes("Try again?"):
                sys.exit("Setup cancelled.")


def cmd_setup(_args) -> None:
    print(f"moodle-mcp {__version__}: connect your Moodle account (read-only)\n")
    try:
        current = config.site_url()
    except config.NotConfigured:
        current = ""
    site = normalise_url(ask("Your Moodle address, e.g. moodle.university.edu", current))
    try:
        pub = api.public_config(site)
        site = (pub.get("httpswwwroot") or pub.get("wwwroot") or site).rstrip("/")
        print(f"Found: {api.text(pub.get('sitename')) or site} ({site})")
        if not pub.get("enablewebservices", 1) or not pub.get("enablemobilewebservice", 1):
            print("Warning: this site has the mobile app service switched off, so tokens may not work.")
        login_type = pub.get("typeoflogin", 2)
    except Exception as e:
        print(f"Could not read the site's login settings ({api.safe(e)}). Is the address right?")
        if not yes("Continue anyway?", False):
            sys.exit(1)
        login_type = 2

    if login_type == 1 and yes("Sign in with your Moodle username and password?"):
        tok = get_token_by_password(site)
    else:
        tok = get_token_by_browser(site)

    try:
        info = api.call("core_webservice_get_site_info", url=site, token=tok)
    except Exception as e:
        sys.exit(f"Moodle did not accept the token: {api.safe(e)}")
    available = {f.get("name") for f in info.get("functions", [])}
    missing = sorted(api.READ_ONLY - available) if available else []
    where = config.save(site, tok)
    print(f"\n✓ Connected as {info.get('fullname')} on {api.text(info.get('sitename'))}. Token saved in {where}.")
    if missing:
        print("  Note: this site does not allow " + ", ".join(missing) + "; the matching tools will report an error.")
    print()
    offer_clients()


def offer_clients() -> None:
    cmd = server_command()
    if shutil.which("claude") and yes("Add it to Claude Code now?"):
        add_to_claude()
    snippet = {"mcpServers": {"moodle": {"command": cmd[0], "args": cmd[1:]}}}
    print("\nFor Claude Desktop, Cursor and other MCP clients, add this to their MCP config:\n")
    print(json.dumps(snippet, indent=2))
    print("\nThen ask things like \"What's due on Moodle this week?\" or \"Summarise the latest slides in <course>\".")


def add_to_claude(_args=None) -> None:
    if not shutil.which("claude"):
        sys.exit("The `claude` command (Claude Code) was not found.")
    cmd = ["claude", "mcp", "add", "--scope", "user", "moodle", "--", *server_command()]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print("✓ Added to Claude Code as `moodle` (all projects). Start a new Claude Code session to use it.")
    else:
        print((r.stderr or r.stdout).strip())
        print("If a server named `moodle` already exists, remove it first: claude mcp remove moodle --scope user")


def cmd_test(_args) -> None:
    try:
        info = api.call("core_webservice_get_site_info")
        n = len(api.courses())
    except Exception as e:
        sys.exit(api.safe(e))
    print(f"✓ {api.text(info.get('sitename'))} ({config.site_url()}): signed in as {info.get('fullname')}, {n} courses.")


def cmd_logout(_args) -> None:
    config.forget()
    print("Removed the saved Moodle site and token from this computer.")
    print("To also invalidate the token on Moodle, delete it under Preferences > Security keys (/user/managetoken.php).")


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="moodle-mcp", description="Read-only Moodle MCP server.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n", 2)[2])
    ap.add_argument("--version", action="version", version=f"moodle-mcp {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="connect your Moodle account")
    sub.add_parser("test", help="check the saved connection")
    sub.add_parser("add-to-claude", help="register the server with Claude Code")
    sub.add_parser("logout", help="delete the saved site and token")
    sub.add_parser("stdio", help="run the MCP server over stdio (default)")
    s = sub.add_parser("serve", help="run as a remote connector (HTTPS + OAuth) for claude.ai")
    s.add_argument("--public-url", required=True, help="the https:// address clients use, e.g. https://box.tailnet.ts.net")
    s.add_argument("--port", type=int, default=8790, help="local port (default 8790)")
    s.add_argument("--allow-redirect-host", action="append", default=[], metavar="HOST",
                   help="extra OAuth redirect host to allow (repeatable)")
    sub.add_parser("set-passphrase", help="set the remote connector's sign-in passphrase")
    sub.add_parser("revoke-all", help="sign out every remote client")
    args = ap.parse_args(argv)

    if args.cmd is None and sys.stdin.isatty():
        ap.print_help()
        print("\nFirst time? Run: moodle-mcp setup")
        return
    if args.cmd in (None, "stdio"):
        from .tools import local_server
        local_server().run()
    elif args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "test":
        cmd_test(args)
    elif args.cmd == "add-to-claude":
        add_to_claude(args)
    elif args.cmd == "logout":
        cmd_logout(args)
    elif args.cmd == "serve":
        from . import remote
        remote.serve(args.public_url, args.port, args.allow_redirect_host)
    elif args.cmd == "set-passphrase":
        from . import remote
        remote.set_passphrase()
    elif args.cmd == "revoke-all":
        from . import remote
        remote.revoke_all()


if __name__ == "__main__":
    main()
