"""Where the Moodle site URL and token live.

Order of precedence:
  1. environment: MOODLE_URL, and MOODLE_TOKEN or MOODLE_TOKEN_FILE (a file with the token or a
     MOODLE_TOKEN=... line). Handy for servers and containers.
  2. saved by `moodle-mcp setup`: the URL in ~/.config/moodle-mcp/config.json, the token in the
     system keyring (or, if there is no keyring, in ~/.config/moodle-mcp/token with mode 600).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

KEYRING_SERVICE = "moodle-mcp"
CONFIG_DIR = Path(os.environ.get("MOODLE_MCP_CONFIG", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "moodle-mcp"))
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "token"


class NotConfigured(RuntimeError):
    pass


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def site_url() -> str:
    url = os.environ.get("MOODLE_URL") or _read_config().get("url")
    if not url:
        raise NotConfigured("No Moodle site configured. Run `moodle-mcp setup` in a terminal first.")
    return url.rstrip("/")


def _token_from_file(path: Path) -> str:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("MOODLE_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
        if line and not line.startswith("#") and "=" not in line:
            return line
    raise NotConfigured(f"No token found in {path}.")


def token() -> str:
    if os.environ.get("MOODLE_TOKEN"):
        return os.environ["MOODLE_TOKEN"].strip()
    if os.environ.get("MOODLE_TOKEN_FILE"):
        return _token_from_file(Path(os.environ["MOODLE_TOKEN_FILE"]))
    url = site_url()
    try:
        import keyring
        t = keyring.get_password(KEYRING_SERVICE, url) or keyring.get_password(KEYRING_SERVICE, "token")  # 2nd: pre-1.0 setups
        if t:
            return t
    except Exception:  # no usable keyring backend
        pass
    if TOKEN_FILE.exists():
        return _token_from_file(TOKEN_FILE)
    raise NotConfigured("No Moodle token saved. Run `moodle-mcp setup` in a terminal first.")


def save(url: str, tok: str) -> str:
    """Save the site URL and token; returns where the token went."""
    url = url.rstrip("/")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = _read_config()
    cfg["url"] = url
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, url, tok)
        TOKEN_FILE.unlink(missing_ok=True)
        return "the system keyring"
    except Exception:
        TOKEN_FILE.touch(mode=0o600, exist_ok=True)
        TOKEN_FILE.chmod(0o600)
        TOKEN_FILE.write_text(tok + "\n")
        return str(TOKEN_FILE)


def forget() -> None:
    url = _read_config().get("url")
    if url:
        try:
            import keyring
            keyring.delete_password(KEYRING_SERVICE, url.rstrip("/"))
        except Exception:
            pass
    TOKEN_FILE.unlink(missing_ok=True)
    CONFIG_FILE.unlink(missing_ok=True)
