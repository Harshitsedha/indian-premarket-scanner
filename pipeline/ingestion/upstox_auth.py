"""
upstox_auth.py — One-time OAuth2 authorisation script.

Run once manually from your LOCAL machine (not VPS):
    python -m ingestion.upstox_auth

What it does:
  1. Builds the Upstox auth URL and opens it in your browser
  2. Spins a local HTTP server on port 8765 to catch the callback
  3. Exchanges the auth code for access_token + extended_token
  4. Stores tokens in Postgres (upstox_tokens table)
  5. Prints tokens to console as a fallback

Re-run once per year when the extended_token approaches expiry
(the weekly expiry-check job will warn you 30 days in advance).
"""

import sys
import json
import base64
import time
import urllib.parse
import webbrowser
import http.server
import threading
from datetime import datetime, timezone, timedelta

import httpx
import psycopg2
from loguru import logger

from utils.config import settings


# ── Captured from callback ──────────────────────────────────────────────────
_auth_code: str | None = None
_server_ready = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler — catches exactly one GET /callback?code=XXX."""

    def do_GET(self):
        global _auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _auth_code = params["code"][0]
            body = b"<h2>Auth complete. You can close this tab.</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            logger.info("Auth code received via callback.")
        else:
            # Upstox sometimes hits /favicon.ico — ignore silently
            self.send_response(204)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass


def _run_server(server: http.server.HTTPServer) -> None:
    _server_ready.set()
    # Serve until _auth_code is populated; timeout after 120 s
    deadline = time.time() + 120
    while _auth_code is None and time.time() < deadline:
        server.handle_request()
    # One extra handle to flush any trailing browser request
    server.handle_request()
    server.server_close()


# ── JWT helper ─────────────────────────────────────────────────────────────
def _jwt_exp(token: str) -> "datetime | None":
    """Return the exp claim from a JWT without verifying its signature."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore padding
        data = json.loads(base64.urlsafe_b64decode(payload_b64))
        if exp := data.get("exp"):
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        pass
    return None


# ── Step 1: Build auth URL ──────────────────────────────────────────────────
def build_auth_url() -> str:
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": settings.upstox_api_key,
        "redirect_uri": settings.upstox_redirect_uri,
    })
    return f"https://api.upstox.com/v2/login/authorization/dialog?{params}"


# ── Step 2: Spin up callback server + open browser ──────────────────────────
def get_auth_code() -> str:
    parsed_uri = urllib.parse.urlparse(settings.upstox_redirect_uri)
    port = parsed_uri.port or 8765

    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    thread = threading.Thread(target=_run_server, args=(server,), daemon=True)
    thread.start()
    _server_ready.wait(timeout=5)

    url = build_auth_url()
    logger.info("Opening browser for Upstox login...")
    print(f"\n{'='*60}")
    print("  Opening Upstox login in your browser.")
    print("  If it doesn't open, paste this URL manually:\n")
    print(f"  {url}")
    print(f"{'='*60}\n")
    webbrowser.open(url)

    deadline = time.time() + 120
    while _auth_code is None and time.time() < deadline:
        time.sleep(0.5)

    if _auth_code is None:
        logger.error("Timed out waiting for auth callback (120 s).")
        print("\n[ERR] No callback received within 120 seconds.")
        print("  Make sure the redirect URI in your Upstox app settings is:")
        print(f"  {settings.upstox_redirect_uri}")
        sys.exit(1)

    logger.info("Auth code captured successfully.")
    return _auth_code


# ── Step 3: Exchange code for tokens ────────────────────────────────────────
def exchange_code(code: str) -> dict:
    logger.info("Exchanging auth code for tokens...")

    resp = httpx.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data={
            "code": code,
            "client_id": settings.upstox_api_key,
            "client_secret": settings.upstox_api_secret,
            "redirect_uri": settings.upstox_redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )

    if resp.status_code != 200:
        logger.error(f"Token exchange failed: {resp.status_code} {resp.text}")
        print(f"\n[ERR] Token exchange failed (HTTP {resp.status_code})")
        print(f"  Response: {resp.text}")
        print("\n  Common causes:")
        print("  - redirect_uri doesn't exactly match your Upstox app settings")
        print("  - Auth code has already been used (codes are single-use)")
        print("  - API key or secret is wrong")
        sys.exit(1)

    data = resp.json()

    # Upstox v2 wraps response in {"status":"success","data":{...}}
    if "data" in data:
        data = data["data"]

    required = {"access_token", "extended_token"}
    if not required.issubset(data.keys()):
        logger.error(f"Unexpected token response: {data}")
        print(f"\n[ERR] Unexpected response shape: {json.dumps(data, indent=2)}")
        sys.exit(1)

    logger.info("Tokens received from Upstox.")
    return data


# ── Step 4: Store tokens in Postgres ────────────────────────────────────────
def store_tokens(
    access_token: str,
    refresh_token: str,
    extended_token: str,
    expires_in: int,
) -> None:
    # Decode the extended_token's actual exp claim so the expiry-check job gets
    # an accurate ~1-year window. Fall back to 365 days if decode fails.
    expires_at = (
        _jwt_exp(extended_token)
        or datetime.now(timezone.utc) + timedelta(days=365)
    )

    try:
        conn = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO upstox_tokens (access_token, refresh_token, extended_token, expires_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (access_token, refresh_token, extended_token, expires_at),
                )
        conn.close()
        logger.info("Tokens stored in Postgres (upstox_tokens).")
        print("[OK] Tokens saved to database.")
    except Exception as e:
        logger.error(f"Failed to store tokens in Postgres: {e}")
        print(f"\n[ERR] DB write failed: {e}")
        print("  Tokens are printed below — paste them into .env manually.")


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    print("\n" + "=" * 60)
    print("  Upstox OAuth2 — one-time authorisation")
    print("=" * 60)

    missing = [
        name for name, val in [
            ("UPSTOX_API_KEY",    settings.upstox_api_key),
            ("UPSTOX_API_SECRET", settings.upstox_api_secret),
            ("UPSTOX_REDIRECT_URI", settings.upstox_redirect_uri),
            ("POSTGRES_PASSWORD", settings.postgres_password),
        ]
        if not val
    ]
    if missing:
        print(f"\n[ERR] Missing required env vars: {', '.join(missing)}")
        print("  Check your .env file.")
        sys.exit(1)

    print(f"\n  API Key  : {settings.upstox_api_key[:8]}{'*' * 12}")
    print(f"  Redirect : {settings.upstox_redirect_uri}")
    print()

    code = get_auth_code()
    token_data = exchange_code(code)

    access_token   = token_data["access_token"]
    extended_token = token_data["extended_token"]
    expires_in     = token_data.get("expires_in", 43200)  # unused for expires_at; kept for compat

    store_tokens(access_token, extended_token, extended_token, expires_in)

    print()
    print("=" * 60)
    print("  Tokens (add to .env as fallback):")
    print("=" * 60)
    print(f"  UPSTOX_ACCESS_TOKEN={access_token}")
    print(f"  UPSTOX_EXTENDED_TOKEN={extended_token}")
    print("=" * 60)
    print()
    print("[OK] Auth complete.")
    print("  Re-run this script in ~1 year when the extended_token expires.")
    print("  The weekly expiry-check job will warn you 30 days in advance.")
    print()


if __name__ == "__main__":
    main()
