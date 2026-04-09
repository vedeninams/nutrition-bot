"""
database.py — SQLite foundation for the Nutrition Bot.

Handles all data storage: meals, user settings, and goals.
Every other module imports from here — nothing else touches the DB directly.
"""

import sqlite3
import os
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
    """Create user row if it doesn't exist yet."""
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,)
        )
    conn.close()


def get_user(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_daily_goal(user_id: int, kcal: int):
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

def classify_meal_type(hour: int, kcal: float, total_dish_kcal: float = None) -> str:
    """
    Fallback meal classification by time of day and dish size.
    Only used when the user's caption does NOT mention a meal type.
    Caption always takes priority — this is the last resort.

    Rules:
      05:00–10:59  → breakfast
      11:00–14:59  → lunch
      15:00–16:59  → snack  (afternoon)
      17:00–22:59  → dinner
      23:00–04:59  → snack  (late night)
      Any time, total dish < 150 kcal → snack

    total_dish_kcal: use the full plate total so a 30-kcal broccoli
    in a 600-kcal dinner plate isn't labelled a snack.
    """
    ref_kcal = total_dish_kcal if total_dish_kcal is not None else kcal
    if ref_kcal < 150:
        return "snack"
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 15 <= hour < 17:
        return "snack"
    if 17 <= hour < 21 and ref_kcal >= 300:
        return "dinner"           # substantial meal in the evening
    if 17 <= hour < 21 and ref_kcal < 300:
        return "snack"            # light bite in the evening
    if hour >= 21:
        return "snack"            # anything after 21:00 → snack
    return "snack"


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
    total_dish_kcal: Optional[float] = None,
    notes: Optional[str] = None,
) -> int:
    """
    Insert one meal row and return its id.
    dish_name = parent dish/plate (e.g. "Udon Noodle Bowl"); defaults to dish.
    total_dish_kcal = sum of all ingredients in the dish (used for meal classification
                      so a 30-kcal broccoli in a 600-kcal dinner isn't called a snack).
    meal_type is auto-classified if not provided.
    """
    ensure_user(user_id)
    now = datetime.now()
    if meal_type is None:
        meal_type = classify_meal_type(now.hour, kcal, total_dish_kcal)
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

    meal_type classification uses the TOTAL dish kcal so that individual
    low-kcal ingredients (e.g. broccoli 30 kcal) are not wrongly labelled
    as "snack" when they belong to a 600 kcal dinner plate.
    """
    # Total kcal across all items in this batch — used for meal classification
    total_dish_kcal = sum(item.get("kcal", 0) for item in items)

    ids = []
    for item in items:
        # If Claude already detected a meal_type from caption, use it.
        # Otherwise classify using the full dish total, not just this ingredient.
        meal_type = item.get("meal_type") or None  # None → auto-classify in log_meal
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
            meal_type=meal_type,
            total_dish_kcal=total_dish_kcal,
            notes=item.get("notes"),
        )
        ids.append(meal_id)

    # If Claude used "Plate" as placeholder (no caption meal_type) and the item
    # was classified as breakfast, replace "Plate" with "Breakfast".
    # Only applies to breakfast — lunch/dinner/snack use descriptive names already.
    if ids:
        conn = get_conn()
        first_row = conn.execute(
            "SELECT meal_type FROM meals WHERE id = ?", (ids[0],)
        ).fetchone()
        if first_row and first_row["meal_type"] == "breakfast":
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


def scale_dish_items(user_id: int, dish_name: str, factor: float) -> int:
    """
    Multiply all macro values of every non-deleted item in this dish by factor.
    E.g. factor=0.5 → user ate half the dish.
    Returns the number of rows updated.
    Uses case-insensitive matching so "udon noodle bowl" matches "Udon Noodle Bowl".
    """
    conn = get_conn()
    with conn:
        cur = conn.execute(
            """UPDATE meals SET
                   kcal      = ROUND(kcal      * ?, 1),
                   protein_g = ROUND(protein_g * ?, 1),
                   fat_g     = ROUND(fat_g     * ?, 1),
                   carbs_g   = ROUND(carbs_g   * ?, 1),
                   sugar_g   = ROUND(sugar_g   * ?, 1)
               WHERE user_id = ? AND LOWER(dish_name) = LOWER(?) AND confidence != 'deleted'""",
            (factor, factor, factor, factor, factor, user_id, dish_name)
        )
    conn.close()
    return cur.rowcount


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
