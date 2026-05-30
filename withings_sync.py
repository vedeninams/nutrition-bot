"""
withings_sync.py — Pull weight readings from the Withings Health API.

Runs hourly via cron. For each user who has connected their Withings account
(via setup_withings.py), this script:

  1. Refreshes the access_token if it's about to expire (or has expired).
  2. Calls Withings' measure?action=getmeas endpoint to fetch new weight
     readings since the last successful sync.
  3. Inserts new readings into the `weight_readings` table.
     UNIQUE(user_id, measured_at) makes this idempotent — re-fetching the
     same window is safe.
  4. `database.insert_weight_reading` automatically updates
     `daily_stats.weight_kg` to the day's MIN reading, so existing summary
     code continues to see "one weight per day."

Withings API docs: https://developer.withings.com/api-reference

Run:
    python withings_sync.py                # sync all connected users
    python withings_sync.py --user 12345   # sync one user
    python withings_sync.py --days 7       # fetch the last 7 days (default 2)

Cron entry (added to /etc/cron.d or root's crontab):
    # Withings weight sync — every hour at :07
    7 * * * * cd /opt/nutrition-bot && /opt/nutrition-bot/venv/bin/python withings_sync.py >> /opt/nutrition-bot/withings.log 2>&1
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

import database as db

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

WITHINGS_CLIENT_ID = os.getenv("WITHINGS_CLIENT_ID", "")
WITHINGS_CLIENT_SECRET = os.getenv("WITHINGS_CLIENT_SECRET", "")

# Withings API endpoints (stable, documented).
WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_MEASURE_URL = "https://wbsapi.withings.net/measure"

# Withings measurement type code for weight (kg). See:
# https://developer.withings.com/api-reference/#section/Withings-Data-API/Measurement-Types
WITHINGS_TYPE_WEIGHT = 1

# Refresh the access token if it expires within this many seconds. Withings
# access tokens last 3 hours; we refresh proactively to avoid a race with
# the API call that follows.
TOKEN_REFRESH_LEEWAY_S = 600   # 10 min

# Dedicated log file so it's easy to tail. Separate from the main bot log.
_log_path = Path(__file__).parent / "withings.log"
logging.basicConfig(
    filename=_log_path,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
# Also echo to stdout when run interactively (cron captures stdout via >>).
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_console)
log = logging.getLogger("withings_sync")


# ─────────────────────────────────────────────────────────────────────────────
# OAuth token refresh
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    """Tolerant ISO parser — accepts 'Z' suffix and '+00:00' both."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def refresh_access_token(user_id: int) -> Optional[dict]:
    """Use the stored refresh_token to fetch a new access_token. Writes the
    new tokens back to the DB and returns the updated row dict (or None on
    failure).
    """
    auth = db.get_withings_auth(user_id)
    if not auth:
        log.warning(f"user={user_id} refresh: no withings_auth row")
        return None

    payload = {
        "action": "requesttoken",
        "client_id": WITHINGS_CLIENT_ID,
        "client_secret": WITHINGS_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": auth["refresh_token"],
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(WITHINGS_TOKEN_URL, data=payload)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        log.error(f"user={user_id} refresh: HTTP failed: {e!r}")
        return None

    # Withings wraps everything in {"status": int, "body": {...}}. status=0 OK.
    if body.get("status") != 0:
        log.error(f"user={user_id} refresh: API status={body.get('status')} body={body}")
        return None

    data = body.get("body", {})
    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token", auth["refresh_token"])
    expires_in = int(data.get("expires_in", 10800))
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    withings_uid = str(data.get("userid") or auth.get("withings_user_id") or "")

    if not new_access:
        log.error(f"user={user_id} refresh: no access_token in response: {data}")
        return None

    db.save_withings_auth(
        user_id=user_id,
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=expires_at,
        withings_user_id=withings_uid or None,
    )
    log.info(f"user={user_id} refresh OK; new expiry={expires_at}")
    return db.get_withings_auth(user_id)


def ensure_fresh_token(user_id: int) -> Optional[dict]:
    """Return the user's withings_auth row, refreshing the access_token first
    if it's expired or about to expire (within TOKEN_REFRESH_LEEWAY_S)."""
    auth = db.get_withings_auth(user_id)
    if not auth:
        return None

    try:
        expires_at = _parse_iso(auth["expires_at"])
    except (ValueError, KeyError):
        # Malformed timestamp — force a refresh.
        return refresh_access_token(user_id)

    leeway = timedelta(seconds=TOKEN_REFRESH_LEEWAY_S)
    if datetime.now(timezone.utc) + leeway >= expires_at:
        return refresh_access_token(user_id)
    return auth


# ─────────────────────────────────────────────────────────────────────────────
# Measurement fetch
# ─────────────────────────────────────────────────────────────────────────────

def _decode_weight(value: int, unit: int) -> float:
    """Withings encodes measurements as (value, unit) where the real number
    is value * 10^unit. E.g. (67230, -3) = 67.230 kg."""
    return value * (10 ** unit)


def fetch_recent_weights(
    user_id: int,
    days: int = 2,
) -> Optional[list[dict]]:
    """Call Withings measure?action=getmeas for the last `days` days.
    Returns a list of {measured_at: ISO, weight_kg: float} dicts, or None
    on transport/API failure.

    Idempotent re-fetching: the DB insert dedups on (user_id, measured_at)
    so calling this repeatedly with overlapping windows is fine.
    """
    auth = ensure_fresh_token(user_id)
    if not auth:
        return None

    end_ts = int(time.time())
    start_ts = end_ts - days * 86400

    payload = {
        "action": "getmeas",
        "meastype": str(WITHINGS_TYPE_WEIGHT),
        "category": "1",          # 1 = real measurements (not user-entered targets)
        "startdate": str(start_ts),
        "enddate": str(end_ts),
    }
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(WITHINGS_MEASURE_URL, data=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        log.error(f"user={user_id} getmeas: HTTP failed: {e!r}")
        return None

    if body.get("status") != 0:
        log.error(f"user={user_id} getmeas: API status={body.get('status')} body={body}")
        # Status 401/283 = bad token: force a refresh on the next run by
        # bumping expiry. Done deliberately small so we try again hourly.
        return None

    readings: list[dict] = []
    for grp in body.get("body", {}).get("measuregrps", []) or []:
        ts = int(grp.get("date", 0))
        for m in grp.get("measures", []) or []:
            if m.get("type") != WITHINGS_TYPE_WEIGHT:
                continue
            try:
                kg = _decode_weight(int(m["value"]), int(m["unit"]))
            except (KeyError, ValueError):
                continue
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            readings.append({"measured_at": iso, "weight_kg": float(kg)})
    return readings


# ─────────────────────────────────────────────────────────────────────────────
# Per-user sync
# ─────────────────────────────────────────────────────────────────────────────

def sync_user(user_id: int, days: int = 2) -> dict:
    """Refresh tokens if needed, fetch last `days` of readings, insert any
    new ones. Returns a small stats dict for logging."""
    readings = fetch_recent_weights(user_id, days=days)
    if readings is None:
        return {"user_id": user_id, "ok": False, "fetched": 0, "inserted": 0}

    inserted = 0
    for r in readings:
        if db.insert_weight_reading(
            user_id=user_id,
            measured_at=r["measured_at"],
            weight_kg=r["weight_kg"],
            source="withings_api",
        ):
            inserted += 1

    log.info(
        f"user={user_id} sync OK fetched={len(readings)} new={inserted}"
    )
    return {"user_id": user_id, "ok": True, "fetched": len(readings), "inserted": inserted}


def sync_all_users(days: int = 2) -> list[dict]:
    """Iterate every user who has stored Withings tokens."""
    user_ids = db.list_withings_users()
    log.info(f"sync_all_users start; {len(user_ids)} user(s) connected")
    return [sync_user(uid, days=days) for uid in user_ids]


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Withings weight sync")
    parser.add_argument("--user", type=int, default=None,
                        help="Sync only this Telegram user_id")
    parser.add_argument("--days", type=int, default=2,
                        help="How many days of history to fetch (default 2)")
    args = parser.parse_args()

    if not WITHINGS_CLIENT_ID or not WITHINGS_CLIENT_SECRET:
        log.error("WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET missing from .env")
        sys.exit(1)

    db.init_db()

    if args.user:
        result = sync_user(args.user, days=args.days)
        log.info(f"done: {result}")
    else:
        results = sync_all_users(days=args.days)
        ok = sum(1 for r in results if r["ok"])
        inserted = sum(r["inserted"] for r in results)
        log.info(f"done: {ok}/{len(results)} users OK; {inserted} new readings")


if __name__ == "__main__":
    main()
