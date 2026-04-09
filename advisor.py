"""
advisor.py -Proactive nutrition intelligence.

Handles:
  1. Real-time limit alerts (after every log ->80% daily kcal)
  2. Daily morning summary (called by cron at 08:00)
  3. Weekly Sunday review (called by cron on Sundays)
  4. Answer questions about nutrition history
  5. Generate the text response after logging a meal (confirmation + context)
"""

import anthropic
import json
from datetime import date, timedelta
from typing import Optional
from dotenv import load_dotenv

import database as db

load_dotenv()   # must happen before Anthropic() reads the env
client = anthropic.Anthropic()


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

MEAL_EMOJI = {
    "breakfast": "🌅",
    "lunch":     "☀️",
    "dinner":    "🌙",
    "snack":     "🍎",
}

# Keyword → emoji mapping for common foods.
# Checked in order -first match wins.
FOOD_EMOJI_MAP = [
    # Eggs & dairy
    (["egg", "яйц", "ei"],                              "🥚"),
    (["yogurt", "yoghurt", "йогурт", "joghurt"],        "🥛"),
    (["milk", "молок", "milch"],                        "🥛"),
    (["cheese", "käse", "kase", "сыр"],                 "🧀"),
    (["butter", "масло", "margarine"],                  "🧈"),
    (["cream"],                                         "🍦"),
    # Meat & fish
    (["chicken", "курица", "hähnchen", "poultry"],      "🍗"),
    (["beef", "steak", "говядина", "rind"],             "🥩"),
    (["pork", "свинина", "schwein", "bacon", "ham"],    "🥓"),
    (["fish", "рыба", "lachs", "salmon", "tuna", "тунец"], "🐟"),
    (["shrimp", "prawn", "креветк"],                    "🍤"),
    # Vegetables
    (["avocado", "авокадо"],                            "🥑"),
    (["pepper", "перец", "paprika"],                    "🫑"),
    (["tomato", "томат", "помидор"],                    "🍅"),
    (["cucumber", "огурец", "gurke"],                   "🥒"),
    (["carrot", "морковь", "möhre"],                    "🥕"),
    (["broccoli", "брокколи"],                          "🥦"),
    (["spinach", "шпинат", "salad", "салат", "lettuce","greens"], "🥗"),
    (["mushroom", "гриб", "pilz"],                      "🍄"),
    (["onion", "лук", "zwiebel", "garlic", "чеснок"],  "🧅"),
    (["potato", "картофель", "kartoffel", "fries"],     "🥔"),
    (["corn", "кукуруза", "mais"],                      "🌽"),
    # Fruits
    (["banana", "банан"],                               "🍌"),
    (["apple", "яблок"],                                "🍎"),
    (["orange", "апельсин"],                            "🍊"),
    (["grape", "виноград", "traube"],                   "🍇"),
    (["strawberr", "клубник"],                          "🍓"),
    (["berr", "ягод", "blueberr", "raspberry"],         "🫐"),
    (["mango", "манго"],                                "🥭"),
    (["lemon", "лимон", "lime"],                        "🍋"),
    (["watermelon", "арбуз"],                           "🍉"),
    (["pear", "груша"],                                 "🍐"),
    (["cherry", "вишня", "kirsch"],                     "🍒"),
    (["peach", "персик", "pfirsich"],                   "🍑"),
    (["pineapple", "ананас"],                           "🍍"),
    # Grains & bread
    (["oat", "овсян", "porridge", "müsli", "muesli", "granola"], "🥣"),
    (["bread", "хлеб", "brot", "toast", "baguette"],   "🍞"),
    (["pasta", "макарон", "nudel", "spaghetti", "penne"], "🍝"),
    (["rice", "рис", "reis"],                           "🍚"),
    (["wrap", "tortilla", "pita", "питa"],              "🫓"),
    (["cracker", "crisp", "chip"],                      "🫘"),
    # Legumes & nuts
    (["bean", "фасоль", "bohne", "lentil", "чечевиц", "chickpea", "нут"], "🫘"),
    (["nut", "орех", "almond", "миндал", "walnut", "грецк", "cashew"], "🥜"),
    (["peanut", "арахис"],                              "🥜"),
    # Sweets & snacks
    (["chocolate", "шоколад", "schokolade"],            "🍫"),
    (["cake", "торт", "kuchen", "pastry", "muffin"],    "🎂"),
    (["cookie", "печень", "biscuit", "keks"],           "🍪"),
    (["ice cream", "мороженое", "eis"],                 "🍨"),
    (["honey", "мёд", "honig"],                         "🍯"),
    (["jam", "джем", "marmelade"],                      "🍓"),
    # Drinks
    (["coffee", "кофе", "kaffee", "espresso", "latte", "cappuccino"], "☕"),
    (["tea", "чай", "tee"],                             "🍵"),
    (["juice", "сок", "saft"],                          "🧃"),
    (["smoothie", "смузи"],                             "🥤"),
    (["water", "вода", "wasser"],                       "💧"),
    (["wine", "вино", "wein"],                          "🍷"),
    (["beer", "пиво", "bier"],                          "🍺"),
    # Prepared dishes
    (["soup", "суп", "suppe"],                          "🍲"),
    (["salad", "салат"],                                "🥗"),
    (["sandwich", "бутерброд"],                         "🥪"),
    (["burger", "бургер"],                              "🍔"),
    (["pizza", "пицца"],                                "🍕"),
    (["sushi", "суши", "roll"],                         "🍱"),
]

def _food_emoji(dish: str) -> str:
    """Pick a relevant emoji from the dish name. Falls back to 🍽 if no match."""
    d = dish.lower()
    for keywords, emoji in FOOD_EMOJI_MAP:
        if any(k in d for k in keywords):
            return emoji
    return "🍽"

def _fmt_meal(m: dict) -> str:
    emoji = _food_emoji(m.get("dish", ""))
    protein = m.get("protein_g", 0)
    return f"{emoji} {m['dish']} - {m['kcal']:.0f} kcal - {protein:.0f}g protein"

def _fmt_totals(totals: dict, goal: int) -> str:
    kcal = totals.get("kcal", 0)
    pct = int(kcal / goal * 100) if goal else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    remaining = goal - kcal
    if remaining > 0:
        remaining_str = f"✳️ *{remaining:.0f} kcal left today*"
    else:
        remaining_str = f"🚫 *{abs(remaining):.0f} kcal over goal*"
    return (
        f"🔢 {kcal:.0f} / {goal} kcal  [{bar}] {pct}%\n"
        f"🥩 Protein: {totals.get('protein_g', 0):.0f}g  "
        f"🧈 Fat: {totals.get('fat_g', 0):.0f}g  "
        f"🌾 Carbs: {totals.get('carbs_g', 0):.0f}g\n"
        + remaining_str
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Post-log confirmation message
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_dish_group(items: list[dict]) -> str:
    """
    Format a group of items that belong to the same dish.
    Single-item dishes → one line.
    Multi-ingredient dishes → dish name header + indented ingredient list.
    """
    dish_name = items[0].get("dish_name") or items[0].get("dish", "?")
    total_kcal = sum(i.get("kcal", 0) for i in items)
    total_protein = sum(i.get("protein_g", 0) for i in items)

    if len(items) == 1:
        emoji = _food_emoji(dish_name)
        return (
            f"{emoji} *{dish_name}* -{total_kcal:.0f} kcal -{total_protein:.0f}g protein"
        )
    else:
        emoji = _food_emoji(dish_name)
        header = f"{emoji} *{dish_name}* -{total_kcal:.0f} kcal -{total_protein:.0f}g protein"
        ingredient_lines = "\n".join(
            f"  · {i.get('dish', '?')} -{i.get('kcal', 0):.0f} kcal -{i.get('protein_g', 0):.0f}g protein"
            for i in items
        )
        return f"{header}\n{ingredient_lines}"


def log_confirmation(items: list[dict], user_id: int) -> str:
    """
    Return a friendly confirmation after logging food.
    Groups items by dish_name so multi-ingredient dishes show cleanly.
    """
    user = db.get_user(user_id) or {}
    goal = user.get("daily_kcal", 2000)
    totals = db.get_today_totals(user_id)

    # Group by dish_name
    from collections import OrderedDict
    groups: OrderedDict[str, list] = OrderedDict()
    for i in items:
        key = i.get("dish_name") or i.get("dish", "?")
        groups.setdefault(key, []).append(i)

    total_kcal = sum(i.get("kcal", 0) for i in items)
    total_protein = sum(i.get("protein_g", 0) for i in items)

    dish_blocks = "\n".join(_fmt_dish_group(g) for g in groups.values())

    if len(groups) == 1 and len(items) == 1:
        # Single standalone item -just show it once
        i = items[0]
        emoji = _food_emoji(i.get("dish_name") or i.get("dish", ""))
        item_lines = (
            f"✅ Logged: {emoji} *{i.get('dish_name') or i.get('dish', '?')}*\n"
            f"   {i.get('kcal', 0):.0f} kcal -{i.get('protein_g', 0):.0f}g protein"
        )
    elif len(groups) == 1:
        # One dish, multiple ingredients -show dish name once as header, then ingredients only
        dish_name = list(groups.keys())[0]
        g = list(groups.values())[0]
        emoji = _food_emoji(dish_name)
        ingredient_lines = "\n".join(
            f"  · {i.get('dish', '?')} -{i.get('kcal', 0):.0f} kcal -{i.get('protein_g', 0):.0f}g protein"
            for i in g
        )
        item_lines = (
            f"✅ Logged {emoji} *{dish_name}* ({len(items)} ingredients, "
            f"{total_kcal:.0f} kcal / {total_protein:.0f}g protein):\n{ingredient_lines}"
        )
    else:
        # Multiple dishes
        item_lines = (
            f"✅ Logged {len(groups)} dish(es), {len(items)} item(s) total "
            f"({total_kcal:.0f} kcal / {total_protein:.0f}g protein):\n{dish_blocks}"
        )

    summary = f"\n\n{_fmt_totals(totals, goal)}"

    # Alert if >80% of goal
    alert = ""
    pct = (totals.get("kcal", 0) / goal * 100) if goal else 0
    if pct >= 100:
        alert = "\n\n⚠️ You've hit your daily calorie goal!"
    elif pct >= 80:
        remaining = goal - totals.get("kcal", 0)
        alert = f"\n\n⚡ Heads up -you're at {pct:.0f}% of your goal. ~{remaining:.0f} kcal remaining."

    # Low confidence note
    low_conf = [i for i in items if i.get("confidence") == "low"]
    conf_note = ""
    if low_conf:
        conf_note = "\n\n🔍 *Low confidence* on some items -the estimate may be off. Feel free to correct me."

    return item_lines + summary + alert + conf_note


# ─────────────────────────────────────────────────────────────────────────────
# 2. Daily morning summary (triggered by cron at 08:00)
# ─────────────────────────────────────────────────────────────────────────────

def daily_morning_summary(user_id: int) -> str:
    """
    Short motivational summary of yesterday + encouragement for today.
    Called at 08:00 every day.
    """
    user = db.get_user(user_id) or {}
    goal = user.get("daily_kcal", 2000)

    # Yesterday's meals
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    from database import get_conn
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM meals
           WHERE user_id = ? AND date(logged_at) = ? AND confidence != 'deleted'
           ORDER BY logged_at""",
        (user_id, yesterday)
    ).fetchall()
    conn.close()
    yesterday_meals = [dict(r) for r in rows]

    if not yesterday_meals:
        return (
            "☀️ Good morning! I don't have any logs from yesterday.\n\n"
            "Don't forget to take a photo of your breakfast today! 📸"
        )

    ycal = sum(m["kcal"] for m in yesterday_meals)
    meal_lines = "\n".join(_fmt_meal(m) for m in yesterday_meals)
    pct = int(ycal / goal * 100) if goal else 0

    if pct < 80:
        verdict = f"Under goal -you had {ycal:.0f} kcal ({pct}% of your {goal} goal). Were you not hungry, or did you forget to log something?"
    elif pct <= 110:
        verdict = f"Right on track -{ycal:.0f} kcal ({pct}% of your {goal} goal). 🎯"
    else:
        over = ycal - goal
        verdict = f"Over goal by {over:.0f} kcal ({pct}%). Maybe a lighter start today? 💪"

    return (
        f"☀️ *Good morning!* Here's yesterday at a glance:\n\n"
        f"{meal_lines}\n\n"
        f"📊 {verdict}\n\n"
        f"Ready to log today's breakfast? Send me a photo! 📸"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Weekly Sunday review (triggered by cron on Sundays)
# ─────────────────────────────────────────────────────────────────────────────

def weekly_review(user_id: int) -> str:
    """
    Comprehensive weekly review with Claude analysis.
    Includes food, activity, and weight trend if available.
    """
    user = db.get_user(user_id) or {}
    goal = user.get("daily_kcal", 2000)
    week_data = db.get_week_totals(user_id)
    week_stats = db.get_week_stats(user_id)

    if not week_data:
        return "📅 *Weekly review:* No data logged this week yet. Start tracking and I'll give you insights!"

    # Build nutrition table for Claude
    table_lines = ["Day | kcal eaten | protein | fat | carbs | burned | net"]
    # Index activity stats by date for easy lookup
    stats_by_date = {s["date"]: s for s in week_stats}
    for row in week_data:
        activity = stats_by_date.get(row["day"], {})
        burned = activity.get("kcal_burned_est") or 0
        net = row["kcal"] - burned
        table_lines.append(
            f"{row['day']} | {row['kcal']:.0f} | {row['protein_g']:.0f}g | "
            f"{row['fat_g']:.0f}g | {row['carbs_g']:.0f}g | "
            f"{'~' + str(int(burned)) if burned else '—'} | {net:.0f}"
        )
    table = "\n".join(table_lines)

    # Weight trend
    weights = [
        (s["date"], s["weight_kg"])
        for s in week_stats
        if s.get("weight_kg")
    ]
    weight_section = ""
    if weights:
        w_first, w_last = weights[0][1], weights[-1][1]
        delta = w_last - w_first
        trend = f"↓ {abs(delta):.1f} kg" if delta < 0 else (f"↑ {delta:.1f} kg" if delta > 0 else "stable")
        weight_section = f"\nWeight this week: {w_first:.1f} kg → {w_last:.1f} kg ({trend})"

    # Activity summary
    total_steps = sum(s.get("steps") or 0 for s in week_stats)
    active_days = sum(1 for s in week_stats if s.get("workouts") and len(s["workouts"]) > 0)
    activity_section = ""
    if total_steps or active_days:
        activity_section = f"\nActivity: {total_steps:,} total steps, {active_days} workout day(s)"

    profile = db.get_profile_for_prompt(user_id)
    prompt = f"""The user's weekly data:
Daily calorie goal: {goal} kcal
{f"{chr(10)}{profile}" if profile else ""}
{table}{weight_section}{activity_section}

Write a SHORT, friendly weekly review (max 200 words). Include:
- Overall verdict on eating (over/under/on target, mention net calories if burn data is present)
- Weight trend comment if weight data is available
- One positive observation
- One concrete suggestion for next week that respects the user's preferences
- An encouraging closing line

Note: "net" column = calories eaten minus calories burned from exercise and walking.
Use emoji sparingly. Be like a supportive coach.
Use Telegram formatting: *bold* for emphasis, no markdown tables."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    avg_kcal = sum(r["kcal"] for r in week_data) / len(week_data)
    avg_burned = (
        sum(s.get("kcal_burned_est") or 0 for s in week_stats) / len(week_stats)
        if week_stats else 0
    )
    days_on_track = sum(1 for r in week_data if abs(r["kcal"] - goal) / goal < 0.15)

    header_parts = [
        f"📅 *Weekly Review*",
        f"Avg eaten: {avg_kcal:.0f} kcal/day | Goal: {goal} kcal | On-track: {days_on_track}/{len(week_data)} days",
    ]
    if avg_burned:
        header_parts.append(f"Avg burned: ~{avg_burned:.0f} kcal/day | Avg net: ~{avg_kcal - avg_burned:.0f} kcal/day")
    if weights:
        header_parts.append(f"Weight: {weights[0][1]:.1f} → {weights[-1][1]:.1f} kg")

    header = "\n".join(header_parts) + "\n\n"
    return header + response.content[0].text


# ─────────────────────────────────────────────────────────────────────────────
# 4. Answer user nutrition questions
# ─────────────────────────────────────────────────────────────────────────────

def answer_question(user_id: int, question: str) -> str:
    """
    Answer any nutrition question -data queries AND general advice.
    Acts as a knowledgeable personal nutritionist, not just a data lookup.
    """
    user = db.get_user(user_id) or {}
    goal = user.get("daily_kcal", 2000)
    today_meals = db.get_today_meals(user_id)
    week = db.get_week_totals(user_id)

    profile = db.get_profile_for_prompt(user_id)
    context = f"""You are a friendly, knowledgeable personal nutritionist.
You have access to the user's food tracking data below. Use it when relevant.

User's current daily calorie goal: {goal} kcal
Today's logged meals: {json.dumps(today_meals, default=str)}
This week's daily totals: {json.dumps(week, default=str)}
{f"{chr(10)}{profile}" if profile else ""}
Your role:
- Answer data questions using the actual numbers above ("you had 30g protein today")
- Answer general nutrition questions with real expert advice (calorie needs, macros, weight loss, meal suggestions, etc.)
- If asked about calorie goals or weight loss, calculate properly using standard formulas (TDEE, BMR) based on any info the user gives -weight, height, activity, goals
- ALWAYS respect the user's preferences -never suggest foods they dislike or can't eat
- Be direct and specific. Give actual numbers, not vague advice.
- Keep answers concise (2-4 sentences) unless the question needs more detail.
- Be warm and supportive, like a coach who wants them to succeed.
- Reply in the same language the user writes in.

FORMATTING RULES -this message will be displayed in Telegram:
- Use *text* for bold (single asterisk, NOT double)
- Use _text_ for italic (single underscore)
- NEVER use markdown tables (| col | col |) -Telegram does not render them
- Instead of tables, format options as a simple list like:
    🏃 Sedentary: 1,500 kcal
    🚶 Lightly active: 1,775 kcal
    🏋️ Moderately active: 2,050 kcal
- Use emojis to add visual structure instead of headers
- NEVER use --- as a divider
- Keep formatting clean and readable on a phone screen
"""

    # Sonnet for advice -needs reasoning ability, not just data lookup
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=context,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text


# ─────────────────────────────────────────────────────────────────────────────
# Activity formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

WORKOUT_EMOJI_MAP = [
    (["yoga", "pilates", "stretch"],                    "🧘"),
    (["run", "jogg", "sprint"],                         "🏃"),
    (["swim", "pool"],                                  "🏊"),
    (["cycl", "bike", "spinning"],                      "🚴"),
    (["hiit", "circuit", "crossfit", "box"],            "🥊"),
    (["strength", "weight", "lift", "gym", "тренаж"],   "🏋️"),
    (["walk", "hike", "прогулк"],                       "🚶"),
    (["tennis", "badminton", "squash"],                 "🎾"),
    (["football", "soccer", "basketball", "sport"],     "⚽"),
    (["dance", "zumba"],                                "💃"),
]

def _workout_emoji(name: str) -> str:
    n = name.lower()
    for keywords, emoji in WORKOUT_EMOJI_MAP:
        if any(k in n for k in keywords):
            return emoji
    return "🏃"

def _fmt_activity(stats: dict) -> str:
    """Format daily activity block for display in Telegram."""
    lines = []

    weight = stats.get("weight_kg")
    if weight:
        lines.append(f"⚖️ Weight: *{weight:.1f} kg*")

    steps = stats.get("steps")
    if steps:
        lines.append(f"👟 Steps: *{steps:,}*")

    workouts = stats.get("workouts") or []
    for w in workouts:
        name = w.get("name", "Workout")
        dur = w.get("duration_min")
        kcal = w.get("kcal_est") or w.get("kcal_estimated")
        emoji = _workout_emoji(name)
        dur_str = f" -{dur} min" if dur else ""
        kcal_str = f" -~{kcal:.0f} kcal" if kcal else ""
        lines.append(f"{emoji} {name}{dur_str}{kcal_str}")

    burned = stats.get("kcal_burned_est")
    if burned:
        lines.append(f"🔥 Total estimated burn: *~{burned:.0f} kcal*")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Day summary -works for any date (today, yesterday, etc.)
# ─────────────────────────────────────────────────────────────────────────────

def day_summary(user_id: int, for_date: str, label: str) -> str:
    """
    On-demand meal summary for any calendar date.
    for_date: YYYY-MM-DD string
    label:    display label shown in the header, e.g. "Today" or "Yesterday (April 8th)"
    """
    user = db.get_user(user_id) or {}
    goal = user.get("daily_kcal", 2000)
    dish_groups = db.get_meals_grouped_for_date(user_id, for_date)
    totals = db.get_totals_for_date(user_id, for_date)
    activity = db.get_daily_stats(user_id, for_date)

    if not dish_groups and not activity:
        return f"📋 Nothing logged for {label}."

    parts = [f"📋 *{label}:*"]

    if dish_groups:
        meal_order = ["breakfast", "lunch", "dinner", "snack"]
        by_meal: dict[str, list] = {}
        for group in dish_groups:
            mt = group.get("meal_type", "snack")
            by_meal.setdefault(mt, []).append(group)

        meal_sections = []
        for mt in meal_order:
            if mt not in by_meal:
                continue
            emoji = MEAL_EMOJI.get(mt, "🍽")
            section_lines = [f"{emoji} *{mt.capitalize()}*"]
            for g in by_meal[mt]:
                section_lines.append(_fmt_dish_group(g["ingredients"]))
            meal_sections.append("\n".join(section_lines))

        parts.append("\n" + "\n\n".join(meal_sections))
        parts.append(f"\n{_fmt_totals(totals, goal)}")
    else:
        parts.append("\n_No food logged._")

    if activity:
        activity_block = _fmt_activity(activity)
        if activity_block:
            burned = activity.get("kcal_burned_est", 0) or 0
            eaten = totals.get("kcal", 0)
            net = eaten - burned
            parts.append(f"\n\n🏃 *Activity:*\n{activity_block}")
            if burned and eaten:
                parts.append(f"\n📊 *Net intake: ~{net:.0f} kcal* (ate {eaten:.0f} -burned {burned:.0f})")

    return "\n".join(parts)


def today_summary(user_id: int) -> str:
    """On-demand summary of today's meals + activity."""
    return day_summary(user_id, date.today().isoformat(), "Today so far")
