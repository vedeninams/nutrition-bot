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
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_reply(text: str) -> str:
    """Truncate very long replies so Telegram doesn't reject them."""
    return text[:4000] if len(text) > 4000 else text


async def _typing(update: Update):
    """Send 'typing...' indicator."""
    await update.effective_chat.send_chat_action("typing")


# ─────────────────────────────────────────────────────────────────────────────
# /start  /help
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)
    await update.message.reply_text(
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
    await update.message.reply_text(_safe_reply(text), parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────────────────────────
# /week
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await _typing(update)
    text = advisor.weekly_review(user_id)
    await update.message.reply_text(_safe_reply(text), parse_mode=ParseMode.MARKDOWN)


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
    await update.message.reply_text("\n".join(parts))


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
    await update.message.reply_text(
        "🧹 Tidying up my notes about you… this takes a few seconds."
    )

    # ── 1. Tidy ──────────────────────────────────────────────────────────────
    try:
        result = await lint.lint_user_wiki(user_id)
    except Exception as e:
        log.exception(f"lint failed for user={user_id}: {e}")
        await update.message.reply_text(
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

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

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
            await update.message.reply_text(
                _format_contradiction_dm(pending),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        log.warning(f"contradiction DM failed in /lint for user={user_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# /goal  — set or view daily calorie target
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_clear_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    removed = db.clear_today(user_id)
    if removed:
        await update.message.reply_text(
            f"🗑 Cleared {removed} item{'s' if removed != 1 else ''} logged today. Fresh start!",
        )
    else:
        await update.message.reply_text("Nothing logged today to clear.")


async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)
    args = ctx.args  # list of words after /goal

    if not args:
        # Show current goal — read from goals.md (single source of truth)
        goal = wiki.get_daily_kcal(user_id, 2000)
        await update.message.reply_text(
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
        await update.message.reply_text("Please give a number between 500 and 10000. E.g. `/goal 1800`")
        return

    # Write to goals.md under the per-user lock so a concurrent Haiku ingest
    # on the same page can't race with us.
    async with wiki.get_lock(user_id):
        wiki.set_daily_kcal(user_id, new_goal)
    await update.message.reply_text(
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

    try:
        items, source = analyzer.analyze_photo(image_bytes, caption)
    except Exception as e:
        log.error(f"analyze_photo failed: {e}")
        await update.message.reply_text(
            "😕 Something went wrong analyzing the photo. Please try again."
        )
        return

    if not items:
        await update.message.reply_text(
            "🤔 I couldn't identify any food in that photo. "
            "Try a clearer shot, or tell me what it is in text."
        )
        return

    # Log everything
    db.log_meal_items(user_id, items, source=source)

    # Confirmation + alert
    reply = advisor.log_confirmation(items, user_id)
    await update.message.reply_text(_safe_reply(reply), parse_mode=ParseMode.MARKDOWN)


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
        await update.message.reply_text("😕 Couldn't transcribe your voice message. Please try again or type it.")
        return

    if not text:
        await update.message.reply_text("🤔 I couldn't hear anything. Please try again.")
        return

    log.info(f"Voice transcribed: {text[:80]}")

    # Echo the transcription so the user knows what was understood
    await update.message.reply_text(f"🎙 _{text}_", parse_mode=ParseMode.MARKDOWN)

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
                await update.message.reply_text(
                    "😕 Couldn't apply that change to my notes — please try again."
                )
                return

            if result.get("ok"):
                await update.message.reply_text(f"✅ {result['summary']}")
            else:
                await update.message.reply_text(
                    f"⚠️ {result.get('summary', 'Could not resolve that.')}"
                )
            return
        # else: user wrote about something unrelated — continue to the router.

    intent = analyzer.detect_intent(text)
    log.info(f"user={user_id} intent={intent} text={text[:60]}")

    # ── iPhone Shortcut: health snapshot (steps + weight) ────────────────────
    if intent == "health_update":
        await _typing(update)
        from datetime import date as _date
        parsed = analyzer.parse_health_message(text)
        date_str = parsed.get("date") or _date.today().isoformat()
        steps = parsed.get("steps")
        weight = parsed.get("weight_kg")

        if not steps and not weight:
            await update.message.reply_text("🤔 I couldn't read the health data. Make sure the Shortcut sends it in the expected format.")
            return

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

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    # ── iPhone Shortcut: workout / calendar events ────────────────────────────
    if intent == "workout_log":
        await _typing(update)
        from datetime import date as _date
        workouts = analyzer.parse_workout_message(text)

        if not workouts:
            await update.message.reply_text("🤔 I couldn't find any workout entries. Check the Shortcut format.")
            return

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

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Remember: personal fact or intention to store ────────────────────────
    if intent == "remember":
        # The wiki is now the single source of truth for long-term memory.
        # Fire-and-forget ingest decides whether this is a durable identity
        # fact (profile.md) or an active intention (goals.md) and writes there.
        await update.message.reply_text("✅ Got it, I'll remember that.")
        advisor.schedule_ingest(user_id, "remember", text, "Saved.")
        return

    # ── Natural language: today summary ──────────────────────────────────────
    if intent == "cmd_today":
        text_reply = advisor.today_summary(user_id)
        await update.message.reply_text(_safe_reply(text_reply), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Natural language: specific past day summary ───────────────────────────
    if intent == "cmd_date_query":
        from datetime import date as _date
        query_date, label = analyzer.extract_query_date(text, _date.today().isoformat())
        text_reply = advisor.day_summary(user_id, query_date, label)
        await update.message.reply_text(_safe_reply(text_reply), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Natural language: weekly review ──────────────────────────────────────
    if intent == "cmd_week":
        text_reply = advisor.weekly_review(user_id)
        await update.message.reply_text(_safe_reply(text_reply), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Natural language: set/check goal ─────────────────────────────────────
    if intent == "cmd_goal":
        new_goal = analyzer.extract_goal_from_text(text)
        if new_goal:
            async with wiki.get_lock(user_id):
                wiki.set_daily_kcal(user_id, new_goal)
            await update.message.reply_text(
                f"✅ Daily goal set to *{new_goal} kcal*.", parse_mode=ParseMode.MARKDOWN
            )
        else:
            current = wiki.get_daily_kcal(user_id, 2000)
            await update.message.reply_text(
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
        recent = db.get_recent_meals(user_id, limit=10)
        if not recent:
            await update.message.reply_text(
                "I don't have any recent meals to correct. Log something first!"
            )
            return

        # Pass the most recent logging batch (e.g. 6 items from one photo)
        # so Claude knows exactly which IDs form "this dish I just added"
        last_batch = db.get_last_meal_batch(user_id, window_seconds=120)

        # resolve_correction now returns a LIST — one action per requested change
        results = analyzer.resolve_correction(text, recent, last_batch=last_batch)

        # Filter out "none" actions before processing
        valid_results = [r for r in results if r.get("action", "none") != "none"]

        if not valid_results:
            await update.message.reply_text(
                "🤔 I'm not sure which meal you want to change. "
                "Try being more specific, e.g. \"The feta — it was 70g not 80g. Also remove the hummus.\""
            )
            return

        # Execute each correction in order, collect summary lines
        reply_lines = []
        goal = wiki.get_daily_kcal(user_id, 2000)

        for result in valid_results:
            action = result.get("action", "none")

            # ── Bulk meal type reclassification ──────────────────────────────
            if action == "update_many":
                meal_ids = result.get("meal_ids", [])
                updates = result.get("updates", {})
                if meal_ids and updates:
                    updated = sum(1 for mid in meal_ids if db.update_meal(mid, updates, reason=result.get("reason", "")))
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
                if dish_name:
                    deleted = db.delete_by_dish_name(user_id, dish_name)
                    if deleted == 0 and meal_ids:
                        # dish_name didn't match (wording/casing difference) —
                        # fall back to the explicit IDs Claude already resolved
                        deleted = sum(1 for mid in meal_ids if db.delete_meal(mid))
                    reply_lines.append(f"🗑 Removed *{dish_name}* ({deleted} items)")
                elif meal_ids:
                    deleted = sum(1 for mid in meal_ids if db.delete_meal(mid))
                    reply_lines.append(f"🗑 Removed {deleted} items")

            # ── Delete single item ────────────────────────────────────────────
            elif action == "delete":
                meal_id = result.get("meal_id")
                meal = db.get_meal_by_id(meal_id) if meal_id else None
                if db.delete_meal(meal_id):
                    name = meal.get("dish", "item") if meal else "item"
                    reply_lines.append(f"🗑 Removed *{name}*")
                else:
                    reply_lines.append("⚠️ Couldn't find that entry to remove")

            # ── Update single item ────────────────────────────────────────────
            elif action == "update":
                meal_id = result.get("meal_id")
                updates = result.get("updates", {})
                if db.update_meal(meal_id, updates, reason=result.get("reason", "")):
                    meal = db.get_meal_by_id(meal_id)
                    name = meal.get("dish", "item") if meal else "item"
                    new_kcal = meal.get("kcal", "?") if meal else "?"
                    reply_lines.append(f"✏️ *{name}* → {new_kcal:.0f} kcal")
                else:
                    reply_lines.append("⚠️ Couldn't find that entry to update")

        # Send one combined reply with all changes + updated totals
        totals = db.get_today_totals(user_id)
        combined = "\n".join(reply_lines) + f"\n\n{advisor._fmt_totals(totals, goal)}"
        await update.message.reply_text(_safe_reply(combined), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Question ─────────────────────────────────────────────────────────────
    if intent == "question":
        answer = advisor.answer_question(user_id, text)
        await update.message.reply_text(_safe_reply(answer), parse_mode=ParseMode.MARKDOWN)
        # Fire-and-forget: let Haiku decide whether this question reveals
        # something about the user worth filing (usually a self-concern like
        # "am I low on protein?").  When in doubt it leans self-question.
        advisor.schedule_ingest(user_id, "question", text, answer)
        return

    # ── Log text description ──────────────────────────────────────────────────
    if intent == "log_text":
        try:
            items = analyzer.analyze_text(text)
        except Exception as e:
            log.exception(f"analyze_text failed: {e}")   # prints full traceback
            await update.message.reply_text(f"😕 Error: {e}")  # show real error in Telegram too
            return

        if not items:
            await update.message.reply_text(
                "🤔 I couldn't figure out what food that describes. "
                "Try being more specific, e.g. \"oatmeal 80g with banana\"."
            )
            return

        db.log_meal_items(user_id, items, source="text")
        reply = advisor.log_confirmation(items, user_id)
        await update.message.reply_text(_safe_reply(reply), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Fallback ─────────────────────────────────────────────────────────────
    await update.message.reply_text(
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
# Saturday 10:05 Europe/Berlin — weekly tidy + contradiction check
#
# Runs one day before the weekly review so the user's memory is clean and any
# flagged conflicts are surfaced before the review lands.
#
# Install on the server (crontab -e) with:
#     CRON_TZ=Europe/Berlin
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
    app.add_handler(CommandHandler("clear_today", cmd_clear_today))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("🥗 Nutrition bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
