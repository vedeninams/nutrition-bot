"""
wiki.py — Per-user markdown wiki (long-term memory).

Implements the long-term memory layer described in wiki_instructions.md.
Each user has a folder of markdown pages that the LLM updates incrementally.

Architecture inspired by Andrej Karpathy's "LLM Wiki":
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

This module is ONLY the read/write layer. The ingest/query/lint logic lives
in advisor.py and will be wired up in later steps.
"""

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

# Base directory for all user wikis. On server = /opt/nutrition-bot/wiki.
# Local dev defaults to ./wiki relative to working directory.
WIKI_BASE = Path(os.getenv("WIKI_DIR", "./wiki"))

# Template pages copied into a new user's folder on first init.
TEMPLATES_DIR = Path(__file__).parent / "wiki_templates"

# The five pages every user has.
PAGES = ["profile", "goals", "patterns", "wins", "log"]


def user_wiki_dir(user_id: int) -> Path:
    """Where this user's wiki lives on disk."""
    return WIKI_BASE / f"user_{user_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Per-user async locks
# Prevents two concurrent ingests for the same user from stomping each other.
# One lock per user — different users don't block each other.
# ─────────────────────────────────────────────────────────────────────────────

_locks: dict[int, asyncio.Lock] = {}


def get_lock(user_id: int) -> asyncio.Lock:
    """Return the asyncio.Lock for this user, creating it on first access."""
    if user_id not in _locks:
        _locks[user_id] = asyncio.Lock()
    return _locks[user_id]


# ─────────────────────────────────────────────────────────────────────────────
# Initialization
# ─────────────────────────────────────────────────────────────────────────────

def ensure_user_wiki(user_id: int) -> None:
    """
    Create the wiki folder and copy templates if they don't exist.
    Safe to call on every message — returns immediately if already set up.
    """
    wiki_dir = user_wiki_dir(user_id)
    if wiki_dir.exists() and all((wiki_dir / f"{p}.md").exists() for p in PAGES):
        return  # already initialized

    wiki_dir.mkdir(parents=True, exist_ok=True)

    for page in PAGES:
        dest = wiki_dir / f"{page}.md"
        if dest.exists():
            continue  # don't overwrite existing content
        template_path = TEMPLATES_DIR / f"{page}.md"
        if template_path.exists():
            dest.write_text(template_path.read_text(), encoding="utf-8")
        else:
            # Fallback if a template is missing — create minimal page
            dest.write_text(f"# {page.capitalize()}\n\n_(Empty)_\n", encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def read_page(user_id: int, page_name: str) -> str:
    """Return the raw markdown of a page. Empty string if missing."""
    path = user_wiki_dir(user_id) / f"{page_name}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_all_pages(user_id: int) -> dict[str, str]:
    """Return all pages as {page_name: content}."""
    return {name: read_page(user_id, name) for name in PAGES}


def read_wiki_for_prompt(user_id: int) -> str:
    """
    Format the whole wiki as a single string for inclusion in an AI prompt.
    Skips pages that are still empty (just the template placeholder).
    """
    pages = read_all_pages(user_id)
    parts = []
    for name, content in pages.items():
        # Skip pages that are still just the empty template.
        # Heuristic: a meaningful page has content beyond the header comment.
        stripped = content.strip()
        if not stripped or "_(Empty" in stripped:
            continue
        parts.append(f"## {name}.md\n{content.strip()}")
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────

# Matches the template placeholder: `_(Empty — the bot will populate this…)_`
# on its own line.  Italic-parenthetical markdown starting with "_(Empty".
_EMPTY_PLACEHOLDER_RE = re.compile(r"^\s*_\(Empty[^\)]*\)_\s*$\n?", re.MULTILINE)


def strip_empty_placeholder(content: str) -> str:
    """
    Remove the template's `_(Empty — ...)_` placeholder line from a page's
    content, if present.  Leaves the `# Heading`, HTML comments, and any
    existing bullets untouched.

    Called from ingest right before appending the first real bullet, so the
    "nothing here yet" sign comes down automatically the moment a page gets
    real content.  Self-healing: runs on every append, no-op once the line
    is gone.
    """
    return _EMPTY_PLACEHOLDER_RE.sub("", content)


def write_page(user_id: int, page_name: str, content: str) -> None:
    """
    Overwrite a page with new content.
    Caller is responsible for holding get_lock(user_id) if concurrent writes
    are possible.
    """
    if page_name not in PAGES:
        raise ValueError(f"Unknown page: {page_name}. Valid pages: {PAGES}")
    wiki_dir = user_wiki_dir(user_id)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    path = wiki_dir / f"{page_name}.md"
    path.write_text(content, encoding="utf-8")


def append_log(user_id: int, summary: str, details: str = "") -> None:
    """
    Append a dated entry to log.md.
    Format:
        ## [YYYY-MM-DD] summary
        details (optional)
    """
    ensure_user_wiki(user_id)
    date_str = datetime.now().strftime("%Y-%m-%d")
    entry = f"\n## [{date_str}] {summary.strip()}\n"
    if details:
        entry += f"{details.strip()}\n"

    log_path = user_wiki_dir(user_id) / "log.md"
    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    log_path.write_text(existing + entry, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Calorie goal — read/write helpers backed by goals.md
#
# Per Karpathy's "LLM Wiki" design, everything about the user lives in markdown
# pages. The calorie goal used to sit in SQL (`users.daily_kcal`) as a legacy
# — we've moved it here so goals.md is the single source of truth. Budget math
# reads via get_daily_kcal(); /goal N and natural-language setters write via
# set_daily_kcal(); a one-time migration copies the old SQL value over.
# ─────────────────────────────────────────────────────────────────────────────

# Canonical shape we always WRITE. One visual anchor, distinctive label, clean
# to parse. Haiku is told to use exactly this shape when recording a calorie
# target (see wiki_instructions.md).
_CALORIE_GOAL_CANONICAL = "- **Daily calorie goal**: {kcal} kcal"

# Lenient reader/consolidator regex. Matches a bullet line whose label is some
# combination of "daily" / "calorie" / "goal" / "target" AND whose unit is
# "kcal". Requiring the kcal unit means lines like "Goal weight: 60kg" or
# "target 30g protein" are *never* matched — their unit is kg/g, not kcal.
#
# Tolerates the `[YYYY-MM-DD]` ingest date-prefix convention (see
# wiki_instructions.md §Ingest rule 0) between the bullet and the label.
#
# Captures the integer in group 1. MULTILINE so ^ anchors per-line.
_CALORIE_GOAL_RE = re.compile(
    r"^[ \t]*[-*+][ \t]+"                              # bullet marker
    r"(?:\[\d{4}-\d{2}-\d{2}\][ \t]+)?"                # optional date prefix
    r"(?:\*\*)?"                                        # optional open bold
    r"(?:daily\s+)?(?:calorie\s+)?(?:goal|target)"     # label words
    r"(?:\*\*)?"                                        # optional close bold
    r"[ \t]*[:—–\-]?[ \t]*"                            # optional separator
    r"(\d[\d,]*)"                                       # the number
    r"[ \t]*kcal"                                       # unit MUST be kcal
    r".*$",                                             # rest of line
    re.IGNORECASE | re.MULTILINE,
)


def get_daily_kcal(user_id: int, default: int = 2000) -> int:
    """Read the calorie goal from goals.md. Returns `default` if the line is
    missing or malformed — never crashes on hand-edits or Haiku drift."""
    content = read_page(user_id, "goals")
    if not content:
        return default
    m = _CALORIE_GOAL_RE.search(content)
    if not m:
        return default
    try:
        return int(m.group(1).replace(",", ""))
    except (ValueError, AttributeError):
        return default


def consolidate_goal_line(user_id: int) -> dict:
    """Consolidate calorie-goal-ish lines in goals.md to the single canonical form.

    Run by `/lint`. Idempotent — does nothing if there are 0 or 1 matches, or
    if the only match is already in canonical shape and placed correctly.

    Rule when multiple matches exist: keep the LAST one's number (later wins,
    matching append-ordered ingest semantics) and rewrite to canonical form.

    Returns {"found": N, "kept": int|None, "rewrote": bool}.

    Caller is responsible for holding `get_lock(user_id)`.
    """
    ensure_user_wiki(user_id)
    content = read_page(user_id, "goals")
    if not content:
        return {"found": 0, "kept": None, "rewrote": False}

    matches = list(_CALORIE_GOAL_RE.finditer(content))
    if not matches:
        return {"found": 0, "kept": None, "rewrote": False}

    # Later match wins (append-ordered ingest → LAST = most recent).
    try:
        kept_value = int(matches[-1].group(1).replace(",", ""))
    except (ValueError, AttributeError):
        return {"found": len(matches), "kept": None, "rewrote": False}

    canonical = _CALORIE_GOAL_CANONICAL.format(kcal=kept_value)

    # No consolidation needed if there's exactly one match AND it already
    # matches the canonical bytes AND it sits right below the `# Goals`
    # heading (modulo blank lines). The simplest-correct check: rebuild what
    # set_daily_kcal would produce and compare.
    rebuilt = _rewrite_goals_with_canonical(content, canonical)
    if rebuilt == content:
        return {"found": len(matches), "kept": kept_value, "rewrote": False}

    write_page(user_id, "goals", rebuilt)
    return {"found": len(matches), "kept": kept_value, "rewrote": True}


def _rewrite_goals_with_canonical(content: str, canonical: str) -> str:
    """Shared helper used by both set_daily_kcal and consolidate_goal_line.

    Produces a goals.md where:
      - every calorie-goal-ish line is stripped,
      - one canonical bullet is inserted right under `# Goals`,
      - the `_(Empty ...)_` placeholder is removed if still present,
      - blank-line runs are collapsed, single trailing newline.
    """
    stripped = _CALORIE_GOAL_RE.sub("", content)
    stripped = strip_empty_placeholder(stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)

    heading_re = re.compile(r"^#[ \t]+goals[ \t]*$", re.IGNORECASE | re.MULTILINE)
    m = heading_re.search(stripped)
    if m:
        insert_at = m.end()
        new_content = stripped[:insert_at] + "\n\n" + canonical + stripped[insert_at:]
    else:
        new_content = f"# Goals\n\n{canonical}\n\n{stripped.lstrip()}"

    return re.sub(r"\n{3,}", "\n\n", new_content).rstrip() + "\n"


def set_daily_kcal(user_id: int, kcal: int) -> None:
    """Upsert the canonical calorie-goal bullet in goals.md.

    Strips EVERY existing calorie-goal-ish line (canonical form, legacy
    phrasings, Haiku-ingested prose — anything the lenient regex matches) and
    inserts exactly one canonical bullet right under the `# Goals` heading.
    All non-calorie bullets (weight in kg, sugar, protein in g, etc.) are
    preserved byte-for-byte since the regex never matches them.

    Caller is responsible for holding `get_lock(user_id)` if concurrent
    Haiku ingests may be running against the same page.
    """
    ensure_user_wiki(user_id)
    content = read_page(user_id, "goals")
    canonical = _CALORIE_GOAL_CANONICAL.format(kcal=int(kcal))
    new_content = _rewrite_goals_with_canonical(content, canonical)
    write_page(user_id, "goals", new_content)


# ─────────────────────────────────────────────────────────────────────────────
# One-time migration of legacy SQL profile → profile.md
# ─────────────────────────────────────────────────────────────────────────────

_MIGRATION_MARKER = "<!-- migrated_from_sql_profile"


def migrate_sql_profile_if_needed(user_id: int, sql_profile: str) -> bool:
    """
    Copy a legacy SQL user_profile.profile text into this user's profile.md.
    Idempotent — stamps a marker into profile.md so the migration runs at most
    once per user. Safe to call on every ensure_user call.

    Returns True if content was migrated, False if already stamped or empty.
    """
    ensure_user_wiki(user_id)
    profile_path = user_wiki_dir(user_id) / "profile.md"
    current = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""

    # Already migrated — nothing to do
    if _MIGRATION_MARKER in current:
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")

    # Even with nothing to migrate, stamp the marker so we don't re-check
    # the SQL table on every ensure_user call forever.
    if not sql_profile or not sql_profile.strip():
        stamped = f"{_MIGRATION_MARKER} on {date_str}: empty -->\n{current}"
        profile_path.write_text(stamped, encoding="utf-8")
        return False

    migrated = (
        f"{_MIGRATION_MARKER} on {date_str} -->\n"
        f"# Profile\n\n"
        f"{sql_profile.strip()}\n"
    )
    profile_path.write_text(migrated, encoding="utf-8")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# One-time migration of legacy SQL daily_kcal → goals.md canonical line
# ─────────────────────────────────────────────────────────────────────────────

_GOAL_MIGRATION_MARKER = "<!-- migrated_sql_daily_kcal"


def migrate_sql_goal_if_needed(user_id: int, sql_kcal) -> bool:
    """Copy legacy SQL `users.daily_kcal` value into goals.md as the canonical
    `- **Daily calorie goal**: N kcal` bullet.

    Idempotent — stamps a marker into goals.md so the migration runs at most
    once per user. Safe to call on every ensure_user call. Uses set_daily_kcal
    under the hood so any pre-existing calorie-goal-ish lines (legacy Haiku
    phrasings like "Target 1850 kcal/day") are consolidated into one canonical
    line at the same time.

    Returns True if a line was written, False if already stamped or SQL was
    empty/None.
    """
    ensure_user_wiki(user_id)
    goals_path = user_wiki_dir(user_id) / "goals.md"
    current = goals_path.read_text(encoding="utf-8") if goals_path.exists() else ""

    # Already migrated — nothing to do.
    if _GOAL_MIGRATION_MARKER in current:
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")

    # Nothing to migrate — stamp the marker so we don't re-check forever.
    try:
        kcal_int = int(sql_kcal) if sql_kcal is not None else 0
    except (TypeError, ValueError):
        kcal_int = 0

    if kcal_int <= 0:
        stamped = f"{_GOAL_MIGRATION_MARKER} on {date_str}: empty -->\n{current}"
        goals_path.write_text(stamped, encoding="utf-8")
        return False

    # Place the canonical line (set_daily_kcal handles stripping existing
    # variants), then prepend the migration marker so we don't re-run.
    set_daily_kcal(user_id, kcal_int)
    migrated = goals_path.read_text(encoding="utf-8")
    stamped = f"{_GOAL_MIGRATION_MARKER} on {date_str} -->\n{migrated}"
    goals_path.write_text(stamped, encoding="utf-8")
    return True
