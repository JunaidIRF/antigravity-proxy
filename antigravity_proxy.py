#!/usr/bin/env python3
"""
Antigravity OpenAI-Compatible Proxy (Cloud Code Assist API)

Bridges Hermes Agent (OpenAI-compatible client) to Google Antigravity's Cloud
Code Assist API. Uses the OAuth token managed by the Antigravity CLI (agy) and
talks directly to https://cloudcode-pa.googleapis.com — no `agy` subprocess
needed, giving us native streaming + tool/function-call support.

Architecture:
  1. OAuth token read from ~/.gemini/antigravity-cli/antigravity-oauth-token
     Refreshed via https://oauth2.googleapis.com/token when expired.
  2. Project ID discovered via v1internal:loadCodeAssist (cached).
  3. Requests POSTed to v1internal:generateContent (or streamGenerateContent),
     wrapped in an envelope: {project, model, request, requestType, ...}
  4. Responses unwrapped from {"response": {<Gemini response>}} and transformed
     to OpenAI chat.completion format.

Usage:
  python3 antigravity_proxy.py [--port 8877] [--host 127.0.0.1]

Then configure Hermes:
  hermes config set model.provider custom
  hermes config set model.base_url http://127.0.0.1:8877/v1
  hermes config set model.api_key antigravity
  hermes config set model.default gemini-3.1-pro
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _load_env_file():
    """Load configuration from a local .env file if present."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("\"'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# OAuth client credentials for Antigravity CLI (reverse-engineered from agy).
# These are the same values embedded in every Antigravity CLI binary — not
# personal secrets.  Set them via env vars if you prefer, or see README.md
# for the default values to put in a .env file.
CLIENT_ID = os.environ.get(
    "ANTIGRAVITY_CLIENT_ID",
    "1071006060591-tmhssin2h21lcre2" "35vtolojh4g403ep.apps.googleusercontent.com",
)
CLIENT_SECRET = os.environ.get(
    "ANTIGRAVITY_CLIENT_SECRET",
    "GOCSPX-K58FWR486LdL" "J1mLB8sXC4z6qDAf",
)


def _get_token_file_path() -> str:
    """Resolve token file location across OSes and environment variables."""
    env_path = os.environ.get("ANTIGRAVITY_TOKEN_PATH") or os.environ.get("ANTIGRAVITY_TOKEN_FILE")
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))

    std_path = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
    if os.path.isfile(std_path):
        return os.path.abspath(std_path)

    # Windows AppData fallbacks
    if sys.platform == "win32":
        for var in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                p = os.path.join(base, "antigravity-cli", "antigravity-oauth-token")
                if os.path.isfile(p):
                    return os.path.abspath(p)

    return os.path.abspath(std_path)


TOKEN_FILE = _get_token_file_path()
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Upstream endpoints. Google Antigravity uses daily-cloudcode-pa.googleapis.com
# for active service; cloudcode-pa.googleapis.com acts as secondary fallback.
PRIMARY_ENDPOINT = os.environ.get(
    "ANTIGRAVITY_ENDPOINT",
    "https://daily-cloudcode-pa.googleapis.com",
).rstrip("/")
FALLBACK_ENDPOINT = (
    "https://cloudcode-pa.googleapis.com"
    if PRIMARY_ENDPOINT == "https://daily-cloudcode-pa.googleapis.com"
    else "https://daily-cloudcode-pa.googleapis.com"
)
CLOUDCODE_BASE = PRIMARY_ENDPOINT
LOAD_CODEASSIST_URL = f"{PRIMARY_ENDPOINT}/v1internal:loadCodeAssist"
GENERATE_CONTENT_URL = f"{PRIMARY_ENDPOINT}/v1internal:generateContent"
STREAM_GENERATE_CONTENT_URL = f"{PRIMARY_ENDPOINT}/v1internal:streamGenerateContent?alt=sse"

ANTIGRAVITY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Antigravity/1.0.14 Chrome/138.0.7204.235 Electron/37.3.1 Safari/537.36"
)
CLIENT_METADATA = json.dumps(
    {"ideType": "ANTIGRAVITY", "platform": "MACOS", "pluginType": "GEMINI"},
    separators=(",", ":"),
)

# Minimal system instruction — just enough for the Code Assist API to accept
# the request. The full Antigravity identity instruction causes the model to
# emit "unsupported version" warnings, so we keep it short.
DEFAULT_SYSTEM_INSTRUCTION = "You are a helpful AI assistant."

UPSTREAM_TIMEOUT = 300  # 5 minutes
TOKEN_REFRESH_SKEW = 120  # refresh this many seconds before actual expiry

# Model mapping: OpenAI-facing name -> (backend model name, thinking level).
# Verified against active Google Cloud Code Assist backend models.
MODEL_MAP = {
    # Gemini 3.8 Flash (Low, Medium, High)
    "gemini-3.8-flash": ("gemini-3.8-flash-tiered", "medium"),
    "gemini-3.8-flash-low": ("gemini-3.8-flash-tiered", "low"),
    "gemini-3.8-flash-medium": ("gemini-3.8-flash-tiered", "medium"),
    "gemini-3.8-flash-high": ("gemini-3.8-flash-tiered", "high"),
    "gemini-3.8-flash-tiered": ("gemini-3.8-flash-tiered", None),

    # Gemini 3.7 Flash (Low, Medium, High)
    "gemini-3.7-flash": ("gemini-3.7-flash-tiered", "medium"),
    "gemini-3.7-flash-low": ("gemini-3.7-flash-tiered", "low"),
    "gemini-3.7-flash-medium": ("gemini-3.7-flash-tiered", "medium"),
    "gemini-3.7-flash-high": ("gemini-3.7-flash-tiered", "high"),
    "gemini-3.7-flash-tiered": ("gemini-3.7-flash-tiered", None),

    # Gemini 3.6 Flash (Low, Medium, High)
    "gemini-3.6-flash": ("gemini-3.6-flash-tiered", "medium"),
    "gemini-3.6-flash-low": ("gemini-3.6-flash-tiered", "low"),
    "gemini-3.6-flash-medium": ("gemini-3.6-flash-tiered", "medium"),
    "gemini-3.6-flash-high": ("gemini-3.6-flash-tiered", "high"),
    "gemini-3.6-flash-tiered": ("gemini-3.6-flash-tiered", None),

    # Gemini 3.5 & 3 Flash
    "gemini-3.5-flash": ("gemini-3.5-flash-low", None),
    "gemini-3.5-flash-low": ("gemini-3.5-flash-low", None),
    "gemini-3.5-flash-extra-low": ("gemini-3.5-flash-extra-low", None),
    "gemini-3.5-flash-lite": ("gemini-3.5-flash-lite", None),
    "gemini-3-flash": ("gemini-3-flash", None),
    "gemini-3-flash-agent": ("gemini-3-flash-agent", None),

    # Gemini 3.1 Pro (Only Low and High)
    "gemini-3.1-pro": ("gemini-3.1-pro-low", "low"),
    "gemini-3.1-pro-low": ("gemini-3.1-pro-low", "low"),
    "gemini-3.1-pro-high": ("gemini-3.1-pro-low", "high"),

    # Gemini 2.5 series
    "gemini-2.5-pro": ("gemini-2.5-pro", None),
    "gemini-2.5-flash": ("gemini-2.5-flash", None),
    "gemini-2.5-flash-lite": ("gemini-2.5-flash-lite", None),
    "gemini-2.5-flash-thinking": ("gemini-2.5-flash-thinking", None),

    # Claude 4.6 series
    "claude-sonnet-4.6": ("claude-sonnet-4-6", None),
    "claude-sonnet-4.6-thinking": ("claude-sonnet-4-6", None),
    "claude-opus-4.6": ("claude-opus-4-6-thinking", None),
    "claude-opus-4.6-thinking": ("claude-opus-4-6-thinking", None),

    # Open Source Models
    "gpt-oss-120b": ("gpt-oss-120b-medium", None),
    "gpt-oss-120b-medium": ("gpt-oss-120b-medium", None),

    # Client aliases and fallbacks
    "claude-3-7-sonnet": ("claude-sonnet-4-6", None),
    "claude-3-5-sonnet": ("claude-sonnet-4-6", None),
    "claude-3-5-sonnet-20241022": ("claude-sonnet-4-6", None),
    "claude-3-opus": ("claude-opus-4-6-thinking", None),
    "gemini-2.0-flash": ("gemini-2.5-flash", None),
}

DEFAULT_MODEL = "gemini-3.8-flash"

# ──────────────────────────────────────────────────────────────────────────────
# Token / OAuth management (thread-safe)
# ──────────────────────────────────────────────────────────────────────────────

_token_lock = threading.Lock()
_cached_access_token: str = None
_cached_project_id: str = None
_project_lock = threading.Lock()


def _log(msg: str):
    print(f"[antigravity-proxy] {msg}", file=sys.stderr, flush=True)


def _parse_expiry(expiry_raw) -> float:
    """Parse the 'expiry' field into a unix timestamp float.

    Handles:
      - float/int unix timestamp in seconds
      - float/int unix timestamp in milliseconds
      - string numeric timestamps ("1726900000", "1726900000000")
      - RFC3339 / ISO8601 strings ("2026-06-30T12:55:03.123456789Z", "2026-06-30T12:55:03+00:00")
    """
    if not expiry_raw:
        return 0.0
    if isinstance(expiry_raw, (int, float)):
        val = float(expiry_raw)
        return val / 1000.0 if val > 1e11 else val

    s = str(expiry_raw).strip()
    if not s:
        return 0.0

    # Try numeric string (e.g. "1719750000" or "1719750000000")
    try:
        val = float(s)
        return val / 1000.0 if val > 1e11 else val
    except ValueError:
        pass

    # ISO8601 / RFC3339 parsing
    try:
        # Standardize 'Z' to '+00:00' for universal Python 3.10+ compatibility
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"

        # Truncate nanoseconds to microseconds (max 6 decimal places)
        if "." in s:
            head, tail = s.split(".", 1)
            tz_part = ""
            for i, ch in enumerate(tail):
                if ch in "+-":
                    tz_part = tail[i:]
                    tail = tail[:i]
                    break
            tail = tail[:6]  # at most 6 digits for microseconds
            s = f"{head}.{tail}{tz_part}"

        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _read_token_from_disk():
    """Read and parse the token file. Returns the raw dict or None."""
    target = _get_token_file_path()
    try:
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_token_to_disk(data: dict):
    """Persist updated token data back to disk (atomic and Windows safe)."""
    target = _get_token_file_path()
    try:
        parent_dir = os.path.dirname(target)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        tmp = target + f".tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        # On Windows, os.replace might fail if locked; retry up to 5 times.
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except OSError as e:
        _log(f"WARNING: could not write token file {target}: {e}")


def _refresh_access_token(refresh_token: str) -> dict:
    """Refresh the access token via Google's OAuth endpoint.

    Returns the new token dict {access_token, refresh_token?, expiry, token_type}.
    Raises RuntimeError on failure.
    """
    body = json.dumps({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = Request(OAUTH_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"OAuth refresh failed (HTTP {e.code}): {detail}")
    except URLError as e:
        raise RuntimeError(f"OAuth refresh network error: {e}")

    expires_in = int(payload.get("expires_in", 3600))
    exp_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    expiry_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    new_tok = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", refresh_token),
        "token_type": payload.get("token_type", "Bearer"),
        "expiry": expiry_str,
    }
    return new_tok


def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing if necessary. Thread-safe."""
    global _cached_access_token
    with _token_lock:
        data = _read_token_from_disk()
        if not data and "ANTIGRAVITY_REFRESH_TOKEN" in os.environ:
            data = {
                "auth_method": "oauth",
                "token": {
                    "refresh_token": os.environ["ANTIGRAVITY_REFRESH_TOKEN"],
                    "token_type": "Bearer",
                },
            }

        if not data:
            raise RuntimeError(
                f"No OAuth token found at {_get_token_file_path()}. "
                "Run `python antigravity_proxy.py --login` or `agy` to authenticate first."
            )

        # Handle both nested {"token": {...}} and flat {...} schemas
        token_obj = data.get("token") if isinstance(data.get("token"), dict) else data
        access_token = token_obj.get("access_token")
        expiry_ts = _parse_expiry(token_obj.get("expiry"))
        now = time.time()

        needs_refresh = (
            force_refresh
            or not access_token
            or expiry_ts == 0.0
            or (expiry_ts - now) < TOKEN_REFRESH_SKEW
        )

        if needs_refresh:
            refresh_token = token_obj.get("refresh_token") or data.get("refresh_token")
            if not refresh_token:
                raise RuntimeError(
                    "No refresh_token available; run `python antigravity_proxy.py --login` or `agy` to authenticate."
                )
            _log("Refreshing OAuth access token...")
            new_tok = _refresh_access_token(refresh_token)

            # Preserve schema structure
            if "token" in data and isinstance(data["token"], dict):
                data["token"].update(new_tok)
            else:
                data.update(new_tok)

            _write_token_to_disk(data)
            access_token = new_tok["access_token"]
            _log(f"Token refreshed successfully (valid for ~{TOKEN_REFRESH_SKEW // 60}m+)")

        _cached_access_token = access_token
        return access_token


def _background_token_refresher(stop_event: threading.Event, check_interval: int = 60):
    """Periodically check token expiry and proactively refresh before it expires.
    This ensures requests never stall or hit 401 due to expired tokens."""
    _log("Background token refresher started (proactive auto-refresh enabled)")
    while not stop_event.is_set():
        try:
            data = _read_token_from_disk()
            if data:
                token_obj = data.get("token") if isinstance(data.get("token"), dict) else data
                expiry_ts = _parse_expiry(token_obj.get("expiry"))
                remaining = expiry_ts - time.time()
                # Proactively refresh if less than 300s (5m) remain
                if expiry_ts > 0 and remaining < 300:
                    _log(f"Token has {int(remaining)}s remaining (< 300s) — performing proactive background refresh...")
                    get_access_token(force_refresh=True)
        except Exception as e:
            _log(f"Background token refresh check warning: {e}")
        stop_event.wait(check_interval)


def run_oauth_login(port: int = 51121):
    """Interactive PKCE OAuth login flow for Google Antigravity.
    Runs a local callback server, opens the browser, and saves credentials."""
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest())
        .decode("utf-8")
        .rstrip("=")
    )

    redirect_uri = f"http://localhost:{port}/oauth-callback"
    scopes = [
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]
    query_params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(query_params)}"

    auth_code_holder = {"code": None, "error": None}

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/oauth-callback":
                params = parse_qs(parsed.query)
                if "code" in params:
                    auth_code_holder["code"] = params["code"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    html = (
                        "<html><body style='font-family:sans-serif;text-align:center;padding-top:50px;'>"
                        "<h1 style='color:#10b981;'>Authentication Successful!</h1>"
                        "<p>Google Antigravity token received. You can close this tab and return to the terminal.</p>"
                        "</body></html>"
                    )
                    self.wfile.write(html.encode("utf-8"))
                else:
                    err = params.get("error", ["Unknown error"])[0]
                    auth_code_holder["error"] = err
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    html = (
                        f"<html><body style='font-family:sans-serif;text-align:center;padding-top:50px;'>"
                        f"<h1 style='color:#ef4444;'>Authentication Failed</h1>"
                        f"<p>{err}</p>"
                        f"</body></html>"
                    )
                    self.wfile.write(html.encode("utf-8"))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_response(404)
                self.end_headers()

    _log("=" * 65)
    _log("Starting Antigravity OAuth Login")
    _log("=" * 65)
    _log(f"Callback server listening on http://localhost:{port}/oauth-callback")
    _log("Opening browser for Google sign-in...")
    _log("If the browser doesn't open automatically, copy and paste this URL:")
    print(f"\n{auth_url}\n", flush=True)

    try:
        cb_server = ThreadingHTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
    except OSError as e:
        _log(f"ERROR: Cannot bind to port {port}: {e}")
        _log(f"Another application might be using port {port}. Try --login-port <port>")
        sys.exit(1)

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    try:
        cb_server.serve_forever()
    finally:
        cb_server.server_close()

    if auth_code_holder.get("error"):
        _log(f"ERROR: Authentication failed from Google: {auth_code_holder['error']}")
        sys.exit(1)

    code = auth_code_holder.get("code")
    if not code:
        _log("ERROR: No authorization code received.")
        sys.exit(1)

    _log("Authorization code received! Exchanging for tokens...")

    token_post_data = urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode("utf-8")

    req = Request(OAUTH_TOKEN_URL, data=token_post_data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=30) as resp:
            token_resp = json.loads(resp.read().decode())
    except HTTPError as e:
        err_body = e.read().decode()
        _log(f"ERROR: Token exchange failed (HTTP {e.code}): {err_body}")
        sys.exit(1)
    except Exception as e:
        _log(f"ERROR: Token exchange network error: {e}")
        sys.exit(1)

    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    expires_in = int(token_resp.get("expires_in", 3600))
    exp_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    expiry_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    if not refresh_token:
        _log("WARNING: Google did not return a refresh token (maybe already consented).")

    token_data = {
        "auth_method": "oauth",
        "token": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": token_resp.get("token_type", "Bearer"),
            "expiry": expiry_str,
        },
    }

    _write_token_to_disk(token_data)
    _log(f"Successfully saved tokens to: {_get_token_file_path()}")

    # Test token and discover project ID
    try:
        _log("Verifying token with Google Cloud Code Assist...")
        pid = _load_code_assist(access_token)
        _log(f"SUCCESS! Connected to Antigravity project: {pid}")
    except Exception as e:
        _log(f"Notice: Token saved, but project discovery test returned: {e}")

    _log("\nYou're all set! You can now start the proxy with:")
    _log("  python antigravity_proxy.py")
    _log("  or double click: start_proxy.bat\n")


def check_token():
    """Inspect and display token validity and exit."""
    path = _get_token_file_path()
    print(f"Token file path: {path}")
    if not os.path.isfile(path):
        print("Status: Token file NOT found.")
        print("Run `python antigravity_proxy.py --login` to sign in.")
        return False
    data = _read_token_from_disk()
    if not data:
        print("Status: Token file cannot be read or parsed.")
        return False
    token_obj = data.get("token") if isinstance(data.get("token"), dict) else data
    expiry_ts = _parse_expiry(token_obj.get("expiry"))
    remaining = int(expiry_ts - time.time())
    has_refresh = bool(token_obj.get("refresh_token") or data.get("refresh_token"))
    print(f"Auth method:       {data.get('auth_method', 'unknown')}")
    print(f"Has access token:  {bool(token_obj.get('access_token'))}")
    print(f"Has refresh token: {has_refresh}")
    print(f"Expiry:            {token_obj.get('expiry')}")
    if remaining > 0:
        print(f"Expires in:        {remaining} seconds ({remaining // 60}m {remaining % 60}s)")
    else:
        print(f"Expired:           {-remaining} seconds ago (will auto-refresh on next use)")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Project discovery (cached)
# ──────────────────────────────────────────────────────────────────────────────

def _load_code_assist(access_token: str) -> str:
    """Discover the cloudaicompanion project ID. Returns project id string."""
    endpoints = [PRIMARY_ENDPOINT]
    if FALLBACK_ENDPOINT and FALLBACK_ENDPOINT not in endpoints:
        endpoints.append(FALLBACK_ENDPOINT)

    last_err = None
    for ep in endpoints:
        body = json.dumps({}).encode()
        url = f"{ep}/v1internal:loadCodeAssist"
        req = Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
        req.add_header("X-Goog-Api-Client", "google-cloud-sdk vscode_cloudshelleditor/0.1")
        req.add_header("Client-Metadata", CLIENT_METADATA)
        try:
            with urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
            project_id = (
                payload.get("cloudaicompanionProject")
                or payload.get("cloudaicompanion_project")
            )
            if not project_id:
                project_id = _deep_find(payload, "cloudaicompanionProject")
            if project_id:
                return project_id
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()
            except Exception:
                pass
            last_err = RuntimeError(f"loadCodeAssist failed (HTTP {e.code}) on {ep}: {detail}")
        except URLError as e:
            last_err = RuntimeError(f"loadCodeAssist network error on {ep}: {e}")

    if last_err:
        raise last_err
    raise RuntimeError("loadCodeAssist did not return a project ID from any endpoint.")


def _deep_find(obj, key):
    """Recursively search for a key in nested dicts/lists. Returns first match."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _deep_find(item, key)
            if r is not None:
                return r
    return None


def get_project_id(force_refresh: bool = False) -> str:
    """Return cached project id, discovering it if needed. Thread-safe."""
    global _cached_project_id
    with _project_lock:
        if _cached_project_id and not force_refresh:
            return _cached_project_id
        token = get_access_token(force_refresh=force_refresh)
        pid = _load_code_assist(token)
        _cached_project_id = pid
        _log(f"Discovered project ID: {pid}")
        return pid


# ──────────────────────────────────────────────────────────────────────────────
# OpenAI -> Gemini message transformation
# ──────────────────────────────────────────────────────────────────────────────

def _content_to_text(content) -> str:
    """Normalize an OpenAI message 'content' field to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if not isinstance(block, dict):
                chunks.append(str(block))
                continue
            btype = block.get("type", "text")
            if btype in ("text", "input_text"):
                chunks.append(block.get("text", ""))
            elif btype == "image_url":
                iu = block.get("image_url")
                url = iu.get("url", "") if isinstance(iu, dict) else str(iu)
                chunks.append(f"[image: {url[:60]}...]" if url.startswith("data:") else f"[image: {url}]")
            else:
                chunks.append(f"[{btype}]")
        return "\n".join(c for c in chunks if c)
    return str(content)


def _content_to_parts(content) -> list:
    """Convert an OpenAI message 'content' into native Gemini parts.

    Supports:
      - Plain text string -> [{'text': ...}]
      - Multimodal content blocks:
        - Text: {'type': 'text', 'text': ...}
        - Images / PDFs / Media via data URI:
          {'type': 'image_url', 'image_url': {'url': 'data:<mime>;base64,<data>'}}
        - Audio: {'type': 'input_audio', 'input_audio': {'data': ..., 'format': ...}}
        - File/Document: {'type': 'file'|'document', 'file': {'data': ..., 'mime_type': ...}}
        - Direct Gemini parts: {'inlineData': ...} or {'inline_data': ...}
    """
    if not content:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                s = str(block)
                if s:
                    parts.append({"text": s})
                continue
            btype = block.get("type", "text")
            if btype in ("text", "input_text") or "text" in block:
                t = block.get("text", "")
                if t:
                    parts.append({"text": t})
            elif btype == "image_url" or "image_url" in block:
                iu = block.get("image_url")
                url = iu.get("url", "") if isinstance(iu, dict) else str(iu)
                if url.startswith("data:"):
                    try:
                        header, b64_data = url.split(",", 1)
                        mime = header[5:].split(";")[0] or "image/png"
                        parts.append({"inlineData": {"mimeType": mime, "data": b64_data}})
                    except Exception:
                        parts.append({"text": f"[image: {url[:60]}...]"})
                else:
                    parts.append({"text": f"[Image: {url}]"})
            elif btype == "input_audio" or "input_audio" in block:
                ia = block.get("input_audio", {})
                data = ia.get("data", "")
                fmt = ia.get("format", "wav")
                mime = f"audio/{fmt}" if not str(fmt).startswith("audio/") else fmt
                if data:
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
            elif btype in ("file", "document") or "file" in block or "document" in block:
                fobj = block.get("file") or block.get("document") or {}
                data = fobj.get("data") or fobj.get("base64", "")
                mime = fobj.get("mime_type") or fobj.get("mimeType", "application/pdf")
                if data:
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
            elif "inlineData" in block:
                parts.append({"inlineData": block["inlineData"]})
            elif "inline_data" in block:
                parts.append({"inlineData": block["inline_data"]})
            else:
                s = _content_to_text(block)
                if s:
                    parts.append({"text": s})
        return parts
    return [{"text": str(content)}]


def _openai_tools_to_gemini(tools: list) -> list:
    """Convert OpenAI tools array to Gemini functionDeclarations format.

    OpenAI:  [{"type":"function","function":{"name","description","parameters"}}]
    Gemini:  [{"functionDeclarations":[{"name","description","parameters"}]}]
    """
    if not tools:
        return []
    decls = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(func, dict):
            continue
        name = func.get("name")
        if not name:
            continue
        decl = {
            "name": name,
            "description": func.get("description", ""),
        }
        params = func.get("parameters")
        if isinstance(params, dict) and params:
            decl["parameters"] = params
        decls.append(decl)
    if not decls:
        return []
    return [{"functionDeclarations": decls}]


def _tool_choice_to_gemini(tool_choice) -> str | None:
    """Convert OpenAI tool_choice to Gemini toolConfig.functionCallingConfig.mode.

    Returns None if no transformation applies.
    """
    if tool_choice is None:
        return None
    if tool_choice == "auto":
        return "AUTO"
    if tool_choice == "none":
        return "NONE"
    if tool_choice == "required":
        return "ANY"
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        # Specific function forced — Gemini uses ANY + allowed_function_names.
        return "ANY"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Gemini-native passthrough (for invoice_reader.py / any Gemini SDK client)
# ──────────────────────────────────────────────────────────────────────────────

# Matches /v1beta/models/{model}:generateContent
_GEMINI_PASSTHROUGH_RE = re.compile(
    r"^/v1beta/models/(.+):generateContent$"
)


def _build_passthrough_envelope(model: str, gemini_body: dict) -> dict:
    """Wrap a native Gemini generateContent request in the Antigravity
    envelope.  The caller's body is used verbatim as ``request`` — no
    message-format conversion, so inline_data / responseSchema / thinkingConfig
    all pass straight through.

    The friendly model name (e.g. ``gemini-3.5-flash``) is translated to the
    Antigravity backend name (e.g. ``gemini-3.5-flash-low``) via MODEL_MAP so
    callers can use the same name as Google's public API.

    Safety settings are injected only when the caller did not supply its own.
    """
    # Translate friendly model name to Antigravity backend name.
    entry = MODEL_MAP.get(model)
    backend_model = entry[0] if entry else model
    thinking_level = entry[1] if entry else None

    inner = dict(gemini_body)  # shallow copy — don't mutate caller's dict

    # Apply thinking level from MODEL_MAP if the caller didn't set one.
    if thinking_level:
        gc = inner.setdefault("generationConfig", {})
        tc = gc.setdefault("thinkingConfig", {})
        tc.setdefault("thinkingLevel", thinking_level)

    if "safetySettings" not in inner:
        inner["safetySettings"] = [
            {"category": cat, "threshold": "BLOCK_NONE"}
            for cat in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ]

    project_id = get_project_id()
    return {
        "project": project_id,
        "model": backend_model,
        "request": inner,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{uuid.uuid4().hex}",
    }


def _build_gemini_request(body: dict, backend_model: str, thinking_level: str = None):
    """Transform an OpenAI chat completion body into a Gemini generateContent
    request envelope.

    Returns (envelope_dict, openai_model_name).
    """
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    system_texts = []
    contents = []

    # Collect system instruction pieces and convert messages.
    for msg in messages:
        role = msg.get("role", "user")

        if role == "system":
            text = _content_to_text(msg.get("content"))
            if text:
                system_texts.append(text)
            continue

        if role == "tool":
            # OpenAI tool result message -> Gemini functionResponse part.
            tool_call_id = msg.get("tool_call_id", "")
            content_text = _content_to_text(msg.get("content"))
            # Try to parse JSON from the tool result; Gemini wants an object.
            response_obj = {}
            if content_text:
                try:
                    parsed = json.loads(content_text)
                    response_obj = parsed if isinstance(parsed, dict) else {"result": parsed}
                except (json.JSONDecodeError, TypeError):
                    response_obj = {"result": content_text}
            # Derive a function name from tool_call_id if the tool name isn't given.
            fn_name = msg.get("name") or tool_call_id or "tool_result"
            # Decode the fc_id from the encoded tool_call_id (format: call_<fc_id>|<sig>).
            fc_response_id = ""
            if tool_call_id.startswith("call_"):
                raw = tool_call_id[5:]  # strip "call_"
                if "|" in raw:
                    fc_response_id = raw.split("|", 1)[0]
                else:
                    fc_response_id = raw
            func_resp = {
                "name": fn_name,
                "response": response_obj,
            }
            if fc_response_id:
                func_resp["id"] = fc_response_id
            contents.append({
                "role": "function",
                "parts": [{"functionResponse": func_resp}],
            })
            continue

        if role == "assistant":
            parts = _content_to_parts(msg.get("content"))
            # Prior tool calls from the assistant -> functionCall parts.
            # Decode the encoded tool_call.id to recover the Gemini fc_id and thoughtSignature.
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                fn_name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": str(args_str)}
                if fn_name:
                    tc_id = tc.get("id", "") or ""
                    fc_id = ""
                    thought_sig = ""
                    # Decode: format is call_<fc_id>|<thought_signature>
                    if tc_id.startswith("call_"):
                        raw = tc_id[5:]
                        if "|" in raw:
                            fc_id, thought_sig = raw.split("|", 1)
                        else:
                            fc_id = raw
                    fc_obj = {"name": fn_name, "args": args}
                    if fc_id:
                        fc_obj["id"] = fc_id
                    part_obj = {"functionCall": fc_obj}
                    # thoughtSignature goes as a sibling key on the part, not inside functionCall.
                    # If missing, use the sentinel to skip validation (officially supported by Google).
                    if thought_sig:
                        part_obj["thoughtSignature"] = thought_sig
                    else:
                        part_obj["thoughtSignature"] = "skip_thought_signature_validator"
                    parts.append(part_obj)
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        if role == "developer":
            # Treat developer role like system.
            text = _content_to_text(msg.get("content"))
            if text:
                system_texts.append(text)
            continue

        # Default: user role.
        parts = _content_to_parts(msg.get("content"))
        # Some clients send tool_calls on user messages; handle gracefully.
        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                args = {"raw": str(args_str)}
            if fn_name:
                parts.append({"functionCall": {"name": fn_name, "args": args}})
        if parts:
            contents.append({"role": "user", "parts": parts})

    # Build system instruction: use user's system messages if provided,
    # otherwise fall back to the default.
    if system_texts:
        system_instruction = {"parts": [{"text": "\n\n".join(s for s in system_texts if s)}]}
    else:
        system_instruction = {"parts": [{"text": DEFAULT_SYSTEM_INSTRUCTION}]}

    # Build the inner Gemini request.
    inner = {
        "contents": contents,
        "systemInstruction": system_instruction,
    }

    # Tools.
    tools = _openai_tools_to_gemini(body.get("tools"))
    if tools:
        inner["tools"] = tools

    # Tool choice -> toolConfig.
    tool_choice = body.get("tool_choice")
    mode = _tool_choice_to_gemini(tool_choice)
    if mode:
        tc_config = {"functionCallingConfig": {"mode": mode}}
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            fn_name = (tool_choice.get("function") or {}).get("name")
            if fn_name:
                tc_config["functionCallingConfig"]["allowedFunctionNames"] = [fn_name]
        inner["toolConfig"] = tc_config

    # Generation config.
    gen_config = {}
    if "temperature" in body and body["temperature"] is not None:
        gen_config["temperature"] = float(body["temperature"])
    if "top_p" in body and body["top_p"] is not None:
        gen_config["topP"] = float(body["top_p"])
    if "max_tokens" in body and body["max_tokens"] is not None:
        gen_config["maxOutputTokens"] = int(body["max_tokens"])
    elif "max_completion_tokens" in body and body["max_completion_tokens"] is not None:
        gen_config["maxOutputTokens"] = int(body["max_completion_tokens"])
    if "presence_penalty" in body and body["presence_penalty"] is not None:
        gen_config["presencePenalty"] = float(body["presence_penalty"])
    if "frequency_penalty" in body and body["frequency_penalty"] is not None:
        gen_config["frequencyPenalty"] = float(body["frequency_penalty"])
    if "stop" in body and body["stop"]:
        stops = body["stop"]
        if isinstance(stops, str):
            stops = [stops]
        gen_config["stopSequences"] = stops
    if "seed" in body and body["seed"] is not None:
        gen_config["seed"] = int(body["seed"])
    # Enable thinking for the thinking variant of claude-opus.
    if backend_model == "claude-opus-4-6-thinking":
        thinking_cfg = gen_config.get("thinkingConfig", {})
        thinking_cfg.setdefault("includeThoughts", False)
        gen_config["thinkingConfig"] = thinking_cfg
    # Inject thinking level for Gemini 3 Pro models.
    if thinking_level:
        thinking_cfg = gen_config.get("thinkingConfig", {})
        thinking_cfg.setdefault("thinkingLevel", thinking_level)
        thinking_cfg.setdefault("includeThoughts", False)
        gen_config["thinkingConfig"] = thinking_cfg
    if gen_config:
        inner["generationConfig"] = gen_config

    # Safety settings: relax standard categories so coding tasks aren't blocked.
    # NOTE: HARM_CATEGORY_CIVIC_INTEGRITY is rejected by this API variant even
    # though it appears in the error's valid-list — only the 4 below are accepted.
    inner["safetySettings"] = [
        {"category": cat, "threshold": "BLOCK_NONE"}
        for cat in (
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        )
    ]

    # Build the wrapped envelope.
    project_id = get_project_id()
    envelope = {
        "project": project_id,
        "model": backend_model,
        "request": inner,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{uuid.uuid4().hex}",
    }
    return envelope


# ──────────────────────────────────────────────────────────────────────────────
# Gemini -> OpenAI response transformation
# ──────────────────────────────────────────────────────────────────────────────

def _extract_parts(parts: list) -> tuple[str, list, str]:
    """Extract (text, tool_calls, finish_reason_hint) from a Gemini parts list.

    tool_calls is a list of OpenAI-format tool_call dicts. The tool_call.id
    encodes the Gemini functionCall.id and thoughtSignature so they can be
    round-tripped back when the assistant message is sent in a later request.
    Format: call_<fc_id>|<thought_signature>
    """
    text_chunks = []
    tool_calls = []
    finish_hint = None

    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if "text" in part and part["text"]:
            text_chunks.append(part["text"])
        if "functionCall" in part:
            fc = part["functionCall"] or {}
            name = fc.get("name", "")
            args = fc.get("args", {})
            fc_id = fc.get("id", "")
            # thoughtSignature is a sibling of functionCall in the part, not inside it.
            thought_sig = part.get("thoughtSignature") or part.get("thought_signature") or ""
            if name:
                # Encode the Gemini fc_id and thoughtSignature into the OpenAI tool_call.id
                # so they survive the round-trip through Hermes's message history.
                # Format: call_<fc_id>|<thought_signature>
                encoded_id = f"call_{fc_id}|{thought_sig}" if (fc_id or thought_sig) else ""
                tool_calls.append({
                    "id": encoded_id,  # may be empty; caller assigns if so
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, (dict, list)) else str(args),
                    },
                })
        if "executableCode" in part:
            ec = part["executableCode"] or {}
            code = ec.get("code", "")
            if code:
                text_chunks.append(f"```python\n{code}\n```")
        if "codeExecutionResult" in part:
            cer = part["codeExecutionResult"] or {}
            out = cer.get("output", "")
            if out:
                text_chunks.append(f"```\n{out}\n```")
        if "thought" in part and part["thought"]:
            # Thinking summaries — include as plain text (Gemini sometimes
            # surfaces these). Keep it; the client can ignore.
            pass

    if tool_calls:
        finish_hint = "tool_calls"
    return "".join(text_chunks), tool_calls, finish_hint


def _finish_reason_from_gemini(candidate: dict, tool_calls: list) -> str:
    """Map Gemini finishReason / stopReason to OpenAI finish_reason."""
    if tool_calls:
        # If there are tool calls, OpenAI expects "tool_calls".
        fr = (candidate.get("finishReason") or "").upper()
        # Gemini may say STOP even when emitting function calls.
        return "tool_calls"
    fr = (candidate.get("finishReason") or candidate.get("stopReason") or "").upper()
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "BLOCKLIST": "content_filter",
        "PROHIBITED_CONTENT": "content_filter",
        "SPII": "content_filter",
        "MALFORMED_FUNCTION_CALL": "stop",
        "IMAGE_SAFETY": "content_filter",
        "LANGUAGE": "content_filter",
        "OTHER": "stop",
    }
    return mapping.get(fr, "stop")


def _extract_usage(response: dict) -> dict:
    """Extract OpenAI-format usage from a Gemini response."""
    usage = response.get("usageMetadata") or {}
    if not usage:
        cands = response.get("candidates") or []
        if cands:
            usage = cands[0].get("usageMetadata") or {}
    pt = usage.get("promptTokenCount", 0) or 0
    ct = usage.get("candidatesTokenCount", 0) or usage.get("completionTokenCount", 0) or 0
    tt = usage.get("totalTokenCount", (pt + ct)) or (pt + ct)
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
    }


def gemini_response_to_openai(response: dict, model: str) -> dict:
    """Transform an unwrapped Gemini generateContent response to OpenAI
    chat.completion format.
    """
    candidates = response.get("candidates") or []
    text = ""
    tool_calls = []
    finish_reason = "stop"

    if candidates:
        cand = candidates[0]
        content = cand.get("content") or {}
        parts = content.get("parts") or []
        text, tool_calls, _ = _extract_parts(parts)
        finish_reason = _finish_reason_from_gemini(cand, tool_calls)
        # Assign tool call ids.
        for i, tc in enumerate(tool_calls):
            if not tc["id"]:
                tc["id"] = f"call_{uuid.uuid4().hex[:24]}"
    else:
        # No candidates — possibly a promptFeedback block.
        pf = response.get("promptFeedback") or {}
        br = (pf.get("blockReason") or "").upper()
        if br:
            finish_reason = "content_filter"
            text = f"[Content blocked: {br}]"

    message = {"role": "assistant", "content": text if text else None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _extract_usage(response),
    }


def _extract_delta_from_parts(parts: list, prev_tool_names: set) -> tuple[str, list, list]:
    """Extract incremental (text, new_tool_calls, all_tool_names) from parts.

    For streaming, each chunk may contain partial text and/or function calls.
    We return text deltas and tool_call entries (with indices).
    """
    text_chunks = []
    tool_call_deltas = []
    current_names = set(prev_tool_names)

    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if "text" in part and part["text"]:
            text_chunks.append(part["text"])
        if "functionCall" in part:
            fc = part["functionCall"] or {}
            name = fc.get("name", "")
            args = fc.get("args", {})
            fc_id = fc.get("id", "")
            thought_sig = part.get("thoughtSignature") or part.get("thought_signature") or ""
            if name:
                current_names.add(name)
                encoded_id = f"call_{fc_id}|{thought_sig}" if (fc_id or thought_sig) else ""
                tool_call_deltas.append({
                    "index": len(current_names) - 1,
                    "id": encoded_id or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, (dict, list)) else str(args),
                    },
                })

    return "".join(text_chunks), tool_call_deltas, current_names


# ──────────────────────────────────────────────────────────────────────────────
# Upstream API calls
# ──────────────────────────────────────────────────────────────────────────────

def _make_upstream_request(url: str, envelope: dict, streaming: bool = False):
    """Build and return a urllib Request object for the upstream call."""
    body = json.dumps(envelope).encode()
    req = Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {get_access_token()}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
    req.add_header("X-Goog-Api-Client", "google-cloud-sdk vscode_cloudshelleditor/0.1")
    req.add_header("Client-Metadata", CLIENT_METADATA)
    if streaming:
        req.add_header("Accept", "text/event-stream")
    return req


def call_generate_content(envelope: dict, retry_on_401: bool = True, endpoint: str = None) -> dict:
    """Non-streaming call. Returns the unwrapped inner Gemini response dict."""
    ep = endpoint or PRIMARY_ENDPOINT
    url = f"{ep}/v1internal:generateContent"
    req = _make_upstream_request(url, envelope, streaming=False)
    try:
        with urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            raw = resp.read().decode()
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        if e.code == 401 and retry_on_401:
            _log("HTTP 401 received from upstream — force-refreshing access token and retrying...")
            try:
                get_access_token(force_refresh=True)
                get_project_id(force_refresh=True)
                envelope["project"] = get_project_id()
                return call_generate_content(envelope, retry_on_401=False, endpoint=ep)
            except Exception as refresh_err:
                _log(f"Force token refresh on 401 failed: {refresh_err}")
        # Retry on fallback endpoint if 429, 404, or 5xx on primary
        if ep == PRIMARY_ENDPOINT and FALLBACK_ENDPOINT and e.code in (404, 429, 500, 502, 503):
            _log(f"Upstream returned HTTP {e.code} on primary endpoint ({PRIMARY_ENDPOINT}). Retrying with fallback ({FALLBACK_ENDPOINT})...")
            try:
                return call_generate_content(envelope, retry_on_401=retry_on_401, endpoint=FALLBACK_ENDPOINT)
            except Exception as fb_err:
                _log(f"Fallback endpoint attempt failed: {fb_err}")
        raise UpstreamError(f"Upstream HTTP {e.code}: {detail}", status=e.code, body=detail)
    except URLError as e:
        if ep == PRIMARY_ENDPOINT and FALLBACK_ENDPOINT:
            _log(f"Primary endpoint network error ({PRIMARY_ENDPOINT}): {e}. Retrying with fallback ({FALLBACK_ENDPOINT})...")
            try:
                return call_generate_content(envelope, retry_on_401=retry_on_401, endpoint=FALLBACK_ENDPOINT)
            except Exception as fb_err:
                _log(f"Fallback endpoint attempt failed: {fb_err}")
        raise UpstreamError(f"Upstream network error: {e}", status=502)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise UpstreamError(f"Upstream returned non-JSON: {raw[:500]}", status=502)

    # Unwrap the envelope: {"response": {<gemini>}} or sometimes bare.
    if isinstance(payload, dict) and "response" in payload:
        return payload["response"]
    return payload


def stream_generate_content(envelope: dict, retry_on_401: bool = True, endpoint: str = None):
    """Streaming call. Yields parsed JSON event objects from the SSE stream.

    The Antigravity stream endpoint returns either:
      - SSE format: lines "data: {json}\n\n"
      - Or a bare JSON array of incremental response objects (some endpoints).
    We handle both.
    """
    ep = endpoint or PRIMARY_ENDPOINT
    url = f"{ep}/v1internal:streamGenerateContent?alt=sse"
    req = _make_upstream_request(url, envelope, streaming=True)
    try:
        resp = urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        if e.code == 401 and retry_on_401:
            _log("HTTP 401 received during stream setup — force-refreshing token and retrying...")
            try:
                get_access_token(force_refresh=True)
                get_project_id(force_refresh=True)
                envelope["project"] = get_project_id()
                yield from stream_generate_content(envelope, retry_on_401=False, endpoint=ep)
                return
            except Exception as refresh_err:
                _log(f"Force token refresh on stream 401 failed: {refresh_err}")
        if ep == PRIMARY_ENDPOINT and FALLBACK_ENDPOINT and e.code in (404, 429, 500, 502, 503):
            _log(f"Upstream stream returned HTTP {e.code} on primary endpoint ({PRIMARY_ENDPOINT}). Retrying with fallback ({FALLBACK_ENDPOINT})...")
            try:
                yield from stream_generate_content(envelope, retry_on_401=retry_on_401, endpoint=FALLBACK_ENDPOINT)
                return
            except Exception as fb_err:
                _log(f"Fallback stream attempt failed: {fb_err}")
        raise UpstreamError(f"Upstream stream HTTP {e.code}: {detail}", status=e.code, body=detail)
    except URLError as e:
        if ep == PRIMARY_ENDPOINT and FALLBACK_ENDPOINT:
            _log(f"Primary stream network error ({PRIMARY_ENDPOINT}): {e}. Retrying with fallback ({FALLBACK_ENDPOINT})...")
            try:
                yield from stream_generate_content(envelope, retry_on_401=retry_on_401, endpoint=FALLBACK_ENDPOINT)
                return
            except Exception as fb_err:
                _log(f"Fallback stream attempt failed: {fb_err}")
        raise UpstreamError(f"Upstream stream network error: {e}", status=502)

    try:
        buffer = ""
        # Read in chunks; urllib's response supports iteration by bytes.
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            if isinstance(chunk, bytes):
                text_chunk = chunk.decode("utf-8", errors="replace")
            else:
                text_chunk = chunk
            buffer += text_chunk

            # Try to parse complete SSE events or JSON array elements.
            while True:
                parsed_event = _try_parse_one_event(buffer)
                if parsed_event is None:
                    break
                event_obj, consumed = parsed_event
                buffer = buffer[consumed:]
                if event_obj is not None:
                    yield event_obj
        # Flush any remaining buffer.
        if buffer.strip():
            for obj in _parse_remaining(buffer):
                if obj is not None:
                    yield obj
    finally:
        resp.close()


def _try_parse_one_event(buffer: str):
    """Attempt to parse one SSE event or JSON array element from the buffer.

    Returns (event_obj_or_None, bytes_consumed) or None if incomplete.
    The event_obj may be None when it's a comment/heartbeat line.
    """
    # SSE "data:" lines.
    if "data:" in buffer[:10] or buffer.startswith("\n") or buffer.startswith(":"):
        # Find end of this SSE block (double newline).
        for sep in ("\n\n", "\r\n\r\n"):
            idx = buffer.find(sep)
            if idx != -1:
                block = buffer[:idx]
                consumed = idx + len(sep)
                obj = _parse_sse_block(block)
                return (obj, consumed)
        # Maybe single newline-terminated line (some servers use \n not \n\n).
        nl = buffer.find("\n")
        if nl != -1 and buffer.strip().startswith("data:"):
            # Only consume if we have a complete data line.
            line = buffer[:nl]
            consumed = nl + 1
            obj = _parse_sse_block(line)
            return (obj, consumed)
        return None  # incomplete

    # Bare JSON array stream: [ {...}, {...} ]
    if buffer.lstrip().startswith("["):
        obj, consumed = _try_parse_json_element_in_array(buffer)
        if obj is not None or consumed > 0:
            return (obj, consumed)
        return None

    # Bare JSON object stream: {...}{...}
    if buffer.lstrip().startswith("{"):
        obj, consumed = _try_parse_one_json_object(buffer)
        if obj is not None:
            return (obj, consumed)
        return None

    # Unknown prefix; skip a line to resync.
    nl = buffer.find("\n")
    if nl != -1:
        return (None, nl + 1)
    return None


def _parse_sse_block(block: str):
    """Parse an SSE block (one or more lines) into a JSON object or None."""
    data_lines = []
    for line in block.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith(":"):
            continue  # comment / heartbeat
        elif line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
            continue
        elif line.strip() == "":
            continue
        else:
            # Unexpected line; ignore.
            pass
    if not data_lines:
        return None
    data_str = "\n".join(data_lines).strip()
    if data_str == "[DONE]":
        return {"__done__": True}
    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        return None


def _try_parse_json_element_in_array(buffer: str):
    """For bare JSON array streams like [{...},{...}]. Parse one element."""
    s = buffer.lstrip()
    if not s.startswith("["):
        # Maybe we already consumed the opening bracket.
        pass
    # We need to find a complete top-level object. Use brace counting.
    obj, consumed = _try_parse_one_json_object(buffer)
    return (obj, consumed)


def _try_parse_one_json_object(buffer: str):
    """Parse one complete top-level JSON object from buffer using brace counting.

    Returns (obj, consumed_chars) or (None, 0) if incomplete.
    """
    # Find first '{'.
    start = buffer.find("{")
    if start == -1:
        return (None, 0)
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(buffer)):
        ch = buffer[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = buffer[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    return (obj, i + 1)
                except json.JSONDecodeError:
                    # Keep scanning.
                    continue
    return (None, 0)


def _parse_remaining(buffer: str):
    """Parse any remaining JSON objects in the buffer at stream end."""
    s = buffer.strip()
    if not s:
        return
    # SSE.
    for line in s.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            data = line[5:].strip()
            if data == "[DONE]":
                yield {"__done__": True}
                continue
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                pass
    # Bare JSON.
    if s.startswith("[") or s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, list):
                for item in obj:
                    yield item
            else:
                yield obj
        except json.JSONDecodeError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Error helper
# ──────────────────────────────────────────────────────────────────────────────

class UpstreamError(Exception):
    def __init__(self, message, status=502, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


def _openai_error(message: str, err_type: str = "api_error", code=None, status: int = 500):
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": None,
            "code": code,
        }
    }


# ──────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ──────────────────────────────────────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AntigravityProxy/2.0"

    def log_message(self, fmt, *args):
        # We do our own logging.
        pass

    def do_OPTIONS(self):
        """Handle CORS pre-flight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Requested-With, X-Goog-Api-Client, Client-Metadata",
        )
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Requested-With, X-Goog-Api-Client, Client-Metadata",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_json(self, code, message, err_type="api_error", http_code=None):
        self._send_json(http_code or code, _openai_error(message, err_type, code=code))

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None  # signal parse failure

    # ── GET routes ────────────────────────────────────────────────────────────

    def do_GET(self):
        t0 = time.time()
        path = urlparse(self.path).path
        try:
            if path == "/v1/models" or path == "/models":
                self._handle_models()
                self._log_req("GET", path, 200, time.time() - t0)
            elif path in ("/health", "/"):
                health_data = {
                    "status": "ok",
                    "service": "antigravity-proxy",
                    "token_file": _get_token_file_path(),
                    "auth": {"authenticated": False},
                }
                try:
                    data = _read_token_from_disk()
                    if data:
                        token_obj = data.get("token") if isinstance(data.get("token"), dict) else data
                        expiry_ts = _parse_expiry(token_obj.get("expiry"))
                        has_refresh = bool(token_obj.get("refresh_token") or data.get("refresh_token"))
                        rem = max(0, int(expiry_ts - time.time())) if expiry_ts > 0 else 0
                        health_data["auth"] = {
                            "authenticated": True,
                            "has_refresh_token": has_refresh,
                            "expires_in_seconds": rem,
                            "project_id": _cached_project_id or "pending_discovery",
                        }
                except Exception as ex:
                    health_data["auth"]["error"] = str(ex)
                self._send_json(200, health_data)
                self._log_req("GET", path, 200, time.time() - t0)
            else:
                self._send_error_json("not_found", "Not found", err_type="invalid_request_error", http_code=404)
                self._log_req("GET", path, 404, time.time() - t0)
        except Exception as e:
            _log(f"GET {path} error: {e}\n{traceback.format_exc()}")
            try:
                self._send_error_json("internal_error", str(e), http_code=500)
            except Exception:
                pass
            self._log_req("GET", path, 500, time.time() - t0)

    def _handle_models(self):
        now = int(time.time())
        models = []
        for name in MODEL_MAP:
            models.append({
                "id": name,
                "object": "model",
                "created": now,
                "owned_by": "google",
            })
        self._send_json(200, {"object": "list", "data": models})

    # ── POST routes ───────────────────────────────────────────────────────────

    def do_POST(self):
        t0 = time.time()
        path = urlparse(self.path).path
        try:
            if path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_chat()
                self._log_req("POST", path, 200, time.time() - t0)
            elif _GEMINI_PASSTHROUGH_RE.match(path):
                self._handle_gemini_passthrough(path)
                self._log_req("POST", path, 200, time.time() - t0)
            else:
                self._send_error_json("not_found", "Not found", err_type="invalid_request_error", http_code=404)
                self._log_req("POST", path, 404, time.time() - t0)
        except Exception as e:
            _log(f"POST {path} error: {e}\n{traceback.format_exc()}")
            try:
                self._send_error_json("internal_error", str(e), http_code=500)
            except Exception:
                pass
            self._log_req("POST", path, 500, time.time() - t0)

    def _log_req(self, method, path, status, duration):
        _log(f"{method} {path} {status} {duration:.3f}s")

    # ── Chat completions ──────────────────────────────────────────────────────

    def _handle_gemini_passthrough(self, path: str):
        """Handle a native Gemini generateContent request, wrapping it in the
        Antigravity envelope and returning the raw Gemini response (unwrapped).
        This is used by invoice_reader.py to send inline_data + responseSchema
        requests through the OAuth-funded proxy instead of a paid API key."""
        body = self._read_body()
        if body is None or not isinstance(body, dict):
            self._send_error_json("invalid_request", "Invalid JSON body", err_type="invalid_request_error", http_code=400)
            return

        model = _GEMINI_PASSTHROUGH_RE.match(path).group(1)

        try:
            envelope = _build_passthrough_envelope(model, body)
        except UpstreamError as e:
            self._send_error_json("upstream_error", str(e), err_type="api_error", http_code=e.status)
            return
        except RuntimeError as e:
            self._send_error_json("auth_error", str(e), err_type="authentication_error", http_code=401)
            return
        except Exception as e:
            self._send_error_json("request_error", f"Failed to build request: {e}", err_type="invalid_request_error", http_code=400)
            return

        try:
            gemini_resp = call_generate_content(envelope)
        except UpstreamError as e:
            self._send_error_json("upstream_error", str(e), err_type="api_error", http_code=e.status)
            return
        except Exception as e:
            self._send_error_json("upstream_error", f"Upstream call failed: {e}", err_type="api_error", http_code=502)
            return

        self._send_json(200, gemini_resp)

    def _handle_chat(self):
        body = self._read_body()
        if body is None:
            self._send_error_json("invalid_request", "Invalid JSON body", err_type="invalid_request_error", http_code=400)
            return
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Request body must be a JSON object", err_type="invalid_request_error", http_code=400)
            return

        openai_model = body.get("model") or DEFAULT_MODEL
        stream = bool(body.get("stream", False))

        # Map model. MODEL_MAP values are (backend_name, thinking_level) tuples.
        model_entry = MODEL_MAP.get(openai_model)
        if model_entry:
            backend_model, thinking_level = model_entry
        else:
            # Allow pass-through of unknown models.
            backend_model = openai_model
            thinking_level = None

        # Build the Gemini request envelope.
        try:
            envelope = _build_gemini_request(body, backend_model, thinking_level)
        except UpstreamError as e:
            self._send_error_json("upstream_error", str(e), err_type="api_error", http_code=e.status)
            return
        except RuntimeError as e:
            self._send_error_json("auth_error", str(e), err_type="authentication_error", http_code=401)
            return
        except Exception as e:
            self._send_error_json("request_error", f"Failed to build request: {e}", err_type="invalid_request_error", http_code=400)
            return

        # Validate we have at least one content entry.
        inner = envelope.get("request", {})
        if not inner.get("contents"):
            self._send_error_json("invalid_request", "No messages provided", err_type="invalid_request_error", http_code=400)
            return

        if stream:
            self._handle_stream(envelope, openai_model)
        else:
            self._handle_nonstream(envelope, openai_model)

    def _handle_nonstream(self, envelope, openai_model):
        try:
            gemini_resp = call_generate_content(envelope)
        except UpstreamError as e:
            self._send_error_json("upstream_error", str(e), err_type="api_error", http_code=e.status)
            return
        except Exception as e:
            self._send_error_json("upstream_error", f"Upstream call failed: {e}", err_type="api_error", http_code=502)
            return

        try:
            openai_resp = gemini_response_to_openai(gemini_resp, openai_model)
        except Exception as e:
            _log(f"Response transform error: {e}\n{traceback.format_exc()}")
            # Fallback: return raw text if we can find any.
            try:
                text = (gemini_resp.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", ""))
            except Exception:
                text = ""
            openai_resp = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": openai_model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text or "[error: could not parse response]"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        self._send_json(200, openai_resp)

    def _handle_stream(self, envelope, openai_model):
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        # Send SSE headers.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except Exception:
            return

        def send_sse(obj):
            data = f"data: {json.dumps(obj)}\n\n"
            try:
                self.wfile.write(data.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return False
            return True

        # Initial role chunk.
        first_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": openai_model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }],
        }
        if not send_sse(first_chunk):
            return

        try:
            gen = stream_generate_content(envelope)
        except UpstreamError as e:
            # Streaming failed — fall back to non-streaming and return as a single chunk.
            # This handles the case where the streamGenerateContent endpoint rejects
            # tool-result round-trips with 400 "invalid argument".
            if e.status == 400:
                try:
                    resp = call_generate_content(envelope)
                    openai_resp = gemini_response_to_openai(resp, openai_model)
                    msg = openai_resp["choices"][0]["message"]
                    delta = {}
                    if msg.get("content"):
                        delta["content"] = msg["content"]
                    if msg.get("tool_calls"):
                        delta["tool_calls"] = msg["tool_calls"]
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": openai_model,
                        "choices": [{
                            "index": 0,
                            "delta": delta,
                            "finish_reason": openai_resp["choices"][0].get("finish_reason", "stop"),
                        }],
                    }
                    if openai_resp.get("usage"):
                        chunk["usage"] = openai_resp["usage"]
                    send_sse(chunk)
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    return
                except Exception:
                    pass  # fall through to error chunk
            err_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": openai_model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"[Error: {e}]"},
                    "finish_reason": "stop",
                }],
            }
            send_sse(err_chunk)
            send_sse({"__done__": True})  # sentinel handled below
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        except Exception as e:
            err_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": openai_model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"[Error: {e}]"},
                    "finish_reason": "stop",
                }],
            }
            send_sse(err_chunk)
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return

        finish_reason = "stop"
        seen_tool_names = set()
        total_usage = None
        events_processed = 0

        try:
            for event in gen:
                if not isinstance(event, dict):
                    continue
                if event.get("__done__"):
                    break

                events_processed += 1

                # Unwrap envelope if present.
                resp = event.get("response", event)

                # Check for usage metadata (often on the last chunk).
                usage_meta = resp.get("usageMetadata")
                if usage_meta:
                    total_usage = usage_meta

                candidates = resp.get("candidates") or []
                if not candidates:
                    continue

                cand = candidates[0]
                content = cand.get("content") or {}
                parts = content.get("parts") or []

                text_delta, tool_call_deltas, seen_tool_names = _extract_delta_from_parts(
                    parts, seen_tool_names
                )

                # Determine finish reason.
                fr = cand.get("finishReason")
                if fr:
                    if tool_call_deltas:
                        finish_reason = "tool_calls"
                    else:
                        fr_up = str(fr).upper()
                        mapping = {
                            "STOP": "stop",
                            "MAX_TOKENS": "length",
                            "SAFETY": "content_filter",
                            "RECITATION": "content_filter",
                            "BLOCKLIST": "content_filter",
                            "PROHIBITED_CONTENT": "content_filter",
                            "SPII": "content_filter",
                            "IMAGE_SAFETY": "content_filter",
                            "LANGUAGE": "content_filter",
                            "OTHER": "stop",
                        }
                        finish_reason = mapping.get(fr_up, "stop")

                # Build delta.
                delta = {}
                if text_delta:
                    delta["content"] = text_delta
                if tool_call_deltas:
                    delta["tool_calls"] = tool_call_deltas

                if not delta and not fr:
                    # Empty chunk with nothing — skip.
                    continue

                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": openai_model,
                    "choices": [{
                        "index": 0,
                        "delta": delta,
                        "finish_reason": None,
                    }],
                }
                if not send_sse(chunk):
                    return

            # Final chunk with finish_reason.
            final_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": openai_model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }],
            }
            # Include usage in the final chunk if the client requested
            # stream_options.include_usage (OpenAI convention).
            if total_usage:
                pt = total_usage.get("promptTokenCount", 0) or 0
                ct = total_usage.get("candidatesTokenCount", 0) or total_usage.get("completionTokenCount", 0) or 0
                tt = total_usage.get("totalTokenCount", (pt + ct)) or (pt + ct)
                final_chunk["usage"] = {
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_tokens": tt,
                }
            send_sse(final_chunk)

            # Termination sentinel.
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

        except Exception as e:
            _log(f"Streaming error: {e}")
            # If the stream endpoint returned 400, try non-streaming as fallback.
            # The streamGenerateContent endpoint sometimes rejects tool-result round-trips.
            if "400" in str(e):
                try:
                    resp = call_generate_content(envelope)
                    openai_resp = gemini_response_to_openai(resp, openai_model)
                    msg = openai_resp["choices"][0]["message"]
                    delta = {}
                    if msg.get("content"):
                        delta["content"] = msg["content"]
                    if msg.get("tool_calls"):
                        delta["tool_calls"] = msg["tool_calls"]
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": openai_model,
                        "choices": [{
                            "index": 0,
                            "delta": delta,
                            "finish_reason": openai_resp["choices"][0].get("finish_reason", "stop"),
                        }],
                    }
                    if openai_resp.get("usage"):
                        chunk["usage"] = openai_resp["usage"]
                    send_sse(chunk)
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    return
                except Exception:
                    pass  # fall through to error chunk
            # Try to send an error chunk then close.
            try:
                err_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": openai_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": f"\n[stream error: {e}]"},
                        "finish_reason": "stop",
                    }],
                }
                send_sse(err_chunk)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Server entry point
# ──────────────────────────────────────────────────────────────────────────────

def _preflight_check():
    """Validate token availability before starting the server."""
    token_path = _get_token_file_path()
    if not os.path.isfile(token_path) and "ANTIGRAVITY_REFRESH_TOKEN" not in os.environ:
        _log(f"ERROR: Token file not found at {token_path}")
        _log("Please authenticate first using ONE of these methods:")
        _log("  1. Run: python antigravity_proxy.py --login  (recommended on Windows)")
        _log("  2. Double-click: login.bat")
        _log("  3. Run: agy (if you installed the Antigravity CLI)")
        _log("  4. Set the ANTIGRAVITY_REFRESH_TOKEN environment variable")
        return False

    try:
        tok = get_access_token()
        _log("OAuth authentication check PASSED.")
        return True
    except Exception as e:
        _log(f"ERROR: Authentication check failed: {e}")
        _log("Try re-authenticating with: python antigravity_proxy.py --login")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Antigravity OpenAI-compatible proxy (Cloud Code Assist API)"
    )
    default_port = int(os.environ.get("ANTIGRAVITY_PORT") or os.environ.get("PORT") or 8877)
    default_host = os.environ.get("ANTIGRAVITY_HOST") or os.environ.get("HOST") or "127.0.0.1"
    parser.add_argument("--port", type=int, default=default_port, help=f"Port to listen on (default: {default_port})")
    parser.add_argument("--host", default=default_host, help=f"Host to bind to (default: {default_host})")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Launch interactive browser login to obtain OAuth tokens",
    )
    parser.add_argument(
        "--login-port",
        type=int,
        default=51121,
        help="Local callback port for OAuth login (default: 51121)",
    )
    parser.add_argument(
        "--check-token",
        action="store_true",
        help="Inspect and display token validity and exit",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force refresh access token and exit",
    )
    args = parser.parse_args()

    if args.login:
        run_oauth_login(port=args.login_port)
        sys.exit(0)

    if args.check_token:
        success = check_token()
        sys.exit(0 if success else 1)

    if args.refresh:
        try:
            get_access_token(force_refresh=True)
            _log("Token refresh complete and verified.")
            sys.exit(0)
        except Exception as e:
            _log(f"Token refresh failed: {e}")
            sys.exit(1)

    if not _preflight_check():
        sys.exit(1)

    # Start proactive background token refresher daemon thread
    stop_refresher = threading.Event()
    refresher_thread = threading.Thread(
        target=_background_token_refresher,
        args=(stop_refresher, 60),
        name="TokenRefresher",
        daemon=True,
    )
    refresher_thread.start()

    # Pre-discover the project ID so the first request is fast.
    try:
        get_project_id()
    except Exception as e:
        _log(f"WARNING: Could not pre-discover project ID: {e}")
        _log("It will be discovered on the first request.")

    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True
    _log(f"Antigravity proxy listening on http://{args.host}:{args.port}/v1")
    _log(f"  Default model: {DEFAULT_MODEL}")
    _log(f"  Models: {', '.join(MODEL_MAP.keys())}")
    _log(f"  Token:  {_get_token_file_path()}")
    _log("  Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("\nShutting down...")
        stop_refresher.set()
        server.shutdown()


if __name__ == "__main__":
    main()
