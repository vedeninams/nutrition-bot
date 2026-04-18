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
