# moodle-mcp

Ask Claude (or any MCP client) about your Moodle: courses, slides, deadlines,
announcements, forums, private messages and grades.

> "What's due on Moodle in the next two weeks?"
> "Summarise the slides of lecture 3 in Linear Algebra."
> "Anything new in my courses since Monday?"
> "Did anyone message me on Moodle?"

It works with **any Moodle site** (4.x and most 3.x) that has the Moodle mobile app enabled,
which almost every university does. It is **read-only**: it can't submit, post, change
or mark anything as read.

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (one command to install).

```bash
uv tool install git+https://github.com/rahul069/moodle-mcp
moodle-mcp setup
```

`setup` asks for your Moodle address, signs you in, and adds the server to Claude Code
if it is installed. For other apps it prints the config to paste. That's it.

<details>
<summary>Without installing (uvx)</summary>

```bash
uvx --from git+https://github.com/rahul069/moodle-mcp moodle-mcp setup
```
</details>

## Signing in

`moodle-mcp setup` looks up how your Moodle handles logins and picks the right way:

- **Username and password** (sites with their own Moodle accounts): type them once. They go only
  to your Moodle to get a token and are not saved.
- **University single sign-on** (Shibboleth, SAML, Microsoft, ...): setup gives you a link.
  1. Log in to Moodle in your browser as usual.
  2. Open the link from setup in the same browser. Moodle then tries to open the Moodle app
     with an address starting with `moodlemobile://token=`.
  3. Copy that address and paste it into setup.
     - **Firefox:** it is in the address bar or on the error page.
     - **Chrome / Edge:** cancel the "open app" dialog, press F12, open **Network**, reload,
       click the `launch.php` row and copy **Location** from the response headers.

The token is stored in your system keyring (macOS Keychain, Windows Credential Manager,
GNOME Keyring / KWallet). On machines without a keyring it is stored in
`~/.config/moodle-mcp/token` with permissions 600.

## Use it in your MCP client

**Claude Code:** `moodle-mcp setup` offers to add it, or run `moodle-mcp add-to-claude`.

**Claude Desktop, Cursor, Windsurf, VS Code, ...:** add this to the app's MCP config
(Claude Desktop: Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "moodle": { "command": "moodle-mcp" }
  }
}
```

If the app can't find `moodle-mcp`, use the full path (`which moodle-mcp`) or
`"command": "uvx", "args": ["--from", "git+https://github.com/rahul069/moodle-mcp", "moodle-mcp"]`.

## Tools

| Tool | What it does |
|---|---|
| `moodle_courses` | your courses with ids |
| `moodle_course` | sections and materials of a course, with `[module ids]` |
| `moodle_open` | text of a page or PDF, saves files to `~/Downloads/moodle/<course>/` |
| `moodle_updates` | what changed in the last N days (files, forum posts, grades) |
| `moodle_deadlines` | upcoming deadlines and calendar events |
| `moodle_notifications` | latest notifications (announcements, feedback, grades) |
| `moodle_forum`, `moodle_discussion` | forums, discussions and posts |
| `moodle_assignments` | assignments with due dates and submission or grading status |
| `moodle_grades` | your grades in a course |
| `moodle_messages`, `moodle_conversation` | private and group chats |

Courses can be named by id or by any part of their name ("linear alg", "algorithms").

## Commands

```
moodle-mcp                 run the MCP server (what your MCP client starts)
moodle-mcp setup           connect your Moodle account
moodle-mcp test            check the connection
moodle-mcp add-to-claude   register with Claude Code
moodle-mcp logout          delete the saved site and token
```

Settings via environment variables (all optional):

| Variable | Meaning |
|---|---|
| `MOODLE_URL`, `MOODLE_TOKEN` | use these instead of what `setup` saved |
| `MOODLE_TOKEN_FILE` | file containing the token (or a `MOODLE_TOKEN=` line) |
| `MOODLE_DOWNLOADS` | where `moodle_open` saves files (default `~/Downloads/moodle`) |
| `MOODLE_INLINE_TEXT=0` | don't return the text of PDFs, only save them |

## Use it from claude.ai and your phone (optional)

Run moodle-mcp on an always-on machine (a home server or a small cloud VM) as a **remote
connector** and add it in claude.ai → Settings → Connectors → *Add custom connector*.
It then also works in the Claude mobile apps.

```bash
moodle-mcp setup             # on the server (paste the token; no browser needed there)
moodle-mcp set-passphrase    # the passphrase you'll type when connecting claude.ai
moodle-mcp serve --public-url https://your-server.example.com
```

It listens on `127.0.0.1:8790` only, so put HTTPS in front of it, e.g.
`tailscale funnel --bg 8790`, a Cloudflare Tunnel, or Caddy/nginx. In claude.ai, use
`https://your-server.example.com/mcp` as the connector URL. A sign-in page asks for your
passphrase (5 wrong tries lock it for 15 minutes).

<details>
<summary>systemd service</summary>

```ini
# /etc/systemd/system/moodle-mcp.service
[Unit]
Description=Moodle MCP connector
After=network-online.target

[Service]
User=youruser
Environment=MOODLE_TOKEN_FILE=/home/youruser/.config/moodle-mcp/token
ExecStart=/home/youruser/.local/bin/moodle-mcp serve --public-url https://your-server.example.com
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/youruser/.local/state/moodle-mcp /home/youruser/Downloads
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
</details>

Other clients: only claude.ai/claude.com (and localhost) may receive sign-in codes. To allow
another client, add its redirect host, e.g. for Gemini:
`--allow-redirect-host oauth-redirect.googleusercontent.com`. `moodle-mcp revoke-all` signs
every client out.

## Privacy and security

- **Read-only by construction.** The client can only call the Moodle web-service functions
  listed in [`api.py`](moodle_mcp/api.py) (`READ_ONLY`); everything else is refused before
  any request is sent.
- **Your data stays between your computer and your Moodle.** No telemetry, no third-party servers.
  Your MCP client (e.g. Claude) sees the tool results you ask for.
- **The token gives access to your Moodle account.** Keep it private. To revoke it, delete it
  on Moodle under *Preferences → Security keys* (`/user/managetoken.php`), then run
  `moodle-mcp logout`.
- Content from Moodle (posts, messages, files) is written by other people. Good MCP clients
  treat it as data, but be aware of prompt injection when you let an assistant act on it.

## Development

```bash
git clone https://github.com/rahul069/moodle-mcp && cd moodle-mcp
uv sync
uv run pytest
uv run moodle-mcp setup
```

## License

MIT. Not affiliated with Moodle Pty Ltd. "Moodle" is a registered trademark of Moodle Pty Ltd.
