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
    user = db.get_user(user_id) or {}
    profile = db.get_profile(user_id)

    goal = user.get("daily_kcal", 2000)
    header = f"📋 *What I know about you:*\n\n🎯 Daily calorie goal: {goal} kcal\n"

    if profile and profile.strip():
        body = f"\n*Profile & preferences:*\n{profile}"
    else:
        body = "\nNo preferences saved yet. Just tell me things like \"I don't eat meat\" or \"I weigh 67kg\" and I'll remember."

    await update.message.reply_text(header + body, parse_mode=ParseMode.MARKDOWN)


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
        # Show current goal
        user = db.get_user(user_id) or {}
        goal = user.get("daily_kcal", 2000)
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

    db.set_daily_goal(user_id, new_goal)
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
# Text message handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    await _typing(update)

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
        profile = db.get_profile(user_id)

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
        profile = db.get_profile(user_id)

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

    # ── Preference / personal info to remember ───────────────────────────────
    if intent == "preference":
        current_profile = db.get_profile(user_id)
        result = analyzer.update_profile(current_profile, text)
        if result.get("understood"):
            db.save_profile(user_id, result["profile"])
            await update.message.reply_text("✅ Got it, I'll remember that.")
        else:
            ask = result.get("ask", "I couldn't quite understand that. Could you rephrase?")
            await update.message.reply_text(f"🤔 {ask}")
        return

    # ── Natural language: today summary ──────────────────────────────────────
    if intent == "cmd_today":
        text_reply = advisor.today_summary(user_id)
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
            db.set_daily_goal(user_id, new_goal)
            await update.message.reply_text(
                f"✅ Daily goal set to *{new_goal} kcal*.", parse_mode=ParseMode.MARKDOWN
            )
        else:
            user_data = db.get_user(user_id) or {}
            current = user_data.get("daily_kcal", 2000)
            await update.message.reply_text(
                f"🎯 Your current daily goal is *{current} kcal*.\n"
                f"To change it say: \"set my goal to 1800 calories\"",
                parse_mode=ParseMode.MARKDOWN,
            )
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
        user_data = db.get_user(user_id) or {}
        goal = user_data.get("daily_kcal", 2000)

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

            # ── Scale whole dish ──────────────────────────────────────────────
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
    """21:00 push — today's food log + how you stand vs goal."""
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await _push_to_all_users(advisor.today_summary, bot)
    log.info("Evening summary sent.")


async def run_weekly_review():
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await _push_to_all_users(advisor.weekly_review, bot)
    log.info("Weekly review sent.")


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

    # Normal bot run
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("clear_today", cmd_clear_today))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("🥗 Nutrition bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
