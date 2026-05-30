"""
telegram_bot.py — Nutrition Bot main entry point.

Receives messages and photos from Telegram, routes to the right handler,
and replies. Nothing else.

Architecture:
  Telegram → this file → analyzer.py (Claude vision) → database.py (SQLite)
                       → advisor.py (smart replies & proactive push)

Run:   python telegram_bot.py
Cron:  python telegram_bot.py --daily-summary  (08:00 every day)
Cron:  python telegram_bot.py --weekly-review  (Sunday morning)
Cron:  python telegram_bot.py --lint-cron      (Saturday 10:05 Europe/Berlin)
"""

import asyncio
import logging
import os
import sys
import io

import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

import database as db
import analyzer
import advisor
import wiki
import lint
import contradictions

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("nutrition-bot")

BOT_TOKEN = os.getenv("NUTRITION_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    sys.exit("❌ No TELEGRAM_BOT_TOKEN set in .env")

# When a user sends multiple photos as an album, Telegram attaches the caption
# only to the first photo.  We cache it here so subsequent photos in the same
# album inherit the same caption (and therefore the same meal_type hint).
_album_caption_cache: dict[str, str] = {}   # media_group_id → caption text

# ─────────────────────────────────────────────────────────────────────────────
# Transient-error recovery (issue #18)
#
# When an Anthropic API call fails with a transient error (HTTP 529 overload,
# 503/504 gateway, rate-limit, network blip), the SDK already retries up to
# ~30 seconds in-process — that handles ~99% of cases silently. The layer
# below catches the remaining cases where 30s wasn't enough.
#
# Design:
#   - Each LLM-using branch in handle_text wraps its body in _run_with_recovery.
#   - On transient API failure: send a friendly "I'm still working on it" reply
#     that includes the user's original text (so they don't have to retype).
#     Then schedule a background task that periodically retries the same work
#     for several more minutes before giving up.
#   - One pending retry per user — a new message from the same user cancels
#     the previous pending retry (latest message wins).
#   - Permanent (non-transient) errors fall through and are reported via the
#     existing log/reply machinery in each branch.
#
# Limitation: pending retries live in process memory only. A bot restart
# during an outage drops them. A persistent on-disk queue would survive
# restarts; out of scope for v1 per the issue description.
# ─────────────────────────────────────────────────────────────────────────────

# Background retries are spaced 30s apart for up to 6 attempts → ~3 min of
# silent background recovery on top of the SDK's ~30s of inline retry.
_BG_RETRY_INTERVAL_S = 30
_BG_RETRY_MAX_ATTEMPTS = 6

# One pending background retry task per user. New work cancels the old task.
_pending_retries: dict[int, asyncio.Task] = {}


def _is_transient_api_error(e: Exception) -> bool:
    """True if `e` is the kind of transient API/network error worth retrying.

    Covers: HTTP 502/503/504 gateway errors, HTTP 529 overload, rate-limit,
    connection blips, request timeouts. Anything else (auth, billing, bad
    request, our bugs) is permanent and gets reported normally.
    """
    if isinstance(e, anthropic.APIStatusError):
        return e.status_code in (502, 503, 504, 529)
    if isinstance(e, (
        anthropic.RateLimitError,
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
    )):
        return True
    return False


def _truncate_for_echo(text: str, limit: int = 200) -> str:
    """Shorten user text for inclusion in friendly retry messages."""
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


async def _send_overload_ack(update: Update, original_text: str) -> None:
    """First friendly message — sent when the inline SDK retry exhausts and
    we start background retries. Echoes the user's original input so they
    don't have to remember what they wrote.
    """
    snip = _truncate_for_echo(original_text)
    body = (
        "⏳ Anthropic is busy right now. I haven't lost what you sent — "
        "I'll keep trying in the background and reply when it's through.\n\n"
        f"_Original: {snip}_" if snip else
        "⏳ Anthropic is busy right now. I'll keep trying in the background "
        "and reply when it's through."
    )
    await _send(update, body, parse_mode=ParseMode.MARKDOWN)


async def _send_overload_giveup(update: Update, original_text: str) -> None:
    """Final friendly message — sent when background retries are also
    exhausted. Includes original text so the user can resend without
    retyping.
    """
    snip = _truncate_for_echo(original_text)
    if snip:
        body = (
            "😕 Anthropic stayed busy for too long — I couldn't get through "
            "after several minutes of retrying. Here's what you sent so you "
            "don't have to retype:\n\n"
            f"_{snip}_\n\n"
            "Try again in a few minutes."
        )
    else:
        body = (
            "😕 Anthropic stayed busy for too long. Please try again in a "
            "few minutes."
        )
    await _send(update, body, parse_mode=ParseMode.MARKDOWN)


async def _background_retry(
    user_id: int,
    update: Update,
    original_text: str,
    work_fn,
) -> None:
    """Periodically retry `work_fn` while Anthropic is overloaded.

    Sleeps _BG_RETRY_INTERVAL_S between attempts, up to _BG_RETRY_MAX_ATTEMPTS.
    If `work_fn` succeeds, it has already sent its own reply — we just exit.
    If it fails permanently (non-transient), we give up immediately with the
    final friendly message. If we run out of attempts, same final message.
    """
    try:
        for attempt in range(1, _BG_RETRY_MAX_ATTEMPTS + 1):
            await asyncio.sleep(_BG_RETRY_INTERVAL_S)
            try:
                await work_fn()
                log.info(
                    f"background retry user={user_id} attempt={attempt} succeeded"
                )
                return
            except Exception as e:
                if not _is_transient_api_error(e):
                    log.exception(
                        f"background retry user={user_id} attempt={attempt} "
                        f"permanent error: {e!r}"
                    )
                    await _send_overload_giveup(update, original_text)
                    return
                log.warning(
                    f"background retry user={user_id} attempt={attempt} "
                    f"still transient: {e!r}"
                )
        # Exhausted all attempts and they were all transient.
        log.warning(
            f"background retry user={user_id} exhausted "
            f"{_BG_RETRY_MAX_ATTEMPTS} attempts; giving up"
        )
        await _send_overload_giveup(update, original_text)
    except asyncio.CancelledError:
        # Cancelled because the user sent a new message; that new message
        # replaces this pending work. Don't send any more replies for the
        # old one — silently exit.
        log.info(f"background retry user={user_id} cancelled (newer message arrived)")
        raise
    except Exception as e:
        log.exception(f"background retry user={user_id} crashed: {e!r}")
    finally:
        _pending_retries.pop(user_id, None)


async def _run_with_recovery(
    update: Update,
    original_text: str,
    work_fn,
) -> None:
    """Run `work_fn` (a no-arg async function that does the whole branch
    body — LLM call, DB writes, sending the reply). If it fails with a
    transient API error, acknowledge the user with their original text and
    schedule background retries. Permanent errors propagate to the caller.

    work_fn is responsible for sending its OWN successful reply (we don't
    know what shape its output takes). The recovery layer only handles the
    error path.
    """
    user_id = update.effective_user.id

    # Cancel any pending retry from this user's previous message — the new
    # message replaces it.
    old_task = _pending_retries.pop(user_id, None)
    if old_task and not old_task.done():
        old_task.cancel()

    try:
        await work_fn()
        return
    except Exception as e:
        if not _is_transient_api_error(e):
            # Permanent error — let the caller's existing logging/handling
            # surface it. We don't wrap it in a friendly message because it
            # might be a real bug worth showing the original exception for.
            raise
        log.warning(
            f"inline retry exhausted for user={user_id}: {e!r}; "
            f"falling back to background retry"
        )

    # Inline (SDK) retries didn't get through. Acknowledge + schedule background.
    await _send_overload_ack(update, original_text)
    task = asyncio.create_task(
        _background_retry(user_id, update, original_text, work_fn)
    )
    _pending_retries[user_id] = task


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_reply(text: str) -> str:
    """Truncate very long replies so Telegram doesn't reject them."""
    return text[:4000] if len(text) > 4000 else text


async def _typing(update: Update):
    """Send 'typing...' indicator."""
    await update.effective_chat.send_chat_action("typing")


async def _send(update: Update, text: str, **kwargs):
    """Send a Telegram reply AND log it to the short-term conversation memory.

    Behaves exactly like `update.message.reply_text(text, **kwargs)` from the
    caller's point of view — same positional args, same keyword args, same
    return value — so migration is a mechanical rename.

    Logging is a best-effort step: if the DB write fails for any reason, the
    reply still goes out. We log AFTER the send so we don't record messages
    that Telegram rejected (e.g. bad markdown, too long). kwargs (parse_mode,
    etc.) are deliberately NOT stored — the conversation memory is about
    semantic content, not rendering.
    """
    result = await update.message.reply_text(text, **kwargs)
    try:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and text:
            db.log_message(user_id, "assistant", str(text))
    except Exception as e:
        log.warning(f"conversation log (assistant) failed: {e}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# /start  /help
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)
    await _send(update, 
        "👋 Hi! I'm your personal nutritionist bot.\n\n"
        "📸 *Send a photo of your food* and I'll estimate calories and macros.\n"
        "🏷 *Send a photo of a nutrition label* (I'll auto-detect it).\n"
        "💬 *Or just describe what you ate* in text.\n\n"
        "Commands:\n"
        "/today — see today's summary\n"
        "/week — weekly review\n"
        "/goal — set your daily calorie goal\n"
        "/profile — see everything I know about you\n"
        "/lint — tidy up my notes about you (dedup + drop stale)\n"
        "/reset\\_today — wipe today's meals, stats, conversation + today's wiki lines (for testing)\n"
        "/help — this message",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# ─────────────────────────────────────────────────────────────────────────────
# /today
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await _typing(update)
    text = advisor.today_summary(user_id)
    await _send(update, _safe_reply(text), parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────────────────────────
# /week
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await _typing(update)
    text = advisor.weekly_review(user_id)
    await _send(update, _safe_reply(text), parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────────────────────────
# /profile  — show everything the bot knows about the user
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)  # triggers one-time SQL→wiki migrations if needed

    header = "📋 What I know about you:"

    # Pretty-print each populated wiki page for the user.
    # goals.md is the single source of truth for the calorie goal (and every
    # other goal) — it already contains the canonical "- **Daily calorie
    # goal**: N kcal" bullet, so we don't inject anything here. Everything
    # goal-related lives in one place.
    section_titles = {
        "profile":  "👤 Profile",
        "goals":    "🎯 Goals",
        "patterns": "📊 Patterns",
        "wins":     "🏆 Wins",
    }
    pages = wiki.read_all_pages(user_id)

    parts = [header]
    any_content = False
    for key, title in section_titles.items():
        content = _clean_wiki_for_display(pages.get(key, ""))
        if not content:
            continue
        parts.append(f"\n{title}\n{content}")
        any_content = True

    if not any_content:
        parts.append(
            "\nI haven't learned much about you yet. Just tell me things like "
            "\"I don't eat meat\" or \"I'm trying to lose 5kg\" and I'll remember."
        )

    # No parse_mode — wiki content contains `#`/`##` markdown that Telegram doesn't understand.
    await _send(update, "\n".join(parts))


def _clean_wiki_for_display(content: str) -> str:
    """Strip template scaffolding (HTML comments, markdown headings, horizontal
    rules, empty placeholder, ingest date prefix, bold markers) from a wiki
    page so it reads cleanly in Telegram.

    Intentionally strips ALL `# Heading` lines (not just the first) — the bot
    adds its own section title (like "🎯 Goals") before the content, so the
    in-file `# Goals` heading would always duplicate.  Same for `---` style
    horizontal rules, which the Telegram view never needs.

    Telegram is sent without parse_mode (wiki may contain `#` and other chars
    Telegram's Markdown can't handle), so any `**bold**` or `*italic*` markers
    would leak through as literal asterisks.  We strip them here — the label
    itself is distinctive enough without the weight.
    """
    import re
    if not content:
        return ""
    # Drop HTML comment blocks (migration marker, template instructions)
    content = re.sub(r"<!--[\s\S]*?-->", "", content)
    # Treat empty-placeholder as no content
    if "_(Empty" in content:
        return ""
    # Drop ALL markdown headings (#, ##, ### ...).  The bot supplies the
    # section title; an in-file heading would just duplicate it.
    content = re.sub(r"^#{1,6}\s+.*$\n?", "", content, flags=re.MULTILINE)
    # Drop horizontal rules (---, ***, ___) on their own line.
    content = re.sub(r"^\s*[-*_]{3,}\s*$\n?", "", content, flags=re.MULTILINE)
    # Strip the leading [YYYY-MM-DD] ingest date prefix — it's internal
    # metadata used by lint for supersede/stale decisions, not for the user.
    content = re.sub(
        r"^(\s*[-*•]?\s*)\[\d{4}-\d{2}-\d{2}\]\s+",
        r"\1",
        content,
        flags=re.MULTILINE,
    )
    # Strip markdown bold/italic markers — Telegram without parse_mode would
    # show them as literal asterisks/underscores.  `**bold**`, `__bold__`,
    # `*italic*`, `_italic_` all become plain text.
    content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)
    content = re.sub(r"__(.+?)__", r"\1", content)
    # Single-asterisk italic: match only when both anchors sit next to non-space
    # so we don't eat bullet markers (`- ` at line start).
    content = re.sub(r"(?<!\w)\*(\S.*?\S|\S)\*(?!\w)", r"\1", content)
    # Single-underscore italic: same word-boundary guard so we don't eat
    # underscores inside identifiers like `user_id`.
    content = re.sub(r"(?<!\w)_(\S.*?\S|\S)_(?!\w)", r"\1", content)
    # Collapse blank lines BETWEEN consecutive bullets so the list reads tight.
    # `- foo\n\n- bar`  →  `- foo\n- bar`.  Runs repeatedly until stable.
    _bullet_blank_re = re.compile(r"(^\s*[-*•].*)\n\s*\n(\s*[-*•])", re.MULTILINE)
    while _bullet_blank_re.search(content):
        content = _bullet_blank_re.sub(r"\1\n\2", content)
    # Collapse any remaining 3+ blank-line runs down to one blank line.
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


# ─────────────────────────────────────────────────────────────────────────────
# /lint  — on-demand wiki tidy-up (manual trigger; 4b will add the Sunday cron)
# ─────────────────────────────────────────────────────────────────────────────

# Human-readable names for each lintable page in the Telegram reply.
_LINT_PAGE_TITLES = {
    "profile":  "👤 Profile",
    "goals":    "🎯 Goals",
    "patterns": "📊 Patterns",
    "wins":     "🏆 Wins",
}


async def cmd_lint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    On-demand version of the Saturday cron.  Runs in three phases:

      1. Tidy each page (dedup / supersede / drop stale).  Backups are
         written to wiki/user_<id>/.backups/ before any page is overwritten
         (last 3 kept per page), so this is safe to run ad-hoc.
      2. Detect new contradictions across the cleaned wiki.
      3. If any OPEN contradictions exist (new or previously unanswered),
         DM the user about the oldest one — same phrasing the weekly cron
         uses.  The user's next text reply is caught by the pre-route
         intercept in handle_text and resolved via the Haiku classifier.
    """
    user_id = update.effective_user.id
    db.ensure_user(user_id)  # make sure their wiki exists
    await _typing(update)
    await _send(update, 
        "🧹 Tidying up my notes about you… this takes a few seconds."
    )

    # ── 1. Tidy ──────────────────────────────────────────────────────────────
    try:
        result = await lint.lint_user_wiki(user_id)
    except Exception as e:
        log.exception(f"lint failed for user={user_id}: {e}")
        await _send(update, 
            "😕 Something went wrong while tidying up. Your notes weren't changed."
        )
        return

    # Build a per-page summary.  Collapse to two outcomes the user cares
    # about: tidied or nothing to tidy.  Line-count details live in log.md.
    lines = ["✅ Done."]
    any_change = False
    for page, title in _LINT_PAGE_TITLES.items():
        info = result.get(page, {})
        if info.get("rewritten"):
            any_change = True
            lines.append(f"{title} — tidied")
        else:
            lines.append(f"{title} — nothing to tidy")

    if not any_change:
        lines.append("\nYour notes are already clean.")
    else:
        lines.append(
            "\n_Backups of the previous versions are saved in case I got "
            "anything wrong — ask me to restore if something looks off._"
        )

    await _send(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # ── 2. Detect + record new contradictions ────────────────────────────────
    try:
        new_conflicts = await contradictions.detect(user_id)
        if new_conflicts:
            contradictions.record(user_id, new_conflicts)
    except Exception as e:
        # Don't block /lint on detection errors — user already got their tidy reply.
        log.warning(f"contradiction detect failed in /lint for user={user_id}: {e}")

    # ── 3. DM about the oldest OPEN contradiction, if any ────────────────────
    try:
        pending = contradictions.oldest_open(user_id)
        if pending is not None:
            await _send(update, 
                _format_contradiction_dm(pending),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        log.warning(f"contradiction DM failed in /lint for user={user_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# /goal  — set or view daily calorie target
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_reset_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Full test-cleanup: wipe everything written today for this user.

    Nukes (all scoped to TODAY):
      - meals (HARD delete, not soft — so row counts go back to zero)
      - daily_stats (steps / weight / workouts)
      - conversation_messages (short-term memory from today)
      - [YYYY-MM-DD]-stamped lines in every wiki page (profile, goals,
        patterns, wins, log)

    Leaves intact: historical data from prior days, long-term wiki facts
    that were stamped on earlier dates.

    Also wired as the handler for /clear_today (backward-compat alias —
    previously /clear_today was a lighter meals-only soft-delete; it's
    now unified with /reset_today to avoid the "which one do I use"
    problem).
    """
    user_id = update.effective_user.id

    # Wrap everything in a top-level try/except so any error surfaces to
    # the user instead of silently vanishing — a silent handler failure
    # looks identical to the command not being registered, which is
    # exactly the kind of ambiguity worth ruling out.
    try:
        db.ensure_user(user_id)

        # Hold the wiki lock for the page edits so a parallel ingest from
        # the last test message can't race us.
        async with wiki.get_lock(user_id):
            meals_removed = db.delete_today_meals(user_id)
            stats_removed = db.delete_today_stats(user_id)
            conv_removed = db.delete_today_conversation(user_id)
            try:
                wiki_stripped = wiki.strip_today_lines(user_id)
            except Exception as e:
                log.warning(f"strip_today_lines failed: {e}")
                wiki_stripped = {}

        lines = ["🧹 *Reset today* — cleared test data:"]
        lines.append(f"• Meals: {meals_removed}")
        lines.append(f"• Daily stats: {stats_removed}")
        lines.append(f"• Conversation turns: {conv_removed}")
        if wiki_stripped:
            total = sum(wiki_stripped.values())
            per_page = ", ".join(f"{k}: {v}" for k, v in wiki_stripped.items())
            lines.append(f"• Wiki lines stamped today: {total} ({per_page})")
        else:
            lines.append("• Wiki lines stamped today: 0")
        lines.append("")
        lines.append("History from prior days untouched.")

        await _send(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.exception(f"cmd_reset_today failed for user={user_id}: {e}")
        await _send(update, f"😕 Reset failed: {e}")


async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)
    args = ctx.args  # list of words after /goal

    if not args:
        # Show current goal — read from goals.md (single source of truth)
        goal = wiki.get_daily_kcal(user_id, 2000)
        await _send(update, 
            f"🎯 Your daily calorie goal is *{goal} kcal*.\n"
            f"To change it: `/goal 1800`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        new_goal = int(args[0])
        if new_goal < 500 or new_goal > 10000:
            raise ValueError
    except ValueError:
        await _send(update, "Please give a number between 500 and 10000. E.g. `/goal 1800`")
        return

    # Write to goals.md under the per-user lock so a concurrent Haiku ingest
    # on the same page can't race with us.
    async with wiki.get_lock(user_id):
        wiki.set_daily_kcal(user_id, new_goal)
    await _send(update, 
        f"✅ Daily goal set to *{new_goal} kcal*.", parse_mode=ParseMode.MARKDOWN
    )


# ─────────────────────────────────────────────────────────────────────────────
# Photo handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # ── Album caption sharing ─────────────────────────────────────────────────
    # When photos are sent as an album, only the first message carries the
    # caption. For subsequent photos we look up the cached caption so the
    # meal_type hint ("for lunch") is applied to every photo in the batch.
    raw_caption = update.message.caption or ""
    group_id = update.message.media_group_id  # None for single photos

    if group_id:
        if raw_caption:
            _album_caption_cache[group_id] = raw_caption   # store from first photo
        caption = _album_caption_cache.get(group_id, "")   # inherit for later photos
    else:
        caption = raw_caption

    await _typing(update)

    # Download the highest-res photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    image_bytes = buf.getvalue()

    # The "original text" for the recovery layer is the caption — or a
    # placeholder if there wasn't one, since we can't echo the photo bytes
    # back to the user in a Telegram text reply.
    echo_text = caption or "your photo"

    async def _work_photo():
        items, source = analyzer.analyze_photo(image_bytes, caption)

        if not items:
            await _send(update,
                "🤔 I couldn't identify any food in that photo. "
                "Try a clearer shot, or tell me what it is in text."
            )
            return

        # Log everything
        db.log_meal_items(user_id, items, source=source)

        # ── Conversation memory: record a text summary of what the photo logged ──
        # Photos themselves can't be replayed back to Claude in later turns, so we
        # store a compact text stand-in. That way a follow-up like "two eggs" or
        # "remove the salad" has a referent in the conversation history.
        try:
            parts = []
            for i in items:
                name = i.get("dish") or i.get("dish_name") or "item"
                kcal = i.get("kcal")
                if kcal:
                    parts.append(f"{name} ({kcal:.0f} kcal)")
                else:
                    parts.append(str(name))
            total_kcal = sum(i.get("kcal", 0) for i in items)
            summary = f"[Photo logged ({source}): {', '.join(parts)} — total {total_kcal:.0f} kcal]"
            if caption:
                summary = f"[Caption: {caption}] {summary}"
            db.log_message(user_id, "user", summary)
        except Exception as e:
            log.warning(f"conversation log (photo summary) failed: {e}")

        # Confirmation + alert
        reply = advisor.log_confirmation(items, user_id)
        await _send(update, _safe_reply(reply), parse_mode=ParseMode.MARKDOWN)

    await _run_with_recovery(update, echo_text, _work_photo)


# ─────────────────────────────────────────────────────────────────────────────
# Voice message handler — transcribes via OpenAI Whisper, then reuses handle_text
# ─────────────────────────────────────────────────────────────────────────────

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _typing(update)

    # Download the voice file from Telegram
    voice = update.message.voice
    tg_file = await voice.get_file()
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    buf.seek(0)
    buf.name = "voice.ogg"  # Whisper needs a filename with extension

    # Transcribe with OpenAI Whisper
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
        )
        text = transcript.text.strip()
    except Exception as e:
        log.error(f"Whisper transcription failed: {e}")
        await _send(update, "😕 Couldn't transcribe your voice message. Please try again or type it.")
        return

    if not text:
        await _send(update, "🤔 I couldn't hear anything. Please try again.")
        return

    log.info(f"Voice transcribed: {text[:80]}")

    # Echo the transcription so the user knows what was understood
    await _send(update, f"🎙 _{text}_", parse_mode=ParseMode.MARKDOWN)

    # Reuse the exact same text handler — pass transcribed text via context
    ctx.user_data["voice_text"] = text
    await handle_text(update, ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Text message handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Voice handler stores transcribed text here; fall back to typed message
    text = ctx.user_data.pop("voice_text", None) or update.message.text or ""

    # ── Short-term conversation memory ───────────────────────────────────────
    # Record the user's turn FIRST so it's visible to every AI call below.
    # Then fetch the rolling window (16h OR 20 messages, whichever is longer)
    # ONCE and reuse it for intent detection, correction, and Q&A.
    try:
        if text:
            db.log_message(user_id, "user", text)
    except Exception as e:
        log.warning(f"conversation log (user) failed: {e}")

    try:
        history = db.get_recent_conversation(user_id)
    except Exception as e:
        log.warning(f"get_recent_conversation failed: {e}")
        history = None

    await _typing(update)

    # ── Contradiction pre-route ──────────────────────────────────────────────
    # If the bot flagged a contradiction in this user's wiki and asked them
    # about it, every incoming text is first run through a Haiku classifier
    # to see if they're answering.  If yes → apply resolution and stop.
    # If unrelated → fall through to the normal intent router below.
    pending = contradictions.oldest_open(user_id)
    if pending is not None:
        try:
            decision = await contradictions.classify_user_reply(pending, text)
        except Exception as e:
            log.warning(f"contradiction classifier failed: {e}")
            decision = {"action": "unrelated"}

        if decision.get("action") and decision["action"] != "unrelated":
            try:
                result = await contradictions.resolve(
                    user_id,
                    pending["ts"],
                    decision["action"],
                    custom_text=decision.get("custom_text"),
                    target_page=decision.get("target_page"),
                )
            except Exception as e:
                log.exception(f"contradiction resolve failed: {e}")
                await _send(update, 
                    "😕 Couldn't apply that change to my notes — please try again."
                )
                return

            if result.get("ok"):
                await _send(update, f"✅ {result['summary']}")
            else:
                await _send(update, 
                    f"⚠️ {result.get('summary', 'Could not resolve that.')}"
                )
            return
        # else: user wrote about something unrelated — continue to the router.

    intent, topic = analyzer.detect_intent(text, history=history)
    log.info(f"user={user_id} intent={intent} topic={topic} text={text[:60]}")

    # ── iPhone Shortcut: health snapshot (steps + weight) ────────────────────
    if intent == "health_update":
        await _typing(update)
        from datetime import date as _date
        parsed = analyzer.parse_health_message(text)
        date_str = parsed.get("date") or _date.today().isoformat()
        steps = parsed.get("steps")
        weight = parsed.get("weight_kg")

        if not steps and not weight:
            await _send(update, "🤔 I couldn't read the health data. Make sure the Shortcut sends it in the expected format.")
            return

        async def _work_health():
            # Estimate walking calories using saved weight (or parsed weight)
            user_weight = weight or db.get_latest_weight(user_id) or 70.0
            existing = db.get_daily_stats(user_id, date_str) or {}
            workouts = existing.get("workouts") or []
            profile = wiki.read_wiki_for_prompt(user_id)

            burn_data = analyzer.estimate_activity_calories(steps, workouts, user_weight, profile)
            kcal_burned = burn_data.get("total_kcal", 0)

            db.upsert_daily_stats(
                user_id=user_id,
                date_str=date_str,
                steps=steps,
                weight_kg=weight,
                kcal_burned_est=kcal_burned,
            )

            lines = ["✅ *Health data logged!*"]
            if weight:
                lines.append(f"⚖️ Weight: {weight:.1f} kg")
            if steps:
                walking_kcal = burn_data.get("walking_kcal", 0)
                lines.append(f"👟 Steps: {steps:,} (~{walking_kcal:.0f} kcal extra from walking)")
            bmr = burn_data.get("bmr_kcal", 0)
            if bmr:
                lines.append(f"🫀 Resting + sedentary base: ~{bmr:.0f} kcal")
            if kcal_burned:
                lines.append(f"🔥 *Total estimated burn: ~{kcal_burned:.0f} kcal*")
            assumed = burn_data.get("assumed", "")
            if assumed:
                lines.append(f"_ℹ️ Assumed: {assumed}_")

            await _send(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

        await _run_with_recovery(update, text, _work_health)
        return

    # ── iPhone Shortcut: workout / calendar events ────────────────────────────
    if intent == "workout_log":
        await _typing(update)
        from datetime import date as _date
        workouts = analyzer.parse_workout_message(text)

        if not workouts:
            await _send(update, "🤔 I couldn't find any workout entries. Check the Shortcut format.")
            return

        async def _work_workouts():
            user_weight = db.get_latest_weight(user_id) or 70.0
            existing = db.get_daily_stats(user_id, _date.today().isoformat()) or {}
            steps = existing.get("steps")
            profile = wiki.read_wiki_for_prompt(user_id)

            burn_data = analyzer.estimate_activity_calories(steps, workouts, user_weight, profile)
            kcal_burned = burn_data.get("total_kcal", 0)

            # Attach kcal estimates back to workout dicts for storage
            for i, w in enumerate(workouts):
                estimated = burn_data.get("workouts", [{}])
                if i < len(estimated):
                    w["kcal_est"] = estimated[i].get("kcal_est", 0)
                    w["intensity_note"] = estimated[i].get("intensity_note", "")

            db.upsert_daily_stats(
                user_id=user_id,
                date_str=_date.today().isoformat(),
                workouts=workouts,
                kcal_burned_est=kcal_burned,
            )

            lines = ["✅ *Workouts logged!*"]
            for w in workouts:
                from advisor import _workout_emoji
                emoji = _workout_emoji(w["name"])
                dur_str = f" — {w['duration_min']} min" if w.get("duration_min") else ""
                kcal_str = f" — ~{w['kcal_est']:.0f} kcal" if w.get("kcal_est") else ""
                note = f" _{w['intensity_note']}_" if w.get("intensity_note") else ""
                lines.append(f"{emoji} {w['name']}{dur_str}{kcal_str}{note}")
            lines.append(f"\n🔥 Total estimated burn: *~{kcal_burned:.0f} kcal*")
            if burn_data.get("summary"):
                lines.append(f"_{burn_data['summary']}_")

            await _send(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

        await _run_with_recovery(update, text, _work_workouts)
        return

    # ── Remember: personal fact or intention to store ────────────────────────
    if intent == "remember":
        # The wiki is now the single source of truth for long-term memory.
        # Fire-and-forget ingest decides whether this turn is an add, a
        # removal, or a rewrite — we don't know which at reply time, so the
        # confirmation needs to be NEUTRAL.  "I'll remember that" would be
        # wrong (and a little contradictory) when the user has just asked to
        # forget or retract something.
        await _send(update, "✅ Got it — updating my notes.")
        advisor.schedule_ingest(user_id, "remember", text, "Saved.")
        return

    # ── Natural language: today summary ──────────────────────────────────────
    if intent == "cmd_today":
        text_reply = advisor.today_summary(user_id)
        await _send(update, _safe_reply(text_reply), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Natural language: specific past day summary ───────────────────────────
    if intent == "cmd_date_query":
        from datetime import date as _date
        async def _work_date():
            query_date, label = analyzer.extract_query_date(text, _date.today().isoformat())
            text_reply = advisor.day_summary(user_id, query_date, label)
            await _send(update, _safe_reply(text_reply), parse_mode=ParseMode.MARKDOWN)
        await _run_with_recovery(update, text, _work_date)
        return

    # ── Natural language: weekly review ──────────────────────────────────────
    if intent == "cmd_week":
        async def _work_week():
            text_reply = advisor.weekly_review(user_id)
            await _send(update, _safe_reply(text_reply), parse_mode=ParseMode.MARKDOWN)
        await _run_with_recovery(update, text, _work_week)
        return

    # ── Natural language: set/check goal ─────────────────────────────────────
    if intent == "cmd_goal":
        new_goal = analyzer.extract_goal_from_text(text)
        if new_goal:
            async with wiki.get_lock(user_id):
                wiki.set_daily_kcal(user_id, new_goal)
            await _send(update, 
                f"✅ Daily goal set to *{new_goal} kcal*.", parse_mode=ParseMode.MARKDOWN
            )
        else:
            current = wiki.get_daily_kcal(user_id, 2000)
            await _send(update, 
                f"🎯 Your current daily goal is *{current} kcal*.\n"
                f"To change it say: \"set my goal to 1800 calories\"",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── Natural language: tidy / lint the wiki ───────────────────────────────
    # "lint", "tidy up your notes", "clean up", "dedup" etc. should get the
    # same full flow as /lint — tidy the pages, detect contradictions, DM the
    # user about the oldest open one. Never expose the raw audit/tech output.
    if intent == "cmd_lint":
        await cmd_lint(update, ctx)
        return

    # ── Correction ──────────────────────────────────────────────────────────
    if intent == "correction":
        async def _work_correction():
            # Use today's FULL log as the candidate pool, not just the last 10
            # rows. Rationale: on a busy day (e.g. breakfast + lunch + dinner +
            # multiple snacks, 15-20 rows) a "last 10 meals" cap pushes the
            # morning meals out of the resolver's view. When the user then
            # corrects the morning cottage cheese, Haiku can't see it in history
            # and hallucinates a meal_id that *is* in the window — which
            # silently rewrites the wrong row (see 2026-04-20 incident where
            # "Cottage cheese for breakfast was 100g" overwrote the Borscht
            # lunch row because id 757 wasn't actually in the candidate set but
            # Haiku returned it anyway).
            #
            # Fall back to recent-10 only if today has no meals at all (rare
            # edge case: user corrects yesterday's log at 00:01 Berlin).
            recent = db.get_today_meals(user_id)
            if not recent:
                recent = db.get_recent_meals(user_id, limit=10)
            if not recent:
                await _send(update,
                    "I don't have any recent meals to correct. Log something first!"
                )
                return

            # Pass the most recent logging batch (e.g. 6 items from one photo)
            # so Claude knows exactly which IDs form "this dish I just added"
            last_batch = db.get_last_meal_batch(user_id, window_seconds=120)

            # resolve_correction now returns a LIST — one action per requested change
            results = analyzer.resolve_correction(text, recent, last_batch=last_batch, conversation=history)

            # Diagnostic logging — when the resolver keeps returning 'none' we want
            # to see exactly what Haiku was handed and what it said. Kept terse so
            # journalctl stays readable.
            log.info(
                f"correction user={user_id} text={text!r} "
                f"history_len={len(history) if history else 0} "
                f"recent_count={len(recent)} last_batch_count={len(last_batch) if last_batch else 0} "
                f"results={results}"
            )

            # Integrity guard — build the set of meal_ids that were actually
            # visible to Haiku. Any `update`/`delete`/`update_many`/`delete_many`
            # pointing at an id outside this set is a hallucination and must
            # be rejected BEFORE touching the DB. (batch items are always a
            # subset of today's log, so `recent` alone is the right anchor.)
            candidate_ids: set[int] = {m["id"] for m in recent}

            def _id_is_valid(mid) -> bool:
                """Accept only int-coercible ids that match a row we passed to Haiku."""
                try:
                    return int(mid) in candidate_ids
                except (TypeError, ValueError):
                    return False

            # Filter out "none" actions before processing
            valid_results = [r for r in results if r.get("action", "none") != "none"]

            if not valid_results:
                await _send(update,
                    "🤔 I'm not sure which meal you want to change. "
                    "Try being more specific, e.g. \"The feta — it was 70g not 80g. Also remove the hummus.\""
                )
                return

            # Execute each correction in order, collect summary lines
            reply_lines = []
            goal = wiki.get_daily_kcal(user_id, 2000)

            for result in valid_results:
                action = result.get("action", "none")

                # ── Add MORE of something already logged ─────────────────────────
                # e.g. bot logged 2 eggs from a photo; user says "there are three
                # eggs" → resolver emits add_items with one new egg entry.
                if action == "add_items":
                    new_items = result.get("items", [])
                    dish_name = result.get("dish_name", "")
                    if dish_name and new_items:
                        # Force the new rows into the existing dish grouping so
                        # later corrections/edits treat them as one dish.
                        for it in new_items:
                            it["dish_name"] = dish_name
                        try:
                            db.log_meal_items(user_id, new_items, source="correction")
                            # Show *what* was added, not just "1 more" — Maria
                            # asked for the item name to be visible in the reply.
                            added_desc = ", ".join(
                                it.get("dish", "item") for it in new_items
                            )
                            reply_lines.append(
                                f"➕ Added *{added_desc}* to *{dish_name}*"
                            )
                        except Exception as e:
                            log.warning(f"add_items failed: {e}")
                            reply_lines.append(f"⚠️ Couldn't add extra items to *{dish_name}*")
                    else:
                        reply_lines.append("⚠️ I didn't have enough info to add the extra item")

                # ── Bulk meal type reclassification ──────────────────────────────
                elif action == "update_many":
                    meal_ids = result.get("meal_ids", [])
                    updates = result.get("updates", {})
                    # Drop any hallucinated ids before writing — see integrity-guard
                    # note above.  If ALL ids are bad we refuse the whole action.
                    safe_ids = [mid for mid in meal_ids if _id_is_valid(mid)]
                    if meal_ids and not safe_ids:
                        log.warning(
                            f"update_many rejected: all meal_ids {meal_ids} outside candidate set "
                            f"{sorted(candidate_ids)} — likely Haiku hallucination"
                        )
                        reply_lines.append(
                            "⚠️ I couldn't confidently identify which entries you meant. "
                            "Try naming the dish more specifically."
                        )
                    elif safe_ids and updates:
                        updated = sum(1 for mid in safe_ids if db.update_meal(mid, updates, reason=result.get("reason", "")))
                        meal_type = updates.get("meal_type", "")
                        meal_emoji = {"breakfast": "🌅", "lunch": "☀️", "dinner": "🌙", "snack": "🍎"}.get(meal_type, "🍽")
                        reply_lines.append(f"✅ Updated {updated} items → {meal_emoji} *{meal_type}*")

                # ── Remove duplicates ─────────────────────────────────────────────
                elif action == "delete_duplicates":
                    dish_name = result.get("dish_name", "")
                    if dish_name:
                        deleted = db.delete_duplicate_dishes(user_id, dish_name)
                        if deleted:
                            reply_lines.append(f"🗑 Removed {deleted} duplicate(s) of *{dish_name}* — kept first log")
                        else:
                            reply_lines.append(f"No duplicates found for *{dish_name}*")

                # ── Scale dish to specific gram weight ────────────────────────────
                elif action == "scale_dish_grams":
                    dish_name = result.get("dish_name", "")
                    target_grams = float(result.get("target_grams", 0))
                    if dish_name and target_grams > 0:
                        items = db.get_dish_items_today(user_id, dish_name)
                        current_grams = sum(advisor._parse_grams(i.get("dish", "")) for i in items)
                        if current_grams > 0:
                            factor = target_grams / current_grams
                            db.scale_dish_items(user_id, dish_name, factor)
                            reply_lines.append(f"✏️ *{dish_name}* adjusted to {target_grams:.0f}g ({int(factor*100)}% of logged)")
                        else:
                            reply_lines.append(f"⚠️ Couldn't calculate current grams for *{dish_name}* — try 'I ate X% of that' instead")

                # ── Scale whole dish by fraction ──────────────────────────────────
                elif action == "scale_dish":
                    dish_name = result.get("dish_name", "")
                    factor = float(result.get("factor", 1.0))
                    if dish_name and 0.05 <= factor <= 0.99:
                        updated = db.scale_dish_items(user_id, dish_name, factor)
                        reply_lines.append(f"✏️ *{dish_name}* adjusted to {int(factor*100)}%")

                # ── Delete multiple items ─────────────────────────────────────────
                elif action == "delete_many":
                    meal_ids = result.get("meal_ids", [])
                    dish_name = result.get("dish_name")
                    # Trust dish_name delete only if it returns something, because
                    # delete_by_dish_name is scoped to the user's own rows so it
                    # cannot accidentally hit another meal via a hallucinated id.
                    # For the fallback-by-id path we DO filter to the candidate
                    # set first.
                    safe_ids = [mid for mid in meal_ids if _id_is_valid(mid)]
                    if dish_name:
                        deleted = db.delete_by_dish_name(user_id, dish_name)
                        if deleted == 0 and safe_ids:
                            # dish_name didn't match (wording/casing difference) —
                            # fall back to the explicit IDs Claude already resolved
                            deleted = sum(1 for mid in safe_ids if db.delete_meal(mid))
                        reply_lines.append(f"🗑 Removed *{dish_name}* ({deleted} items)")
                    elif safe_ids:
                        deleted = sum(1 for mid in safe_ids if db.delete_meal(mid))
                        reply_lines.append(f"🗑 Removed {deleted} items")
                    elif meal_ids:
                        # All ids were outside the candidate set — refuse silently
                        # rather than delete something unrelated.
                        log.warning(
                            f"delete_many rejected: ids {meal_ids} outside candidate set "
                            f"{sorted(candidate_ids)}"
                        )
                        reply_lines.append(
                            "⚠️ I couldn't confidently identify which entries to remove. "
                            "Try naming the dish."
                        )

                # ── Delete single item ────────────────────────────────────────────
                elif action == "delete":
                    meal_id = result.get("meal_id")
                    if not _id_is_valid(meal_id):
                        log.warning(
                            f"delete rejected: meal_id={meal_id!r} outside candidate set "
                            f"{sorted(candidate_ids)}"
                        )
                        reply_lines.append(
                            "⚠️ I couldn't confidently find that entry. Try being more specific."
                        )
                    else:
                        meal = db.get_meal_by_id(meal_id)
                        if db.delete_meal(meal_id):
                            name = meal.get("dish", "item") if meal else "item"
                            reply_lines.append(f"🗑 Removed *{name}*")
                        else:
                            reply_lines.append("⚠️ Couldn't find that entry to remove")

                # ── Update single item ────────────────────────────────────────────
                elif action == "update":
                    meal_id = result.get("meal_id")
                    updates = result.get("updates", {})
                    # CRITICAL guard — Haiku has been observed to return a
                    # meal_id that wasn't in the context we gave it, causing
                    # an unrelated row (e.g. lunch Borscht) to be silently
                    # overwritten with update values meant for a different
                    # meal (e.g. breakfast cottage cheese). Reject those before
                    # they reach the DB.
                    if not _id_is_valid(meal_id):
                        log.warning(
                            f"update rejected: meal_id={meal_id!r} outside candidate set "
                            f"{sorted(candidate_ids)}; updates={updates!r} reason={result.get('reason')!r}"
                        )
                        reply_lines.append(
                            "⚠️ I couldn't confidently match that to one of your logged meals. "
                            "Please re-state which dish, e.g. \"cottage cheese 100g\"."
                        )
                    elif db.update_meal(meal_id, updates, reason=result.get("reason", "")):
                        meal = db.get_meal_by_id(meal_id)
                        name = meal.get("dish", "item") if meal else "item"
                        new_kcal = meal.get("kcal", "?") if meal else "?"
                        reply_lines.append(f"✏️ *{name}* → {new_kcal:.0f} kcal")
                    else:
                        reply_lines.append("⚠️ Couldn't find that entry to update")

            # Send one combined reply with all changes + updated totals
            totals = db.get_today_totals(user_id)
            combined = "\n".join(reply_lines) + f"\n\n{advisor._fmt_totals(totals, goal)}"
            await _send(update, _safe_reply(combined), parse_mode=ParseMode.MARKDOWN)

        await _run_with_recovery(update, text, _work_correction)
        return

    # ── Question ─────────────────────────────────────────────────────────────
    if intent == "question":
        async def _work_question():
            # topic comes from detect_intent — passes through as-is to
            # answer_question, which decides whether to load the heavy
            # weight history into Sonnet's prompt.
            answer = advisor.answer_question(user_id, text, conversation=history, topic=topic)
            await _send(update, _safe_reply(answer), parse_mode=ParseMode.MARKDOWN)
            # Fire-and-forget: let Haiku decide whether this question reveals
            # something about the user worth filing (usually a self-concern like
            # "am I low on protein?").  When in doubt it leans self-question.
            advisor.schedule_ingest(user_id, "question", text, answer)
        await _run_with_recovery(update, text, _work_question)
        return

    # ── Log text description ──────────────────────────────────────────────────
    if intent == "log_text":
        async def _work_log_text():
            items = analyzer.analyze_text(text)
            if not items:
                await _send(update,
                    "🤔 I couldn't figure out what food that describes. "
                    "Try being more specific, e.g. \"oatmeal 80g with banana\"."
                )
                return
            db.log_meal_items(user_id, items, source="text")
            reply = advisor.log_confirmation(items, user_id)
            await _send(update, _safe_reply(reply), parse_mode=ParseMode.MARKDOWN)
        await _run_with_recovery(update, text, _work_log_text)
        return

    # ── Fallback ─────────────────────────────────────────────────────────────
    await _send(update, 
        "Send me a photo of your food 📸, describe what you ate 💬, or use /help."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cron entry points (called from command line)
# ─────────────────────────────────────────────────────────────────────────────

async def _push_to_all_users(message_fn, bot):
    """
    Call message_fn(user_id) → text, send to every registered user.
    message_fn receives user_id and returns a string.
    """
    from database import get_conn
    conn = get_conn()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    for row in users:
        uid = row["user_id"]
        try:
            text = message_fn(uid)
            await bot.send_message(
                chat_id=uid,
                text=_safe_reply(text),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.warning(f"Could not push to user {uid}: {e}")


async def run_daily_summary():
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await _push_to_all_users(advisor.daily_morning_summary, bot)
    log.info("Daily morning summary sent.")


async def run_evening_summary():
    """21:00 push — today's food log + AI analysis + 5-day recommendation."""
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await _push_to_all_users(advisor.evening_summary, bot)
    log.info("Evening summary sent.")


async def run_weekly_review():
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await _push_to_all_users(advisor.weekly_review, bot)
    log.info("Weekly review sent.")


# ─────────────────────────────────────────────────────────────────────────────
# Saturday 10:05 Berlin — weekly tidy + contradiction check
#
# Runs one day before the weekly review so the user's memory is clean and any
# flagged conflicts are surfaced before the review lands.
#
# Assumes the server's OS timezone is Europe/Berlin (Ubuntu's cron daemon
# ignores CRON_TZ directives — set the OS timezone instead). Install with:
#     5 10 * * 6 /opt/nutrition-bot/venv/bin/python /opt/nutrition-bot/telegram_bot.py --lint-cron
# ─────────────────────────────────────────────────────────────────────────────

_REPLY_HINT = (
    "_Just reply in your own words — e.g. “the first one”, “actually yes”, "
    "“keep both”, or “neither”._"
)

# Strips a leading `[YYYY-MM-DD] ` prefix so fallback DMs don't expose raw dates.
_DATE_PREFIX_RE = __import__("re").compile(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s*")


def _format_contradiction_dm(c: dict) -> str:
    """
    Friendly DM text for an OPEN contradiction.  Primary path: use the
    `ask` Haiku produced at detection time (short, conversational, no
    file names, natural dates).  Fallback path for legacy sections that
    don't carry an `ask` yet: strip file names and date prefixes from
    A/B and show them plainly.
    """
    ask = (c.get("ask") or "").strip()
    if ask:
        return f"🤔 {ask}\n\n{_REPLY_HINT}"

    # Fallback — older contradictions.md entries from before the `ask` field.
    a_clean = _DATE_PREFIX_RE.sub("", c.get("line_a", "")).strip()
    b_clean = _DATE_PREFIX_RE.sub("", c.get("line_b", "")).strip()
    return (
        "🤔 I spotted something that doesn't quite line up in my notes about you:\n\n"
        f"• *{a_clean}*\n"
        f"• *{b_clean}*\n\n"
        f"{_REPLY_HINT}"
    )


async def run_lint_cron():
    """
    Weekly job: lint + detect contradictions for every user, DM one prompt per
    user if there's at least one OPEN contradiction afterwards.
    """
    from telegram import Bot
    from database import get_conn

    bot = Bot(token=BOT_TOKEN)
    conn = get_conn()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    for row in users:
        uid = row["user_id"]
        try:
            # 1) Tidy first so we're detecting on a clean wiki.
            await lint.lint_user_wiki(uid)
        except Exception as e:
            log.warning(f"lint failed for user {uid}: {e}")

        try:
            # 2) Detect new contradictions across the cleaned wiki.
            new_conflicts = await contradictions.detect(uid)
            if new_conflicts:
                contradictions.record(uid, new_conflicts)
        except Exception as e:
            log.warning(f"contradiction detect failed for user {uid}: {e}")

        try:
            # 3) DM one prompt per user for the oldest open conflict, if any.
            pending = contradictions.oldest_open(uid)
            if pending is not None:
                await bot.send_message(
                    chat_id=uid,
                    text=_format_contradiction_dm(pending),
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as e:
            log.warning(f"could not DM contradiction prompt to user {uid}: {e}")

    # 4) Housekeeping: trim the short-term conversation memory. The rolling
    # window reader only looks at the last ~16 hours anyway; anything older
    # than two weeks is pure dead weight. One global call, not per-user.
    try:
        deleted = db.purge_conversation_older_than(14)
        log.info(f"purged {deleted} conversation_messages rows older than 14 days")
    except Exception as e:
        log.warning(f"purge_conversation_older_than failed: {e}")

    log.info("Weekly lint + contradiction pass done.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    db.init_db()

    # CLI flags for cron jobs
    if "--daily-summary" in sys.argv:
        asyncio.run(run_daily_summary())
        return
    if "--evening-summary" in sys.argv:
        asyncio.run(run_evening_summary())
        return
    if "--weekly-review" in sys.argv:
        asyncio.run(run_weekly_review())
        return
    if "--lint-cron" in sys.argv:
        asyncio.run(run_lint_cron())
        return

    # Normal bot run
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("lint", cmd_lint))
    # /clear_today kept as a backward-compat alias for /reset_today so any
    # muscle memory or old pinned messages still work.
    app.add_handler(CommandHandler("clear_today", cmd_reset_today))
    app.add_handler(CommandHandler("reset_today", cmd_reset_today))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("🥗 Nutrition bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
