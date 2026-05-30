"""
lint.py — Periodic tidy-up pass over a user's wiki.

The wiki is append-y: every ingest call adds or edits lines.  Over weeks
it accumulates near-duplicates, stale time-bounded statements, and the
occasional slightly-contradictory fact.  Lint is the cleanup pass.

Scope of 4a (this module):
  - Rewrite profile.md, goals.md, patterns.md, wins.md
    → dedup + drop clearly-stale entries
  - NEVER touch log.md (append-only audit trail)
  - Back up every page before overwriting (last 3 kept per page)
  - Write a summary entry to log.md so there's a record of what changed

Not in 4a (coming in 4b):
  - Sunday cron hookup
  - Contradiction detection + user resolution dialog

Architecture note:
  Lint is a narrow text task — given a messy page, return a tidy page.
  We use Haiku (cheap, fast) rather than Sonnet.  If the quality turns
  out to be poor on real wikis, we bump to Sonnet.

Date-prefix convention (set up in Step 4a.0):
  Every new or edited bullet in profile/goals/patterns/wins is prefixed
  with `[YYYY-MM-DD]` — the day it was added or last edited.  Lint uses
  that prefix to reason about recency: which of two conflicting lines
  is newer, and whether a time-bounded goal has passed its window.
  Lines without a prefix are "pre-convention" — we leave them alone
  rather than guess their age.
"""

import asyncio
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

import wiki

load_dotenv()  # must happen before AsyncAnthropic() reads the env

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

# Pages we rewrite.  log.md is intentionally absent — it's the audit trail.
LINTABLE_PAGES = ["profile", "goals", "patterns", "wins"]

# How many past versions of a page to keep in .backups/ per page.
BACKUP_RETENTION = 3

# Haiku is the right tool: narrow, mechanical text task.
_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 1500

# Lazy async client (parallel to the one in advisor.py).
_async_client: Optional[anthropic.AsyncAnthropic] = None


def _get_async_client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        # max_retries=6 ≈ 30s of in-process retry on transient errors. See issue #18.
        _async_client = anthropic.AsyncAnthropic(max_retries=6)
    return _async_client


# Dedicated file logger — separate from the bot log so we can tail lint.log
# and see exactly what each lint pass decided to do.
_log = logging.getLogger("wiki_lint")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    _h = logging.FileHandler(Path(__file__).parent / "lint.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _log.addHandler(_h)
    _log.propagate = False  # don't also spam the main bot log


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────

# Page-specific guidance about what to clean up on each page.
#
# Every bullet should start with `[YYYY-MM-DD]` (the ingest-time date stamp).
# That lets us compute age reliably: today - prefix_date = age in days.
# For lines without a prefix (pre-convention), don't guess — keep them.
_PAGE_GUIDANCE = {
    "profile": (
        "This page holds durable identity facts (age, weight, height, dietary "
        "restrictions, medical notes, structural preferences).\n"
        "Operations on this page:\n"
        "  - DEDUP: merge two lines that mean the same thing.  Keep the "
        "fuller wording; use the LATER of the two date prefixes (or no "
        "prefix if both are pre-convention).  Dedup is ALLOWED between two "
        "pre-convention lines — merging is lossless.\n"
        "  - SUPERSEDE: if two lines describe the same fact with different "
        "values (e.g. '[2026-01-10] Current weight: 67kg' and "
        "'[2026-04-15] Current weight: 60kg'), KEEP ONLY the line with the "
        "LATER date prefix — it's the current truth.\n"
        "    Supersede REQUIRES at least one dated line; do NOT supersede "
        "between two pre-convention lines (no way to tell which is newer).\n"
        "Time-bounded entries are rare here — usually keep."
    ),
    "goals": (
        "This page holds active intentions (targets, things the user is trying "
        "to achieve right now).\n"
        "Operations on this page:\n"
        "  - DEDUP: 'reduce sweets' and 'cut sugar' are the same goal — "
        "merge, keep the LATER date prefix (or no prefix if both are "
        "pre-convention).  Dedup is ALLOWED between two pre-convention lines.\n"
        "  - SUPERSEDE: if the target itself changed (e.g. "
        "'[2026-02-01] goal weight: 62kg' then "
        "'[2026-04-10] goal weight: 60kg'), KEEP ONLY the line with the "
        "LATER date prefix.\n"
        "  - STALE TIME-BOUNDED GOALS: use the date prefix to judge if a "
        "relative-phrase goal has passed its window.  Treat common phrases as:\n"
        "      'for the next week'   → window = 7 days\n"
        "      'for the next month'  → window = 31 days\n"
        "      'this week'           → window = 7 days\n"
        "      'this month'          → window = 31 days\n"
        "      'next few days'       → window = 7 days\n"
        "    If (today - prefix_date) > window, the goal has expired — drop it.\n"
        "    Example: '[2026-03-01] cutting sugar for the next month' and "
        "today is 2026-04-15 → 45 days old > 31-day window → drop.\n"
        "  - EXPIRED EXPLICIT DEADLINES: also drop entries whose text "
        "contains an EXPLICIT date that has passed (e.g. 'hit 60kg by "
        "summer 2025').\n"
        "  - Lines without a date prefix: don't guess their age — keep them "
        "unless an explicit deadline is in the text and has passed."
    ),
    "patterns": (
        "This page holds behavioral observations the bot has noticed "
        "(e.g. 'skips breakfast on busy days').\n"
        "Operations on this page:\n"
        "  - DEDUP: merge near-identical patterns.  Keep the LATER date "
        "prefix (or no prefix if both are pre-convention) and the fuller "
        "wording.  Dedup is ALLOWED between two pre-convention lines.\n"
        "  - SUPERSEDE: if a pattern has been explicitly contradicted by a "
        "later-dated entry, drop the older one.\n"
        "  - Do NOT drop a pattern just because it hasn't been observed "
        "recently — silent absence is not contradiction."
    ),
    "wins": (
        "This page holds accomplishments and milestones.\n"
        "Operations on this page:\n"
        "  - DEDUP ONLY: if the same win is recorded twice, keep the fuller "
        "wording and the LATER date prefix.\n"
        "  - NEVER drop a win. NEVER supersede a win. Past achievements stay "
        "valid forever, regardless of age."
    ),
}

_LINT_PROMPT = """You are tidying a personal memory page for a nutrition bot user.

Page name: {page}.md
{guidance}

Current page content:
---
{content}
---

Today's date: {today}

About the date prefix:
Every bullet should start with `[YYYY-MM-DD]` — the day that bullet was
added or last edited at ingest.  Use it to compute age: today − prefix_date.
When merging two lines, keep the LATER date prefix (it's the more recent
knowledge).  PRESERVE the `[YYYY-MM-DD]` prefix exactly on every line you
keep.  If a line has no prefix, leave it unprefixed — do NOT invent a date.

Task: rewrite this page to be tidier.

STRICT rules:
- DO NOT add any new information that isn't already stated on the page.
- DO NOT change meaning — only dedup, supersede, drop stale, and tighten wording.
- PRESERVE every `[YYYY-MM-DD]` prefix on the lines you keep.  When merging, use the later date.
- When in doubt whether two lines are duplicates, or whether an entry is stale, or which of two values is current, KEEP BOTH / KEEP IT. Safer to be redundant than to delete truth.
- Preserve the page's existing structure: keep the top-level `# {Page}` heading, keep any HTML `<!-- ... -->` comments at the top (they may be migration markers or template instructions).
- Preserve the user's own wording where possible. Don't rewrite into your own style.
- Return ONLY the rewritten page content. No preamble, no explanation, no code fences."""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_BULLET_LINE_RE = re.compile(r"^\s*[-*+]\s+\S", re.MULTILINE)


def _is_empty_page(content: str) -> bool:
    """
    True if the page has no real content yet — i.e. no bullet lines.

    We used to test for the substring `_(Empty` (the template placeholder),
    but that was too loose: once ingest appended a bullet below the
    placeholder without removing it, the check kept matching and skipped
    perfectly-non-empty pages.  The presence of any bullet is the reliable
    signal.
    """
    if not content or not content.strip():
        return True
    return _BULLET_LINE_RE.search(content) is None


def _line_count(content: str) -> int:
    """Count non-blank lines, for before/after stats."""
    return sum(1 for line in content.splitlines() if line.strip())


def _backup_dir(user_id: int) -> Path:
    return wiki.user_wiki_dir(user_id) / ".backups"


def _backup_page(user_id: int, page_name: str, content: str) -> Path:
    """
    Save the current content of a page to .backups/{page}.md.{timestamp}.
    Keep only the most recent BACKUP_RETENTION backups for this page;
    delete older ones.  Returns the path of the new backup.
    """
    bdir = _backup_dir(user_id)
    bdir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup_path = bdir / f"{page_name}.md.{ts}"
    backup_path.write_text(content, encoding="utf-8")

    # Purge older backups of this same page, keep only BACKUP_RETENTION.
    existing = sorted(
        bdir.glob(f"{page_name}.md.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # newest first
    )
    for old in existing[BACKUP_RETENTION:]:
        try:
            old.unlink()
        except OSError:
            pass  # best-effort; not worth failing a lint over

    return backup_path


# ─────────────────────────────────────────────────────────────────────────────
# Per-page Claude call
# ─────────────────────────────────────────────────────────────────────────────

async def _lint_page(page_name: str, content: str) -> str:
    """
    Send one page to Haiku, return the tidied version.
    Returns the ORIGINAL content unchanged if the model fails or returns
    something suspicious (empty, or much longer than input).
    """
    prompt = _LINT_PROMPT.format(
        page=page_name,
        Page=page_name.capitalize(),
        guidance=_PAGE_GUIDANCE[page_name],
        content=content,
        today=datetime.now().strftime("%Y-%m-%d"),
    )

    try:
        client = _get_async_client()
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        new_content = response.content[0].text.strip()
    except Exception as e:
        _log.warning(f"lint call failed for {page_name}: {e}")
        return content  # fall back to original

    # Strip code fences if Haiku added them despite instructions.
    if new_content.startswith("```"):
        new_content = new_content.strip("`")
        if new_content.lower().startswith("markdown"):
            new_content = new_content[len("markdown"):]
        new_content = new_content.strip()

    # Sanity checks — if output looks wrong, keep the original.
    if not new_content:
        _log.warning(f"lint returned empty for {page_name}; keeping original")
        return content

    # Lint should shrink or hold steady — never grow the page.
    # Allow a small buffer (1.1x) for formatting tweaks.
    if len(new_content) > len(content) * 1.1 and len(content) > 50:
        _log.warning(
            f"lint for {page_name} GREW from {len(content)} to {len(new_content)} chars; "
            f"rejecting and keeping original"
        )
        return content

    return new_content


# ─────────────────────────────────────────────────────────────────────────────
# log.md append — audit trail for every lint pass
# ─────────────────────────────────────────────────────────────────────────────

def _append_log(user_id: int, per_page: dict[str, dict]) -> None:
    """
    Append a dated summary of this lint pass to log.md.

    Format:
        ## [YYYY-MM-DD] Lint pass
        - profile: 12 lines → 10 (cleaned)
        - goals:   5 lines → 4 (cleaned)
        - patterns: skipped (empty)
        - wins:    2 lines → 2 (no change)
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"\n## [{date_str}] Lint pass"]
    for page, info in per_page.items():
        if info.get("skipped"):
            lines.append(f"- {page}: skipped ({info['skipped']})")
        else:
            before = info["before"]
            after = info["after"]
            tag = "no change" if before == after else "cleaned"
            lines.append(f"- {page}: {before} lines → {after} ({tag})")

    entry = "\n".join(lines) + "\n"

    log_path = wiki.user_wiki_dir(user_id) / "log.md"
    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    log_path.write_text(existing + entry, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Proactive weight-pattern observations (issue #7 surfacing)
#
# Each Saturday lint pass also checks the user's weight history and, if a
# clear trend / plateau / milestone is present, appends a dated observation
# to patterns.md. The bot's existing patterns.md format expects bullet lines
# stamped with [YYYY-MM-DD], so these entries plug in cleanly.
#
# Deterministic templates — we don't use an LLM here. The observations are
# short, factual, and dated. The LLM-driven summaries (evening, weekly,
# Q&A) then surface these as part of the wiki context.
# ─────────────────────────────────────────────────────────────────────────────

# A pattern is "interesting enough to add to the wiki" when:
#   - sustained trend ≥ 0.2 kg/week (absolute) over ≥ 3 weeks → "trending down/up"
#   - plateau: max-min over the past 14 days < 0.4 kg                 → "steady"
#   - milestone: crossed below/above an integer kg in the past 7 days → "crossed"
#
# All thresholds tuned to be conservative — we'd rather miss a borderline
# trend than spam patterns.md with noise.

_WEIGHT_TREND_KG_PER_WEEK_THRESHOLD = 0.2
_WEIGHT_TREND_MIN_WEEKS = 3
_WEIGHT_PLATEAU_WINDOW_DAYS = 14
_WEIGHT_PLATEAU_BAND_KG = 0.4
_WEIGHT_MILESTONE_WINDOW_DAYS = 7


def _detect_weight_observations(user_id: int) -> list[str]:
    """Return a list of single-sentence observations about this user's weight
    over the last ~30 days. Empty list if nothing notable. Each string is
    ready to be wrapped in a `- [YYYY-MM-DD] ...` bullet.

    Imported lazily to keep the lint module's top-level imports independent
    of database (lint pages are about wiki content, not DB schemas).
    """
    import database as db
    from datetime import date, timedelta

    today = date.today()
    since = (today - timedelta(days=35)).isoformat()
    history = db.get_daily_min_weights(user_id, since=since)
    if len(history) < 7:
        return []   # not enough data to say anything meaningful

    observations: list[str] = []
    values = [(date.fromisoformat(r["date"]), r["weight_kg"]) for r in history]

    # ── Sustained trend ──────────────────────────────────────────────────────
    # Fit a simple end-to-end average and report if ≥ threshold for ≥ 3 weeks.
    first_d, first_w = values[0]
    last_d, last_w = values[-1]
    span_days = max(1, (last_d - first_d).days)
    if span_days >= _WEIGHT_TREND_MIN_WEEKS * 7:
        kg_per_wk = (last_w - first_w) / span_days * 7
        if abs(kg_per_wk) >= _WEIGHT_TREND_KG_PER_WEEK_THRESHOLD:
            direction = "down" if kg_per_wk < 0 else "up"
            observations.append(
                f"Weight trending {direction} ~{abs(kg_per_wk):.2f} kg/week "
                f"over the past {span_days // 7} weeks"
            )

    # ── Plateau ──────────────────────────────────────────────────────────────
    plateau_window = [
        (d, w) for d, w in values
        if (today - d).days <= _WEIGHT_PLATEAU_WINDOW_DAYS
    ]
    if len(plateau_window) >= 5:
        weights_window = [w for _, w in plateau_window]
        band = max(weights_window) - min(weights_window)
        if band < _WEIGHT_PLATEAU_BAND_KG:
            avg = sum(weights_window) / len(weights_window)
            observations.append(
                f"Weight steady around {avg:.1f} kg (within ±{band/2:.1f} kg "
                f"for {len(plateau_window)} days)"
            )

    # ── Milestone: crossed an integer kg recently ────────────────────────────
    # If today's weight is below floor(prior weight) — i.e. crossed below
    # the next-lower integer — in the past 7 days, flag it.
    import math
    recent = [(d, w) for d, w in values if (today - d).days <= _WEIGHT_MILESTONE_WINDOW_DAYS]
    older  = [(d, w) for d, w in values if (today - d).days >  _WEIGHT_MILESTONE_WINDOW_DAYS]
    if recent and older:
        cur = recent[-1][1]
        prior = older[-1][1]
        cur_floor = math.floor(cur)
        prior_floor = math.floor(prior)
        if cur_floor < prior_floor:
            observations.append(
                f"Crossed below {prior_floor} kg (now {cur:.1f} kg)"
            )
        elif cur_floor > prior_floor:
            observations.append(
                f"Crossed above {prior_floor + 1} kg (now {cur:.1f} kg)"
            )

    return observations


def _existing_weight_observations(content: str) -> set[str]:
    """Pull the gist of any existing weight bullets out of patterns.md so we
    don't append a near-duplicate this week.

    Match heuristic: anything containing the word 'weight' OR 'kg' inside a
    bullet line. We compare on the lowercase-stripped string to avoid
    re-stamping a week-old observation that's still true.
    """
    out: set[str] = set()
    for line in content.splitlines():
        s = line.strip()
        if not s.startswith(("- ", "* ", "• ")):
            continue
        low = s.lower()
        if "weight" in low or " kg" in low:
            # Strip bullet marker + date prefix for similarity match
            body = re.sub(r"^[\s\-\*•]+", "", s).strip()
            body = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", body).strip()
            out.add(body.lower())
    return out


def _append_weight_observations(user_id: int) -> int:
    """Detect any notable weight patterns and append new (non-duplicate) ones
    to patterns.md as dated bullets. Returns the number of new bullets added.
    Caller holds `wiki.get_lock(user_id)`.
    """
    new_obs = _detect_weight_observations(user_id)
    if not new_obs:
        return 0

    existing = wiki.read_page(user_id, "patterns") or ""
    existing_low = _existing_weight_observations(existing)
    date_str = datetime.now().strftime("%Y-%m-%d")

    appended: list[str] = []
    for obs in new_obs:
        # Skip if a similar observation is already in patterns.md.
        if any(_observations_match(obs, e) for e in existing_low):
            continue
        appended.append(f"- [{date_str}] {obs}")

    if not appended:
        return 0

    # Strip the empty-placeholder line if it's still there (self-healing).
    cleaned = wiki.strip_empty_placeholder(existing).rstrip()
    new_content = cleaned + "\n" + "\n".join(appended) + "\n"
    wiki.write_page(user_id, "patterns", new_content)

    # Leave a breadcrumb in log.md for each new observation, matching the
    # existing append-to-page audit convention.
    for line in appended:
        wiki.append_log(
            user_id,
            "Added to patterns.md (weight observation)",
            line,
        )

    _log.info(
        f"user={user_id} appended {len(appended)} weight observation(s)"
    )
    return len(appended)


def _observations_match(a: str, b: str) -> bool:
    """Loose similarity check between two weight observations — same notion
    (trending up vs trending down vs steady vs crossed) → match.
    Prevents stamping "Weight trending down 0.3 kg/week" when patterns.md
    already has "Weight trending down 0.25 kg/week" from last week.
    """
    al, bl = a.lower(), b.lower()
    for keyword in ("trending down", "trending up", "steady", "crossed below", "crossed above"):
        if keyword in al and keyword in bl:
            return True
    return False


async def lint_user_wiki(user_id: int) -> dict:
    """
    Run a lint pass over every lintable page for this user.

    Returns:
        {
            "profile":  {"before": 12, "after": 10},
            "goals":    {"before": 5,  "after": 4},
            "patterns": {"skipped": "empty"},
            "wins":     {"before": 2,  "after": 2},
        }

    Holds the per-user wiki lock for the whole pass so a concurrent ingest
    can't write between our read and our write.
    """
    wiki.ensure_user_wiki(user_id)

    result: dict[str, dict] = {}

    async with wiki.get_lock(user_id):
        for page_name in LINTABLE_PAGES:
            content = wiki.read_page(user_id, page_name)

            if _is_empty_page(content):
                result[page_name] = {"skipped": "empty"}
                _log.info(f"user={user_id} page={page_name} skipped (empty)")
                continue

            before_lines = _line_count(content)

            new_content = await _lint_page(page_name, content)

            if new_content == content:
                # No change — either Haiku judged it already clean, or one of
                # our sanity checks rejected the output.  Either way, don't
                # bother backing up or rewriting.
                result[page_name] = {
                    "before": before_lines,
                    "after": before_lines,
                    "rewritten": False,
                }
                _log.info(f"user={user_id} page={page_name} no change ({before_lines} lines)")
                continue

            # Back up BEFORE overwriting.
            _backup_page(user_id, page_name, content)
            wiki.write_page(user_id, page_name, new_content)

            after_lines = _line_count(new_content)
            result[page_name] = {
                "before": before_lines,
                "after": after_lines,
                "rewritten": True,
            }
            _log.info(
                f"user={user_id} page={page_name} "
                f"lines {before_lines}→{after_lines} (rewritten)"
            )

        # Deterministic pass on goals.md: consolidate any calorie-goal-ish
        # drift (multiple "Target 1800 kcal/day" style lines, or a legacy
        # phrasing that Haiku kept) into the single canonical bullet. Runs
        # after Haiku so Haiku's text-level dedup has already happened; this
        # is the belt-and-braces fix-up for the one line the app reads
        # programmatically. Still inside the lock.
        try:
            consol = wiki.consolidate_goal_line(user_id)
            if consol.get("rewrote"):
                # Fold it into the goals result so the log line reflects reality.
                after_lines = _line_count(wiki.read_page(user_id, "goals"))
                goals_info = result.get("goals", {})
                if goals_info.get("skipped"):
                    # Goals was empty at the top of the loop but somehow had a
                    # kcal line — surface it as its own entry instead.
                    result["goals"] = {
                        "before": goals_info.get("before", 0),
                        "after": after_lines,
                        "rewritten": True,
                        "consolidated_goal_line": True,
                    }
                else:
                    goals_info["after"] = after_lines
                    goals_info["rewritten"] = True
                    goals_info["consolidated_goal_line"] = True
                    result["goals"] = goals_info
                _log.info(
                    f"user={user_id} consolidated goal line: "
                    f"found={consol['found']} kept={consol['kept']}"
                )
        except Exception as e:
            # Consolidation must never break a lint pass; log and move on.
            _log.warning(f"user={user_id} consolidate_goal_line failed: {e}")

        # Same belt-and-braces pass for the target-weight canonical line.
        try:
            consol_tw = wiki.consolidate_target_weight_line(user_id)
            if consol_tw.get("rewrote"):
                _log.info(
                    f"user={user_id} consolidated target weight line: "
                    f"found={consol_tw['found']} kept={consol_tw['kept']}"
                )
        except Exception as e:
            _log.warning(f"user={user_id} consolidate_target_weight_line failed: {e}")

        # Proactive weight-pattern observations (issue #7). Detects clear
        # trends / plateaus / milestones from weight_readings and appends
        # dated bullets to patterns.md when something new is worth noting.
        # Errors are swallowed so a missing weight feature can't break lint.
        try:
            n_added = _append_weight_observations(user_id)
            if n_added:
                # Fold into the patterns result so the lint summary shows it.
                patterns_info = result.get("patterns", {})
                after_lines = _line_count(wiki.read_page(user_id, "patterns"))
                patterns_info["after"] = after_lines
                patterns_info["rewritten"] = True
                patterns_info["weight_observations_added"] = n_added
                # If patterns was previously skipped (empty), it isn't empty now.
                patterns_info.pop("skipped", None)
                patterns_info.setdefault("before", after_lines - n_added)
                result["patterns"] = patterns_info
        except Exception as e:
            _log.warning(f"user={user_id} weight observations failed: {e}")

        # After all pages done, append the summary to log.md (still holding
        # the lock, so we're safe from concurrent writes).
        #
        # Skip the log entry when NO page was rewritten — since lint now runs
        # in the background after every ingest change, a page-by-page "no
        # change" summary after every user message would flood log.md with
        # noise.  The rule is simple: log.md gets an entry only when lint
        # actually did something.  Individual appends/removes already left
        # their own breadcrumbs (from ``_apply_wiki_update``), so when lint
        # is a no-op the audit trail is still complete.
        if any(info.get("rewritten") for info in result.values()):
            _append_log(user_id, result)
        else:
            _log.info(f"user={user_id} lint pass no-op, skipping log.md entry")

    return result
