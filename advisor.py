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
import wiki

load_dotenv()   # must happen before Anthropic() reads the env
# max_retries=6 ≈ 30s of in-process retry on transient errors. See issue #18.
client = anthropic.Anthropic(max_retries=6)


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
    # Always surface a unit — if the dish string has no grams/ml encoded
    # (mostly legacy label-scan rows), fall back to "(serving)" so the
    # user always sees SOMETHING where the weight would be.
    grams_str = f" - {grams:.0f}g" if grams > 0 else " - (serving)"
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

# Pattern for the "× Npcs" marker the analyzer embeds in dish strings for
# countable multi-piece items. Tolerant of formatting variants — plain "x",
# unicode "×", with/without spaces, with/without the "pcs" suffix.
_PCS_RE = _re.compile(r'\s*[x×]\s*(\d+)\s*(?:pcs)?\b', _re.IGNORECASE)


def _parse_grams(dish: str) -> float:
    """Extract weight in grams (or ml) from a dish string like 'Chicken breast 120g'."""
    m = _re.search(r'(\d+(?:\.\d+)?)\s*(?:g|ml)\b', dish, _re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


def _parse_pcs(dish: str) -> int:
    """
    Extract piece count from "× Npcs" marker. Returns 1 if no marker found.

      'Fried egg × 2pcs 120g'  -> 2
      'Pancake ×3pcs 150g'     -> 3
      'Fried egg 60g'          -> 1
      'Avocado 80g'            -> 1

    The analyzer puts this marker on countable multi-piece items so the
    display shows the count explicitly. Corrections that add more of an
    existing item may or may not include a marker — consolidation sums
    whatever is there.
    """
    m = _PCS_RE.search(dish)
    return int(m.group(1)) if m else 1


def _dish_stem(dish: str) -> tuple[str, str]:
    """
    Split a dish string into (stem, unit) by stripping piece-count
    markers and the trailing weight.

      'Fried egg × 2pcs 120g'      -> ('Fried egg', 'g')
      'Fried egg 60g'              -> ('Fried egg', 'g')
      'Fried egg 1pc × 2pcs 120g'  -> ('Fried egg', 'g')  ← defensive
      'Cooking oil 5ml'            -> ('Cooking oil', 'ml')
      'König Käse'                 -> ('König Käse', '')

    The stem is used as the grouping key so a photo's "Fried egg × 2pcs
    120g" and a correction's "Fried egg 60g" collapse under one key and
    their counts can be summed. Unit is preserved so we can rebuild the
    label with the summed amount.

    Defensive note: the analyzer sometimes hallucinates a lone "1pc" /
    "2pcs" token with no "×" in front of it — e.g. when it's asked to
    add one more piece to a row that's already "× 2pcs". We strip those
    too so the consolidation key still matches the clean row.
    """
    # Strip "× Npcs" markers first (can appear anywhere).
    dish = _PCS_RE.sub(' ', dish)
    # Then strip any LEFTOVER bare "Npcs" / "Npc" tokens (no x/× in
    # front) — defensive against analyzer quirks.
    dish = _re.sub(r'\b\d+\s*pcs?\b', ' ', dish, flags=_re.IGNORECASE)
    # Collapse whitespace the substitutions left behind.
    dish = _re.sub(r'\s+', ' ', dish).strip()
    # Then strip trailing weight.
    m = _re.search(r'\s*(\d+(?:\.\d+)?)\s*(g|ml)\s*$', dish, _re.IGNORECASE)
    if m:
        return dish[:m.start()].strip(), m.group(2).lower()
    return dish.strip(), ""


def _consolidate_items(items: list[dict]) -> list[dict]:
    """
    Collapse identical ingredients into one row per unique dish stem.

    Three separate `Fried egg 60g / 90 kcal / 6g protein` rows become
    one `Fried egg 180g / 270 kcal / 18g protein` row. Order is by
    first appearance so the display still reflects how the meal was
    logged. Rows with no parseable weight pass through as-is (we still
    dedupe, but the dish string stays literal).
    """
    from collections import OrderedDict
    merged: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    for it in items:
        stem, unit = _dish_stem(it.get("dish", "") or "")
        key = (stem.lower(), unit)
        if key not in merged:
            # Start a fresh accumulator — copy so we don't mutate input.
            merged[key] = {
                "_stem": stem,
                "_unit": unit,
                "_count": 0,
                "dish_name": it.get("dish_name"),
                "grams": 0.0,
                "kcal": 0.0,
                "protein_g": 0.0,
                "fat_g": 0.0,
                "carbs_g": 0.0,
                "sugar_g": 0.0,
            }
        acc = merged[key]
        # Count PIECES, not rows — so a single "Fried egg × 2pcs 120g" row
        # contributes 2 to the count, and a plain "Fried egg 60g" row
        # contributes 1. Merging a 2-piece photo row with a 1-piece
        # correction row naturally yields 3 pieces.
        acc["_count"]    += _parse_pcs(it.get("dish", "") or "")
        acc["grams"]     += _parse_grams(it.get("dish", "") or "")
        acc["kcal"]      += it.get("kcal", 0) or 0
        acc["protein_g"] += it.get("protein_g", 0) or 0
        acc["fat_g"]     += it.get("fat_g", 0) or 0
        acc["carbs_g"]   += it.get("carbs_g", 0) or 0
        acc["sugar_g"]   += it.get("sugar_g", 0) or 0

    out: list[dict] = []
    for acc in merged.values():
        # Rebuild a dish string from stem + count + summed amount.
        # The "× Npcs" marker only appears when we actually merged 2+
        # rows — single items stay clean (no noisy "× 1pcs"). The "pcs"
        # word is deliberate visual punctuation between the count and
        # the gram total, so `3 180g` can't read as one number.
        count_str = f" × {acc['_count']}pcs" if acc["_count"] > 1 else ""
        if acc["_unit"] and acc["grams"] > 0:
            dish = f"{acc['_stem']}{count_str} {acc['grams']:.0f}{acc['_unit']}"
        else:
            dish = f"{acc['_stem']}{count_str}"
        out.append({
            "dish": dish,
            "dish_name": acc["dish_name"],
            "kcal": acc["kcal"],
            "protein_g": acc["protein_g"],
            "fat_g": acc["fat_g"],
            "carbs_g": acc["carbs_g"],
            "sugar_g": acc["sugar_g"],
        })
    return out


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
    grams_str = f" - {total_grams:.0f}g" if total_grams > 0 else " - (serving)"

    if len(items) == 1:
        emoji = _food_emoji(dish_name)
        stats = f"{total_grams:.0f}g - " if total_grams > 0 else "(serving) - "
        # Surface the "× Npcs" count from the dish string so a standalone
        # "Boiled eggs" row shows "Boiled eggs × 3pcs" in the header
        # instead of just "Boiled eggs" (which loses the count). dish_name
        # alone often doesn't carry the count — only the dish string does.
        pcs = _parse_pcs(items[0].get("dish", "") or "")
        pcs_str = f" × {pcs}pcs" if pcs > 1 else ""
        return (
            f"{emoji} *{dish_name}*{pcs_str}\n{stats}{total_kcal:.0f} kcal - {total_protein:.0f}g protein"
        )
    else:
        emoji = _food_emoji(dish_name)
        stats = f"{total_grams:.0f}g - " if total_grams > 0 else "(serving) - "
        header = f"{emoji} *{dish_name}*\n{stats}{total_kcal:.0f} kcal - {total_protein:.0f}g protein"
        # Consolidate identical ingredients — e.g. three "Fried egg 60g"
        # rows collapse to one "Fried egg 180g" row — so corrections that
        # add more of something already on the plate don't show up as a
        # stray duplicate line at the bottom of the list.
        display_items = _consolidate_items(items)
        ingredient_lines = "\n".join(
            f"  · {i.get('dish', '?')} - {i.get('kcal', 0):.0f} kcal - {i.get('protein_g', 0):.0f}g protein"
            for i in display_items
        )
        return f"{header}\n{ingredient_lines}"


def log_confirmation(items: list[dict], user_id: int) -> str:
    """
    Return a friendly confirmation after logging food.
    Groups items by dish_name so multi-ingredient dishes show cleanly.
    """
    user = db.get_user(user_id) or {}
    goal = wiki.get_daily_kcal(user_id, 2000)
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
        grams_str = f" - {grams:.0f}g" if grams > 0 else " - (serving)"
        stats = f"{grams:.0f}g - " if grams > 0 else "(serving) - "
        # Surface "× Npcs" from the dish string (the count lives there,
        # not in dish_name). Matches _fmt_dish_group's single-item branch.
        pcs = _parse_pcs(i.get("dish", "") or "")
        pcs_str = f" × {pcs}pcs" if pcs > 1 else ""
        item_lines = (
            f"✅ Logged: {emoji} *{i.get('dish_name') or i.get('dish', '?')}*{pcs_str}\n"
            f"{stats}{i.get('kcal', 0):.0f} kcal - {i.get('protein_g', 0):.0f}g protein"
        )
    elif len(groups) == 1:
        # One dish, multiple ingredients - show dish name once as header, then ingredients only.
        # Route the ingredient list through _consolidate_items so any "× Npcs"
        # markers from the analyzer are preserved in the display, and any
        # duplicate rows (e.g. from past corrections) collapse cleanly.
        dish_name = list(groups.keys())[0]
        g = list(groups.values())[0]
        emoji = _food_emoji(dish_name)
        total_grams = sum(_parse_grams(i.get("dish", "")) for i in g)
        grams_str = f" - {total_grams:.0f}g" if total_grams > 0 else " - (serving)"
        display_g = _consolidate_items(g)
        ingredient_lines = "\n".join(
            f"  · {i.get('dish', '?')} - {i.get('kcal', 0):.0f} kcal - {i.get('protein_g', 0):.0f}g protein"
            for i in display_g
        )
        # "N items" in the header reflects the consolidated count so it
        # matches what the user sees on screen (not the raw DB row count).
        item_lines = (
            f"✅ Logged {emoji} *{dish_name}*\n"
            f"{len(display_g)} items{grams_str} - {total_kcal:.0f} kcal - {total_protein:.0f}g protein:\n{ingredient_lines}"
        )
    else:
        # Multiple dishes
        total_grams = sum(_parse_grams(i.get("dish", "")) for i in items)
        grams_str = f" - {total_grams:.0f}g" if total_grams > 0 else " - (serving)"
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
    goal = wiki.get_daily_kcal(user_id, 2000)

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
    goal = wiki.get_daily_kcal(user_id, 2000)
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

    # Stats header (numbers only, no AI)
    header_parts = [
        f"📅 *Sunday Review* ({len(month_data)} days logged)",
        f"This week: avg *{avg_kcal_week:.0f} kcal/day* — *{avg_prot_week:.0f}g protein* — on-track {days_on_track}/{len(this_week)} days",
    ]
    if older:
        header_parts.append(f"Past month avg: {avg_kcal_month:.0f} kcal/day — {avg_prot_month:.0f}g protein")

    # Weight block (richer "weekly" mode — start→end, month delta, target progress + pace).
    # Reads from weight_readings (issue #7); empty string if no readings yet,
    # in which case we don't add a weight line at all.
    weight_block = _fmt_weight_block(user_id, mode="weekly")
    if weight_block:
        header_parts.append(weight_block)
    header = "\n".join(header_parts)

    # Build data summary for Claude
    month_summary = "\n".join(
        f"  {d['day']}: {d['kcal']:.0f} kcal, {d['protein_g']:.0f}g protein, "
        f"{d['fat_g']:.0f}g fat, {d['carbs_g']:.0f}g carbs"
        for d in month_data
    )

    profile = wiki.read_wiki_for_prompt(user_id)
    weight_context_for_prompt = _fmt_weight_context_for_prompt(user_id)

    prompt = f"""You are a supportive personal nutritionist sending a detailed Sunday weekly review to your client.

User's daily calorie goal: {goal} kcal
{weight_context_for_prompt}
{f"What I know about this user (long-term memory):{chr(10)}{profile}" if profile else ""}

Data for the past {len(month_data)} days (all available history):
{month_summary}

Write a concise review in exactly 3 short paragraphs, each separated by a blank line. Keep it tight — the user wants signal, not padding.

Paragraph 1 — This week verdict: overall calorie consistency + protein, with specific numbers and a clear verdict. Mention the best and worst day briefly. If weight context is available, weave in this week's weight movement.

Paragraph 2 — The main pattern worth noticing: one observation from the longer arc. Combine wins and gaps honestly. If a weight trend is visible (steady, down, up), call it out and connect it to the eating pattern.

Paragraph 3 — One concrete recommendation for the coming week, tied directly to what you just said. If a target weight exists, frame the recommendation around progress toward it.

Each paragraph should be 2-3 sentences MAX. Warm, coach-like tone. No bullet points. No headers. Just short paragraphs.
Use Telegram formatting: *bold* sparingly for key numbers.
Reply in the same language the user typically uses."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )

    return f"{header}\n\n💬 {response.content[0].text.strip()}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Answer user nutrition questions
# ─────────────────────────────────────────────────────────────────────────────

def answer_question(user_id: int, question: str, conversation: list[dict] | None = None) -> str:
    """
    Answer any nutrition question -data queries AND general advice.
    Acts as a knowledgeable personal nutritionist, not just a data lookup.

    conversation: short-term rolling-window memory (list of {role, content}
    dicts, oldest first, ending with the current user question). When
    supplied it's passed as the messages list so follow-ups ("and what
    about if I add cardio?") retain context from earlier turns.
    """
    user = db.get_user(user_id) or {}
    goal = wiki.get_daily_kcal(user_id, 2000)
    today_meals = db.get_today_meals(user_id)
    week = db.get_week_totals(user_id)

    profile = wiki.read_wiki_for_prompt(user_id)
    weight_context_for_prompt = _fmt_weight_context_for_prompt(user_id)
    context = f"""You are a friendly, knowledgeable personal nutritionist.
You have access to the user's food tracking data below. Use it when relevant.

User's current daily calorie goal: {goal} kcal
{weight_context_for_prompt}
Today's logged meals: {json.dumps(today_meals, default=str)}
This week's daily totals: {json.dumps(week, default=str)}
{f"{chr(10)}What I know about this user (long-term memory):{chr(10)}{profile}" if profile else ""}
USING THE CONVERSATION (IMPORTANT):
The message list you receive is today's rolling conversation, not a single
turn. READ the earlier messages — the user's follow-ups almost always
refer back to something you just said. Pronouns ("it", "that", "the
second one"), numeric picks ("option 1", "#2"), agreement ("sounds great,
let's do it"), and terse questions ("how many grams?", "and without the
bread?") are continuations, not new topics. Interpret the latest message
in light of what you just suggested, not in isolation.

Your role:
- Answer data questions using the actual numbers above ("you had 30g protein today")
- Answer general nutrition questions with real expert advice (calorie needs, macros, weight loss, meal suggestions, recipes, etc.)
- If the user picks one of your earlier options ("option 1", "the chicken one"), drill into THAT — give the recipe / plan / details for that specific pick, not generic advice.
- If asked about calorie goals or weight loss, calculate properly using standard formulas (TDEE, BMR) based on any info the user gives -weight, height, activity, goals
- ALWAYS respect the user's preferences -never suggest foods they dislike or can't eat
- Be direct and specific. Give actual numbers, not vague advice.
- Keep answers concise (2-4 sentences) unless the question needs more detail (recipes and plans can be longer).
- Be warm and supportive, like a coach who wants them to succeed.
- Reply in the same language the user writes in.

LENGTH BUDGET (HARD CAP — Telegram limits one message to 4000 characters):
- Your reply MUST fit in ~3500 characters.  Treat this as a hard limit and
  plan your sections to finish inside it.
- Simple data questions ("how much protein did I eat?") should stay short —
  2-4 sentences is plenty.
- For BROAD questions (weight-loss advice, full day/week analysis, meal plans,
  "give me advice"), cover ALL the main angles BRIEFLY rather than deep-diving
  into a few.  Think: a compact tour, not a thesis.  Each angle gets one
  tight paragraph or a 2-3 bullet mini-list — enough to be useful, short
  enough that the whole overview fits the budget.  The USER decides what
  matters most to them, not you.
- End a broad answer by offering deeper dives — e.g. "Want me to go deeper
  on any of these — nutrition, strength training, meal timing, something
  else?"  Let the user pick the priority for the next message.
- If a topic is so big that even brief coverage would overflow, list every
  angle you'd cover but compress the less-critical ones to a single line
  each ("⏱ Meal timing: mostly fine, ask for details if you want") — never
  drop angles silently.
- Your response must ALWAYS end with a complete sentence.  If you feel you're
  running out of room, wrap the current point cleanly and pivot to the
  "want me to go deeper on X?" invitation.  Never leave a thought half-finished.

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
    # If conversation history was supplied, use it directly (it already ends
    # with the current user question). Otherwise fall back to a single turn.
    messages = conversation if conversation else [{"role": "user", "content": question}]

    # Ceiling sized to Telegram's 4000-char message cap (≈1000 tokens in English).
    # The system prompt already tells the model to stay concise for data questions
    # and go long only for recipes/plans/advice, so short answers stay short.
    # Previously set to 500 — which cut off detailed advice mid-sentence.
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=context,
        messages=messages,
    )
    text = response.content[0].text

    # Safety net: if the model STILL ran over the budget its system prompt set,
    # don't leave the user staring at a half-sentence.  Append a short note so
    # they know to ask for more — much better UX than an abrupt cliff.  Logged
    # so we can track how often this fires and tune the prompt if it's common.
    if response.stop_reason == "max_tokens":
        import logging as _logging
        _logging.getLogger(__name__).warning(
            f"answer_question hit max_tokens for user {user_id} "
            f"(q={question[:80]!r})"
        )
        # Match the reply language roughly by peeking at the last user turn —
        # if it contains Cyrillic, use Russian; otherwise English.  Good enough
        # for a one-line hint; we don't want a full language detector here.
        last_user = (messages[-1].get("content") if messages else question) or ""
        has_cyrillic = any("\u0400" <= c <= "\u04FF" for c in str(last_user))
        if has_cyrillic:
            hint = "\n\n_…тут я остановилась, чтобы не превысить лимит сообщения. Спросить про остальное?_"
        else:
            hint = "\n\n_…I paused here to stay within the message limit. Want me to keep going?_"
        text = text.rstrip() + hint

    return text


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
# Weight tracking — analysis + display helpers
#
# Reads from the `weight_readings` table populated by withings_sync.py (issue
# #7 plumbing) and the target weight in goals.md. All helpers gracefully
# return empty/None when there's no data — they're safe to call on a fresh
# user who hasn't connected Withings yet.
# ─────────────────────────────────────────────────────────────────────────────

def _weight_context(user_id: int) -> dict:
    """Compute weight stats for summaries and Q&A.

    Returns a dict with whatever's computable. All numeric values may be None
    when there isn't enough data. Shape:

        {
            "current_kg":     float | None,  # most recent reading
            "yesterday_kg":   float | None,  # min reading from yesterday
            "week_ago_kg":    float | None,  # min reading from ~7 days back
            "month_ago_kg":   float | None,  # min reading from ~30 days back
            "delta_day":      float | None,  # current - yesterday
            "delta_week":     float | None,  # current - week_ago
            "delta_month":    float | None,  # current - month_ago
            "target_kg":      float | None,  # from goals.md
            "delta_target":   float | None,  # current - target_kg (signed)
            "pace_kg_per_wk": float | None,  # average weekly trend over 30 days
            "history":        list[dict],    # last 30 days of (date, weight_kg)
        }
    """
    out = {
        "current_kg": None, "yesterday_kg": None,
        "week_ago_kg": None, "month_ago_kg": None,
        "delta_day": None, "delta_week": None, "delta_month": None,
        "target_kg": None, "delta_target": None,
        "pace_kg_per_wk": None,
        "history": [],
    }

    # Per-date min series for trend math. Pulls last 35 days to give headroom
    # for the "7 days ago" / "30 days ago" lookups.
    today = date.today()
    since = (today - timedelta(days=35)).isoformat()
    history = db.get_daily_min_weights(user_id, since=since)
    out["history"] = history

    if not history:
        # No raw readings yet — fall back to most recent individual reading
        # or legacy daily_stats.weight_kg so we still display SOMETHING.
        latest = db.get_latest_weight_reading(user_id)
        out["current_kg"] = (
            latest["weight_kg"] if latest else db.get_latest_weight(user_id)
        )
        target = wiki.get_target_weight(user_id)
        if target is not None:
            out["target_kg"] = target
            if out["current_kg"] is not None:
                out["delta_target"] = out["current_kg"] - target
        return out

    by_date = {row["date"]: row["weight_kg"] for row in history}

    # current_kg = today's morning-low if we have a reading today, else the
    # most recent day's min in the history. We deliberately use the day-min
    # (not the most-recent individual reading) so it's consistent with the
    # deltas (also day-mins) and stable across the day.
    today_str = today.isoformat()
    if today_str in by_date:
        out["current_kg"] = by_date[today_str]
    else:
        out["current_kg"] = history[-1]["weight_kg"]

    def _lookup_around(target_date: date, window_days: int = 3) -> Optional[float]:
        """Return the weight nearest to target_date within ±window_days."""
        for offset in range(window_days + 1):
            for sign in (0, -1, 1):
                d = (target_date + timedelta(days=sign * offset)).isoformat()
                if d in by_date:
                    return by_date[d]
        return None

    out["yesterday_kg"] = _lookup_around(today - timedelta(days=1), window_days=1)
    out["week_ago_kg"]  = _lookup_around(today - timedelta(days=7), window_days=2)
    out["month_ago_kg"] = _lookup_around(today - timedelta(days=30), window_days=4)

    if out["current_kg"] is not None:
        if out["yesterday_kg"] is not None:
            out["delta_day"] = out["current_kg"] - out["yesterday_kg"]
        if out["week_ago_kg"] is not None:
            out["delta_week"] = out["current_kg"] - out["week_ago_kg"]
        if out["month_ago_kg"] is not None:
            out["delta_month"] = out["current_kg"] - out["month_ago_kg"]

    # Pace: linear weekly trend from the oldest reading in the window to the
    # current. Simple end-to-end average; doesn't try to be clever about
    # short-term noise. Only computed if we have at least 14 days of data.
    if len(history) >= 14:
        first = history[0]
        last = history[-1]
        try:
            d_start = date.fromisoformat(first["date"])
            d_end = date.fromisoformat(last["date"])
            span_days = max(1, (d_end - d_start).days)
            kg_change = last["weight_kg"] - first["weight_kg"]
            out["pace_kg_per_wk"] = kg_change / span_days * 7
        except (ValueError, KeyError):
            pass

    # Target weight + distance.
    target = wiki.get_target_weight(user_id)
    if target is not None:
        out["target_kg"] = target
        if out["current_kg"] is not None:
            out["delta_target"] = out["current_kg"] - target

    return out


def _fmt_arrow_delta(delta: float, unit: str = "kg") -> str:
    """Render a signed delta with an arrow.
        +0.3 → '↑ 0.3 kg'
        -0.2 → '↓ 0.2 kg'
         0.0 → 'steady'
    """
    if abs(delta) < 0.05:
        return "steady"
    arrow = "↑" if delta > 0 else "↓"
    return f"{arrow} {abs(delta):.1f} {unit}"


def _fmt_weight_block(user_id: int, mode: str = "evening") -> str:
    """Format a weight block for inclusion in a summary.

    Returns empty string if there's no weight data at all (so the caller
    can append unconditionally without checking).

    Args:
        mode: 'evening' for compact 2–3 lines (evening push, /today),
              'weekly' for a richer block (Sunday review).
    """
    ctx = _weight_context(user_id)
    cur = ctx["current_kg"]
    if cur is None:
        return ""

    lines: list[str] = []

    if mode == "weekly":
        # Header: this-week start → current, plus one-line delta.
        wk = ctx["week_ago_kg"]
        if wk is not None:
            lines.append(f"⚖️ Weight: *{wk:.1f} → {cur:.1f} kg* this week ({_fmt_arrow_delta(cur - wk)})")
        else:
            lines.append(f"⚖️ Weight: *{cur:.1f} kg*")

        if ctx["delta_month"] is not None:
            lines.append(f"   {_fmt_arrow_delta(ctx['delta_month'])} over the past month")

        if ctx["target_kg"] is not None and ctx["delta_target"] is not None:
            dt = ctx["delta_target"]
            if abs(dt) < 0.3:
                lines.append(f"   🎯 At your *{ctx['target_kg']:.0f} kg* target")
            else:
                direction = "to lose" if dt > 0 else "to gain"
                lines.append(f"   🎯 *{abs(dt):.1f} kg {direction}* — target {ctx['target_kg']:.0f} kg")

            pace = ctx["pace_kg_per_wk"]
            # Only narrate pace if it's heading toward the target.
            if pace is not None and abs(pace) >= 0.05:
                target_heading = "down" if dt > 0 else "up"
                pace_heading = "down" if pace < 0 else "up"
                if target_heading == pace_heading:
                    weeks = max(1, round(abs(dt) / max(0.05, abs(pace))))
                    lines.append(
                        f"   📈 Trend: {_fmt_arrow_delta(pace * 4)} per month "
                        f"— at this pace, ~{weeks} week(s) to target"
                    )
                else:
                    lines.append(
                        f"   📈 Trend: {_fmt_arrow_delta(pace * 4)} per month "
                        f"(opposite direction from target)"
                    )

    else:  # mode == "evening" — keep it compact
        lines.append(f"⚖️ Weight: *{cur:.1f} kg*")

        # Combine day + week deltas into one line if both present.
        deltas: list[str] = []
        if ctx["delta_day"] is not None:
            deltas.append(f"{_fmt_arrow_delta(ctx['delta_day'])} vs yesterday")
        if ctx["delta_week"] is not None:
            deltas.append(f"{_fmt_arrow_delta(ctx['delta_week'])} vs last week")
        if deltas:
            lines.append("   " + " • ".join(deltas))

        if ctx["target_kg"] is not None and ctx["delta_target"] is not None:
            dt = ctx["delta_target"]
            if abs(dt) < 0.3:
                lines.append(f"   🎯 At your *{ctx['target_kg']:.0f} kg* target")
            else:
                direction = "to lose" if dt > 0 else "to gain"
                lines.append(f"   🎯 *{abs(dt):.1f} kg {direction}* to your {ctx['target_kg']:.0f} kg target")

    return "\n".join(lines)


def _fmt_weight_context_for_prompt(user_id: int) -> str:
    """Compact text block describing the user's current weight situation,
    suitable for injection into an LLM system prompt (answer_question,
    evening_summary's analysis paragraph, weekly_review).

    Returns empty string if there's no weight data.
    """
    ctx = _weight_context(user_id)
    cur = ctx["current_kg"]
    if cur is None:
        return ""

    parts = [f"Weight today: {cur:.1f} kg"]
    if ctx["yesterday_kg"] is not None:
        parts.append(f"yesterday {ctx['yesterday_kg']:.1f} kg")
    if ctx["week_ago_kg"] is not None:
        parts.append(f"a week ago {ctx['week_ago_kg']:.1f} kg")
    if ctx["month_ago_kg"] is not None:
        parts.append(f"a month ago {ctx['month_ago_kg']:.1f} kg")
    if ctx["pace_kg_per_wk"] is not None:
        sign = "down" if ctx["pace_kg_per_wk"] < 0 else "up"
        parts.append(f"30-day trend: {sign} {abs(ctx['pace_kg_per_wk']):.2f} kg/week")
    if ctx["target_kg"] is not None:
        if ctx["delta_target"] is not None:
            dt = ctx["delta_target"]
            if abs(dt) < 0.3:
                parts.append(f"AT target ({ctx['target_kg']:.0f} kg)")
            else:
                direction = "to lose" if dt > 0 else "to gain"
                parts.append(f"{abs(dt):.1f} kg {direction} to reach target {ctx['target_kg']:.0f} kg")
        else:
            parts.append(f"target {ctx['target_kg']:.0f} kg")

    return "Weight context — " + "; ".join(parts) + "."


# ─────────────────────────────────────────────────────────────────────────────
# 5. Day summary -works for any date (today, yesterday, etc.)
# ─────────────────────────────────────────────────────────────────────────────

def day_summary(
    user_id: int,
    for_date: str,
    label: str,
    *,
    include_totals: bool = True,
) -> str:
    """
    On-demand meal summary for any calendar date.
    for_date: YYYY-MM-DD string
    label:    display label shown in the header, e.g. "Today" or "Yesterday (April 8th)"
    include_totals: when False, suppress the macro totals block at the bottom
        of the meals section.  Used by evening_summary, which builds its own
        totals block at the TOP of the message and wants the day-overview to
        be pure "here's what you ate" content — no duplicated totals.
    """
    user = db.get_user(user_id) or {}
    goal = wiki.get_daily_kcal(user_id, 2000)
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
        if include_totals:
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
    Evening push summary.  Three blocks, in this order:
      1. Macro totals for today (kcal progress + protein/fat/carbs + kcal left)
      2. AI analysis — what went well, a 5-day pattern, one tip for tomorrow
      3. Meal-by-meal overview for today (plus activity if present)

    Totals go at the top so the first thing the user sees on the notification
    is the day's headline numbers; the narrative analysis follows; the
    dish-by-dish detail sits at the bottom for anyone who wants to scroll.
    """
    # Gather data for the AI analysis + totals header
    user = db.get_user(user_id) or {}
    goal = wiki.get_daily_kcal(user_id, 2000)
    profile = wiki.read_wiki_for_prompt(user_id)
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

    # Compact weight context block — empty string if no readings yet.
    weight_context_for_prompt = _fmt_weight_context_for_prompt(user_id)

    prompt = f"""You are a supportive personal nutritionist sending a short evening check-in message.

Today's summary:
- Calories eaten: {eaten:.0f} kcal ({pct}% of {goal} kcal goal)
- Protein: {today_totals.get('protein_g', 0):.0f}g
- Fat: {today_totals.get('fat_g', 0):.0f}g
- Carbs: {today_totals.get('carbs_g', 0):.0f}g
{f'- Estimated burn: {burned:.0f} kcal (net {eaten - burned:.0f} kcal)' if burned else ''}
{weight_context_for_prompt}

Last 5 days:
{past_lines}

{f"What I know about this user (long-term memory):{chr(10)}{profile}" if profile else ""}

Write a SHORT evening message in exactly 3 separate paragraphs (one sentence each, separated by a blank line):

Paragraph 1: What went well today — be specific (mention protein, staying under goal, balance, variety, etc.). If weight context is provided, you may reference it here when relevant.
Paragraph 2: A pattern you notice from the last 5 days (e.g. consistently low protein, good calorie control, heavy on carbs, etc.). If a clear weight trend exists, you can highlight that instead.
Paragraph 3: One concrete, actionable tip for tomorrow based on what you see.

Be warm and specific, not generic. Use Telegram formatting: *bold* for emphasis only.
Reply in the same language the user typically uses."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )

    analysis = response.content[0].text.strip()

    # Assemble: totals → weight → analysis → meal overview.
    # The weight block goes second so the headline numbers (kcal + weight)
    # are both visible at a glance before the analysis paragraphs.
    # The meals block uses include_totals=False so the totals aren't
    # duplicated (we already put them at the top).
    totals_block = f"📋 *Today:*\n\n{_fmt_totals(today_totals, goal)}"
    weight_block = _fmt_weight_block(user_id, mode="evening")
    meals_block = day_summary(
        user_id,
        date.today().isoformat(),
        "Today's meals",
        include_totals=False,
    )

    parts = [totals_block]
    if weight_block:
        parts.append(weight_block)
    parts.append(f"💬 {analysis}")
    parts.append(meals_block)
    return "\n\n".join(parts)


def today_summary(user_id: int) -> str:
    """On-demand summary of today's meals + activity."""
    return day_summary(user_id, date.today().isoformat(), "Today so far")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Wiki ingest — async, fire-and-forget (Step 2)
#
# After a user sends a self-statement ("I'm cutting sugar") or a self-question
# ("am I low on protein?"), we kick off a background LLM call that decides
# whether anything is worth filing into the user's markdown wiki.
#
# Key properties:
#   • Non-blocking  — the user's reply is already sent; ingest runs after.
#   • Safe          — per-user asyncio.Lock prevents concurrent writes.
#   • Additive only — ingest can only append bullets / add log entries.
#     Restructuring & dedup happens in the weekly lint pass (Step 4).
#   • Auditable     — every decision is logged to ./ingest.log so we can see
#     why a bullet appeared (or why nothing did).
#   • Cheap         — uses Haiku (~10× cheaper than Sonnet).  Narrow task.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio as _asyncio
import logging as _logging
from pathlib import Path as _Path

# (`wiki` is already imported at the top of this module.)

# Lazy-initialized async Anthropic client (parallel to the sync `client` above).
_async_client: Optional[anthropic.AsyncAnthropic] = None

def _get_async_client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        # max_retries=6 ≈ 30s of in-process retry on transient errors. See issue #18.
        _async_client = anthropic.AsyncAnthropic(max_retries=6)
    return _async_client


# Dedicated file logger for ingest decisions — separate from the main bot log
# so we can tail `ingest.log` and inspect exactly what Haiku chose to record.
_ingest_logger = _logging.getLogger("wiki_ingest")
if not _ingest_logger.handlers:
    _ingest_logger.setLevel(_logging.INFO)
    _h = _logging.FileHandler(_Path(__file__).parent / "ingest.log", encoding="utf-8")
    _h.setFormatter(_logging.Formatter("%(asctime)s %(message)s"))
    _ingest_logger.addHandler(_h)
    _ingest_logger.propagate = False   # don't also spam the main bot log

# Read the rulebook once at import — this is what the LLM must follow.
_WIKI_RULES = (_Path(__file__).parent / "wiki_instructions.md").read_text(encoding="utf-8")

# asyncio.create_task only keeps a weak reference to the task, so without
# this set the GC could kill an in-flight ingest.  Callback removes the task
# once it finishes so the set doesn't grow forever.
_background_tasks: set = set()


_INGEST_PROMPT_TEMPLATE = """You are the wiki maintainer for a personal nutrition coach.

Your job: decide what (if anything) to add to the user's wiki based on ONE recent interaction.

---

# Wiki rules (the schema you must follow)

{rules}

---

# User's current wiki

{wiki_state}

---

# Recent interaction

Interaction type: {interaction_type}
User said: {user_message}
Bot replied: {bot_reply}

---

# Your output

Respond with ONLY a JSON object (no prose, no markdown code fences) in this exact shape:

{{
  "reasoning": "one sentence explaining your decision",
  "updates": [
    {{"page": "patterns|profile|goals|wins", "action": "append", "content": "- [{today}] bullet text"}},
    {{"page": "patterns|profile|goals|wins", "action": "remove_line", "target": "distinctive substring from the existing bullet"}},
    {{"page": "patterns|profile|goals|wins", "action": "replace_line", "target": "distinctive substring from the existing bullet", "new_content": "- [{today}] rewritten bullet text"}}
  ]
}}

Decision rules:
- The "page" field MUST be one of exactly: "profile", "patterns", "goals", "wins".
  Do NOT include the ".md" extension — write "goals", not "goals.md".
- Do NOT emit updates for "log" — log.md is the audit trail and is written
  AUTOMATICALLY by the code whenever an append or remove_line is applied.
  You do not need to (and MUST not) emit a separate log entry for a wiki change.
- MANDATORY DATE PREFIX (rule 0 of the wiki schema): every bullet you append to
  profile / goals / patterns / wins MUST start with today's date in square
  brackets: `- [{today}] bullet text`.  No exceptions.  Lint relies on this
  prefix to reason about recency, so a missing prefix is a bug.
- If nothing is worth recording, return {{"reasoning": "...", "updates": []}}.
- Do NOT duplicate observations already in the wiki — scan each page first.
- SKIP: plain meal logs, corrections (e.g. "two eggs not one"), and general world-knowledge questions (e.g. "why does fermentation reduce calories?").
- Self-statements ("I'm cutting sugar", "I felt bloated after lunch") usually deserve a bullet in the patterns or profile page.
- Self-questions ("am I low on protein?", "am I over goal?") often reveal concerns or interests — consider a bullet in the patterns page.
- When in doubt whether a question is about the user, LEAN self-question — do not miss important info.
- Bullets are one line, natural human language, no internal file references.
- For patterns.md you may still include observation counts inside the line, e.g.
  `- [{today}] Under-eats protein at breakfast (observed 5x since 2026-04-01)`.
  The `[{today}]` prefix is required; the `(observed Nx since …)` tail is optional.
- Respect the ~30-bullet cap per page.  If a page is nearing the cap, prefer not appending unless clearly new.

When to emit `remove_line` vs `replace_line` (retracting a fact/goal/pattern):

Use one of these actions when the user asks to drop, remove, forget, cancel,
or replace something that is ALREADY in the wiki above.  The user has just
said "remove X", "I no longer want X", "forget about X", "change X to Y",
etc., and a matching bullet exists on the relevant page.

Pick the RIGHT action by looking at the existing bullet:

- `remove_line` — when the existing bullet contains ONLY the fact being
  retracted.  Deletes the whole bullet.
- `replace_line` — when the existing bullet contains MULTIPLE facts jammed
  together (often via ";" or ","), and the user is retracting ONLY ONE of
  them.  Rewrites the bullet in place with the remaining fact(s).
- Two updates (remove_line + append) — when the user is REPLACING a bullet
  with a semantically different one (e.g. "change my goal from X to Y").

Common rules for both:
- The "target" field is a SHORT, DISTINCTIVE substring copied from the
  existing bullet — enough to uniquely identify it.  Case-insensitive
  substring match against bullet lines.  Do NOT include the date prefix.
- Only bullets on patterns/profile/goals/wins are eligible.  Never emit
  remove_line/replace_line for "log".
- If no matching bullet exists, do NOT emit either action and do NOT append
  anything about the retraction — return an empty updates list and explain
  in reasoning.
- For `replace_line`, `new_content` MUST include the `- [{today}]` prefix
  just like an append (we're writing a whole new line; lint relies on the
  date prefix).

Worked examples (given the wiki state above shows the bullet):

  Goals page contains: `- [2026-04-01] Wants to eat more healthy fats`
  User says: "remove from goals eating healthy fats"
  → {{"page": "goals", "action": "remove_line", "target": "healthy fats"}}
  (The bullet is only about healthy fats — safe to delete whole.)

  Profile page contains: `- [2026-03-15] Does not eat olive oil; prefers healthy fats`
  User says: "remove about not eating olive oil"
  → {{"page": "profile", "action": "replace_line",
       "target": "olive oil",
       "new_content": "- [{today}] Prefers healthy fats"}}
  (The bullet has TWO facts — use replace_line to keep the other one.)

  Profile page contains: `- [2026-03-20] Target weight: 68kg`
  User says: "change target weight to 65"
  → two updates:
     remove_line target="Target weight: 68kg",
     append "- [{today}] Target weight: 65kg"
  (Bullet is one fact being replaced by a semantically different one.)

  Patterns page contains: `- [2026-04-05] Skips breakfast on weekends`
  User says: "I eat breakfast every day now, drop that weekend pattern"
  → {{"page": "patterns", "action": "remove_line",
       "target": "Skips breakfast on weekends"}}
"""


async def ingest_interaction(
    user_id: int,
    interaction_type: str,
    user_message: str,
    bot_reply: str = "",
) -> None:
    """
    Decide whether one interaction is wiki-worthy, then apply any updates.

    Fire-and-forget: callers schedule this via asyncio.create_task() and do NOT
    await.  All errors are caught and logged to ingest.log — never raised —
    so a bad ingest can't break the user's reply flow.
    """
    try:
        async with wiki.get_lock(user_id):
            wiki.ensure_user_wiki(user_id)
            wiki_state = (
                wiki.read_wiki_for_prompt(user_id)
                or "_(empty — new user, no pages have content yet)_"
            )

            prompt = _INGEST_PROMPT_TEMPLATE.format(
                rules=_WIKI_RULES,
                wiki_state=wiki_state,
                interaction_type=interaction_type,
                user_message=(user_message or "")[:1500],
                bot_reply=(bot_reply or "(none)")[:1500],
                today=date.today().isoformat(),
            )

            aclient = _get_async_client()
            response = await aclient.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            # Strip code fences if Haiku added them despite instructions
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            try:
                decision = json.loads(raw)
            except json.JSONDecodeError as e:
                _ingest_logger.error(
                    f"user={user_id} JSON_PARSE_FAIL raw={raw[:400]!r} err={e}"
                )
                return

            updates = decision.get("updates") or []
            reasoning = decision.get("reasoning", "")

            _ingest_logger.info(
                f"user={user_id} type={interaction_type} "
                f"msg={user_message[:120]!r} "
                f"reason={reasoning!r} n_updates={len(updates)}"
            )

            applied_count = 0
            for upd in updates:
                try:
                    _apply_wiki_update(user_id, upd)
                    _ingest_logger.info(f"user={user_id} APPLIED {upd}")
                    applied_count += 1
                except Exception as e:
                    _ingest_logger.error(f"user={user_id} APPLY_FAIL upd={upd} err={e!r}")

    except Exception as e:
        _ingest_logger.error(f"user={user_id} INGEST_FAIL err={e!r}")
        return

    # Schedule a background lint pass OUTSIDE the lock if anything actually
    # changed.  Lint holds the same per-user wiki lock itself, so firing it
    # from inside this function's lock would deadlock.  This is fire-and-forget
    # by design — the user-facing reply has already been sent, so the lint
    # just tidies the wiki in the background without blocking anything.
    if applied_count > 0:
        _schedule_post_change_lint(user_id)


# ─────────────────────────────────────────────────────────────────────────────
# Post-change lint scheduler
#
# After every ingest that actually wrote something to the wiki, we kick off a
# background lint pass over profile/goals/patterns/wins (log.md is excluded).
# Replaces the old "only consolidate goals.md" fix-up — now any page gets the
# same tidy treatment right after it changes, not just once a week.
#
# Debounce: if a lint for this user is already pending/running, don't stack
# another one. The currently-running lint already sees the lock-serialized
# latest state of the wiki, so a second concurrent pass is just duplicate work.
# After the first lint finishes, the next ingest will trigger a fresh one if
# there are new changes to clean up.
# ─────────────────────────────────────────────────────────────────────────────

_pending_lints: set[int] = set()


def _schedule_post_change_lint(user_id: int) -> None:
    """
    Fire off a background lint pass for this user.  Skips if one is already
    pending for the same user (debounce: one lint per user at a time).
    Errors inside the lint are swallowed so a bad lint can't break anything.
    """
    if user_id in _pending_lints:
        _ingest_logger.info(f"user={user_id} post-change lint already pending, skipping")
        return
    try:
        loop = _asyncio.get_event_loop()
    except RuntimeError:
        _ingest_logger.error(
            "_schedule_post_change_lint called outside event loop; skipping"
        )
        return
    _pending_lints.add(user_id)
    task = loop.create_task(_run_post_change_lint(user_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_post_change_lint(user_id: int) -> None:
    """
    Thin wrapper around ``lint.lint_user_wiki`` that cleans up
    ``_pending_lints`` when the pass ends, whether it succeeded or raised.
    Imported lazily to avoid a circular import at module load time.
    """
    try:
        import lint  # local import keeps advisor<->lint decoupled at load time
        result = await lint.lint_user_wiki(user_id)
        _ingest_logger.info(f"user={user_id} post-change lint done: {result}")
    except Exception as e:
        _ingest_logger.error(f"user={user_id} post-change lint FAIL err={e!r}")
    finally:
        _pending_lints.discard(user_id)


def _apply_wiki_update(user_id: int, upd: dict) -> None:
    """
    Apply one structured update to the wiki.  Supported actions:
      - append       : add a new bullet to patterns/profile/goals/wins
      - remove_line  : remove an existing bullet whole (case-insensitive
                       substring match against bullet lines)
      - replace_line : find an existing bullet and rewrite it in place.
                       Use this when the user retracts ONE fact from a
                       bullet that currently contains MULTIPLE facts —
                       e.g. "- Does not eat olive oil; prefers healthy
                       fats" and the user only wants the olive-oil part
                       gone. remove_line would lose both facts.
      - log_entry    : add a dated entry to log.md

    Weekly lint is still the only pass that rewrites bulk content;
    remove_line / replace_line are surgical single-bullet operations
    driven by the user's own request, not global passes.

    After this function returns (and the wiki lock is released),
    ``ingest_interaction`` schedules a background lint pass over every
    lintable page — so dedup, superseded facts, stale time-bounded
    goals, and the calorie-goal canonical-line fix-up all happen
    automatically within a second or two of each change, not just
    on the weekly cron.
    """
    page = upd.get("page", "")
    # Be defensive: Haiku sometimes emits "goals.md" instead of "goals"
    # because the instructions reference pages by filename.  Strip any
    # .md extension so we match our canonical page list.
    if page.endswith(".md"):
        page = page[:-3]
    action = upd.get("action", "")

    if action == "append" and page in ("patterns", "profile", "goals", "wins"):
        content = (upd.get("content") or "").strip()
        if not content:
            return
        # Normalize the bullet prefix.  Haiku sometimes emits "- …", sometimes
        # "* …", sometimes "• …" (copying the style it sees in the page).  The
        # migrated profile template uses "•", so without this we'd end up with
        # double-bullet lines like "- • Likes dark chocolate".  Strip every
        # leading bullet-ish char and re-apply our canonical "- ".
        content = _re.sub(r"^[\s\-\*•·‣⁃]+", "", content).strip()
        if not content:
            return
        content = f"- {content}"
        # Strip the template's `_(Empty — ...)_` placeholder if it's still
        # sitting above real content.  Keeps the HTML <!-- ... --> comments
        # and the # Heading — only the "nothing here yet" sign comes down.
        # Self-healing: runs on every append, no-op once the line is gone.
        existing = wiki.strip_empty_placeholder(wiki.read_page(user_id, page)).rstrip()
        new_content = f"{existing}\n{content}\n"
        wiki.write_page(user_id, page, new_content)

        # Audit trail: every wiki append leaves a breadcrumb in log.md, same
        # convention as remove_line / lint / contradictions.  log.md is the
        # single chronological ledger of every change to the wiki, so you can
        # always trace when a line appeared (and later, if removed, when it
        # went).  Note: log.md is append-only — lint and every other pipeline
        # NEVER rewrite past entries, they only add new ones.
        wiki.append_log(user_id, f"Added to {page}.md", content)

        # Post-change tidying (dedup, superseded facts, stale time-bounded
        # goals, calorie-line consolidation) is handled by the background lint
        # pass scheduled from ingest_interaction after this function returns.

    elif action == "remove_line" and page in ("patterns", "profile", "goals", "wins"):
        # Delete one (or more) existing bullets from a page based on a
        # target substring Haiku picked.  Haiku sees the whole wiki in the
        # ingest prompt, so it can emit a target that uniquely matches the
        # line the user wants dropped — e.g. target="healthy fats" when
        # the existing line is "- [2026-04-01] Wants to eat more healthy
        # fats".  Case-insensitive substring match.  Only bullet lines
        # ('-', '*', '•') are eligible — headers, comments, blank lines,
        # and the HTML schema hints are always preserved.
        #
        # IMPORTANT: this deletes the WHOLE bullet.  If the bullet contains
        # multiple facts (e.g. "Does not eat olive oil; prefers healthy
        # fats") and the user wants only ONE of them gone, Haiku should
        # emit `replace_line` instead.
        target = (upd.get("target") or "").strip()
        if not target:
            return
        existing = wiki.read_page(user_id, page)
        if not existing:
            _ingest_logger.warning(
                f"user={user_id} remove_line page={page} NO_PAGE target={target!r}"
            )
            return

        target_lower = target.lower()
        out_lines = []
        removed_lines: list[str] = []
        for line in existing.split("\n"):
            stripped = line.lstrip()
            is_bullet = stripped.startswith(("- ", "* ", "• "))
            if is_bullet and target_lower in line.lower():
                removed_lines.append(line.strip())
                continue
            out_lines.append(line)

        if not removed_lines:
            _ingest_logger.warning(
                f"user={user_id} remove_line page={page} NOT_FOUND target={target!r}"
            )
            return

        if len(removed_lines) > 1:
            _ingest_logger.info(
                f"user={user_id} remove_line page={page} matched_multiple "
                f"removed={len(removed_lines)} target={target!r}"
            )

        # Collapse blank-line runs the removal may have created (3+ newlines → 2)
        new_content = _re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines))
        wiki.write_page(user_id, page, new_content)

        # Audit trail: every wiki retraction leaves a breadcrumb in log.md,
        # symmetric with the append branch above.  log.md is the single
        # chronological ledger of every change to the wiki — adds AND
        # removes — so "when did this line appear / disappear?" always has
        # an answer.
        summary = (
            f"Removed from {page}.md"
            if len(removed_lines) == 1
            else f"Removed {len(removed_lines)} lines from {page}.md"
        )
        wiki.append_log(user_id, summary, "\n".join(removed_lines))

    elif action == "replace_line" and page in ("patterns", "profile", "goals", "wins"):
        # Surgical in-place rewrite of a single bullet.  Finds a bullet by
        # case-insensitive substring on `target`, then swaps its content
        # to `new_content`.  Used when the user retracts ONE fact from a
        # bullet that contains multiple facts — we keep the remaining
        # fact instead of dropping the whole line.
        #
        # Contract: the new_content replaces the ENTIRE bullet (Haiku
        # supplies the `- [date] …` shape).  If multiple bullets match
        # `target`, we only rewrite the first to stay conservative; if
        # that's wrong we'd rather under-edit than over-edit.
        target = (upd.get("target") or "").strip()
        new_content_raw = (upd.get("new_content") or "").strip()
        if not target or not new_content_raw:
            return
        existing = wiki.read_page(user_id, page)
        if not existing:
            _ingest_logger.warning(
                f"user={user_id} replace_line page={page} NO_PAGE target={target!r}"
            )
            return

        # Normalize new_content the same way append does so we get one clean
        # "- " bullet regardless of what Haiku emitted.
        normalized = _re.sub(r"^[\s\-\*•·‣⁃]+", "", new_content_raw).strip()
        if not normalized:
            return
        normalized = f"- {normalized}"

        target_lower = target.lower()
        out_lines: list[str] = []
        replaced = False
        old_line = ""
        for line in existing.split("\n"):
            stripped = line.lstrip()
            is_bullet = stripped.startswith(("- ", "* ", "• "))
            if not replaced and is_bullet and target_lower in line.lower():
                # Preserve leading whitespace from the original line so the
                # rewritten bullet sits at the same indent level.
                indent = line[: len(line) - len(stripped)]
                old_line = line.strip()
                out_lines.append(f"{indent}{normalized}")
                replaced = True
                continue
            out_lines.append(line)

        if not replaced:
            _ingest_logger.warning(
                f"user={user_id} replace_line page={page} NOT_FOUND target={target!r}"
            )
            return

        new_page = _re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines))
        wiki.write_page(user_id, page, new_page)
        wiki.append_log(
            user_id,
            f"Rewrote a line in {page}.md",
            f"from: {old_line}\nto:   {normalized}",
        )

    elif action == "log_entry" and page == "log":
        summary = (upd.get("summary") or "").strip()
        details = (upd.get("details") or "").strip()
        if summary:
            wiki.append_log(user_id, summary, details)

    else:
        raise ValueError(f"Unknown update shape: page={page!r} action={action!r}")


def schedule_ingest(
    user_id: int,
    interaction_type: str,
    user_message: str,
    bot_reply: str = "",
) -> None:
    """
    Kick off a background ingest task.  Must be called from inside a running
    asyncio event loop (i.e. from a bot handler).  Does not await — returns
    immediately so the user-facing reply is never delayed.
    """
    try:
        loop = _asyncio.get_event_loop()
    except RuntimeError:
        _ingest_logger.error("schedule_ingest called outside event loop; skipping")
        return
    task = loop.create_task(
        ingest_interaction(user_id, interaction_type, user_message, bot_reply)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
