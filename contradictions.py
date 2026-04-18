"""
contradictions.py — Step 4b of the Karpathy LLM-wiki memory plan.

Catches cross-page conflicts in a user's wiki and resolves them through
natural-language dialog.  The three responsibilities:

  detect(user_id)   — read the four editable pages + recent log entries,
                       ask Haiku for real contradictions, return a list.
                       (Conservative: miss one is OK, false-flag is bad.)

  record(user_id)   — write new contradictions to contradictions.md under
                       the user's wiki folder, as `## [TIMESTAMP] OPEN`
                       sections.  Never re-records an identical pair.

  oldest_open(uid)  — parse contradictions.md, return the earliest still-
                       OPEN section (so the bot can DM the user once per
                       open conflict rather than spamming).

  classify_user_reply(contradiction, message)
                    — ask Haiku to map a free-text reply onto one of six
                       actions: pick_a, pick_b, keep_both, remove_both,
                       custom (with new text + target page), or unrelated.
                       "unrelated" means the user wrote about something
                       else — fall back to the normal intent router.

  resolve(user_id, ts, action, custom_text=None, target_page=None)
                    — under the per-user wiki lock: apply the wiki change,
                       flip the `## [TIMESTAMP] OPEN` header to
                       `RESOLVED → action`, append a `- **Resolution**` bullet,
                       and write an entry to log.md.  All atomic.

The `log` page is append-only audit history — we never mutate it.  When a
contradiction involves a log line, resolution only touches the non-log side.
"""

import json
import logging
import re as _re
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

import wiki

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

# Pages we scan for contradictions and ALSO rewrite on resolve.
# log.md is read for context but never mutated.
EDITABLE_PAGES = ["profile", "goals", "patterns", "wins"]
CONTEXT_PAGES = EDITABLE_PAGES + ["log"]

# How many lines from the end of log.md to include in detection context.
LOG_TAIL_LINES = 50

# Haiku — narrow text-classification work.  Same model as lint.py.
_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS_DETECT = 1500
_MAX_TOKENS_CLASSIFY = 400

_async_client: Optional[anthropic.AsyncAnthropic] = None


def _get_async_client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic()
    return _async_client


_log = logging.getLogger("contradictions")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    _h = logging.FileHandler(Path(__file__).parent / "contradictions.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _log.addHandler(_h)
    _log.propagate = False


CONTRADICTIONS_HEADER = """# Contradictions

<!--
Pending and resolved conflicts across this user's wiki pages.
OPEN      = awaiting user input (the bot will DM once about the oldest open one).
RESOLVED  = user has decided; kept here for history.

Format of each section:
## [TIMESTAMP] OPEN                    ← or: RESOLVED → <action>
- **A** (page): `line of text`
- **B** (page): `line of text`
- **Why**: short reason this looks contradictory.
- **Resolution** (YYYY-MM-DD): …       ← only on RESOLVED sections.
-->
"""


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

_DETECTION_PROMPT = """You are auditing a nutrition-bot user's personal wiki for contradictions.

A contradiction is when two statements, taken at face value, can't both be true
about the user right now.  Examples:

  CONTRADICTION:
    profile: "Does not eat fish"
    log (2026-04-13): "User ate salmon at dinner"
      → the profile says one thing; a recent log entry implies the opposite.

  CONTRADICTION:
    goals: "[2026-02-01] Goal weight: 62kg"
    goals: "[2026-04-10] Goal weight: 60kg"
      → two different current targets with no bridging explanation.

  NOT a contradiction:
    goals:    "Cut sugar"
    patterns: "Eats chocolate on Fridays"
      → a goal and an observed pattern can coexist; the pattern is what
        motivated the goal.

  NOT a contradiction:
    wins: "Lost 3kg by Jan 2026"
    wins: "Lost 5kg by April 2026"
      → progressively better wins; both true facts about the past.

  NOT a contradiction:
    profile: "Vegetarian"
    wins (2024): "Tried sushi once at a wedding"
      → old one-off event, not a standing conflict.

Be CONSERVATIVE.  If you're unsure, don't flag it.  Returning an empty list
is a perfectly good answer.

Here is the user's wiki (today = {today}):

{pages}

{log_tail}

Return JSON in exactly this shape (no markdown, no code fences, no preamble):

{{
  "contradictions": [
    {{
      "page_a": "<page name where line A lives>",
      "line_a": "<exact text of line A, without the leading '- '>",
      "page_b": "<page name where line B lives>",
      "line_b": "<exact text of line B, without the leading '- '>",
      "why": "<one short sentence explaining the conflict>"
    }}
  ]
}}

Rules for the JSON:
- page_a and page_b must each be one of: profile, goals, patterns, wins, log.
- line_a and line_b must be COPIED VERBATIM from the wiki above — preserve
  any `[YYYY-MM-DD]` date prefix, preserve punctuation.  Do NOT summarise.
- If you find no contradictions, return exactly: {{"contradictions": []}}."""


_CLASSIFIER_PROMPT = """You are classifying a user's natural-language reply to a
contradiction the bot has flagged in their personal wiki.

The contradiction:

  A ({page_a}): {line_a}
  B ({page_b}): {line_b}
  Why: {why}

The user's message:

  "{user_message}"

Decide which of these six actions best fits the user's intent:

  pick_a       — user wants to keep A, discard B.
                   (e.g. "first one is correct", "the 60kg target is right")
  pick_b       — user wants to keep B, discard A.
                   (e.g. "the second one", "actually 62kg")
  keep_both    — user says both are actually valid (no real conflict).
                   (e.g. "both are fine", "they're not contradicting each other")
  remove_both  — user wants neither; drop both entries.
                   (e.g. "neither", "forget both", "delete both of those")
  custom       — user is giving a new/updated value that replaces one or both.
                   (e.g. "actually my new goal is 59kg", "it's really 2000 kcal")
                   In this case, fill `custom_text` with the user's intended
                   new wording, and `target_page` with the page it belongs on.
  unrelated    — the user's message is NOT about this contradiction at all;
                   they wrote about food, a question, a new goal unrelated
                   to this conflict, etc.  The bot will fall back to normal
                   handling of their message.

Return JSON in exactly this shape (no markdown, no code fences, no preamble):

{{
  "action": "<one of: pick_a, pick_b, keep_both, remove_both, custom, unrelated>",
  "custom_text": "<new line text, ONLY if action is custom; else omit>",
  "target_page": "<one of: profile, goals, patterns, wins; ONLY if action is custom>"
}}

Rules:
- If A and B are on the same editable page (profile/goals/patterns/wins) and
  action is custom, `target_page` must be that page.
- If A and B are on different pages and action is custom, pick whichever
  page the new text most naturally belongs on (new goal → goals, identity
  fact → profile, observed habit → patterns, accomplishment → wins).
- `target_page` is never "log" — log.md is append-only history."""


# ─────────────────────────────────────────────────────────────────────────────
# contradictions.md IO
# ─────────────────────────────────────────────────────────────────────────────

def _contradictions_path(user_id: int) -> Path:
    """Per-user contradictions.md — separate from wiki.PAGES."""
    return wiki.user_wiki_dir(user_id) / "contradictions.md"


def _ensure_file(user_id: int) -> Path:
    """Create contradictions.md with the header comment if missing."""
    path = _contradictions_path(user_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONTRADICTIONS_HEADER, encoding="utf-8")
    return path


# A section starts with `## [` and continues until the next `## [` or EOF.
_SECTION_RE = _re.compile(
    r"(?m)^## \[(?P<ts>[^\]]+)\]\s+(?P<status>OPEN|RESOLVED)(?:\s*→\s*(?P<action>\S+))?"
)


_COMMENT_RE = _re.compile(r"<!--.*?-->", _re.DOTALL)


def _strip_html_comments(content: str) -> str:
    """
    Blank out HTML comment blocks so our `## [` section regex doesn't match
    example headers inside the file-level documentation comment.
    Preserves byte offsets by replacing with same-length whitespace, so
    `start`/`end` indices computed on the stripped text still map back to
    the original file.
    """
    return _COMMENT_RE.sub(lambda m: " " * (m.end() - m.start()), content)


def _parse_sections(content: str) -> list[dict]:
    """
    Parse contradictions.md into a list of section dicts:
        {"ts": "...", "status": "OPEN"|"RESOLVED", "action": "...",
         "page_a": "...", "line_a": "...",
         "page_b": "...", "line_b": "...",
         "why": "...", "raw": "<full section text>", "start": int, "end": int}
    """
    # Match against a comment-stripped copy so we don't hit example headers
    # inside the file-level `<!-- ... -->` documentation.  Offsets are
    # preserved because we replaced comments with equal-length whitespace.
    scan_text = _strip_html_comments(content)
    sections: list[dict] = []
    matches = list(_SECTION_RE.finditer(scan_text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[start:end]

        sec = {
            "ts": m.group("ts"),
            "status": m.group("status"),
            "action": m.group("action") or "",
            "raw": block,
            "start": start,
            "end": end,
            "page_a": "",
            "line_a": "",
            "page_b": "",
            "line_b": "",
            "why": "",
        }

        # Pull the A / B / Why bullets out of the block.
        for key in ("A", "B"):
            bm = _re.search(
                rf"^- \*\*{key}\*\*\s*\(([^)]+)\):\s*`(.+?)`\s*$",
                block,
                _re.MULTILINE,
            )
            if bm:
                sec[f"page_{key.lower()}"] = bm.group(1).strip()
                sec[f"line_{key.lower()}"] = bm.group(2).strip()

        wm = _re.search(r"^- \*\*Why\*\*:\s*(.+?)$", block, _re.MULTILINE)
        if wm:
            sec["why"] = wm.group(1).strip()

        sections.append(sec)
    return sections


def oldest_open(user_id: int) -> Optional[dict]:
    """
    Return the oldest OPEN contradiction for this user, or None if none are open.
    The returned dict has keys: ts, page_a, line_a, page_b, line_b, why.
    """
    path = _contradictions_path(user_id)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    opens = [s for s in _parse_sections(content) if s["status"] == "OPEN"]
    if not opens:
        return None
    # Timestamps are sortable as strings: "2026-04-20T10-07-22"
    opens.sort(key=lambda s: s["ts"])
    return opens[0]


def list_open(user_id: int) -> list[dict]:
    path = _contradictions_path(user_id)
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    return [s for s in _parse_sections(content) if s["status"] == "OPEN"]


# ─────────────────────────────────────────────────────────────────────────────
# Detection — read wiki, ask Haiku for conflicts
# ─────────────────────────────────────────────────────────────────────────────

def _is_empty_page(content: str) -> bool:
    return (not content) or (not content.strip()) or ("_(Empty" in content)


def _log_tail(user_id: int, n_lines: int = LOG_TAIL_LINES) -> str:
    content = wiki.read_page(user_id, "log")
    if _is_empty_page(content):
        return ""
    lines = content.splitlines()
    if len(lines) <= n_lines:
        return content
    return "\n".join(lines[-n_lines:])


async def detect(user_id: int) -> list[dict]:
    """
    Read the wiki, ask Haiku to find real contradictions.  Return a list of
    dicts: {page_a, line_a, page_b, line_b, why}.  Empty list is fine — means
    nothing flagged.
    """
    # Build the page dump.
    parts = []
    for name in EDITABLE_PAGES:
        content = wiki.read_page(user_id, name)
        if _is_empty_page(content):
            continue
        parts.append(f"### {name}.md\n{content.strip()}")

    if not parts:
        return []  # no real content yet; nothing to contradict

    pages_block = "\n\n".join(parts)

    log_snip = _log_tail(user_id)
    log_block = f"### log.md (recent entries)\n{log_snip.strip()}" if log_snip.strip() else ""

    prompt = _DETECTION_PROMPT.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        pages=pages_block,
        log_tail=log_block,
    )

    try:
        client = _get_async_client()
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS_DETECT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        _log.warning(f"user={user_id} detection call failed: {e}")
        return []

    # Strip code fences if Haiku added them.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _log.warning(f"user={user_id} detection returned non-JSON: {e}; raw={raw[:200]}")
        return []

    found = data.get("contradictions", []) or []
    cleaned: list[dict] = []
    for c in found:
        try:
            pa = str(c["page_a"]).strip()
            la = str(c["line_a"]).strip()
            pb = str(c["page_b"]).strip()
            lb = str(c["line_b"]).strip()
            why = str(c.get("why", "")).strip()
        except (KeyError, TypeError):
            continue
        if pa not in CONTEXT_PAGES or pb not in CONTEXT_PAGES:
            continue
        if not la or not lb:
            continue
        cleaned.append({
            "page_a": pa, "line_a": la,
            "page_b": pb, "line_b": lb,
            "why": why,
        })

    _log.info(f"user={user_id} detect found {len(cleaned)} contradiction(s)")
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Record — append OPEN sections, skip dupes
# ─────────────────────────────────────────────────────────────────────────────

def _same_pair(a: dict, b: dict) -> bool:
    """Two contradictions refer to the same pair of lines (order-agnostic)."""
    forward = (
        a["page_a"] == b["page_a"] and a["line_a"] == b["line_a"]
        and a["page_b"] == b["page_b"] and a["line_b"] == b["line_b"]
    )
    swapped = (
        a["page_a"] == b["page_b"] and a["line_a"] == b["line_b"]
        and a["page_b"] == b["page_a"] and a["line_b"] == b["line_a"]
    )
    return forward or swapped


def _format_open_section(ts: str, c: dict) -> str:
    return (
        f"\n## [{ts}] OPEN\n"
        f"- **A** ({c['page_a']}): `{c['line_a']}`\n"
        f"- **B** ({c['page_b']}): `{c['line_b']}`\n"
        f"- **Why**: {c.get('why', '').strip() or '(no reason given)'}\n"
    )


def record(user_id: int, conflicts: list[dict]) -> list[str]:
    """
    Append new OPEN sections to contradictions.md for any conflicts not
    already present (in any state — OPEN or RESOLVED).  Returns the list of
    timestamps that were newly added.
    """
    if not conflicts:
        return []

    path = _ensure_file(user_id)
    content = path.read_text(encoding="utf-8")
    existing = _parse_sections(content)

    new_timestamps: list[str] = []
    appended_blocks: list[str] = []

    for c in conflicts:
        if any(_same_pair(c, e) for e in existing if e["page_a"] and e["page_b"]):
            continue
        # Timestamp granular to the second so rapid-fire additions don't collide.
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        # If multiple are added the same second, tack on an index.
        if any(t == ts for t in new_timestamps):
            ts = f"{ts}-{len(new_timestamps)}"
        new_timestamps.append(ts)
        appended_blocks.append(_format_open_section(ts, c))

    if not appended_blocks:
        return []

    new_content = content.rstrip() + "\n" + "".join(appended_blocks)
    if not new_content.endswith("\n"):
        new_content += "\n"
    path.write_text(new_content, encoding="utf-8")

    _log.info(f"user={user_id} recorded {len(new_timestamps)} new contradiction(s)")
    return new_timestamps


# ─────────────────────────────────────────────────────────────────────────────
# Classifier — natural-language reply → action
# ─────────────────────────────────────────────────────────────────────────────

async def classify_user_reply(contradiction: dict, user_message: str) -> dict:
    """
    Ask Haiku to classify the user's free-text reply as one of six actions.
    Returns a dict like:
        {"action": "pick_a"}
        {"action": "custom", "custom_text": "...", "target_page": "goals"}
        {"action": "unrelated"}

    On any failure or malformed response, returns {"action": "unrelated"} so
    the user's message falls through to the normal intent router.
    """
    prompt = _CLASSIFIER_PROMPT.format(
        page_a=contradiction["page_a"],
        line_a=contradiction["line_a"],
        page_b=contradiction["page_b"],
        line_b=contradiction["line_b"],
        why=contradiction.get("why", ""),
        user_message=user_message.strip().replace('"', "'"),
    )

    try:
        client = _get_async_client()
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS_CLASSIFY,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        _log.warning(f"classifier call failed: {e}")
        return {"action": "unrelated"}

    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning(f"classifier returned non-JSON: {raw[:200]}")
        return {"action": "unrelated"}

    action = data.get("action", "unrelated")
    if action not in {"pick_a", "pick_b", "keep_both", "remove_both", "custom", "unrelated"}:
        return {"action": "unrelated"}

    result: dict = {"action": action}
    if action == "custom":
        ct = str(data.get("custom_text", "")).strip()
        tp = str(data.get("target_page", "")).strip()
        if not ct or tp not in EDITABLE_PAGES:
            # Custom without the necessary fields → safer to treat as unrelated.
            return {"action": "unrelated"}
        result["custom_text"] = ct
        result["target_page"] = tp

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Resolve — apply wiki edit, flip section to RESOLVED, append log.md entry
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_for_match(s: str) -> str:
    """Strip leading bullet chars + whitespace so we can line-match robustly."""
    return _re.sub(r"^[\s\-\*•·‣⁃]+", "", s).strip()


def _remove_line_from_page(user_id: int, page: str, target_line: str) -> bool:
    """
    Delete a single bullet matching `target_line` from page.md.  Match is
    tolerant of leading bullet chars and surrounding whitespace.  No-op if
    the line isn't found or the page is "log" (append-only).
    Returns True if a line was removed.
    """
    if page == "log":
        return False  # never mutate the audit trail
    if page not in EDITABLE_PAGES:
        return False

    content = wiki.read_page(user_id, page)
    if not content:
        return False

    needle = _normalize_for_match(target_line)
    if not needle:
        return False

    kept: list[str] = []
    removed = False
    for line in content.splitlines():
        if not removed and _normalize_for_match(line) == needle:
            removed = True
            continue
        kept.append(line)

    if not removed:
        return False

    new_content = "\n".join(kept)
    if content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"
    wiki.write_page(user_id, page, new_content)
    return True


def _append_line_to_page(user_id: int, page: str, line_text: str) -> None:
    """
    Append a dated bullet to page.md (custom action).  Uses the standard
    `- [YYYY-MM-DD] text` convention established in step 4a.0.
    """
    if page not in EDITABLE_PAGES:
        raise ValueError(f"Cannot append to non-editable page: {page}")

    # Strip any leading bullet/date prefix the user's text may already have —
    # we'll re-add a fresh stamp so the convention stays consistent.
    cleaned = _normalize_for_match(line_text)
    cleaned = _re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", cleaned).strip()
    if not cleaned:
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    bullet = f"- [{date_str}] {cleaned}"

    content = wiki.read_page(user_id, page)
    if not content.endswith("\n"):
        content += "\n"
    wiki.write_page(user_id, page, content + bullet + "\n")


def _update_section_to_resolved(
    user_id: int,
    ts: str,
    action: str,
    resolution_text: str,
) -> bool:
    """
    Flip the `## [ts] OPEN` header in contradictions.md to
    `## [ts] RESOLVED → action` and append a `- **Resolution**` bullet.
    Returns True if the section was found and updated.
    """
    path = _contradictions_path(user_id)
    if not path.exists():
        return False

    content = path.read_text(encoding="utf-8")
    sections = _parse_sections(content)

    target = None
    for s in sections:
        if s["ts"] == ts and s["status"] == "OPEN":
            target = s
            break
    if target is None:
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    old_block = target["raw"]
    new_block = old_block

    # Replace the header line.
    new_block = _re.sub(
        rf"^## \[{_re.escape(ts)}\]\s+OPEN\s*$",
        f"## [{ts}] RESOLVED → {action}",
        new_block,
        count=1,
        flags=_re.MULTILINE,
    )

    # Trim any trailing whitespace/newlines, then append the Resolution bullet.
    new_block = new_block.rstrip() + f"\n- **Resolution** ({date_str}): {resolution_text}\n"

    new_content = content[:target["start"]] + new_block + content[target["end"]:]
    path.write_text(new_content, encoding="utf-8")
    return True


async def resolve(
    user_id: int,
    ts: str,
    action: str,
    custom_text: Optional[str] = None,
    target_page: Optional[str] = None,
) -> dict:
    """
    Apply a resolution to the contradiction with the given timestamp.

    Holds the per-user wiki lock for the whole operation so detect/lint/ingest
    can't race us between steps.

    Returns a dict summarizing what happened:
        {"ok": bool, "action": str, "summary": str, "log_entry": str}
    """
    async with wiki.get_lock(user_id):
        path = _contradictions_path(user_id)
        if not path.exists():
            return {"ok": False, "action": action, "summary": "No contradictions file.",
                    "log_entry": ""}

        sections = _parse_sections(path.read_text(encoding="utf-8"))
        target = next((s for s in sections if s["ts"] == ts and s["status"] == "OPEN"), None)
        if target is None:
            return {"ok": False, "action": action, "summary": "Contradiction not found or already resolved.",
                    "log_entry": ""}

        page_a = target["page_a"]
        line_a = target["line_a"]
        page_b = target["page_b"]
        line_b = target["line_b"]

        resolution_text = ""
        log_summary = ""
        log_details = ""
        reply_summary = ""

        if action == "pick_a":
            # Keep A, drop B.  If B is on log, we can't drop it; just record.
            removed = _remove_line_from_page(user_id, page_b, line_b)
            if page_b == "log" or not removed:
                resolution_text = f"User kept A ({page_a}); B ({page_b}) left as-is."
            else:
                resolution_text = f"User picked A. Removed B from {page_b}.md."
            log_summary = "Contradiction resolved: picked A"
            log_details = f"Kept: {line_a}\nRemoved: {line_b} (from {page_b}.md)" if removed \
                else f"Kept: {line_a}\nB left in place on {page_b}.md (append-only or not found)"
            reply_summary = f"Got it — keeping “{line_a}”."

        elif action == "pick_b":
            removed = _remove_line_from_page(user_id, page_a, line_a)
            if page_a == "log" or not removed:
                resolution_text = f"User kept B ({page_b}); A ({page_a}) left as-is."
            else:
                resolution_text = f"User picked B. Removed A from {page_a}.md."
            log_summary = "Contradiction resolved: picked B"
            log_details = f"Kept: {line_b}\nRemoved: {line_a} (from {page_a}.md)" if removed \
                else f"Kept: {line_b}\nA left in place on {page_a}.md (append-only or not found)"
            reply_summary = f"Got it — keeping “{line_b}”."

        elif action == "keep_both":
            resolution_text = "User confirmed both entries are valid."
            log_summary = "Contradiction reviewed: user confirmed both entries are valid"
            log_details = f"Kept both:\n- {line_a} ({page_a}.md)\n- {line_b} ({page_b}.md)"
            reply_summary = "Got it — keeping both as valid."

        elif action == "remove_both":
            removed_a = _remove_line_from_page(user_id, page_a, line_a)
            removed_b = _remove_line_from_page(user_id, page_b, line_b)
            parts = []
            if removed_a:
                parts.append(f"{page_a}.md")
            if removed_b:
                parts.append(f"{page_b}.md")
            where = ", ".join(parts) if parts else "(nothing removed — log-only or not found)"
            resolution_text = f"User dropped both. Removed from: {where}."
            log_summary = "Contradiction resolved: both entries dropped"
            log_details = f"Removed: {line_a} ({page_a}.md)\nRemoved: {line_b} ({page_b}.md)"
            reply_summary = "Got it — dropped both."

        elif action == "custom":
            if not custom_text:
                return {"ok": False, "action": action,
                        "summary": "Custom action needs custom_text.", "log_entry": ""}
            # Default target page: same page if A/B share one and it's editable;
            # else use the classifier's suggestion; else fall back to goals.
            if not target_page:
                if page_a == page_b and page_a in EDITABLE_PAGES:
                    target_page = page_a
                else:
                    target_page = "goals"
            if target_page not in EDITABLE_PAGES:
                target_page = "goals"

            # Replace both A and B with the new custom line.
            _remove_line_from_page(user_id, page_a, line_a)
            _remove_line_from_page(user_id, page_b, line_b)
            _append_line_to_page(user_id, target_page, custom_text)

            resolution_text = (
                f"User provided a new value. Replaced A and B with: "
                f"“{custom_text}” (written to {target_page}.md)."
            )
            log_summary = f"Contradiction resolved: replaced with new value on {target_page}.md"
            log_details = (
                f"Removed: {line_a} ({page_a}.md)\n"
                f"Removed: {line_b} ({page_b}.md)\n"
                f"Added: {custom_text} ({target_page}.md)"
            )
            reply_summary = f"Got it — noted “{custom_text}” in {target_page}.md."

        else:
            return {"ok": False, "action": action,
                    "summary": f"Unknown action: {action}.", "log_entry": ""}

        # Flip the contradictions.md section.
        _update_section_to_resolved(user_id, ts, action, resolution_text)

        # Append to log.md — audit trail of what the bot did and why.
        wiki.append_log(user_id, log_summary, log_details)

        _log.info(f"user={user_id} resolved ts={ts} action={action}")

        return {
            "ok": True,
            "action": action,
            "summary": reply_summary,
            "log_entry": log_summary,
        }
