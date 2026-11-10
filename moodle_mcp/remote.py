"""Hosted version: Streamable HTTP with OAuth, for claude.ai custom connectors (and other MCP clients that
support remote servers with OAuth, e.g. the Claude mobile apps once added on claude.ai).

The client registers itself (dynamic client registration), your browser opens /login, and you type the
connector passphrase you set with `moodle-mcp set-passphrase`. Only redirect hosts in ALLOWED_REDIRECT_HOSTS
(claude.ai, claude.com, localhost) plus any you add can receive authorization codes.

Run it behind HTTPS (Tailscale Funnel, Cloudflare Tunnel, Caddy, nginx ...); it listens on 127.0.0.1 only.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
                                      RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import tools

STATE = Path(os.environ.get("MOODLE_MCP_STATE",
                            Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "moodle-mcp"))
DB = STATE / "oauth.json"
PASS = STATE / "passphrase.json"
ACCESS_TTL = 3600
REFRESH_TTL = 90 * 86400
SCOPE = "moodle"
DEFAULT_REDIRECT_HOSTS = {"claude.ai", "claude.com", "localhost", "127.0.0.1"}


# ------------------------------------------------------------------ passphrase

def _hash(p: str, salt: bytes) -> bytes:
    return hashlib.scrypt(p.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 2 ** 20)


def set_passphrase() -> None:
    p1 = getpass.getpass("New connector passphrase (at least 12 characters, hidden): ")
    if len(p1) < 12:
        sys.exit("Too short; use at least 12 characters.")
    if getpass.getpass("Again: ") != p1:
        sys.exit("They don't match.")
    salt = secrets.token_bytes(16)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PASS.write_text(json.dumps({"salt": salt.hex(), "hash": _hash(p1, salt).hex()}))
    PASS.chmod(0o600)
    print(f"Saved to {PASS}. Connected clients stay signed in; `moodle-mcp revoke-all` signs them out.")


def check_passphrase(p: str) -> bool:
    try:
        d = json.loads(PASS.read_text())
    except FileNotFoundError:
        return False
    return hmac.compare_digest(_hash(p, bytes.fromhex(d["salt"])), bytes.fromhex(d["hash"]))


def revoke_all() -> None:
    p = Provider("http://localhost", set())
    p.db = {"clients": {}, "access": {}, "refresh": {}}
    p.save()
    print("All clients signed out. Restart the server if it is running.")


# ------------------------------------------------------------------ OAuth provider (persisted in a JSON file)

class Provider:
    def __init__(self, public_url: str, redirect_hosts: set[str]):
        self.public_url = public_url
        self.redirect_hosts = redirect_hosts
        self.lock = threading.Lock()
        self.pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.failures: list[float] = []
        try:
            self.db = json.loads(DB.read_text())
        except FileNotFoundError:
            self.db = {"clients": {}, "access": {}, "refresh": {}}

    def save(self):
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        for kind in ("access", "refresh"):
            self.db[kind] = {k: v for k, v in self.db[kind].items() if not v.get("expires_at") or v["expires_at"] > now}
        tmp = DB.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.db))
        tmp.chmod(0o600)
        tmp.replace(DB)

    async def get_client(self, client_id):
        d = self.db["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris or []:
            host = urlparse(str(uri)).hostname or ""
            if host not in self.redirect_hosts:
                print(f"rejected client {client_info.client_name!r}: redirect {uri} (allow it with --allow-redirect-host {host})",
                      flush=True)
                raise RegistrationError(error="invalid_redirect_uri", error_description=f"redirect host {host} not allowed")
        with self.lock:
            if len(self.db["clients"]) > 200:
                raise RegistrationError(error="invalid_client_metadata", error_description="too many clients")
            self.db["clients"][client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
            self.save()

    async def authorize(self, client, params):
        rid = secrets.token_urlsafe(24)
        self.pending = {k: v for k, v in self.pending.items() if v[2] > time.time()}
        self.pending[rid] = (client.client_id, params, time.time() + 600)
        return f"{self.public_url}/login?r={rid}"

    def approve(self, rid: str) -> str:
        client_id, params, _ = self.pending.pop(rid)
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + 300, client_id=client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource,
            subject="owner")
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, rid: str) -> str:
        _, params, _ = self.pending.pop(rid)
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        c = self.codes.get(authorization_code)
        if c and c.client_id == client.client_id and c.expires_at > time.time():
            return c
        return None

    def _issue(self, client_id, scopes, resource) -> OAuthToken:
        at, rt, now = secrets.token_urlsafe(32), secrets.token_urlsafe(40), int(time.time())
        with self.lock:
            self.db["access"][at] = {"client_id": client_id, "scopes": scopes, "expires_at": now + ACCESS_TTL,
                                     "resource": resource, "refresh": rt}
            self.db["refresh"][rt] = {"client_id": client_id, "scopes": scopes, "expires_at": now + REFRESH_TTL,
                                      "resource": resource}
            self.save()
        return OAuthToken(access_token=at, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=rt)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        d = self.db["refresh"].get(refresh_token)
        if not d or d["client_id"] != client.client_id or d["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, subject="owner", **d)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self.lock:
            self.db["refresh"].pop(refresh_token.token, None)
            self.db["access"] = {k: v for k, v in self.db["access"].items() if v.get("refresh") != refresh_token.token}
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        d = self.db["access"].get(token)
        if not d or d["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=d["client_id"], scopes=d["scopes"], expires_at=d["expires_at"],
                           resource=d.get("resource"), subject="owner")

    async def revoke_token(self, token):
        with self.lock:
            rt = self.db["access"].pop(token.token, {}).get("refresh") if isinstance(token, AccessToken) else token.token
            self.db["refresh"].pop(rt, None)
            self.db["access"] = {k: v for k, v in self.db["access"].items() if v.get("refresh") != rt}
            self.save()

    async def exchange_identity_assertion(self, client, params):
        raise TokenError(error="unsupported_grant_type")


# ------------------------------------------------------------------ app

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moodle connector</title><style>body{{font-family:system-ui,sans-serif;max-width:420px;margin:12vh auto;padding:0 16px;
color:#222;background:#fafafa}}input,button{{font-size:16px;padding:10px;width:100%;box-sizing:border-box;margin-top:10px}}
button{{background:#f98012;color:#fff;border:0;border-radius:6px}}.d{{background:#eee;color:#333}}.e{{color:#b00020}}
@media(prefers-color-scheme:dark){{body{{background:#1b1b1b;color:#eee}}.d{{background:#333;color:#eee}}}}</style></head><body>
<h2>Moodle connector</h2><p><b>{client}</b> wants read-only access to your Moodle.</p>{err}
<form method="post"><input type="hidden" name="r" value="{rid}">
<input type="password" name="p" placeholder="Connector passphrase" autofocus autocomplete="current-password">
<button name="a" value="allow">Allow</button><button class="d" name="a" value="deny">Deny</button></form></body></html>"""
EXPIRED = "This sign-in link expired. Start again from your MCP client."


def build(public_url: str, redirect_hosts: set[str]) -> MCPServer:
    provider = Provider(public_url, redirect_hosts)
    server = MCPServer(
        "moodle", instructions=tools.INSTRUCTIONS, auth_server_provider=provider,
        auth=AuthSettings(issuer_url=public_url, resource_server_url=f"{public_url}/mcp", validate_token_resource=False,
                          client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                                                 default_scopes=[SCOPE]),
                          revocation_options=RevocationOptions(enabled=True), required_scopes=[SCOPE]))
    tools.register(server)

    async def page(rid: str, err: str = "", status: int = 200) -> Response:
        client = await provider.get_client(provider.pending[rid][0])
        name = html.escape((client.client_name if client else None) or "An MCP client")
        return HTMLResponse(PAGE.format(client=name, rid=html.escape(rid), err=err), status_code=status)

    @server.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        if request.method == "GET":
            rid = request.query_params.get("r", "")
            return await page(rid) if rid in provider.pending else HTMLResponse(EXPIRED, status_code=400)
        form = await request.form()
        rid = str(form.get("r", ""))
        if rid not in provider.pending:
            return HTMLResponse(EXPIRED, status_code=400)
        if form.get("a") == "deny":
            return RedirectResponse(provider.deny(rid), status_code=302)
        now = time.time()
        provider.failures = [t for t in provider.failures if t > now - 900]
        if len(provider.failures) >= 5:
            return HTMLResponse("Too many wrong passphrases. Try again in 15 minutes.", status_code=429)
        if not check_passphrase(str(form.get("p", ""))):
            provider.failures.append(now)
            return await page(rid, '<p class="e">Wrong passphrase.</p>', 401)
        return RedirectResponse(provider.approve(rid), status_code=302)

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return Response("ok")

    return server


def serve(public_url: str, port: int = 8790, extra_redirect_hosts: list[str] | None = None) -> None:
    import uvicorn

    from . import config
    public_url = public_url.rstrip("/")
    if urlparse(public_url).scheme != "https":
        sys.exit("--public-url must be the https:// address clients use to reach this server.")
    if not PASS.exists():
        sys.exit("No connector passphrase yet. Run `moodle-mcp set-passphrase` first.")
    config.token()  # fail early if Moodle isn't set up
    hosts = DEFAULT_REDIRECT_HOSTS | set(extra_redirect_hosts or []) | set(
        filter(None, os.environ.get("EXTRA_REDIRECT_HOSTS", "").split(",")))
    server = build(public_url, hosts)
    app = server.streamable_http_app(
        stateless_http=True, host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[urlparse(public_url).netloc, "127.0.0.1:*", "localhost:*"],
            allowed_origins=["https://claude.ai", "https://claude.com", public_url]))
    print(f"Moodle MCP connector: {public_url}/mcp  (listening on 127.0.0.1:{port})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="info")
