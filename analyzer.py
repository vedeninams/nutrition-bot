"""
analyzer.py — Claude vision API for food and nutrition label analysis.

Three entry points:
  analyze_food_photo(image_bytes, caption)  → list of food items with macros
  analyze_label_photo(image_bytes, caption) → single nutrition item from a label
  analyze_text(text)                        → parse a text description of food

All functions return a list of dicts ready to pass to database.log_meal_items().
"""

import anthropic
import base64
import json
import re
from typing import Optional
from dotenv import load_dotenv

load_dotenv()   # must happen before Anthropic() reads the env
client = anthropic.Anthropic()


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────

FOOD_SYSTEM_PROMPT = """You are a professional nutritionist and food analyst.
When given a photo of food, identify every distinct component on the plate and
estimate its nutritional values. Be realistic with portion sizes — people often
underestimate. If the user adds a caption (e.g. "I only ate half" or "this is 200g"),
use that to adjust your estimates.

IMPORTANT: You must reply with ONLY a JSON array. No prose, no explanation.
Each element must have exactly these keys:
  dish_name   — string, the NAME OF THE WHOLE DISH OR PLATE (e.g. "Udon Noodle Bowl",
                "Caesar Salad", "Overnight Oats"). ALL components of the same plate
                share the EXACT SAME dish_name. For a single standalone item (e.g. an
                apple, a coffee) dish_name = dish.
  dish        — string, the individual ingredient or component
                (e.g. "Udon noodles 150g", "Soft-boiled egg", "Broth 200ml")
  kcal        — number (integer or float)
  protein_g   — number
  fat_g       — number
  carbs_g     — number
  sugar_g     — number
  confidence  — one of: "high", "medium", "low"
  meal_type   — detect from the caption: "breakfast", "lunch", "dinner", "snack", or null if not mentioned

Example (a plate of udon + a side apple):
[
  {"dish_name": "Udon Noodle Bowl", "dish": "Udon noodles 150g", "kcal": 220, "protein_g": 7, "fat_g": 1, "carbs_g": 44, "sugar_g": 2, "confidence": "high", "meal_type": "lunch"},
  {"dish_name": "Udon Noodle Bowl", "dish": "Tofu 80g", "kcal": 90, "protein_g": 9, "fat_g": 5, "carbs_g": 2, "sugar_g": 0, "confidence": "high", "meal_type": "lunch"},
  {"dish_name": "Udon Noodle Bowl", "dish": "Broth 200ml", "kcal": 30, "protein_g": 2, "fat_g": 0, "carbs_g": 4, "sugar_g": 1, "confidence": "medium", "meal_type": "lunch"},
  {"dish_name": "Apple", "dish": "Apple 1 medium", "kcal": 80, "protein_g": 0, "fat_g": 0, "carbs_g": 21, "sugar_g": 15, "confidence": "high", "meal_type": "lunch"}
]

If you genuinely cannot identify anything, return an empty array [].
"""

LABEL_SYSTEM_PROMPT = """You are a nutrition label reader.
Read the nutritional information label in the photo carefully.
The user may say how much of the product they consumed (e.g. "I ate half", "150g").
If no portion is specified, assume one standard serving as shown on the label.

IMPORTANT: You must reply with ONLY a JSON array with ONE element.
Keys: dish_name, dish, kcal, protein_g, fat_g, carbs_g, sugar_g, confidence, meal_type
dish_name — the product name from the label (e.g. "König Käse", "Alpro Soy Yogurt")
dish      — same as dish_name for a packaged product (it's a single item)
meal_type — detect from the caption: "breakfast", "lunch", "dinner", "snack", or null if not mentioned.
confidence should be "high" if you could read the label clearly, "medium" otherwise.

If the label is unreadable, return: [{"dish_name": "Unknown product", "dish": "Unknown product", "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "sugar_g": 0, "confidence": "low", "meal_type": null}]
"""

TEXT_SYSTEM_PROMPT = """You are a nutritionist. The user described food in text.
Estimate the nutritional values based on the description.
If quantities are vague ("a bowl", "some"), make a realistic estimate for an average portion.

IMPORTANT: Reply with ONLY a JSON array. Each element must have these keys:
  dish_name   — the name of the whole dish/meal (e.g. "Oatmeal Bowl", "Caesar Salad").
                All ingredients of the same dish share the SAME dish_name.
                For a single standalone item, dish_name = dish.
  dish        — the individual ingredient or item (e.g. "Rolled oats 80g", "Banana slices")
  kcal, protein_g, fat_g, carbs_g, sugar_g, confidence, meal_type

For meal_type detect it from the user's words:
- "for breakfast", "breakfast" → "breakfast"
- "for lunch", "lunch" → "lunch"
- "for dinner", "dinner", "supper" → "dinner"
- "snack", "quick bite" → "snack"
- If not mentioned, use null (the app will guess from time of day)

Example — "I had oatmeal with banana and honey, and also a coffee":
[
  {"dish_name": "Oatmeal Bowl", "dish": "Rolled oats 80g", "kcal": 300, "protein_g": 10, "fat_g": 5, "carbs_g": 54, "sugar_g": 1, "confidence": "medium", "meal_type": null},
  {"dish_name": "Oatmeal Bowl", "dish": "Banana 1 medium", "kcal": 89, "protein_g": 1, "fat_g": 0, "carbs_g": 23, "sugar_g": 12, "confidence": "high", "meal_type": null},
  {"dish_name": "Oatmeal Bowl", "dish": "Honey 1 tsp", "kcal": 21, "protein_g": 0, "fat_g": 0, "carbs_g": 6, "sugar_g": 6, "confidence": "high", "meal_type": null},
  {"dish_name": "Coffee with milk", "dish": "Coffee with milk", "kcal": 30, "protein_g": 1, "fat_g": 1, "carbs_g": 3, "sugar_g": 3, "confidence": "medium", "meal_type": null}
]
"""


# ─────────────────────────────────────────────────────────────────────────────
# JSON extraction helper
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> list[dict]:
    """
    Pull a JSON array out of a Claude response.
    Handles cases where Claude wraps it in markdown code fences.
    """
    # Strip markdown fences if present
    clean = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()

    # Find the first [...] block
    start = clean.find("[")
    end = clean.rfind("]")
    if start == -1 or end == -1:
        return []

    try:
        items = json.loads(clean[start:end + 1])
        if not isinstance(items, list):
            return []
        # Normalise keys — fill in missing ones with 0
        result = []
        for item in items:
            dish = str(item.get("dish", "Unknown"))
            dish_name = str(item.get("dish_name") or dish)  # fallback to dish if absent
            result.append({
                "dish_name":  dish_name,
                "dish":       dish,
                "kcal":       float(item.get("kcal", 0)),
                "protein_g":  float(item.get("protein_g", 0)),
                "fat_g":      float(item.get("fat_g", 0)),
                "carbs_g":    float(item.get("carbs_g", 0)),
                "sugar_g":    float(item.get("sugar_g", 0)),
                "confidence": item.get("confidence", "medium"),
            })
        return result
    except (json.JSONDecodeError, ValueError):
        return []


def _encode_image(image_bytes: bytes) -> str:
    return base64.standard_b64encode(image_bytes).decode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Detect whether a photo shows a nutrition label
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_label(caption: str) -> bool:
    """Simple keyword check so users don't have to type 'label'."""
    caption_lower = (caption or "").lower()
    keywords = ["label", "package", "packaging", "nährwert", "nahrwert",
                "этикетка", "упаковка", "состав", "100g", "100 g", "per 100"]
    return any(k in caption_lower for k in keywords)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def analyze_food_photo(
    image_bytes: bytes,
    caption: Optional[str] = None,
    media_type: str = "image/jpeg",
) -> list[dict]:
    """
    Analyze a food photo.
    Returns a list of items ready for database.log_meal_items().
    """
    user_text = caption or "Please analyze this food photo."
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=FOOD_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": _encode_image(image_bytes),
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )
    return _extract_json(response.content[0].text)


def analyze_label_photo(
    image_bytes: bytes,
    caption: Optional[str] = None,
    media_type: str = "image/jpeg",
) -> list[dict]:
    """
    Analyze a nutrition label photo.
    Caption may include portion info like "I ate half" or "150g".
    """
    user_text = caption or "Please read this nutrition label."
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        system=LABEL_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": _encode_image(image_bytes),
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )
    return _extract_json(response.content[0].text)


def analyze_photo(
    image_bytes: bytes,
    caption: Optional[str] = None,
    media_type: str = "image/jpeg",
) -> tuple[list[dict], str]:
    """
    Smart router: detect label vs food photo automatically.
    Returns (items, source) where source is 'label' or 'photo'.
    """
    if _looks_like_label(caption or ""):
        items = analyze_label_photo(image_bytes, caption, media_type)
        return items, "label"
    else:
        items = analyze_food_photo(image_bytes, caption, media_type)
        return items, "photo"


def analyze_text(text: str) -> list[dict]:
    """
    Parse a text description of food into nutrition items.
    E.g. "I had oatmeal with banana and a coffee with milk"
    Uses Haiku — fast and cheap for pure text parsing.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=TEXT_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": text}
        ],
    )
    return _extract_json(response.content[0].text)


# ─────────────────────────────────────────────────────────────────────────────
# Natural-language correction resolver
# ─────────────────────────────────────────────────────────────────────────────

CORRECTION_SYSTEM_PROMPT = """You are a nutritionist assistant helping the user correct logged meal entries.

The user will describe what needs to change in plain language.
You have the recent meal history shown below. Each item has a dish_name (the whole dish)
and a dish (the individual ingredient).

{batch_section}

FOUR types of corrections are possible:

1. Single item correction — changing quantity, name, or calories of one specific item:
   Return: {{"action": "update", "meal_id": <id>, "updates": {{"dish": ..., "kcal": ..., etc}}, "reason": "..."}}

2. Meal type reclassification — user says a dish was breakfast/lunch/dinner/snack:
   Return: {{"action": "update_many", "meal_ids": [<id1>, <id2>, ...], "updates": {{"meal_type": "breakfast"}}, "reason": "..."}}
   Include ALL ids that share the same dish_name.

3. Delete a single item:
   Return: {{"action": "delete", "meal_id": <id>, "updates": {{}}, "reason": "..."}}

4. Delete a whole dish (all its ingredients) — user wants to remove a dish by name or a recent batch:
   Return: {{"action": "delete_many", "meal_ids": [<id1>, <id2>, ...], "dish_name": "<dish name>", "updates": {{}}, "reason": "..."}}
   Use when:
   - User names a dish: "remove the udon noodles", "delete the salad" → find all ids with that dish_name
   - User says "remove this dish", "delete what I just added", "that was wrong", "remove all of that"
     → use ALL ids from the LAST LOGGED BATCH if present, or the most recent dish_name group

5. Scale a whole dish by a fraction — user ate only part of it:
   Return: {{"action": "scale_dish", "dish_name": "<dish name>", "factor": <number 0.1–0.9>, "updates": {{}}, "reason": "..."}}
   Use when the user says things like:
   - "I only ate half the udon bowl" → factor: 0.5
   - "actually I had two thirds of the salad" → factor: 0.67
   - "I only ate a quarter of that" → factor: 0.25
   - "I had 3/4 of the dish" → factor: 0.75
   Always return the dish_name exactly as it appears in the meal history.
   factor must be a decimal between 0.1 and 0.9.

If you cannot match to any meal:
   Return: {{"action": "none", "meal_id": null, "updates": {{}}, "reason": "Could not identify meal"}}

Meal history (JSON — includes dish_name field):
{history}
"""

def resolve_correction(
    user_message: str,
    recent_meals: list[dict],
    last_batch: Optional[list[dict]] = None,
) -> dict:
    """
    Given a correction message like "it was a whole avocado not half",
    figure out which meal_id to update and what to change.

    last_batch: items from the most recent 'logging session' (same photo/message).
                When provided, the prompt explicitly highlights them so Claude
                knows exactly which IDs form "the dish I just added".

    Returns a dict with: meal_id, updates, reason, action.
    """
    history_json = json.dumps(recent_meals, indent=2, default=str)

    # Build the batch section shown at the top of the prompt
    if last_batch and len(last_batch) > 1:
        batch_ids = [item["id"] for item in last_batch]
        batch_names = ", ".join(item.get("dish", "?") for item in last_batch)
        batch_section = (
            f"LAST LOGGED BATCH — these {len(last_batch)} items were logged together "
            f"(same photo/message) and form ONE dish/meal:\n"
            f"  IDs: {batch_ids}\n"
            f"  Items: {batch_names}\n\n"
            f"If the user says 'remove this dish', 'remove what I just added', "
            f"'that was wrong', 'delete all of that', or similar, "
            f"use delete_many with ALL of these IDs."
        )
    else:
        batch_section = ""

    system = (
        CORRECTION_SYSTEM_PROMPT
        .replace("{batch_section}", batch_section)
        .replace("{history}", history_json)
    )

    # Haiku — fast and cheap for text/JSON matching
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[
            {"role": "user", "content": user_message}
        ],
    )
    text = response.content[0].text
    clean = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()

    try:
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        return {"meal_id": None, "updates": {}, "reason": "Parse error", "action": "none"}


# ─────────────────────────────────────────────────────────────────────────────
# Intent detection: is this a correction or a new log?
# ─────────────────────────────────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """You are a router for a nutrition tracking bot.
Classify the user's message into exactly one of these intents:

  log_text    — describing food they ate (e.g. "I had oatmeal with banana", "for breakfast I ate eggs")
  correction  — fixing or deleting a previous log entry (e.g. "actually it was a whole avocado", "remove the yogurt", "that was wrong")
  question    — asking for advice, information, or analysis (e.g. "how much protein today?", "what should my calorie goal be?", "is my diet balanced?", "what should I eat for dinner?")
  cmd_today   — wants to see today's food log summary (e.g. "show today", "what did I eat today", "today's summary")
  cmd_week    — wants weekly food log review (e.g. "how was my week", "weekly summary", "show this week")
  cmd_goal    — wants to CHANGE or SET their calorie goal to a specific number (e.g. "set my goal to 1800", "change my goal to 2200 calories") — NOT asking what it should be
  preference  — sharing a personal preference, restriction, or fact about themselves to be remembered (e.g. "I don't eat fish", "I'm vegetarian", "I'm allergic to nuts", "remember I go to the gym 3x a week", "I weigh 67kg", "my height is 165cm")

IMPORTANT:
- "what should my goal be?" → question
- "set my goal to 1800" → cmd_goal (only when setting a specific number)
- "I don't eat fish", "please remember I hate cilantro", "I'm lactose intolerant" → preference
- "wait, actually this was breakfast" → question
- correction is ONLY when changing a specific logged food item

Reply with ONLY the intent word, nothing else. No explanation, no punctuation."""


# Prefix tokens used by the iPhone Shortcuts — detected before calling intent API
HEALTH_PREFIX = "📊 health"
WORKOUT_PREFIX = "🏋️ workouts"


def detect_intent(text: str) -> str:
    """
    Uses Haiku to classify intent — works in any language, any phrasing.
    Falls back to 'log_text' if the API call fails.
    Shortcuts messages are detected by prefix before hitting the API.
    """
    if text.strip().startswith("/"):
        return "command"

    # iPhone Shortcuts send structured messages with these prefixes
    stripped = text.strip()
    if stripped.startswith(HEALTH_PREFIX):
        return "health_update"
    if stripped.startswith(WORKOUT_PREFIX):
        return "workout_log"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=INTENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        intent = response.content[0].text.strip().lower()
        valid = {"log_text", "correction", "question", "cmd_today", "cmd_week", "cmd_goal", "preference"}
        return intent if intent in valid else "log_text"
    except Exception:
        t = text.lower()
        if any(s in t for s in ["fix", "wrong", "actually", "remove", "delete", "исправ", "удали"]):
            return "correction"
        if t.endswith("?") or any(t.startswith(s) for s in ["how", "what", "сколько"]):
            return "question"
        return "log_text"


def parse_health_message(text: str) -> dict:
    """
    Parse the structured health update sent by the iPhone Shortcut.

    Expected format (sent by Shortcut):
        📊 health
        steps: 8432
        weight: 66.8 kg
        date: 2024-01-15

    Returns dict with any of: steps (int), weight_kg (float), date (str YYYY-MM-DD).
    Missing fields are omitted from the dict.
    """
    result = {}
    for line in text.splitlines():
        line = line.strip().lower()
        if line.startswith("steps:"):
            try:
                result["steps"] = int(re.sub(r"[^\d]", "", line.split(":", 1)[1]))
            except (ValueError, IndexError):
                pass
        elif line.startswith("weight:"):
            try:
                val = re.search(r"[\d.]+", line.split(":", 1)[1])
                if val:
                    result["weight_kg"] = float(val.group())
            except (ValueError, IndexError):
                pass
        elif line.startswith("date:"):
            try:
                result["date"] = line.split(":", 1)[1].strip()
            except IndexError:
                pass
    return result


def parse_workout_message(text: str) -> list[dict]:
    """
    Parse the structured workout list sent by the iPhone Shortcut.

    Expected format:
        🏋️ workouts
        Strength training — 60 min
        Yoga — 45 min

    Returns list of dicts: [{name: str, duration_min: int}, ...]
    """
    workouts = []
    lines = text.splitlines()
    for line in lines[1:]:   # skip the header line
        line = line.strip().lstrip("-•").strip()
        if not line:
            continue
        # Extract duration (any number followed by min/mins/minutes)
        dur_match = re.search(r"(\d+)\s*min", line, re.IGNORECASE)
        duration = int(dur_match.group(1)) if dur_match else None
        # Strip duration from name
        name = re.sub(r"[-–—]\s*\d+\s*min\w*", "", line, flags=re.IGNORECASE).strip()
        name = re.sub(r"\d+\s*min\w*", "", name, flags=re.IGNORECASE).strip().strip("-–—").strip()
        if name:
            workouts.append({"name": name, "duration_min": duration})
    return workouts


def estimate_activity_calories(
    steps: Optional[int],
    workouts: list[dict],
    weight_kg: float = 70.0,
    user_profile: str = "",
) -> dict:
    """
    Estimate TOTAL daily calories burned:
      1. BMR — calories burned just being alive (Mifflin-St Jeor formula)
         adjusted for a sedentary desk job (×1.2 multiplier)
      2. Walking — extra calories from steps above baseline
      3. Workouts — calories from sport sessions (type + duration)

    Total = BMR_sedentary + walking_kcal + workout_kcal

    user_profile is the free-text profile — Claude extracts age, gender, height
    from it. Falls back to sensible defaults if info is missing.

    Returns:
      {
        "bmr_kcal": int,          # resting + sedentary baseline
        "walking_kcal": int,
        "workout_kcal": int,
        "total_kcal": int,        # sum of all three
        "workouts": [{name, duration_min, kcal_est, intensity_note}],
        "summary": "human-readable line",
        "missing_info": "note if age/gender/height were assumed"
      }
    """
    system = """You are a sports science and metabolism expert.

Your task: estimate a person's TOTAL calorie burn for one day in three parts.

PART 1 — BMR × sedentary multiplier
Use the Mifflin-St Jeor formula:
  Men:   BMR = 10×weight_kg + 6.25×height_cm − 5×age + 5
  Women: BMR = 10×weight_kg + 6.25×height_cm − 5×age − 161
Multiply BMR by 1.2 for a sedentary desk job (light daily movement, no planned exercise).
Extract age, gender, height from the user profile if available.
If any value is missing, use these defaults and note it: age=35, gender=female, height=165cm.

PART 2 — Walking calories (from steps)
Steps represent ALL walking during the day, some of which is already in the sedentary baseline.
Estimate ADDITIONAL calories above the baseline:
  extra_walking_kcal ≈ steps × 0.035 × (weight_kg / 70)
This is conservative to avoid double-counting with the sedentary multiplier.

PART 3 — Workout calories
Estimate per session:
  Strength training: 5–8 kcal/min (moderate: ~6)
  HIIT / CrossFit / circuit: 10–14 kcal/min
  Running / cardio: 8–12 kcal/min
  Yoga / Pilates / stretching: 3–5 kcal/min
  Cycling: 8–12 kcal/min
  Swimming: 8–11 kcal/min
  Walking workout (dedicated): 4–5 kcal/min
Scale by (weight_kg / 70) for all workout estimates.

Respond with ONLY a JSON object — no prose, no explanation:
{
  "bmr_kcal": <int>,
  "walking_kcal": <int, 0 if no steps>,
  "workout_kcal": <int, 0 if no workouts>,
  "total_kcal": <int, sum of bmr + walking + workout>,
  "workouts": [
    {"name": "...", "duration_min": <int or null>, "kcal_est": <int>, "intensity_note": "brief note"}
  ],
  "assumed": "<list any values assumed due to missing profile info, or empty string>",
  "summary": "e.g. 'BMR 1,680 + 7,200 steps (~180 kcal) + 60 min strength (~390 kcal) = ~2,250 kcal total'"
}"""

    prompt_parts = []
    if user_profile:
        prompt_parts.append(f"User profile (extract age, gender, height from this):\n{user_profile}")
    else:
        prompt_parts.append("User profile: not available — use defaults (age 35, female, height 165cm)")

    prompt_parts.append(f"Current weight: {weight_kg:.1f} kg")

    if steps:
        prompt_parts.append(f"Steps today: {steps:,}")
    else:
        prompt_parts.append("Steps today: not recorded")

    if workouts:
        prompt_parts.append("Workouts today:")
        for w in workouts:
            dur = f"{w['duration_min']} min" if w.get("duration_min") else "duration unknown"
            prompt_parts.append(f"  - {w['name']} — {dur}")
    else:
        prompt_parts.append("Workouts today: none")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
    )
    raw = response.content[0].text.strip()
    clean = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    try:
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        return {
            "bmr_kcal": 0, "walking_kcal": 0, "workout_kcal": 0, "total_kcal": 0,
            "workouts": workouts, "summary": "Could not estimate", "assumed": ""
        }


def update_profile(current_profile: str, new_message: str) -> dict:
    """
    Given the user's existing free-text profile and a new message about
    themselves, return a dict:
      {
        "understood": True/False,
        "profile": "<updated profile text>",   # only meaningful if understood=True
        "ask": "<clarification question>"       # only present if understood=False
      }

    Claude will:
    - Try to decode the message even if it looks like garbled keyboard input
    - Intelligently merge the new info into the existing profile
    - Flag if the text is completely unreadable

    Uses Haiku — fast and cheap for pure text tasks.
    """
    system = """You maintain a concise user profile for a nutrition tracking bot.

You will receive the current profile and a new message from the user about themselves.
The message may sometimes contain typos, wrong keyboard layout (e.g. Cyrillic characters
when the user meant to type in English), or mixed languages. Try to interpret the intent.

Respond with a JSON object:
  If you can understand the message (even partially):
    {"understood": true, "profile": "<full updated profile text>"}
  If the message is completely unreadable and has no recoverable meaning:
    {"understood": false, "ask": "Short friendly clarification question in the same language as the user's message"}

Rules for updating the profile:
- If the new info contradicts something, update it (e.g. "I eat fish now" removes "doesn't eat fish")
- If the new info adds something new, append it
- Keep the profile concise — short sentences or bullet points, no padding
- If the current profile is empty, create a new one from the message
- Do NOT add a header or label — just the raw profile content
- Return ONLY the JSON object, nothing else"""

    prompt = f"""Current profile:
{current_profile if current_profile.strip() else "(empty)"}

New message from user:
{new_message}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    clean = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    try:
        result = json.loads(clean)
        if "understood" in result:
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: assume understood, use whatever Claude returned as the profile
    return {"understood": True, "profile": raw}


def extract_goal_from_text(text: str):
    """
    Pull a calorie number out of a natural language goal message.
    E.g. "set my goal to 1800 calories" → 1800
    Returns int or None.
    """
    import re
    match = re.search(r'\b(\d{3,5})\b', text)
    if match:
        val = int(match.group(1))
        if 500 <= val <= 10000:
            return val
    return None
