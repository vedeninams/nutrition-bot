"""
database.py — SQLite foundation for the Nutrition Bot.

Handles all data storage: meals, user settings, and goals.
Every other module imports from here — nothing else touches the DB directly.
"""

import sqlite3
import os
import re as _re
from datetime import datetime, date, timedelta
from typing import Optional

DB_PATH = os.getenv("NUTRITION_DB_PATH", "nutrition.db")


def get_conn() -> sqlite3.Connection:
    """Return a connection with row_factory so rows behave like dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safer concurrent writes
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist yet. Safe to call on every startup."""
    conn = get_conn()
    with conn:
        conn.executescript("""
            -- ─────────────────────────────────────────────
            -- User settings & goals
            -- ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                daily_kcal      INTEGER DEFAULT 2000,
                language        TEXT    DEFAULT 'en',
                timezone        TEXT    DEFAULT 'Europe/Berlin',
                created_at      TEXT    DEFAULT (datetime('now'))
            );

            -- ─────────────────────────────────────────────
            -- Individual logged items
            -- Each photo / text message → one or more rows
            --
            -- dish_name  = the containing dish / plate name
            --              (e.g. "Udon Noodle Bowl", "König Käse")
            --              All ingredients of the same dish share
            --              the same dish_name.  For a standalone
            --              single item it equals dish.
            -- dish       = the individual ingredient / component
            --              (e.g. "Tofu 80g", "Udon noodles 150g")
            -- ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS meals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                logged_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                meal_type       TEXT    NOT NULL,   -- breakfast / lunch / dinner / snack
                dish_name       TEXT,               -- parent dish / plate name
                dish            TEXT    NOT NULL,   -- individual ingredient or item
                kcal            REAL    DEFAULT 0,
                protein_g       REAL    DEFAULT 0,
                fat_g           REAL    DEFAULT 0,
                carbs_g         REAL    DEFAULT 0,
                sugar_g         REAL    DEFAULT 0,
                confidence      TEXT    DEFAULT 'medium',  -- high / medium / low
                source          TEXT    DEFAULT 'photo',   -- photo / label / text
                notes           TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- Index for fast daily queries
            CREATE INDEX IF NOT EXISTS idx_meals_user_date
                ON meals (user_id, logged_at);

            -- ─────────────────────────────────────────────
            -- Correction log — keeps an audit trail
            -- ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS corrections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                meal_id         INTEGER NOT NULL,
                corrected_at    TEXT    DEFAULT (datetime('now')),
                old_dish        TEXT,
                old_kcal        REAL,
                new_dish        TEXT,
                new_kcal        REAL,
                reason          TEXT,
                FOREIGN KEY (meal_id) REFERENCES meals(id)
            );

            -- ─────────────────────────────────────────────
            -- User profile — free-text persistent memory
            -- One row per user, Claude maintains the text
            -- ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id         INTEGER PRIMARY KEY,
                profile         TEXT    DEFAULT '',
                updated_at      TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- ─────────────────────────────────────────────
            -- Daily activity stats (from iPhone Shortcuts)
            -- steps + weight from Health app
            -- workouts from Calendar (JSON array)
            -- ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS daily_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                date            TEXT    NOT NULL,           -- YYYY-MM-DD
                steps           INTEGER,
                weight_kg       REAL,
                workouts        TEXT    DEFAULT '[]',       -- JSON: [{type, duration_min, kcal_est}]
                kcal_burned_est REAL,                       -- total estimated burn (walk + workouts)
                updated_at      TEXT    DEFAULT (datetime('now')),
                UNIQUE(user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_stats_user_date
                ON daily_stats (user_id, date);

            -- ─────────────────────────────────────────────
            -- Short-term conversation memory
            -- Every incoming user text + every outgoing bot
            -- reply is appended here. The recent window (last
            -- 16h OR last 20 messages, whichever is longer)
            -- is passed as context to every conversational
            -- Claude call so follow-ups like "yes" and
            -- "two eggs" retain their referent.
            --
            -- role: 'user' | 'assistant'  (matches Claude API)
            -- ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                ts              TEXT    NOT NULL DEFAULT (datetime('now')),
                role            TEXT    NOT NULL,
                content         TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_convmsg_user_ts
                ON conversation_messages (user_id, ts);
        """)
    # ── Migration: add dish_name to existing databases ───────────────────────
    # ALTER TABLE is a no-op if the column already exists would raise an
    # OperationalError, so we check first.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(meals)").fetchall()]
    if "dish_name" not in cols:
        conn.execute("ALTER TABLE meals ADD COLUMN dish_name TEXT")
        conn.commit()

    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# User helpers
# ─────────────────────────────────────────────────────────────────────────────

def ensure_user(user_id: int):
    """Create user row if it doesn't exist yet. Also initializes their wiki."""
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,)
        )

    # Read legacy SQL profile text (if any) so the wiki can absorb it on first run.
    row = conn.execute(
        "SELECT profile FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    sql_profile = row["profile"] if row else ""

    # Read legacy SQL daily calorie goal so the wiki can absorb it on first run.
    # The column still exists for backward-compat; the canonical source is now goals.md.
    user_row = conn.execute(
        "SELECT daily_kcal FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    sql_kcal = int(user_row["daily_kcal"]) if user_row and user_row["daily_kcal"] else None
    conn.close()

    # Ensure the user's long-term memory wiki folder exists (idempotent).
    # Import inside function to avoid a circular import at module load time.
    import wiki
    wiki.ensure_user_wiki(user_id)
    # One-time migration of legacy SQL profile → wiki profile.md.
    # Marker inside profile.md makes this a no-op after the first call.
    wiki.migrate_sql_profile_if_needed(user_id, sql_profile)
    # One-time migration of legacy SQL daily_kcal → wiki goals.md.
    # Marker inside goals.md makes this a no-op after the first call.
    if sql_kcal:
        wiki.migrate_sql_goal_if_needed(user_id, sql_kcal)


def get_user(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_daily_goal(user_id: int, kcal: int):
    """DEPRECATED: the canonical daily calorie goal now lives in goals.md.

    Prefer ``wiki.set_daily_kcal(user_id, kcal)`` inside
    ``async with wiki.get_lock(user_id):``. This function is kept only so that
    any lingering callers don't crash; the ``users.daily_kcal`` column is no
    longer read by the app (see ``ensure_user`` which migrates it once into
    goals.md, then leaves it alone).
    """
    ensure_user(user_id)
    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE users SET daily_kcal = ? WHERE user_id = ?",
            (kcal, user_id)
        )
    conn.close()


def set_language(user_id: int, lang: str):
    ensure_user(user_id)
    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE users SET language = ? WHERE user_id = ?",
            (lang, user_id)
        )
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Meal classification
# ─────────────────────────────────────────────────────────────────────────────

def get_last_meal_today(user_id: int) -> Optional[dict]:
    """
    Return the most recent non-deleted meal logged by this user TODAY
    (calendar day, server local time), or None if there is none.

    Used by the Tier-2 classifier to decide whether a new item inherits the
    prior meal's meal_type (≤ 90 min gap) or gets classified independently
    by time-of-day.
    """
    today = date.today().isoformat()
    conn = get_conn()
    row = conn.execute(
        """SELECT id, logged_at, meal_type, dish_name
             FROM meals
            WHERE user_id = ?
              AND date(logged_at) = ?
              AND confidence != 'deleted'
            ORDER BY logged_at DESC
            LIMIT 1""",
        (user_id, today),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _meal_window(now: datetime) -> tuple[str, datetime]:
    """
    Return (window_meal_type, window_start) for the meal window containing
    `now`. Four windows, Berlin wall-clock:
        04:00 → 11:29   breakfast
        11:30 → 15:59   lunch
        16:00 → 22:59   dinner
        23:00 → 03:59   snack      (night, spans midnight)
    The night window started at 23:00 the PREVIOUS calendar day when `now`
    is between 00:00 and 03:59.
    """
    from datetime import time as _time
    minutes = now.hour * 60 + now.minute
    today = now.date()
    if 4 * 60 <= minutes < 11 * 60 + 30:
        return ("breakfast", datetime.combine(today, _time(4, 0)))
    if 11 * 60 + 30 <= minutes < 16 * 60:
        return ("lunch", datetime.combine(today, _time(11, 30)))
    if 16 * 60 <= minutes < 23 * 60:
        return ("dinner", datetime.combine(today, _time(16, 0)))
    if minutes >= 23 * 60:
        return ("snack", datetime.combine(today, _time(23, 0)))
    # 00:00 – 03:59 → night window opened at 23:00 the day before
    return ("snack", datetime.combine(today - timedelta(days=1), _time(23, 0)))


def classify_meal_type(now: datetime, last_meal: Optional[dict] = None) -> str:
    """
    Fallback meal classification — used only when the user's caption does NOT
    mention a meal type. Caption always wins; this is Tier 2.

    The day is divided into four meal windows (Berlin wall-clock):
        04:00 → 11:29   breakfast
        11:30 → 15:59   lunch
        16:00 → 22:59   dinner
        23:00 → 03:59   snack        (night snack, stored as "snack")

    Within each window:
      1. The FIRST meal logged in the window takes the window's meal_type
         (breakfast / lunch / dinner / snack for the night window).
      2. A subsequent meal logged ≤ 90 min after the previous meal inherits
         that meal's meal_type.
      3. A subsequent meal logged > 90 min after the previous meal is a
         snack — even if the clock still says "breakfast time".

    Crossing a window boundary RESETS the anchor: the first meal in the new
    window takes the new window's meal_type regardless of how long ago the
    previous meal was.

    Example day:
      07:00 eggs      → first in breakfast window        → breakfast
      09:00 banana    → 120 min after eggs (>90)         → snack
      12:30 soup      → first in lunch window            → lunch
      15:30 apple     → 180 min after soup (>90)         → snack
      19:00 chicken   → first in dinner window           → dinner
      21:15 chocolate → 135 min after chicken (>90)      → snack

    Args:
      now:       the current timestamp.
      last_meal: the most recent meal logged by this user, or None.
                 Must provide keys "logged_at" (ISO string or datetime) and
                 "meal_type".

    Returns one of: "breakfast", "lunch", "dinner", "snack".
    """
    window_type, window_start = _meal_window(now)

    if last_meal is None:
        return window_type

    raw = last_meal.get("logged_at")
    try:
        prior = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return window_type   # malformed timestamp → safe default

    # Prior meal is in an earlier window → this is the first in the new
    # window, so take the window's default meal_type.
    if prior < window_start:
        return window_type

    # Prior meal is in the SAME window — apply the 90-min rule.
    if (now - prior) <= timedelta(minutes=90):
        inherited = last_meal.get("meal_type")
        if inherited in ("breakfast", "lunch", "dinner", "snack"):
            return inherited
        return window_type   # unknown prior value → window default
    return "snack"           # >90 min inside the same window


# ─────────────────────────────────────────────────────────────────────────────
# Logging meals
# ─────────────────────────────────────────────────────────────────────────────

def log_meal(
    user_id: int,
    dish: str,
    kcal: float,
    protein_g: float = 0,
    fat_g: float = 0,
    carbs_g: float = 0,
    sugar_g: float = 0,
    confidence: str = "medium",
    source: str = "photo",
    meal_type: Optional[str] = None,
    dish_name: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """
    Insert one meal row and return its id.
    dish_name = parent dish/plate (e.g. "Udon Noodle Bowl"); defaults to dish.
    meal_type is auto-classified if not provided (Tier 2: 90-min-inherit
    or time-of-day window). Batched inserts via log_meal_items should pass
    meal_type explicitly so every item in a plate shares the same label.
    """
    ensure_user(user_id)
    now = datetime.now()
    if meal_type is None:
        last_meal = get_last_meal_today(user_id)
        meal_type = classify_meal_type(now, last_meal)
    if dish_name is None:
        dish_name = dish   # standalone item — dish_name = the item itself

    conn = get_conn()
    with conn:
        cur = conn.execute(
            """INSERT INTO meals
               (user_id, logged_at, meal_type, dish_name, dish, kcal,
                protein_g, fat_g, carbs_g, sugar_g,
                confidence, source, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id, now.isoformat(), meal_type, dish_name, dish, kcal,
                protein_g, fat_g, carbs_g, sugar_g,
                confidence, source, notes
            )
        )
        meal_id = cur.lastrowid
    conn.close()
    return meal_id


def log_meal_items(user_id: int, items: list[dict], source: str = "photo") -> list[int]:
    """
    Log multiple items from one meal (e.g. a plate with 8 components).
    Each item is a dict with keys: dish_name, dish, kcal, protein_g, fat_g,
    carbs_g, sugar_g, confidence, meal_type, notes.

    All items inserted in one call share the same meal_type: either the
    caption-detected value (Tier 1) or a single Tier-2 auto-classification
    computed ONCE against the user's state BEFORE any row from this batch
    is inserted — so an 8-item plate won't flip types mid-insert, and the
    first row of the batch doesn't become the "prior meal" for its own
    sibling rows.
    """
    # Tier 1 — caption-detected meal_type from the analyzer. First non-null
    # wins; in practice the analyzer stamps every item in a dish with the
    # same value.
    caption_meal_type: Optional[str] = None
    for item in items:
        mt = item.get("meal_type")
        if mt in ("breakfast", "lunch", "dinner", "snack"):
            caption_meal_type = mt
            break

    # Tier 2 — classify once for the whole batch against the last meal
    # logged BEFORE this batch (don't let earlier items in this batch
    # influence the classification of later items in the same batch).
    if caption_meal_type is not None:
        batch_meal_type = caption_meal_type
    else:
        batch_meal_type = classify_meal_type(
            datetime.now(), get_last_meal_today(user_id)
        )

    ids = []
    for item in items:
        meal_id = log_meal(
            user_id=user_id,
            dish_name=item.get("dish_name"),
            dish=item.get("dish", "Unknown"),
            kcal=item.get("kcal", 0),
            protein_g=item.get("protein_g", 0),
            fat_g=item.get("fat_g", 0),
            carbs_g=item.get("carbs_g", 0),
            sugar_g=item.get("sugar_g", 0),
            confidence=item.get("confidence", "medium"),
            source=source,
            meal_type=batch_meal_type,
            notes=item.get("notes"),
        )
        ids.append(meal_id)

    # If Claude used "Plate" as placeholder (no caption meal_type) and the
    # batch was classified as breakfast, replace "Plate" with "Breakfast".
    # Only applies to breakfast — lunch/dinner/snack use descriptive names.
    if ids and batch_meal_type == "breakfast":
        conn = get_conn()
        with conn:
            conn.execute(
                """UPDATE meals SET dish_name = REPLACE(dish_name, 'Plate', 'Breakfast')
                   WHERE id IN ({})
                     AND dish_name LIKE '%Plate%'""".format(
                    ",".join("?" * len(ids))
                ),
                (*ids,)
            )
        conn.close()

    return ids


# ─────────────────────────────────────────────────────────────────────────────
# Corrections
# ─────────────────────────────────────────────────────────────────────────────

def update_meal(meal_id: int, updates: dict, reason: str = "") -> bool:
    """
    Update fields on an existing meal row.
    Also writes a correction log entry for the audit trail.
    Returns True if a row was actually updated.
    """
    conn = get_conn()
    old = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    if not old:
        conn.close()
        return False

    # Build dynamic UPDATE
    allowed = {"dish", "kcal", "protein_g", "fat_g", "carbs_g", "sugar_g",
                "confidence", "meal_type", "notes"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        conn.close()
        return False

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [meal_id]

    with conn:
        conn.execute(f"UPDATE meals SET {set_clause} WHERE id = ?", values)
        conn.execute(
            """INSERT INTO corrections
               (meal_id, old_dish, old_kcal, new_dish, new_kcal, reason)
               VALUES (?,?,?,?,?,?)""",
            (
                meal_id,
                old["dish"], old["kcal"],
                fields.get("dish", old["dish"]),
                fields.get("kcal", old["kcal"]),
                reason,
            )
        )
    conn.close()
    return True


def delete_meal(meal_id: int) -> bool:
    """Soft-delete by marking confidence='deleted'. Keeps data for audit."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "UPDATE meals SET confidence = 'deleted' WHERE id = ?", (meal_id,)
        )
    conn.close()
    return cur.rowcount > 0


_WEIGHT_RE = _re.compile(r'(\d+(?:\.\d+)?)\s*(g|ml)\b', _re.IGNORECASE)


def _rescale_weight_in_dish(dish: str, factor: float) -> str:
    """Scale the LAST weight token (Ng or Nml) in a dish string by factor.

      'Yogurt 300g'             + 1.333 → 'Yogurt 400g'
      'Fried egg × 2pcs 120g'   + 1.5   → 'Fried egg × 2pcs 180g'
      'Broth 200ml'             + 0.5   → 'Broth 100ml'
      'König Käse'              + 2.0   → 'König Käse'  (no weight → no-op)

    Only the last match is rewritten. Ingredient lines carry at most one
    weight; being explicit avoids touching any earlier number that might
    look like a weight (e.g. the '2' in '× 2pcs' — belt-and-suspenders,
    since 'pcs' is excluded by the regex already).
    """
    matches = list(_WEIGHT_RE.finditer(dish))
    if not matches:
        return dish
    m = matches[-1]
    old_val = float(m.group(1))
    unit = m.group(2)
    new_val = round(old_val * factor)
    return dish[:m.start()] + f"{new_val}{unit}" + dish[m.end():]


def scale_dish_items(user_id: int, dish_name: str, factor: float) -> int:
    """
    Scale every non-deleted item of this dish by `factor`:
      • macro columns (kcal, protein_g, fat_g, carbs_g, sugar_g)
      • the weight token inside the `dish` text ("Yogurt 300g" → "Yogurt 400g")

    Both must move together or /today shows a mismatched row (old grams,
    new kcal) — which is exactly the "300g yogurt, 272 kcal" glitch that
    motivated this change. All writes happen in one transaction.

    E.g. factor=0.5 → user ate half the dish. factor=1.333 → scaled up
    from 300g to 400g.

    Case-insensitive dish_name match, so "udon noodle bowl" still finds
    "Udon Noodle Bowl".

    Returns the number of rows updated.
    """
    conn = get_conn()

    # Pull the rows we're about to scale so we can rewrite each dish
    # string individually — the weight can differ per ingredient.
    rows = conn.execute(
        """SELECT id, dish FROM meals
           WHERE user_id = ?
             AND LOWER(dish_name) = LOWER(?)
             AND confidence != 'deleted'""",
        (user_id, dish_name),
    ).fetchall()

    dish_updates = [
        (_rescale_weight_in_dish(r["dish"] or "", factor), r["id"])
        for r in rows
    ]

    with conn:
        conn.execute(
            """UPDATE meals SET
                   kcal      = ROUND(kcal      * ?, 1),
                   protein_g = ROUND(protein_g * ?, 1),
                   fat_g     = ROUND(fat_g     * ?, 1),
                   carbs_g   = ROUND(carbs_g   * ?, 1),
                   sugar_g   = ROUND(sugar_g   * ?, 1)
               WHERE user_id = ?
                 AND LOWER(dish_name) = LOWER(?)
                 AND confidence != 'deleted'""",
            (factor, factor, factor, factor, factor, user_id, dish_name),
        )
        if dish_updates:
            conn.executemany(
                "UPDATE meals SET dish = ? WHERE id = ?",
                dish_updates,
            )
    conn.close()
    return len(dish_updates)


def clear_today(user_id: int) -> int:
    """Soft-delete all of today's meals for this user. Returns count removed."""
    today = date.today().isoformat()
    conn = get_conn()
    with conn:
        cur = conn.execute(
            """UPDATE meals SET confidence = 'deleted'
               WHERE user_id = ? AND date(logged_at) = ? AND confidence != 'deleted'""",
            (user_id, today)
        )
    conn.close()
    return cur.rowcount


def delete_duplicate_dishes(user_id: int, dish_name: str) -> int:
    """
    Keep the FIRST logged batch of dish_name today, delete all subsequent ones.
    'First batch' = items logged within 2 min of the earliest timestamp for that dish.
    Returns count of deleted rows.
    Uses case-insensitive matching.
    """
    today = date.today().isoformat()
    conn = get_conn()

    first_row = conn.execute(
        """SELECT MIN(logged_at) AS first_ts FROM meals
           WHERE user_id = ? AND LOWER(dish_name) = LOWER(?) AND date(logged_at) = ?
             AND confidence != 'deleted'""",
        (user_id, dish_name, today)
    ).fetchone()

    if not first_row or not first_row["first_ts"]:
        conn.close()
        return 0

    first_ts = first_row["first_ts"]

    # Delete anything with the same dish_name logged more than 2 min after the first
    with conn:
        cur = conn.execute(
            """UPDATE meals SET confidence = 'deleted'
               WHERE user_id = ? AND LOWER(dish_name) = LOWER(?) AND date(logged_at) = ?
                 AND confidence != 'deleted'
                 AND (strftime('%s', logged_at) - strftime('%s', ?)) > 120""",
            (user_id, dish_name, today, first_ts)
        )
    conn.close()
    return cur.rowcount


def delete_by_dish_name(user_id: int, dish_name: str) -> int:
    """
    Soft-delete all non-deleted items for this user that share the given dish_name.
    Uses case-insensitive matching so "udon noodle bowl" matches "Udon Noodle Bowl".
    Returns the number of rows deleted.
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            """UPDATE meals SET confidence = 'deleted'
               WHERE user_id = ? AND LOWER(dish_name) = LOWER(?) AND confidence != 'deleted'""",
            (user_id, dish_name)
        )
    conn.close()
    return cur.rowcount


# ─────────────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────────────

def get_today_meals(user_id: int) -> list[dict]:
    """All non-deleted meals logged today (calendar day in server time)."""
    today = date.today().isoformat()
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM meals
           WHERE user_id = ?
             AND date(logged_at) = ?
             AND confidence != 'deleted'
           ORDER BY logged_at""",
        (user_id, today)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_meals_for_date(user_id: int, date_str: str) -> list[dict]:
    """All non-deleted meals logged on a specific date (YYYY-MM-DD)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM meals
           WHERE user_id = ?
             AND date(logged_at) = ?
             AND confidence != 'deleted'
           ORDER BY logged_at""",
        (user_id, date_str)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_meals_grouped_for_date(user_id: int, date_str: str) -> list[dict]:
    """Meals for a specific date grouped by (dish_name, meal_type)."""
    rows = get_meals_for_date(user_id, date_str)
    from collections import OrderedDict
    groups: OrderedDict[tuple, dict] = OrderedDict()
    for row in rows:
        key = (row.get("dish_name") or row["dish"], row["meal_type"])
        if key not in groups:
            groups[key] = {
                "dish_name": key[0],
                "meal_type": row["meal_type"],
                "kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0,
                "carbs_g": 0.0, "sugar_g": 0.0,
                "confidence": row.get("confidence", "medium"),
                "ingredients": [],
            }
        g = groups[key]
        g["kcal"]      += row.get("kcal", 0)
        g["protein_g"] += row.get("protein_g", 0)
        g["fat_g"]     += row.get("fat_g", 0)
        g["carbs_g"]   += row.get("carbs_g", 0)
        g["sugar_g"]   += row.get("sugar_g", 0)
        g["ingredients"].append(row)
    return list(groups.values())


def get_totals_for_date(user_id: int, date_str: str) -> dict:
    """Summed macros for a specific date."""
    conn = get_conn()
    row = conn.execute(
        """SELECT
               COALESCE(SUM(kcal), 0)      AS kcal,
               COALESCE(SUM(protein_g), 0) AS protein_g,
               COALESCE(SUM(fat_g), 0)     AS fat_g,
               COALESCE(SUM(carbs_g), 0)   AS carbs_g,
               COALESCE(SUM(sugar_g), 0)   AS sugar_g,
               COUNT(*)                    AS items
           FROM meals
           WHERE user_id = ?
             AND date(logged_at) = ?
             AND confidence != 'deleted'""",
        (user_id, date_str)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_today_meals_grouped(user_id: int) -> list[dict]:
    """
    Today's meals grouped by dish_name + meal_type.
    Returns a list of dish groups, each with summed macros and ingredient lines.

    Example:
      [
        {
          "dish_name": "Udon Noodle Bowl",
          "meal_type": "lunch",
          "kcal": 620, "protein_g": 28, ...
          "ingredients": [
            {"dish": "Udon noodles 150g", "kcal": 220, ...},
            {"dish": "Tofu 80g", "kcal": 90, ...},
            ...
          ]
        },
        {
          "dish_name": "König Käse",
          "meal_type": "lunch",
          "kcal": 110, ...
          "ingredients": [{"dish": "König Käse", "kcal": 110, ...}]
        }
      ]
    """
    rows = get_today_meals(user_id)  # already ordered by logged_at

    # Group by (dish_name, meal_type) preserving order of first appearance
    from collections import OrderedDict
    groups: OrderedDict[tuple, dict] = OrderedDict()

    for row in rows:
        key = (row.get("dish_name") or row["dish"], row["meal_type"])
        if key not in groups:
            groups[key] = {
                "dish_name": key[0],
                "meal_type": row["meal_type"],
                "kcal": 0.0,
                "protein_g": 0.0,
                "fat_g": 0.0,
                "carbs_g": 0.0,
                "sugar_g": 0.0,
                "confidence": row.get("confidence", "medium"),
                "ingredients": [],
            }
        g = groups[key]
        g["kcal"]      += row.get("kcal", 0)
        g["protein_g"] += row.get("protein_g", 0)
        g["fat_g"]     += row.get("fat_g", 0)
        g["carbs_g"]   += row.get("carbs_g", 0)
        g["sugar_g"]   += row.get("sugar_g", 0)
        g["ingredients"].append(row)

    return list(groups.values())


def get_today_totals(user_id: int) -> dict:
    """Summed macros for today."""
    today = date.today().isoformat()
    conn = get_conn()
    row = conn.execute(
        """SELECT
               COALESCE(SUM(kcal), 0)      AS kcal,
               COALESCE(SUM(protein_g), 0) AS protein_g,
               COALESCE(SUM(fat_g), 0)     AS fat_g,
               COALESCE(SUM(carbs_g), 0)   AS carbs_g,
               COALESCE(SUM(sugar_g), 0)   AS sugar_g,
               COUNT(*)                    AS items
           FROM meals
           WHERE user_id = ?
             AND date(logged_at) = ?
             AND confidence != 'deleted'""",
        (user_id, today)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_recent_meals(user_id: int, limit: int = 5) -> list[dict]:
    """Last N meals — used for natural-language correction context."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM meals
           WHERE user_id = ? AND confidence != 'deleted'
           ORDER BY logged_at DESC LIMIT ?""",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_month_totals(user_id: int) -> list[dict]:
    """Daily totals for the past 30 days — used for monthly Sunday review."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT
               date(logged_at) AS day,
               COALESCE(SUM(kcal), 0)      AS kcal,
               COALESCE(SUM(protein_g), 0) AS protein_g,
               COALESCE(SUM(fat_g), 0)     AS fat_g,
               COALESCE(SUM(carbs_g), 0)   AS carbs_g
           FROM meals
           WHERE user_id = ?
             AND date(logged_at) >= date('now', '-29 days')
             AND confidence != 'deleted'
           GROUP BY day
           ORDER BY day""",
        (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_week_totals(user_id: int) -> list[dict]:
    """Daily totals for the past 7 days — used for weekly review."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT
               date(logged_at) AS day,
               COALESCE(SUM(kcal), 0)      AS kcal,
               COALESCE(SUM(protein_g), 0) AS protein_g,
               COALESCE(SUM(fat_g), 0)     AS fat_g,
               COALESCE(SUM(carbs_g), 0)   AS carbs_g
           FROM meals
           WHERE user_id = ?
             AND date(logged_at) >= date('now', '-6 days')
             AND confidence != 'deleted'
           GROUP BY day
           ORDER BY day""",
        (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_meal_by_id(meal_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# User profile — free-text persistent memory maintained by Claude
# ─────────────────────────────────────────────────────────────────────────────

def get_profile(user_id: int) -> str:
    """Return the raw profile text for this user (empty string if none)."""
    ensure_user(user_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT profile FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["profile"] if row else ""


def save_profile(user_id: int, profile_text: str):
    """Upsert the free-text profile for a user."""
    ensure_user(user_id)
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO user_profile (user_id, profile, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 profile = excluded.profile,
                 updated_at = excluded.updated_at""",
            (user_id, profile_text)
        )
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Daily activity stats — from iPhone Shortcuts
# ─────────────────────────────────────────────────────────────────────────────

def upsert_daily_stats(
    user_id: int,
    date_str: str,
    steps: Optional[int] = None,
    weight_kg: Optional[float] = None,
    workouts: Optional[list] = None,
    kcal_burned_est: Optional[float] = None,
):
    """
    Insert or update the daily activity record for a given date.
    Only updates fields that are explicitly passed (None = leave existing value).
    """
    ensure_user(user_id)
    import json as _json
    conn = get_conn()

    # Load existing row so we can merge rather than overwrite with NULLs
    existing = conn.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? AND date = ?",
        (user_id, date_str)
    ).fetchone()

    if existing:
        updates = {}
        if steps is not None:
            updates["steps"] = steps
        if weight_kg is not None:
            updates["weight_kg"] = weight_kg
        if workouts is not None:
            updates["workouts"] = _json.dumps(workouts)
        if kcal_burned_est is not None:
            updates["kcal_burned_est"] = kcal_burned_est
        if updates:
            updates["updated_at"] = "datetime('now')"
            set_clause = ", ".join(
                f"{k} = datetime('now')" if v == "datetime('now')" else f"{k} = ?"
                for k, v in updates.items()
            )
            values = [v for v in updates.values() if v != "datetime('now')"]
            values += [user_id, date_str]
            with conn:
                conn.execute(
                    f"UPDATE daily_stats SET {set_clause} WHERE user_id = ? AND date = ?",
                    values
                )
    else:
        with conn:
            conn.execute(
                """INSERT INTO daily_stats
                   (user_id, date, steps, weight_kg, workouts, kcal_burned_est)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id, date_str, steps, weight_kg,
                    _json.dumps(workouts) if workouts is not None else "[]",
                    kcal_burned_est,
                )
            )
    conn.close()


def get_daily_stats(user_id: int, date_str: str) -> Optional[dict]:
    """Return the activity record for one day, or None."""
    import json as _json
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? AND date = ?",
        (user_id, date_str)
    ).fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    try:
        r["workouts"] = _json.loads(r.get("workouts") or "[]")
    except Exception:
        r["workouts"] = []
    return r


def get_week_stats(user_id: int) -> list[dict]:
    """Activity records for the past 7 days (may have gaps if not all days logged)."""
    import json as _json
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM daily_stats
           WHERE user_id = ? AND date >= date('now', '-6 days')
           ORDER BY date""",
        (user_id,)
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        r = dict(row)
        try:
            r["workouts"] = _json.loads(r.get("workouts") or "[]")
        except Exception:
            r["workouts"] = []
        result.append(r)
    return result


def get_latest_weight(user_id: int) -> Optional[float]:
    """Most recently recorded weight for this user."""
    conn = get_conn()
    row = conn.execute(
        """SELECT weight_kg FROM daily_stats
           WHERE user_id = ? AND weight_kg IS NOT NULL
           ORDER BY date DESC LIMIT 1""",
        (user_id,)
    ).fetchone()
    conn.close()
    return row["weight_kg"] if row else None


def get_profile_for_prompt(user_id: int) -> str:
    """
    Return the profile as a short block ready to inject into prompts.
    Returns empty string if no profile saved yet.
    """
    profile = get_profile(user_id)
    if not profile or not profile.strip():
        return ""
    return f"What I know about this user:\n{profile}"


def get_dish_items_today(user_id: int, dish_name: str) -> list[dict]:
    """
    Return all non-deleted items today that belong to the given dish_name.
    Case-insensitive match. Used to compute current total grams for a dish
    before scaling by target weight.
    """
    today = date.today().isoformat()
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM meals
           WHERE user_id = ? AND LOWER(dish_name) = LOWER(?)
             AND date(logged_at) = ? AND confidence != 'deleted'
           ORDER BY logged_at""",
        (user_id, dish_name, today)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_last_meal_batch(user_id: int, window_seconds: int = 120) -> list[dict]:
    """
    Return all non-deleted meal items that were logged in the same
    'batch' as the most recent item — i.e. within window_seconds of it.

    When the user sends a photo of a full plate, all 6 components are
    logged within a second of each other.  This function finds that cluster
    so we can tell Claude exactly which IDs form "this dish".
    """
    conn = get_conn()

    # Find the timestamp of the most recently logged item
    latest_row = conn.execute(
        """SELECT logged_at FROM meals
           WHERE user_id = ? AND confidence != 'deleted'
           ORDER BY logged_at DESC LIMIT 1""",
        (user_id,)
    ).fetchone()

    if not latest_row:
        conn.close()
        return []

    latest_ts = latest_row["logged_at"]

    # Fetch all items within window_seconds of that timestamp
    rows = conn.execute(
        """SELECT * FROM meals
           WHERE user_id = ?
             AND confidence != 'deleted'
             AND ABS(strftime('%s', logged_at) - strftime('%s', ?)) <= ?
           ORDER BY logged_at""",
        (user_id, latest_ts, window_seconds)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Short-term conversation memory
#
# Every incoming user text and every outgoing bot reply is appended to
# conversation_messages. A rolling window of "last 16 hours OR last 20
# messages, whichever is longer" is passed as context to every
# conversational Claude call (intent router, correction resolver, question
# answerer, ingest). The window is rolling rather than calendar-day, so
# chatting around midnight stays continuous and the timezone of "today"
# doesn't matter.
#
# We store BOTH roles so the model can see what it just asked ("Yes." only
# makes sense if the prior turn was a question), and photo captions are
# stored as user-role summary text so follow-ups like "two eggs" have a
# referent.
# ─────────────────────────────────────────────────────────────────────────────

# Defaults used by get_recent_conversation. Tuned for Maria's typical use
# (20-30 short messages per day, mostly food notes). Override at call site
# if you need different bounds — e.g. tests.
_DEFAULT_CONV_HOURS = 16
_DEFAULT_CONV_MIN_MESSAGES = 20


def log_message(user_id: int, role: str, content: str) -> None:
    """Append one conversation turn. `role` must be 'user' or 'assistant'
    (Claude API convention). Empty content is silently skipped so we don't
    pollute the window with blank sends.
    """
    if role not in ("user", "assistant"):
        raise ValueError(f"log_message role must be 'user' or 'assistant', got {role!r}")
    if not content or not content.strip():
        return
    ensure_user(user_id)
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO conversation_messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
    conn.close()


def get_recent_conversation(
    user_id: int,
    hours: int = _DEFAULT_CONV_HOURS,
    min_messages: int = _DEFAULT_CONV_MIN_MESSAGES,
) -> list[dict]:
    """Return the rolling-window conversation history for this user, oldest
    first, shaped for Claude's API:

        [
            {"role": "user",      "content": "..."},
            {"role": "assistant", "content": "..."},
            ...
        ]

    Selection rule: all messages from the last `hours` hours, OR the last
    `min_messages` messages — whichever window is LARGER. So a slow-day
    user always sees at least `min_messages` of context, and an active-day
    user sees everything recent.

    Consecutive same-role messages are MERGED (joined with two newlines)
    because Claude's API requires strict alternation between user and
    assistant turns. Two user messages in a row become one user turn with
    both texts; same for two assistant messages.
    """
    conn = get_conn()

    # Rows from the last N hours, newest first
    rows_by_time = conn.execute(
        f"""SELECT role, content FROM conversation_messages
            WHERE user_id = ?
              AND ts >= datetime('now', ?)
            ORDER BY ts DESC, id DESC""",
        (user_id, f"-{int(hours)} hours"),
    ).fetchall()

    # Fallback: if the time window gave us fewer than min_messages, pull the
    # last min_messages rows regardless of age.
    if len(rows_by_time) < min_messages:
        rows_by_count = conn.execute(
            """SELECT role, content FROM conversation_messages
               WHERE user_id = ?
               ORDER BY ts DESC, id DESC
               LIMIT ?""",
            (user_id, int(min_messages)),
        ).fetchall()
        rows = rows_by_count
    else:
        rows = rows_by_time

    conn.close()

    # We fetched newest-first; reverse to oldest-first for Claude.
    rows = list(reversed(rows))

    # Merge consecutive same-role messages. Claude's API rejects
    # non-alternating sequences, and we anyway want "two user messages in a
    # row" to read like one thought to the model.
    merged: list[dict] = []
    for r in rows:
        role = r["role"]
        content = r["content"]
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] = merged[-1]["content"] + "\n\n" + content
        else:
            merged.append({"role": role, "content": content})
    return merged


def purge_conversation_older_than(days: int = 14) -> int:
    """Housekeeping — drop conversation rows older than N days. Called from
    the weekly lint cron. The rolling-window reader already ignores anything
    older than 16h / 20 messages, so retention here is just about keeping
    the table slim. Returns rows deleted."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM conversation_messages WHERE ts < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        deleted = cur.rowcount or 0
    conn.close()
    return deleted


# ─────────────────────────────────────────────────────────────────────────────
# Test-cleanup helpers — wipe everything that was written today.
#
# Meant for the /reset_today command: when we're actively testing the bot,
# the day's meal log / stats / conversation get cluttered with fake data.
# These helpers return row counts so the caller can report what it nuked.
# ─────────────────────────────────────────────────────────────────────────────

def delete_today_meals(user_id: int) -> int:
    """HARD-delete every meal row logged today (server calendar day). Unlike
    delete_meal (which marks as 'deleted' for the audit trail), this actually
    removes the rows so /today totals start clean. Returns rows deleted.

    Because `corrections` has a FK to meals(id) with no ON DELETE CASCADE,
    we first delete any correction rows that point at today's meals, then
    delete the meals themselves — all inside one transaction so we don't
    leave orphan correction rows if anything fails."""
    today = date.today().isoformat()
    conn = get_conn()
    with conn:
        # Drop child rows first (corrections → meals) so the meals DELETE
        # isn't blocked by `PRAGMA foreign_keys=ON`.
        conn.execute(
            """
            DELETE FROM corrections
            WHERE meal_id IN (
                SELECT id FROM meals
                WHERE user_id = ? AND date(logged_at) = ?
            )
            """,
            (user_id, today),
        )
        cur = conn.execute(
            "DELETE FROM meals WHERE user_id = ? AND date(logged_at) = ?",
            (user_id, today),
        )
        deleted = cur.rowcount or 0
    conn.close()
    return deleted


def delete_today_stats(user_id: int) -> int:
    """Wipe today's daily_stats row (steps / weight / workouts). Returns
    rows deleted (0 or 1)."""
    today = date.today().isoformat()
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM daily_stats WHERE user_id = ? AND date = ?",
            (user_id, today),
        )
        deleted = cur.rowcount or 0
    conn.close()
    return deleted


def delete_today_conversation(user_id: int) -> int:
    """Wipe every conversation_messages row from today for this user.
    Returns rows deleted."""
    today = date.today().isoformat()
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM conversation_messages WHERE user_id = ? AND date(ts) = ?",
            (user_id, today),
        )
        deleted = cur.rowcount or 0
    conn.close()
    return deleted
