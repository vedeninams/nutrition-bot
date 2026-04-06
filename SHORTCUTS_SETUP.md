# iPhone Shortcuts Setup Guide

These two Shortcuts push data from your iPhone to the Nutrition Bot automatically.
You'll create them in the **Shortcuts** app on your iPhone.

---

## Before you start

You need your **Telegram Bot Chat ID** — the number that identifies your private chat with the bot.
Send `/start` to your bot, then visit:

```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```

Look for `"chat": {"id": 123456789}` — that number is your Chat ID. Save it.

---

## Shortcut 1: Daily Health Snapshot

**What it does:** Reads yesterday's step count and your latest weight from Apple Health, sends them to the bot.

**When to run:** Every morning (set as an automation at 07:30 AM, or run manually).

### Steps to build it:

1. Open **Shortcuts** → tap **+** to create a new shortcut
2. Name it: `Nutrition Bot — Health`

3. Add action: **Get Health Samples**
   - Type: **Step Count**
   - In the last: **1 day**
   - Save result as: `steps_sample`

4. Add action: **Calculate Statistics**
   - Input: `steps_sample`
   - Statistic: **Sum**
   - Save as: `total_steps`

5. Add action: **Get Health Samples**
   - Type: **Body Mass** (this is weight)
   - In the last: **30 days**
   - Limit: **1** (most recent)
   - Save result as: `weight_sample`

6. Add action: **Get Details of Health Sample**
   - Detail: **Value**
   - From: `weight_sample`
   - Save as: `weight_value`

7. Add action: **Format Date**
   - Date: **Current Date**
   - Format: **Custom** → `yyyy-MM-dd`
   - Save as: `today_date`

8. Add action: **Text**
   - Content:
     ```
     📊 health
     steps: [total_steps]
     weight: [weight_value] kg
     date: [today_date]
     ```
   - (Tap each `[variable]` and select the variable you saved)
   - Save as: `message_text`

9. Add action: **Send Message** (Telegram)
   - If you have the Telegram app action: use **Send Message via Telegram**
   - Chat: your bot's chat ID
   - Message: `message_text`

   **Alternative if no Telegram action:** Use **Get Contents of URL**
   - URL: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/sendMessage`
   - Method: **POST**
   - Body: **JSON**
   - Fields:
     - `chat_id` → your Chat ID (number)
     - `text` → `message_text`

10. **Save** the shortcut.

### Automate it:
Go to **Automation** tab → **+** → **Time of Day** → 07:30 AM → Daily → Run Shortcut: `Nutrition Bot — Health`

---

## Shortcut 2: Sport Calendar Events

**What it does:** Reads today's events from your sport calendar and sends them to the bot so it can estimate calories burned from your workouts.

**When to run:** End of day (e.g. 21:00), or manually after your last workout.

### Steps to build it:

1. Open **Shortcuts** → tap **+**
2. Name it: `Nutrition Bot — Workouts`

3. Add action: **Find Calendar Events**
   - Calendar: **[your sport calendar name]** ← select your specific sport calendar
   - Date: **Today**
   - Save as: `sport_events`

4. Add action: **Repeat with Each** (loop over `sport_events`)

   Inside the loop:
   - Add action: **Get Details of Calendar Event**
     - Detail: **Title**
     - Save as: `event_title`
   - Add action: **Get Details of Calendar Event**
     - Detail: **Duration** (in minutes)
     - Save as: `event_duration`
   - Add action: **Text**
     - Content: `[event_title] — [event_duration] min`
     - Save as: `event_line`
   - Add action: **Add to Variable**
     - Variable: `workout_lines`
     - Value: `event_line`

5. Add action: **Combine Text** (after the loop)
   - Text: `workout_lines`
   - Separator: **New Line**
   - Save as: `combined_workouts`

6. Add action: **Text**
   - Content:
     ```
     🏋️ workouts
     [combined_workouts]
     ```
   - Save as: `message_text`

7. Add action: **Send Message via Telegram** (or Get Contents of URL, same as Shortcut 1)
   - Message: `message_text`

8. **Save** the shortcut.

### Automate it:
**Automation** → **+** → **Time of Day** → 21:00 → Daily → Run Shortcut: `Nutrition Bot — Workouts`

---

## Message format reference

The bot recognises these exact formats. Do not change the header lines.

**Health snapshot:**
```
📊 health
steps: 8432
weight: 66.8 kg
date: 2024-01-15
```

**Workout log:**
```
🏋️ workouts
Strength training — 60 min
Yoga — 45 min
```

Workout names are flexible — the bot recognises: strength training, yoga, pilates, running, cycling, swimming, HIIT, crossfit, boxing, walking, hiking, tennis, etc.

---

## Testing

You can also send these messages manually from Telegram (just type or paste them) to test before the Shortcuts are set up. The bot will parse them identically.

---

## What happens after you send

**After health snapshot:**
> ✅ Health data logged!
> ⚖️ Weight: 66.8 kg
> 👟 Steps: 8,432 (~310 kcal walking)
> 🔥 Total estimated burn today: ~310 kcal

**After workout log:**
> ✅ Workouts logged!
> 🏋️ Strength training — 60 min — ~360 kcal (moderate-high intensity)
> 🧘 Yoga — 45 min — ~135 kcal
> 🔥 Total estimated burn: ~495 kcal

The bot combines both when you ask for your daily or weekly summary.
