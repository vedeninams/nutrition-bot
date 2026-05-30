"""
setup_withings.py — One-time Withings OAuth bootstrap.

Run this once per user to connect their Withings account to the bot. After
this script writes the access + refresh tokens to the database, the hourly
`withings_sync.py` cron job takes over and pulls weight readings forever
without further user involvement.

Prerequisites — see README's Withings setup section:
  1. Register a developer app at https://developer.withings.com/
       Application type: "Withings (public API)"
       Callback URL: any URL you control or http://localhost:8765/callback
  2. Copy CLIENT_ID and CONSUMER_SECRET into .env as
       WITHINGS_CLIENT_ID=...
       WITHINGS_CLIENT_SECRET=...
       WITHINGS_REDIRECT_URI=...    (same URL you registered above)

Usage:
    python setup_withings.py --user 123456789

What it does:
  1. Builds the Withings authorization URL with your client_id and the
     requested scope (user.metrics for weight).
  2. Prints the URL — open it in a browser, log in to Withings, click
     "Allow".
  3. Withings redirects to your callback URL with `?code=XYZ` in the query
     string. Copy that code from your browser's address bar.
  4. Paste the code back into the terminal.
  5. The script exchanges the code for access + refresh tokens and stores
     them in the `withings_auth` table for the given Telegram user_id.
  6. Done. The hourly cron job picks up from here.
"""

import argparse
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

import database as db

load_dotenv()

WITHINGS_CLIENT_ID = os.getenv("WITHINGS_CLIENT_ID", "").strip()
WITHINGS_CLIENT_SECRET = os.getenv("WITHINGS_CLIENT_SECRET", "").strip()
WITHINGS_REDIRECT_URI = os.getenv(
    "WITHINGS_REDIRECT_URI", "http://localhost:8765/callback"
).strip()

WITHINGS_AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"

# Minimum scope for weight readings. See Withings API docs.
WITHINGS_SCOPE = "user.metrics"


def _build_auth_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": WITHINGS_CLIENT_ID,
        "scope": WITHINGS_SCOPE,
        "redirect_uri": WITHINGS_REDIRECT_URI,
        "state": state,
    }
    return f"{WITHINGS_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_code(code: str) -> dict:
    """Exchange an authorization code for access + refresh tokens.
    Returns the `body` dict from Withings on success, or raises."""
    payload = {
        "action": "requesttoken",
        "client_id": WITHINGS_CLIENT_ID,
        "client_secret": WITHINGS_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": WITHINGS_REDIRECT_URI,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(WITHINGS_TOKEN_URL, data=payload)
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 0:
        raise RuntimeError(
            f"Withings rejected the code: status={body.get('status')} body={body}"
        )
    return body.get("body", {})


def main() -> None:
    parser = argparse.ArgumentParser(description="Withings OAuth bootstrap")
    parser.add_argument(
        "--user", type=int, required=True,
        help="Telegram user_id to connect (e.g. your own).",
    )
    args = parser.parse_args()

    missing = [
        name for name, value in [
            ("WITHINGS_CLIENT_ID", WITHINGS_CLIENT_ID),
            ("WITHINGS_CLIENT_SECRET", WITHINGS_CLIENT_SECRET),
        ] if not value
    ]
    if missing:
        print(
            f"❌ Missing env vars: {', '.join(missing)}\n"
            f"   Add them to .env (see setup_withings.py docstring at the top "
            f"of this file).",
            file=sys.stderr,
        )
        sys.exit(1)

    db.init_db()
    db.ensure_user(args.user)

    state = f"bot-user-{args.user}"
    auth_url = _build_auth_url(state)

    print()
    print("═" * 76)
    print("  Step 1 — Open this URL in your browser:")
    print()
    print(f"  {auth_url}")
    print()
    print("  Step 2 — Log into Withings and click 'Allow access'.")
    print()
    print("  Step 3 — Withings will redirect your browser to:")
    print(f"    {WITHINGS_REDIRECT_URI}?code=XXXXXXXX&state={state}")
    print()
    print("  (The page may show 'site can't be reached' — that's fine. The")
    print("  important thing is the `code=` value in the URL bar.)")
    print()
    print("  Step 4 — Copy the value of `code=` (just the code, not the whole URL)")
    print("  and paste it below.")
    print("═" * 76)
    print()

    code = input("Paste the code here: ").strip()
    if not code:
        print("❌ No code provided. Aborting.", file=sys.stderr)
        sys.exit(1)

    try:
        body = _exchange_code(code)
    except Exception as e:
        print(f"❌ Token exchange failed: {e}", file=sys.stderr)
        sys.exit(1)

    access = body.get("access_token")
    refresh = body.get("refresh_token")
    expires_in = int(body.get("expires_in", 10800))
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    withings_uid = str(body.get("userid") or "")

    if not access or not refresh:
        print(f"❌ Withings response missing tokens: {body}", file=sys.stderr)
        sys.exit(1)

    db.save_withings_auth(
        user_id=args.user,
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        withings_user_id=withings_uid or None,
    )

    print()
    print(f"✅ Connected. Stored tokens for Telegram user_id={args.user}")
    print(f"   Withings user_id: {withings_uid or '(not returned)'}")
    print(f"   Access token expires at: {expires_at}")
    print()
    print("The hourly withings_sync.py cron job will start pulling weight")
    print("readings on its next tick. To test immediately, run:")
    print(f"   python withings_sync.py --user {args.user}")


if __name__ == "__main__":
    main()
