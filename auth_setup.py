#!/usr/bin/env python3
"""One-time helper: obtain a Spotify refresh token (authorization-code flow + PKCE).

Prerequisites
  1. Create an app at https://developer.spotify.com/dashboard
  2. Add this Redirect URI to the app (exact match):  http://127.0.0.1:8888/callback
  3. export SPOTIFY_CLIENT_ID=...  SPOTIFY_CLIENT_SECRET=...

Run
  python3 auth_setup.py            # opens your browser
  python3 auth_setup.py --no-browser   # just prints the URL to open

The refresh token is printed once. Put it in SPOTIFY_REFRESH_TOKEN. This script
makes no network calls other than the Spotify authorize/token endpoints, and
stores nothing on disk.
"""

import argparse
import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spotify_mcp import client  # noqa: E402

DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"


def pkce_pair():
    """Return (code_verifier, code_challenge) for the S256 PKCE method."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(client_id, redirect_uri, state, challenge, scopes=client.SCOPES):
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        # Force the consent screen so newly-added scopes are actually granted.
        "show_dialog": "true",
    })
    return f"{client.AUTH_BASE}/authorize?{params}"


def exchange_code(client_id, client_secret, code, redirect_uri, verifier):
    """Exchange an authorization code for tokens (returns Spotify's token response)."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return client._request(
        "POST", f"{client.AUTH_BASE}/api/token",
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": redirect_uri, "code_verifier": verifier},
        headers={"Authorization": f"Basic {basic}"})


def wait_for_callback(redirect_uri, expected_state, timeout=180):
    """Serve one request on the redirect URI's host:port and return the query params."""
    u = urllib.parse.urlparse(redirect_uri)
    if u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Redirect URI must be a loopback address (e.g. http://127.0.0.1:8888/callback).")
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("state", [None])[0] == expected_state:
                result.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>spotify-mcp: authorization received. You can close this tab.</h1>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port or 80), Handler)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    return srv, t, result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Obtain a Spotify refresh token for spotify-mcp.")
    ap.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    ap.add_argument("--timeout", type=int, default=180, help="seconds to wait for the callback")
    args = ap.parse_args(argv)

    cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    redirect = os.environ.get("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT).strip()
    if not cid or not secret:
        sys.exit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET first (see README).")

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(cid, redirect, state, challenge)

    srv, thread, result = wait_for_callback(redirect, state, args.timeout)
    print("Open this URL to authorize:\n\n  " + url + "\n" if args.no_browser
          else "Opening your browser to authorize...")
    if not args.no_browser:
        webbrowser.open(url)
    thread.join(timeout=args.timeout)
    srv.server_close()

    if "error" in result:
        sys.exit(f"Spotify returned an error: {result['error']}")
    if "code" not in result:
        sys.exit("No authorization code received (timed out, or state mismatch). Try again.")

    tokens = exchange_code(cid, secret, result["code"], redirect, verifier)
    granted = tokens.get("scope", "")
    print("\nAuthorized. Set this in your MCP client's environment:\n")
    print(f"  SPOTIFY_REFRESH_TOKEN={tokens['refresh_token']}\n")
    print("Treat it like a password. Granted scopes: " + (granted or "(not reported)"))


if __name__ == "__main__":
    main()
