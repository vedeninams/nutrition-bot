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
  dish_name   — string, the NAME OF THE WHOLE DISH OR PLATE. ALL components of the
                same plate share the EXACT SAME dish_name.
                Rules by meal type:
                - BREAKFAST: name by ingredient count:
                    ≤ 3 ingredients  → "Small Breakfast"
                    4–6 ingredients  → "Medium Breakfast"
                    ≥ 7 ingredients  → "Big Breakfast"
                  If the caption doesn't say "breakfast", use "Small/Medium/Big Plate"
                  and the system will correct it later.
                - LUNCH / DINNER / SNACK: use a descriptive food name, short and clear.
                    e.g. "Chicken Power Bowl", "Caesar Salad", "Udon Bowl", "Oat Bowl"
                  Never use "Lunch", "Dinner", or "Snack" as the dish_name.
                - SINGLE STANDALONE ITEM (one apple, one coffee, one yogurt):
                  dish_name = dish.
  dish        — string, the individual ingredient or component. Keep it SHORT.
                ALWAYS include a gram (or ml) weight for every ingredient — estimate
                if not visible. Whole items get typical weights (egg ≈ 60g, small
                avocado half ≈ 70g, banana ≈ 120g).
                Examples: "Feta cheese 50g" → "Feta 50g"
                          "Sesame seeds & nigella seeds 5g" → "Sesame & nigella 5g"
                          "Bulgur/couscous salad (kisir) 100g" → "Bulgur & couscous 100g"
                          "Mixed greens (spinach/arugula) 50g" → "Mixed greens 50g"
                          "Dressing (small pot, est. 30ml)" → "Dressing 30ml"
                          "Hard-boiled egg 1 whole" → "Boiled egg 60g"
                          "Diced chicken breast 120g" → "Chicken breast 120g"
                          "Avocado 1/2 small" → "Avocado 70g"

                COUNTABLE MULTI-PIECE ITEMS — when you see 2+ discrete countable
                pieces of the same thing (2 fried eggs, 3 pancakes, 4 cookies,
                5 shrimp, 6 dumplings, 2 slices of bread), output ONE row with
                a SINGULAR name and embed the count in the dish string as
                "× Npcs" RIGHT BEFORE the total weight.
                Format: "<Singular name> × <N>pcs <total_weight>g"
                Examples: 2 fried eggs (≈60g each) → "Fried egg × 2pcs 120g"
                          3 pancakes (≈50g each)  → "Pancake × 3pcs 150g"
                          4 shrimp (≈15g each)    → "Shrimp × 4pcs 60g"
                          1 egg → just "Fried egg 60g" (no × marker when N=1)
                kcal / protein_g / fat_g / carbs_g / sugar_g are the TOTAL
                for all N pieces — not per-piece.

                BULK / AMORPHOUS items (rice, oil, greens, sauce, yogurt, soup,
                diced veg, grated cheese) → never use "× Npcs". Just ONE row
                with the total weight.
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
  {"dish_name": "Apple", "dish": "Apple 120g", "kcal": 80, "protein_g": 0, "fat_g": 0, "carbs_g": 21, "sugar_g": 15, "confidence": "high", "meal_type": "lunch"}
]

Example with a countable multi-piece item — breakfast with 2 fried eggs + avocado:
[
  {"dish_name": "Medium Breakfast", "dish": "Fried egg × 2pcs 120g", "kcal": 180, "protein_g": 12, "fat_g": 14, "carbs_g": 0, "sugar_g": 0, "confidence": "high", "meal_type": "breakfast"},
  {"dish_name": "Medium Breakfast", "dish": "Avocado 80g", "kcal": 128, "protein_g": 2, "fat_g": 12, "carbs_g": 7, "sugar_g": 1, "confidence": "high", "meal_type": "breakfast"}
]

If you genuinely cannot identify anything, return an empty array [].
"""

LABEL_SYSTEM_PROMPT = """You are a nutrition label reader.
Read the nutritional information label in the photo carefully.
The user may say how much of the product they consumed (e.g. "I ate half", "150g").
If no portion is specified, assume one standard serving as shown on the label.

IMPORTANT: You must reply with ONLY a JSON array with ONE element.
Keys: dish_name, dish, kcal, protein_g, fat_g, carbs_g, sugar_g, confidence, meal_type
dish_name — the product name from the label, short and clean (e.g. "König Käse", "Alpro Soy Yogurt")
dish      — same as dish_name for a packaged product. Keep it short — drop redundant words.
meal_type — detect from the caption: "breakfast", "lunch", "dinner", "snack", or null if not mentioned.
confidence should be "high" if you could read the label clearly, "medium" otherwise.

If the label is unreadable, return: [{"dish_name": "Unknown product", "dish": "Unknown product", "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "sugar_g": 0, "confidence": "low", "meal_type": null}]
"""

TEXT_SYSTEM_PROMPT = """You are a nutritionist. The user described food in text.
Estimate the nutritional values based on the description.
If quantities are vague ("a bowl", "some"), make a realistic estimate for an average portion.

IMPORTANT: Reply with ONLY a JSON array. Each element must have these keys:
  dish_name   — the name of the whole dish/meal. All ingredients of the same dish share the SAME dish_name.
                - BREAKFAST: use size prefix based on ingredient count:
                    ≤3 → "Small Breakfast", 4–6 → "Medium Breakfast", ≥7 → "Big Breakfast"
                  If not clear it's breakfast, use "Small/Medium/Big Plate".
                - LUNCH / DINNER / SNACK: descriptive food name (e.g. "Chicken Bowl", "Pasta").
                  Never use "Lunch", "Dinner", or "Snack" as dish_name.
                - Single standalone item: dish_name = dish.
  dish        — the individual ingredient, SHORT + always include a gram/ml weight.
                Estimate weight for whole items (egg ≈ 60g, banana ≈ 120g, small avocado half ≈ 70g).
                "Rolled oats 80g" → "Oats 80g", "Banana 1 medium" → "Banana 120g",
                "Hard-boiled egg 1 whole" → "Boiled egg 60g", "Feta cheese 50g" → "Feta 50g"

                COUNTABLE MULTI-PIECE ITEMS — when the user mentions 2+ discrete
                pieces ("3 fried eggs", "two pancakes", "4 cookies", "5 shrimp",
                "2 bananas"), output ONE row with a SINGULAR name and embed the
                count as "× Npcs" RIGHT BEFORE the total weight.
                Format: "<Singular name> × <N>pcs <total_weight>g"
                Examples: "3 fried eggs" → "Fried egg × 3pcs 180g"
                          "2 pancakes"   → "Pancake × 2pcs 100g"
                          "5 shrimp"     → "Shrimp × 5pcs 75g"
                          "1 egg"        → "Fried egg 60g" (no × marker when N=1)
                kcal / macros are the TOTAL for all N pieces.
                BULK items (rice, oil, greens, sauce, yogurt, soup) stay as ONE
                row with the total weight — never use "× Npcs" for bulk foods.
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
            # meal_type from caption always wins — kept as-is (string or None)
            meal_type = item.get("meal_type") or None
            result.append({
                "dish_name":  dish_name,
                "dish":       dish,
                "kcal":       float(item.get("kcal", 0)),
                "protein_g":  float(item.get("protein_g", 0)),
                "fat_g":      float(item.get("fat_g", 0)),
                "carbs_g":    float(item.get("carbs_g", 0)),
                "sugar_g":    float(item.get("sugar_g", 0)),
                "confidence": item.get("confidence", "medium"),
                "meal_type":  meal_type,
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

The user may request ONE or MULTIPLE corrections in a single message (e.g. "change feta to 70g and remove hummus").
You have the recent meal history shown below. Each item has a dish_name (the whole dish)
and a dish (the individual ingredient).

USING CONVERSATION CONTEXT (IMPORTANT):
You receive a short conversation history above — prior user turns and bot
replies, including compact summaries of what a just-sent photo logged
(e.g. "[Photo logged (photo): Natural Yoghurt 300g (204 kcal) — total 204 kcal]"
or "[Photo logged (photo): Fried egg × 2pcs 120g (180 kcal), Avocado 80g
(128 kcal) — total 308 kcal]"). USE that context — the user's correction
is almost always about what was JUST logged in the previous turn.

Terse corrections without an explicit dish name are the common case. Read
the conversation to find the referent; do NOT return action=none just
because the latest message is short.

Examples (each follows a photo log in the conversation):
  bot logged Natural Yoghurt 300g → "Change to 400g"
    → scale_dish_grams, dish_name="Natural Yoghurt", target_grams=400
  bot logged Natural Yoghurt 300g → "make it 250g"
    → scale_dish_grams, dish_name="Natural Yoghurt", target_grams=250
  bot logged Natural Yoghurt 300g → "remove it"
    → delete (or delete_many with the matching id from history)
  bot logged Natural Yoghurt 300g → "that was breakfast"
    → update_many with meal_type="breakfast" for that dish_name
  bot logged 2 fried eggs → "three eggs"
    → add_items for one more egg
  bot logged an Udon Bowl (6 items) → "remove that"
    → delete_many for all ids of that dish_name

Always cross-reference the conversation with the meal history JSON below
to pick the correct IDs and dish_name.

{batch_section}

Return a JSON ARRAY — one object per correction, even if there is only one.
Example for two corrections: [{{...}}, {{...}}]
Example for one correction: [{{...}}]

The following action types are available:

1. Single item correction — changing quantity, name, or calories of one specific item:
   Return: {{"action": "update", "meal_id": <id>, "updates": {{"dish": ..., "kcal": ..., etc}}, "reason": "..."}}

2. Meal type reclassification — user says a dish was breakfast/lunch/dinner/snack:
   Return: {{"action": "update_many", "meal_ids": [<id1>, <id2>, ...], "updates": {{"meal_type": "breakfast"}}, "reason": "..."}}
   Include ALL ids that share the same dish_name.

3. Delete a single item:
   Return: {{"action": "delete", "meal_id": <id>, "updates": {{}}, "reason": "..."}}

4. Delete a whole dish (all its ingredients) — user wants to remove a dish by name or a recent batch:
   Return: {{"action": "delete_many", "meal_ids": [<id1>, <id2>, ...], "dish_name": "<dish name>", "updates": {{}}, "reason": "..."}}
   CRITICAL RULES for delete_many:
   - meal_ids MUST always be populated — include ALL meal ids that belong to this dish.
     Even if you match by dish_name, always resolve and list every individual id.
     The ids are the primary deletion mechanism; dish_name is just the display label.
   - dish_name should be the EXACT name from the meal history (copy it verbatim).
     If the user used an approximate/misspelled name (e.g. "Syrniky" instead of
     "Syrniki with Cherries 200g"), still use the EXACT name from history for dish_name,
     but resolve the correct meal_ids by semantic matching.
   - Use when:
     · User names a dish approximately: "remove the Syrniky", "delete the salad", "the udon bowl"
       → semantically match to the closest dish in history, return its ids
     · User says "remove this dish", "delete what I just added", "that was wrong", "remove all of that"
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

6. Scale a dish to a specific total gram weight — user says how many grams they actually ate:
   Return: {{"action": "scale_dish_grams", "dish_name": "<exact dish name>", "target_grams": <number>, "updates": {{}}, "reason": "..."}}
   Use when the user specifies a total gram amount for a dish they already logged:
   - "I ate 600g of that bowl" → target_grams: 600
   - "I only had 300g of the salad" → target_grams: 300
   - "the portion was 400 grams" → target_grams: 400
   - User posts a photo and says "I ate 600 grams" → target_grams: 600, dish_name from last batch
   The system will compute the scale factor automatically from (target_grams / current_logged_grams).
   Always use the dish_name exactly as it appears in history.

7. Remove duplicate logs of the same dish — keep the first occurrence, delete later ones:
   Return: {{"action": "delete_duplicates", "dish_name": "<exact dish name>", "updates": {{}}, "reason": "..."}}
   Use when the user says things like "remove duplicate salad bowl entries", "delete the duplicate X",
   "remove extra X logs", "I logged X twice", "remove duplicate entries".
   Match the dish_name exactly as it appears in the meal history.

8. Add MORE of something that's already in the recent log — user is correcting the count upward:
   Return: {{"action": "add_items", "dish_name": "<exact dish name from history>", "items": [<full item dict>, ...], "reason": "..."}}
   Each item in `items` must be a complete nutrition dict with the same keys as the existing
   log rows: {{"dish_name": ..., "dish": ..., "kcal": ..., "protein_g": ..., "fat_g": ...,
   "carbs_g": ..., "sugar_g": ..., "meal_type": ...}}.

   Numbers must be PER-PIECE (for the single new piece you're adding), not copied totals:
   - If the existing row is "Fried egg × 2pcs 120g / 180 kcal / 12g protein" (2 eggs worth),
     one extra egg is 120/2=60g, 180/2=90 kcal, 12/2=6g protein — divide by the N in "× Npcs".
   - If the existing row is plain "Fried egg 60g / 90 kcal / 6g protein" (1 egg),
     copy those numbers directly for the new egg.

   The dish STRING for each new item must be CLEAN and per-piece — just the
   singular name plus the per-piece weight, NO "× Npcs" marker and NO "1pc"
   text. Examples:
   - Existing "Fried egg × 2pcs 120g" + add 1 egg → new item's dish = "Fried egg 60g"
   - Existing "Pancake × 3pcs 150g" + add 2 pancakes → two items, each dish = "Pancake 50g"
   The display layer adds the "× Npcs" marker back automatically when it merges
   the rows for display — don't try to format it yourself.

   Use when the user says things like:
   - Bot logged 2 fried eggs → "there are three eggs" / "actually three eggs"
     → add ONE more fried egg item (difference = 3 − 2 = 1)
   - Bot logged 1 slice of bread → "I had two slices"
     → add ONE more bread slice item
   - Bot logged 100g rice → "it was 150g rice"
     → this is UPDATE (single row quantity change), NOT add_items.
     add_items is for DISCRETE countable things (eggs, slices, pieces), not weights.
   CRITICAL: set dish_name EXACTLY as it appears in existing history so the new rows
   join the same meal/dish grouping. Leave meal_type the same as the existing rows.

If you cannot match a specific correction to any meal, include:
   {{"action": "none", "meal_id": null, "updates": {{}}, "reason": "Could not identify meal"}}

Always return a valid JSON array. No prose, no explanation outside the array.

Meal history (JSON — includes dish_name field):
{history}
"""

def resolve_correction(
    user_message: str,
    recent_meals: list[dict],
    last_batch: Optional[list[dict]] = None,
    conversation: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Given a correction message (possibly containing multiple corrections),
    return a LIST of action dicts — one per requested change.

    E.g. "change feta to 70g and remove hummus" → [update_action, delete_action]

    last_batch: items from the most recent logging session (same photo).
                Highlighted in the prompt so Claude knows what "this dish" means.
    conversation: short-term conversation memory (list of {role, content}
                  dicts). When supplied, it's used as the messages list so
                  the resolver can disambiguate "two eggs" from the recent
                  back-and-forth. Should end with the user's correction
                  message as the last turn.
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

    # Prefer the full conversation window if supplied (it already ends with
    # the user's correction message). Otherwise fall back to a single-turn
    # call with just the correction text.
    messages = conversation if conversation else [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        system=system,
        messages=messages,
    )
    text = response.content[0].text
    clean = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()

    try:
        parsed = json.loads(clean)
        # Normalise: always return a list
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return [{"meal_id": None, "updates": {}, "reason": "Parse error", "action": "none"}]


# ─────────────────────────────────────────────────────────────────────────────
# Intent detection: is this a correction or a new log?
# ─────────────────────────────────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """You are a router for a nutrition tracking bot.
Classify the user's LATEST message into exactly one of these intents.

USING CONVERSATION CONTEXT:
You may receive a short conversation history above the latest message (prior
user turns and bot replies, including compact summaries of what a just-sent
photo logged — e.g. "[Photo logged (photo): Fried egg 60g (90 kcal), Fried
egg 60g (90 kcal), ...]"). USE that context — the same words mean different
things depending on what just happened. In particular:

- If the bot just logged food and the user's next message factually contradicts
  or adjusts that log (quantity, count, meal type, name, or says "remove X") —
  classify as correction, NOT a new log.
  Examples (each follows a "✅ Logged ..." reply):
    bot logged 2 eggs → "there are three eggs"       → correction
    bot logged 2 eggs → "actually three eggs"         → correction
    bot logged avocado 80g → "it was a whole avocado" → correction
    bot logged rice 100g → "150g rice"                → correction
    bot logged breakfast → "that was lunch"           → correction
    bot logged dish with 6 items → "remove the hummus"→ correction
- If the bot just OFFERED suggestions, options, or a list of meal ideas
  (dinner ideas, snack options, recipes, "Option 1 / Option 2", etc.) and
  the user responds by picking one, asking for a recipe, or drilling in on
  one of them — classify as question. These are follow-ups on the advice,
  not new meal logs. Food-sounding words in this follow-up are referring
  to the bot's suggestion, not something the user ate.
  Examples (each follows a multi-option advice reply):
    bot offered 3 dinner options → "sounds great, recipe for option 1?"   → question
    bot offered 3 dinner options → "I like option 2, tell me how to cook" → question
    bot offered 3 dinner options → "the chicken one, how do I make it?"   → question
    bot offered snack ideas → "first one please"                          → question
    bot offered protein ideas → "how much chicken would that be?"         → question
  The word "recipe" / "recipie" / "how do I cook/make" is a strong signal
  for question regardless of context — the user is asking how to prepare
  something, not logging that they ate it.
- If there is NO recent log (or the recent log is unrelated), a food
  description starting a new thought is log_text.
    (no recent log) → "I had oatmeal with banana"     → log_text
    (no recent log) → "three eggs for breakfast"      → log_text

Intents:

  log_text      — describing food they ate, starting a new log (e.g. "I had oatmeal with banana", "for breakfast I ate eggs")
  correction    — fixing, adjusting, or deleting a just-logged entry (quantity, count, name, meal type, or removal). See context rule above.
  question      — asking for advice, information, analysis, a recipe, or cooking instructions (e.g. "how much protein today?", "what should my calorie goal be?", "is my diet balanced?", "what should I eat for dinner?", "give me a recipe for X", "how do I cook X?"). Also any follow-up to bot-offered suggestions/options — see context rule above.
  cmd_today     — wants to see TODAY's food log summary (e.g. "show today", "what did I eat today", "today's summary")
  cmd_date_query — wants to see food log for a SPECIFIC past day (e.g. "what did I eat yesterday", "show yesterday", "what did I eat on Tuesday", "my food last Monday", "show me Wednesday", "what was my food on April 7th")
  cmd_week      — wants the FULL weekly review/summary covering everything (e.g. "weekly review", "how was my week overall", "give me my Sunday review", "show me this week"). NOT when the user asks about a specific nutrient, meal, or aspect — those are questions, even when they mention "this week".
  cmd_goal      — wants to CHANGE or SET their calorie goal to a specific number (e.g. "set my goal to 1800", "change my goal to 2200 calories") — NOT asking what it should be
  cmd_lint      — wants the bot to tidy up / clean up / dedupe / review its own notes or memory about the user (e.g. "lint", "tidy up", "tidy up your notes", "clean up your notes", "dedup my notes", "dedupe", "clean up my profile", "sort out your memory", "review your notes about me", "check your notes for contradictions"). This is about the bot's own housekeeping of what it remembers — NOT about the user's food log or daily summary.
  remember      — sharing a personal fact, restriction, or INTENTION about themselves to be remembered. This covers both current-state facts AND forward-looking goals or intentions.
                  Current-state examples: "I don't eat fish", "I'm vegetarian", "I'm allergic to nuts", "I weigh 67kg", "my height is 165cm", "I go to the gym 3x a week"
                  Forward-looking examples: "I would like to eat more healthy fats", "I'm trying to cut sugar", "I want to eat less sweets", "my goal is to eat healthier", "I'd like to lose 5kg"
                  If the message describes who the user IS or what they're trying to DO, it's a remember.

IMPORTANT:
- "what should my goal be?" → question
- "set my goal to 1800" → cmd_goal (only when setting a specific number)
- "I don't eat fish", "please remember I hate cilantro", "I'm lactose intolerant" → remember
- "wait, actually this was breakfast" AFTER a just-logged meal → correction (meal-type change)
- ANY mention of a specific past day when asking about food/eating → cmd_date_query (NOT cmd_today)
  Examples: "yesterday", "Tuesday", "last Monday", "April 7th" → cmd_date_query
- Specific-topic questions that happen to mention "this week" are STILL questions, not cmd_week:
    "how's my protein this week?" → question
    "am I hitting my calorie goal this week?" → question
    "did I eat enough vegetables this week?" → question
  cmd_week is only for "give me the full weekly review" style requests.
- Forward-looking intention statements are remember, NOT question:
    "I would like to eat more fiber" → remember
    "I'm trying to cut sugar" → remember
    "my goal is to eat healthier" → remember
  Questions ask for information or advice. Statements of intent are remember.
- cmd_lint vs question — if the user is telling the bot to clean / tidy / dedupe / review its own notes, it's cmd_lint. If they're asking for diet advice or an audit of how they're eating, it's question.
    "lint" → cmd_lint
    "tidy up" → cmd_lint
    "tidy up your notes" → cmd_lint
    "clean up your notes about me" → cmd_lint
    "dedup my profile" → cmd_lint
    "check for contradictions in your notes" → cmd_lint
    "how's my diet looking?" → question
    "audit my food this week" → question

Reply with ONLY the intent word, nothing else. No explanation, no punctuation."""


# Prefix tokens used by the iPhone Shortcuts — detected before calling intent API
HEALTH_PREFIX = "📊 health"
WORKOUT_PREFIX = "🏋️ workouts"


def detect_intent(text: str, history: Optional[list[dict]] = None) -> str:
    """
    Uses Haiku to classify intent — works in any language, any phrasing.
    Falls back to 'log_text' if the API call fails.
    Shortcuts messages are detected by prefix before hitting the API.

    `history` is the short-term conversation memory (list of
    {"role", "content"} dicts, oldest first, ending with the current user
    turn). When provided, it is passed as the messages list so follow-ups
    like "yes" or "two eggs" retain their referent and get classified
    correctly (e.g. "two eggs" after a photo → correction, not log_text).
    """
    if text.strip().startswith("/"):
        return "command"

    # iPhone Shortcuts send structured messages with these prefixes
    stripped = text.strip()
    if stripped.startswith(HEALTH_PREFIX):
        return "health_update"
    if stripped.startswith(WORKOUT_PREFIX):
        return "workout_log"

    # Build the messages list. Prefer the full rolling-window history if
    # supplied (it already ends with the current user turn), else fall back
    # to a single-turn shot with just `text`.
    messages = history if history else [{"role": "user", "content": text}]

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=INTENT_SYSTEM_PROMPT,
            messages=messages,
        )
        intent = response.content[0].text.strip().lower()
        valid = {"log_text", "correction", "question", "cmd_today", "cmd_date_query", "cmd_week", "cmd_goal", "cmd_lint", "remember"}
        return intent if intent in valid else "log_text"
    except Exception:
        t = text.lower()
        if any(s in t for s in ["fix", "wrong", "actually", "remove", "delete", "исправ", "удали"]):
            return "correction"
        if t.endswith("?") or any(t.startswith(s) for s in ["how", "what", "сколько"]):
            return "question"
        return "log_text"


def extract_query_date(text: str, today_str: str) -> tuple[str, str]:
    """
    Given a user message like "what did I eat on Tuesday" or "show me yesterday",
    return (date_str, label) where date_str is YYYY-MM-DD and label is a human
    friendly string like "Yesterday (April 8th)" or "Tuesday (April 7th)".

    today_str: today's date as YYYY-MM-DD (passed in so we don't have to import datetime here)
    """
    system = f"""Today is {today_str}.
The user is asking about a past day's food log.
Extract which date they mean and return ONLY a JSON object with two keys:
  "date": the date in YYYY-MM-DD format
  "label": a human-friendly label like "Yesterday (April 8th)" or "Tuesday (April 7th)" or "Last Monday (April 6th)"

Rules:
- "yesterday" → previous calendar day
- A weekday name like "Tuesday" → the most recent past Tuesday (never today or future)
- "last Monday" → the Monday of last week
- A specific date like "April 7th" → that date in the current or most recent year
- Always return a date in the past (not today, not future)

Return ONLY valid JSON, no prose."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
    clean = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    try:
        parsed = json.loads(clean)
        return parsed["date"], parsed["label"]
    except Exception:
        # Fallback to yesterday if parsing fails
        from datetime import date as _date, timedelta as _td
        yesterday = (_date.today() - _td(days=1)).isoformat()
        return yesterday, "Yesterday"


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
