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
    grams = _parse_grams(m.get("dish", ""))
    grams_str = f" - {grams:.0f}g" if grams > 0 else ""
    return f"{emoji} {m['dish']}{grams_str} - {m['kcal']:.0f} kcal - {protein:.0f}g protein"

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
        f"🥩 Protein: {totals.get('protein_g', 0):.0f}g\n"
        f"🧈 Fat: {totals.get('fat_g', 0):.0f}g\n"
        f"🌾 Carbs: {totals.get('carbs_g', 0):.0f}g\n"
        + remaining_str
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Post-log confirmation message
# ─────────────────────────────────────────────────────────────────────────────

import re as _re

def _parse_grams(dish: str) -> float:
    """Extract weight in grams (or ml) from a dish string like 'Chicken breast 120g'."""
    m = _re.search(r'(\d+(?:\.\d+)?)\s*(?:g|ml)\b', dish, _re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


def _fmt_dish_group(items: list[dict]) -> str:
    """
    Format a group of items that belong to the same dish.
    Single-item dishes → one line.
    Multi-ingredient dishes → dish name header + indented ingredient list.
    Shows total grams when parseable from ingredient names.
    """
    dish_name = items[0].get("dish_name") or items[0].get("dish", "?")
    total_kcal = sum(i.get("kcal", 0) for i in items)
    total_protein = sum(i.get("protein_g", 0) for i in items)
    total_grams = sum(_parse_grams(i.get("dish", "")) for i in items)
    grams_str = f" - {total_grams:.0f}g" if total_grams > 0 else ""

    if len(items) == 1:
        emoji = _food_emoji(dish_name)
        stats = f"{total_grams:.0f}g - " if total_grams > 0 else ""
        return (
            f"{emoji} *{dish_name}*\n{stats}{total_kcal:.0f} kcal - {total_protein:.0f}g protein"
        )
    else:
        emoji = _food_emoji(dish_name)
        stats = f"{total_grams:.0f}g - " if total_grams > 0 else ""
        header = f"{emoji} *{dish_name}*\n{stats}{total_kcal:.0f} kcal - {total_protein:.0f}g protein"
        ingredient_lines = "\n".join(
            f"  · {i.get('dish', '?')} - {i.get('kcal', 0):.0f} kcal - {i.get('protein_g', 0):.0f}g protein"
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
        # Single standalone item - just show it once
        i = items[0]
        emoji = _food_emoji(i.get("dish_name") or i.get("dish", ""))
        grams = _parse_grams(i.get("dish", ""))
        grams_str = f" - {grams:.0f}g" if grams > 0 else ""
        stats = f"{grams:.0f}g - " if grams > 0 else ""
        item_lines = (
            f"✅ Logged: {emoji} *{i.get('dish_name') or i.get('dish', '?')}*\n"
            f"{stats}{i.get('kcal', 0):.0f} kcal - {i.get('protein_g', 0):.0f}g protein"
        )
    elif len(groups) == 1:
        # One dish, multiple ingredients - show dish name once as header, then ingredients only
        dish_name = list(groups.keys())[0]
        g = list(groups.values())[0]
        emoji = _food_emoji(dish_name)
        total_grams = sum(_parse_grams(i.get("dish", "")) for i in g)
        grams_str = f" - {total_grams:.0f}g" if total_grams > 0 else ""
        ingredient_lines = "\n".join(
            f"  · {i.get('dish', '?')} - {i.get('kcal', 0):.0f} kcal - {i.get('protein_g', 0):.0f}g protein"
            for i in g
        )
        item_lines = (
            f"✅ Logged {emoji} *{dish_name}*\n"
            f"{len(items)} items{grams_str} - {total_kcal:.0f} kcal - {total_protein:.0f}g protein:\n{ingredient_lines}"
        )
    else:
        # Multiple dishes
        total_grams = sum(_parse_grams(i.get("dish", "")) for i in items)
        grams_str = f" - {total_grams:.0f}g" if total_grams > 0 else ""
        item_lines = (
            f"✅ Logged {len(groups)} dishes\n"
            f"{len(items)} items{grams_str} - {total_kcal:.0f} kcal - {total_protein:.0f}g protein:\n{dish_blocks}"
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
    Sunday review using up to 30 days of data.
    Stats header + 3-paragraph AI analysis (this week / longer pattern / recommendation).
    """
    user = db.get_user(user_id) or {}
    goal = user.get("daily_kcal", 2000)
    month_data = db.get_month_totals(user_id)   # up to 30 days
    week_stats  = db.get_week_stats(user_id)    # activity for past 7 days

    if not month_data:
        return "📅 *Sunday Review:* No data logged yet. Start tracking this week and I'll give you insights!"

    # Last 7 days vs older days
    today_str = date.today().isoformat()
    week_ago  = (date.today() - timedelta(days=7)).isoformat()
    this_week = [d for d in month_data if d["day"] > week_ago]
    older     = [d for d in month_data if d["day"] <= week_ago]

    def _avg(rows, key):
        vals = [r[key] for r in rows if r[key]]
        return sum(vals) / len(vals) if vals else 0

    avg_kcal_week  = _avg(this_week, "kcal")
    avg_prot_week  = _avg(this_week, "protein_g")
    avg_kcal_month = _avg(month_data, "kcal")
    avg_prot_month = _avg(month_data, "protein_g")
    days_on_track  = sum(1 for r in this_week if abs(r["kcal"] - goal) / goal < 0.15)

    # Weight trend from activity stats
    weights = [(s["date"], s["weight_kg"]) for s in week_stats if s.get("weight_kg")]
    weight_line = ""
    if weights:
        w_first, w_last = weights[0][1], weights[-1][1]
        delta = w_last - w_first
        arrow = f"↓ {abs(delta):.1f} kg" if delta < 0 else (f"↑ {delta:.1f} kg" if delta > 0 else "stable")
        weight_line = f"Weight: {w_first:.1f} → {w_last:.1f} kg ({arrow})"

    # Stats header (numbers only, no AI)
    header_parts = [
        f"📅 *Sunday Review* ({len(month_data)} days logged)",
        f"This week: avg *{avg_kcal_week:.0f} kcal/day* — *{avg_prot_week:.0f}g protein* — on-track {days_on_track}/{len(this_week)} days",
    ]
    if older:
        header_parts.append(f"Past month avg: {avg_kcal_month:.0f} kcal/day — {avg_prot_month:.0f}g protein")
    if weight_line:
        header_parts.append(weight_line)
    header = "\n".join(header_parts)

    # Build data summary for Claude
    month_summary = "\n".join(
        f"  {d['day']}: {d['kcal']:.0f} kcal, {d['protein_g']:.0f}g protein, "
        f"{d['fat_g']:.0f}g fat, {d['carbs_g']:.0f}g carbs"
        for d in month_data
    )

    profile = db.get_profile_for_prompt(user_id)
    prompt = f"""You are a supportive personal nutritionist sending a Sunday monthly overview message.

User's daily calorie goal: {goal} kcal
{f"User profile: {profile}" if profile else ""}

Data for the past {len(month_data)} days (all available history):
{month_summary}

Write exactly 3 short paragraphs separated by blank lines:

Paragraph 1: This week's verdict — how did they do vs goal, protein, consistency. Be specific with numbers.
Paragraph 2: A pattern you notice over the full history (longer trends, recurring habits, what's improving or stuck).
Paragraph 3: One focused diet recommendation for the coming week based on everything you see. Make it concrete and actionable.

Keep each paragraph to 1-2 sentences. Warm, coach-like tone. No bullet points. No headers.
Use Telegram formatting: *bold* for key numbers/words only.
Reply in the same language the user typically uses."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}]
    )

    return f"{header}\n\n💬 {response.content[0].text.strip()}"


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


def evening_summary(user_id: int) -> str:
    """
    Evening push summary: today's full log + short AI analysis of what went well
    and a recommendation based on the last 5 days of eating habits.
    """
    # Build the standard today log
    base = day_summary(user_id, date.today().isoformat(), "Today")

    # Gather data for the AI analysis
    user = db.get_user(user_id) or {}
    goal = user.get("daily_kcal", 2000)
    profile = db.get_profile_for_prompt(user_id)
    today_totals = db.get_today_totals(user_id)
    week_data = db.get_week_totals(user_id)  # last 7 days daily totals
    activity = db.get_daily_stats(user_id, date.today().isoformat())

    # Build last-5-days context (excluding today)
    past_days = [d for d in week_data if d["day"] != date.today().isoformat()][-5:]

    past_lines = "\n".join(
        f"  {d['day']}: {d['kcal']:.0f} kcal, {d['protein_g']:.0f}g protein, "
        f"{d['fat_g']:.0f}g fat, {d['carbs_g']:.0f}g carbs"
        for d in past_days
    ) or "  No data for past days."

    burned = (activity or {}).get("kcal_burned_est") or 0
    eaten = today_totals.get("kcal", 0)
    pct = int(eaten / goal * 100) if goal else 0

    prompt = f"""You are a supportive personal nutritionist sending a short evening check-in message.

Today's summary:
- Calories eaten: {eaten:.0f} kcal ({pct}% of {goal} kcal goal)
- Protein: {today_totals.get('protein_g', 0):.0f}g
- Fat: {today_totals.get('fat_g', 0):.0f}g
- Carbs: {today_totals.get('carbs_g', 0):.0f}g
{f'- Estimated burn: {burned:.0f} kcal (net {eaten - burned:.0f} kcal)' if burned else ''}

Last 5 days:
{past_lines}

{f"User profile: {profile}" if profile else ""}

Write a SHORT evening message in exactly 3 separate paragraphs (one sentence each, separated by a blank line):

Paragraph 1: What went well today — be specific (mention protein, staying under goal, balance, variety, etc.)
Paragraph 2: A pattern you notice from the last 5 days (e.g. consistently low protein, good calorie control, heavy on carbs, etc.)
Paragraph 3: One concrete, actionable tip for tomorrow based on what you see.

Be warm and specific, not generic. Use Telegram formatting: *bold* for emphasis only.
Reply in the same language the user typically uses."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )

    analysis = response.content[0].text.strip()
    return f"{base}\n\n💬 {analysis}"


def today_summary(user_id: int) -> str:
    """On-demand summary of today's meals + activity."""
    return day_summary(user_id, date.today().isoformat(), "Today so far")
