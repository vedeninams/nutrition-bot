# Penguin Nutrition Bot — Architecture Reference

> **Working agreement.** This file is the source of truth for *how the bot is
> built*. Before changing any module, **read the section that covers it**.
> After every change that alters behaviour, schema, prompts, triggers, file
> layout, or cron — **update the matching section in this file in the same
> commit**. If the change spans multiple modules, update every section it
> touches. Out-of-date docs here are treated as a bug.

How every script, function, and trigger fits together. Read top to bottom on
a first pass, or jump straight to a module section if you need to understand
or change one specific area.

---

## Table of contents

1. [Project at a glance](#1-project-at-a-glance)
2. [Architecture overview](#2-architecture-overview)
3. [The data model](#3-the-data-model)
4. [Message routing](#4-message-routing)
5. [Module reference: `database.py`](#5-module-reference-databasepy)
6. [Module reference: `wiki.py`](#6-module-reference-wikipy)
7. [Module reference: `analyzer.py`](#7-module-reference-analyzerpy)
8. [Module reference: `advisor.py`](#8-module-reference-advisorpy)
9. [Module reference: `lint.py`](#9-module-reference-lintpy)
10. [Module reference: `contradictions.py`](#10-module-reference-contradictionspy)
11. [Module reference: `telegram_bot.py`](#11-module-reference-telegram_botpy)
12. [Cron triggers and proactive pushes](#12-cron-triggers-and-proactive-pushes)
13. [Deployment and configuration](#13-deployment-and-configuration)
14. [Common cross-cutting flows](#14-common-cross-cutting-flows)
15. [Glossary](#15-glossary)

---

## 1. Project at a glance

The Nutrition Bot is a personal Telegram nutritionist. The user sends photos
of food, photos of nutrition labels, voice notes, or text descriptions. The
bot identifies what was eaten, estimates calories and macros, logs them to a
local SQLite database, and replies with a confirmation. On top of that it
runs three proactive pushes (evening summary, Sunday weekly review, Saturday
memory tidy) and maintains a per-user long-term-memory wiki — a folder of
markdown pages the LLM curates over weeks.

### Operating principles the codebase encodes

- Every external integration (Anthropic API, Telegram, Whisper) is isolated
  to one module so it can be swapped without touching the rest.
- Data is owned by SQLite (meals, daily_stats, conversation log, audit
  trail) and the per-user wiki folder (long-term memory). No other module
  touches them directly — they go through `database.py` and `wiki.py`.
- LLM calls are split by purpose: vision and reasoning use Claude Opus /
  Sonnet; classification, parsing, and dedup use Claude Haiku. Cheap models
  do narrow tasks; the expensive model only handles the open-ended ones.
- Wiki edits are append-only at ingest time; a debounced post-ingest lint
  and the weekly lint pass handle dedup, supersede, and stale removal.
  `log.md` is never rewritten — it's the audit trail.
- Concurrency is handled via per-user `asyncio` locks (one lock per user;
  different users don't block each other). Background tasks (ingest, lint)
  are fire-and-forget so the user-facing reply never waits on them.

### Languages and dependencies

Pure Python 3.10+, ~7,000 lines across 7 modules. Four runtime deps
(`requirements.txt`):

| Package | Used for |
| --- | --- |
| `anthropic >= 0.40` | Claude vision (Opus 4.6), reasoning (Sonnet 4.6), classification/parsing/lint (Haiku 4.5). |
| `openai >= 1.0` | Whisper transcription for voice messages — the only OpenAI call. |
| `python-telegram-bot >= 21.0` | Telegram message handlers, commands, photo/voice download, push messages from cron. |
| `python-dotenv >= 1.0` | Loads `NUTRITION_BOT_TOKEN` and `ANTHROPIC_API_KEY` from `.env` at startup. |

### File layout

| File | LOC | Role |
| --- | --- | --- |
| `telegram_bot.py` | ~1,230 | Entry point. Receives Telegram messages, routes them, calls the right handler. Also the cron entry point — invoked by systemd cron with `--evening-summary`, `--weekly-review`, or `--lint-cron` flags. |
| `analyzer.py` | ~1,170 | All Claude API calls **except** advisor and lint: vision (food/label photo), text food parsing, intent router, correction resolver, date extraction, profile updater, activity calorie estimator, plus parsers for the iPhone Shortcuts. |
| `advisor.py` | ~1,410 | Reply formatting (post-log confirmation, `/today`, `/week`), AI-written summaries (evening, weekly, morning, Q&A), and the wiki ingest pipeline that writes self-statements / self-questions into the long-term memory. |
| `database.py` | ~1,320 | SQLite schema, all CRUD, meal-type classification (the Tier-2 90-min-window rule), short-term conversation memory, soft-delete and audit trail, test cleanup. |
| `wiki.py` | ~480 | Per-user long-term memory: markdown pages on disk, async locks, calorie-goal canonical-line plumbing, one-time SQL→wiki migrations, today-stamp stripping for `/reset_today`. |
| `lint.py` | ~450 | Background tidy pass over the wiki: dedup, supersede, drop stale time-bounded goals. Uses Haiku. Triggered after every wiki write (debounced) and on Saturday cron. |
| `contradictions.py` | ~850 | Cross-page conflict detection in the wiki. Records OPEN sections in `contradictions.md`, DMs the user to resolve, applies the resolution as wiki edits. |

### Supporting files

- `requirements.txt` — runtime dependencies above.
- `nutrition-bot.service` — systemd unit; runs `telegram_bot.py` as the long-poll bot.
- `nutrition-bot-test.service` — sibling unit for a test bot pointing at the same code with a different `.env`.
- `deploy.sh` — push local commits to GitHub, then ssh to the server, fetch+reset, pip install, restart systemd.
- `server_setup.sh` — first-time-server-setup script.
- `wiki_instructions.md` — the schema/rulebook the LLM follows when ingesting into the wiki. Read at module load by `advisor.py` and embedded into every ingest prompt.
- `wiki_templates/` — five starter markdown pages copied into a new user's wiki folder on first use.
- `.env` / `.env.example` — secrets (bot token, Anthropic key, optional OpenAI key for voice).

---

## 2. Architecture overview

### End-to-end request lifecycle

Picture one user message: a photo of a salad with caption *"for lunch"*.

1. Telegram delivers the message via long-poll to the bot process started by
   `main()` in `telegram_bot.py`.
2. python-telegram-bot's dispatcher routes a Photo update to `handle_photo`.
   `handle_photo` extracts and caches the album caption (so subsequent
   photos in the same album inherit it), shows a typing indicator,
   downloads the highest-resolution photo into memory, and calls
   `analyzer.analyze_photo()`.
3. `analyze_photo` is a thin router: it inspects the caption for label-ish
   keywords (`label`, `per 100g`, etc.). For our example it routes to
   `analyze_food_photo`, which sends the image plus the caption to Claude
   Opus 4.6 with the FOOD system prompt. Opus returns a JSON array of
   items — each with `dish_name`, `dish`, `kcal`, `protein_g`, `fat_g`,
   `carbs_g`, `sugar_g`, `confidence`, `meal_type`.
4. Back in `handle_photo`, items are passed to `database.log_meal_items`.
   That function classifies `meal_type` **once** for the whole batch
   (caption-detected if Opus emitted one, else the time-of-day + 90-min
   rule), then inserts one meal row per ingredient. `dish_name` and
   `meal_type` are independent fields — `dish_name` describes what's on
   the plate, `meal_type` describes when it was eaten. The system does
   not rename `dish_name` based on `meal_type` (or vice versa).
5. A short text summary of what was logged (`[Photo logged (photo): Greens
   60g (30 kcal), …]`) is appended to the `conversation_messages` table so
   later text turns like *"actually 600g"* have a referent.
6. `advisor.log_confirmation` builds a friendly Telegram reply — emoji per
   ingredient, totals row, progress bar against the daily goal, and a
   heads-up alert if the user crossed 80% / 100% of the goal. The reply is
   sent. Both the user message and the bot reply are recorded in
   `conversation_messages` by the `_send` helper.
7. After the reply lands the request is done. No background tasks fire for
   plain meal logs; ingest only kicks off after self-statements ("I'm
   cutting sugar") or self-questions ("am I low on protein?").

### The three layers

| Layer | What lives here |
| --- | --- |
| **Edge** (`telegram_bot.py`) | Telegram I/O, command handlers, message routing. Knows nothing about Claude or SQL — it delegates. |
| **Intelligence** (`analyzer.py` + `advisor.py`) | All LLM calls. `analyzer` parses input (vision, text, intent, corrections); `advisor` produces output (confirmations, summaries, Q&A) and curates long-term memory. |
| **Persistence** (`database.py` + `wiki.py` + `lint.py` + `contradictions.py`) | SQLite for structured data; markdown wiki for synthesized long-term memory; lint + contradictions are the LLM-driven housekeeping over the wiki. |

### Two memory layers

Both layers are queried on every conversational LLM call (intent
classification, correction resolution, question answering, ingest). They
serve different purposes:

| Layer | Purpose |
| --- | --- |
| **Short-term** (`conversation_messages` table in SQLite) | Rolling window of "last 16 hours OR last 20 messages, whichever is larger." Lets follow-ups like *"yes,"* *"two eggs,"* *"the second one,"* *"actually 600g"* resolve correctly. Photo events are summarised as a synthetic user message so a follow-up can reference what was just logged. |
| **Long-term** (per-user markdown wiki on disk) | Five pages: `profile` (durable identity), `goals` (current targets), `patterns` (observed habits), `wins` (achievements), `log` (audit). The LLM appends synthesized observations on every self-statement and self-question; lint dedups and drops stale entries. |

This separation is intentional, taken from Karpathy's *"LLM Wiki"* essay.
Most LLM applications use raw retrieval (RAG) and re-derive knowledge on
every query. Here, knowledge is synthesized into the wiki at write time, and
Q&A reads from the synthesized version. Over weeks the wiki becomes a rich
profile that turns generic responses into specific ones.

---

## 3. The data model

All structured data lives in one SQLite file (default `nutrition.db`,
configurable via `NUTRITION_DB_PATH`). The schema is created idempotently
on every startup by `database.init_db()`. WAL mode is enabled for safer
concurrent writes; foreign keys are enforced.

### 3.1 Tables

#### `users`

One row per Telegram user. Created automatically on the first message via
`ensure_user`. `daily_kcal` is **legacy** — the canonical calorie goal now
lives in `goals.md` and is read via `wiki.get_daily_kcal`. The column is
kept only for the one-time SQL→wiki migration that runs the first time
`ensure_user` sees a user with a non-zero legacy value.

| Column | Type | Meaning |
| --- | --- | --- |
| `user_id` | INTEGER PK | Telegram user id. |
| `daily_kcal` | INTEGER (legacy) | Default 2000. No longer the source of truth. |
| `language` | TEXT | Default `'en'`. Currently informational; reply language is matched at LLM-prompt time. |
| `timezone` | TEXT | Default `'Europe/Berlin'`. Informational — server OS timezone is the actual source for cron timing. |
| `created_at` | TEXT | ISO timestamp set on insert. |

#### `meals`

Every logged ingredient is one row. A photo of a 6-component plate becomes
6 rows that share the same `dish_name`. **Soft delete:** rather than
DELETE, the bot updates `confidence='deleted'` so the audit trail
(`corrections` table) and history queries stay sound. The two test-cleanup
helpers in `/reset_today` are the only place that hard-deletes meals.

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | INTEGER PK | Auto-incremented row id. |
| `user_id` | INTEGER | FK → `users`. |
| `logged_at` | TEXT | ISO timestamp at insertion (server local time, which is Europe/Berlin in production). |
| `meal_type` | TEXT | One of `breakfast` / `lunch` / `dinner` / `snack`. Either caption-detected or auto-classified (see §3.3). |
| `dish_name` | TEXT | The parent dish/plate (e.g. *"Udon Noodle Bowl"*). All ingredients of one dish share this. For standalone items it equals `dish`. |
| `dish` | TEXT | The individual ingredient with its weight (e.g. *"Tofu 80g"*, *"Fried egg × 2pcs 120g"*). |
| `kcal` / `protein_g` / `fat_g` / `carbs_g` / `sugar_g` | REAL | Macros for **this row only** — not the whole plate. |
| `confidence` | TEXT | `high` / `medium` / `low` / `deleted`. `deleted` is the soft-delete flag. |
| `source` | TEXT | How the row was created: `photo` / `label` / `text` / `correction`. |
| `notes` | TEXT | Currently unused by callers but available. |

Index `idx_meals_user_date` covers the `(user_id, logged_at)` lookups that
all the daily/weekly queries depend on.

#### `corrections`

Audit trail. `update_meal` writes a row here with the BEFORE and AFTER
values plus the natural-language reason the LLM extracted from the user's
correction. Never read by user-facing code — purely forensic.
Foreign-keyed to `meals(id)`, so `delete_today_meals` must purge
corrections first.

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | INTEGER PK | Auto-incremented. |
| `meal_id` | INTEGER FK | Points at the meal row that was changed. |
| `corrected_at` | TEXT | Default `datetime('now')`. |
| `old_dish` / `old_kcal` | TEXT / REAL | Snapshot before the change. |
| `new_dish` / `new_kcal` | TEXT / REAL | Snapshot after the change. |
| `reason` | TEXT | What the LLM understood the user wanted. |

#### `user_profile`

Legacy free-text profile (one row per user). Migrated to the wiki on first
`ensure_user` via `wiki.migrate_sql_profile_if_needed`; the column is no
longer the source of truth but is preserved so the migration is idempotent
and reversible.

#### `daily_stats`

One row per `(user, date)` for activity data sent by the iPhone Shortcuts
(steps, weight, workouts, kcal_burned_est). `UNIQUE(user_id, date)`.
`workouts` is stored as a JSON-encoded list of
`{name, duration_min, kcal_est, intensity_note}` objects. Upsert
semantics — only fields explicitly passed are updated; passing `None`
leaves an existing value intact.

#### `conversation_messages`

Short-term rolling-window memory. Every incoming user text and every
outgoing bot reply is appended here with `role='user'` or `'assistant'`
(matching Claude API convention). Photo events are appended as user-role
text summaries so follow-ups can reference them. The reader returns the
larger of "last 16 hours" or "last 20 messages" to avoid a midnight cliff.
Anything older than 14 days is purged by the Saturday cron.

#### `weight_readings`

Raw weight history — one row per weighing (typically multiple per day).
Populated by the hourly `withings_sync.py` cron job pulling from the
Withings Health API. After each insert, `daily_stats.weight_kg` is
automatically refreshed to the **minimum** reading of that calendar day
(the morning-low convention), so all existing summary code keeps seeing
"one weight per day."

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | INTEGER PK | Auto-incremented. |
| `user_id` | INTEGER FK | |
| `measured_at` | TEXT | ISO timestamp from the source (Withings). |
| `weight_kg` | REAL | The reading, in kilograms. |
| `source` | TEXT | `'withings_api'` initially; future-proofed for other sources. |
| `inserted_at` | TEXT | Default `datetime('now')`. |

`UNIQUE(user_id, measured_at)` dedups when the polling script catches the
same Withings reading twice.

#### `withings_auth`

OAuth tokens for the Withings Health API. One row per user who has
authorized the bot to read their data. Populated once by
`setup_withings.py` (interactive bootstrap), refreshed automatically by
`withings_sync.py` when the access token expires.

| Column | Type | Meaning |
| --- | --- | --- |
| `user_id` | INTEGER PK | FK → `users`. |
| `access_token` | TEXT | Bearer token (~3h lifetime). |
| `refresh_token` | TEXT | Long-lived. Used to mint new access tokens. |
| `expires_at` | TEXT | ISO timestamp when access_token expires. |
| `withings_user_id` | TEXT | Withings' internal user ID, for reference. |
| `updated_at` | TEXT | Default `datetime('now')`. |

### 3.2 The wiki on disk

Each user has a folder at `wiki/user_<user_id>/` containing five markdown
files. The folder is gitignored; contents never leave the server.

| Page | What it holds |
| --- | --- |
| `profile.md` | Durable identity facts. Vegetarian, lactose-intolerant, has a toddler, allergic to nuts. *"Things you'd put on a medical intake form."* |
| `goals.md` | Active intentions. The canonical `- **Daily calorie goal**: N kcal` bullet lives here as the single source of truth for the calorie target. Other goals are free-form (kg targets, sugar reduction, protein-up, etc.). |
| `patterns.md` | Observed eating behaviours (*"Skips breakfast on busy days,"* *"Under-eats protein at breakfast — observed 5x since 2026-04-01"*). Soft observations the bot can cite when answering. |
| `wins.md` | Real achievements and milestones. **Lint never drops these** — past achievements stay valid forever. |
| `log.md` | Append-only audit trail. Every wiki write leaves a breadcrumb here. Lint does NOT rewrite `log.md`. |

Rules for what goes where, the canonical calorie line, the `[YYYY-MM-DD]`
date prefix on every bullet, and the lint behaviour are all encoded in
`wiki_instructions.md`, which is read by the LLM at every ingest call.

A separate `contradictions.md` file is created on demand by
`contradictions.py` in the same folder. It holds OPEN and RESOLVED
sections that track cross-page conflicts (see §10).

### 3.3 Meal-type classification (Tier-1 caption / Tier-2 90-min rule)

Every meal row carries a `meal_type`. It's set in one of two ways:

1. **Tier 1 — caption.** The vision and text analyzers extract a
   `meal_type` from the user's caption (*"for breakfast"*, *"snack"*).
   If the user said it, that wins. The analyzer prompts spell out the
   four valid values and *"null if not mentioned."*
2. **Tier 2 — auto-classify** (`database.classify_meal_type`). Used only
   when the caption was silent. **Pure function** over `(now, last_meal_today)` —
   no kcal involvement, no DB writes. Algorithm: the day is divided into
   four meal windows (Berlin wall-clock):
    - `04:00 → 11:29` — **breakfast**
    - `11:30 → 15:59` — **lunch**
    - `16:00 → 22:59` — **dinner**
    - `23:00 → 03:59` — **snack** (night, spans midnight)

Within each window: the **first** item logged in the window takes the
window's `meal_type`; subsequent items inherit if logged ≤ 90 min after
the previous, else they're `snack`. **Crossing a window boundary RESETS
the anchor** (so a soup at 12:30 is `lunch` even if the prior log was a
9:00 banana already classified as snack).

Worked example day:

| Time + food | Result |
| --- | --- |
| 07:00 eggs | first in breakfast window → **breakfast** |
| 09:00 banana | 120 min after eggs (>90) → **snack** |
| 12:30 soup | first in lunch window → **lunch** |
| 15:30 apple | 180 min after soup (>90) → **snack** |
| 19:00 chicken | first in dinner window → **dinner** |
| 21:15 chocolate | 135 min after chicken (>90) → **snack** |

Classification is computed **once per insertion batch** in
`log_meal_items`, against the user's state BEFORE any row from the batch
is inserted, then passed explicitly to every `log_meal` call. This
prevents an 8-item plate from flipping types mid-insert when the first
row would otherwise become its own siblings' "prior meal."

---

## 4. Message routing

All inbound messages flow through `telegram_bot.py`. The
python-telegram-bot Application binds three message-type filters and seven
slash commands.

### Slash commands

| Command | Handler | What it does |
| --- | --- | --- |
| `/start`, `/help` | `cmd_start` | Welcome message + command list. |
| `/today` | `cmd_today` | Calls `advisor.today_summary` — meal-by-meal log + totals + activity for today. |
| `/week` | `cmd_week` | Calls `advisor.weekly_review` — Sunday-style 3-paragraph AI review. |
| `/goal [N]` | `cmd_goal` | No arg: prints the current calorie target read from `goals.md`. With a number: writes that number to `goals.md` as the canonical bullet (under the per-user wiki lock). |
| `/profile` | `cmd_profile` | Pretty-prints every populated wiki page (profile, goals, patterns, wins) with section emojis. Strips internal scaffolding (date prefixes, headers, comments, bold markers) so it reads cleanly in Telegram. |
| `/lint` | `cmd_lint` | Three-phase manual run of the Saturday cron: tidy each lintable page, detect new contradictions, DM the oldest open one. Backups are always written before any page is overwritten. |
| `/reset_today` | `cmd_reset_today` | Test cleanup. Hard-deletes today's meals, daily_stats, and conversation_messages; strips every `[today]`-stamped wiki line. Historical data and earlier-stamped wiki facts are untouched. `/clear_today` is kept as a backward-compat alias. |

### Photo messages → `handle_photo`

Caches the caption for albums (so photos 2..N inherit photo 1's caption),
downloads the highest-resolution photo, calls `analyzer.analyze_photo`
(which auto-detects label vs food). The returned items go to
`db.log_meal_items`, then a synthetic `[Photo logged (...)]` line is
appended to `conversation_messages` so later text follow-ups have a
referent. The reply is built by `advisor.log_confirmation`.

### Voice messages → `handle_voice`

Downloads the .ogg file, transcribes with OpenAI Whisper-1, echoes the
transcription back to the user, then forwards the transcribed text to
`handle_text` via `ctx.user_data` so the rest of the pipeline is
identical to a typed message.

### Text messages → `handle_text`

This is the biggest handler. Five things happen, in order:

1. **Conversation memory.** Append the user turn to `conversation_messages`,
   fetch the rolling window once, reuse it for every downstream LLM call.
2. **Contradiction pre-route.** If `contradictions.oldest_open` returns a
   pending OPEN section for this user, run a Haiku classifier on the user's
   text ("is this a reply to that question?"). If yes, apply the resolution
   and stop. If unrelated, fall through.
3. **Intent detection.** `analyzer.detect_intent` classifies the message
   into one of the intents below. The conversation history is rendered as
   a TRANSCRIPT inside one user turn — not passed as raw `messages` — so
   Haiku stays in classifier mode and doesn't try to continue the dialogue.
4. **Branch on intent.** Each intent has its own branch in `handle_text` —
   see the table below.
5. **Send the reply** via `_send`, which also records it in
   `conversation_messages`.

### Intents emitted by `detect_intent`

| Intent | When it fires | Branch in `handle_text` |
| --- | --- | --- |
| `log_text` | User describes food they ate (*"I had oatmeal with banana"*). | `analyzer.analyze_text` → `db.log_meal_items` → `advisor.log_confirmation`. |
| `correction` | After the bot just logged a meal and the user is fixing it (*"actually three eggs"*, *"that was lunch"*, *"remove the hummus"*). | Fetch today's full meal log + last logged batch, run `analyzer.resolve_correction`, integrity-guard the meal_ids, apply each action (`update` / `delete` / `scale_dish_grams` / `scale_dish` / `delete_many` / `delete_duplicates` / `add_items` / `update_many`). |
| `question` | User asks for advice or a number (*"how much protein today?"*, *"what should I eat for dinner?"*, *"give me a recipe for X"*, or any follow-up to bot suggestions). | `advisor.answer_question` + fire-and-forget `advisor.schedule_ingest('question', ...)`. |
| `cmd_today` | Natural-language ask for today's summary. | `advisor.today_summary`. |
| `cmd_date_query` | Asks about a specific past day (*"yesterday"*, *"Tuesday"*, *"April 7th"*). | `analyzer.extract_query_date` → `advisor.day_summary`. |
| `cmd_week` | Wants the FULL weekly review (*"weekly review"*, *"how was my week"*). | `advisor.weekly_review`. Specific-aspect-of-this-week questions go to `question`, not `cmd_week`. |
| `cmd_goal` | Set a calorie number (*"set my goal to 1800"*). Bare number-extraction via `analyzer.extract_goal_from_text`. | `wiki.set_daily_kcal` under the per-user lock. |
| `cmd_lint` | User asks the bot to tidy / clean / dedupe its own notes. | Delegates to `cmd_lint`. |
| `remember` | User states a personal fact, intention, or asks the bot to drop/replace something it just showed (*"I'm vegetarian"*, *"I'd like to eat more fiber"*, *"drop the healthy fats goal"*). | Send neutral *"Got it — updating my notes"* then `advisor.schedule_ingest('remember', ...)`. |
| `health_update` / `workout_log` | iPhone Shortcut prefixes (`📊 health` / `🏋️ workouts`). | Parser-specific branch: parse, estimate burn via Claude, upsert `daily_stats`. |
| `command` | Message starts with `/`. Falls through to python-telegram-bot's own routing. | n/a. |

### Transient-error recovery (issue #18)

Every Anthropic API call is wrapped in two layers of retry:

1. **Inline (in-process) retry.** Each Anthropic client is created with
   `max_retries=6`, so the SDK silently retries transient errors (HTTP 502 /
   503 / 504 gateway, **HTTP 529 overload**, rate limits, connection blips)
   with exponential backoff totalling ~30 seconds. This is enough to silently
   absorb ~99% of real overloads — the user never knows anything happened.

2. **Background retry queue** (for the rare cases where 30 seconds wasn't
   enough). Every LLM-using branch in `handle_text` (and `handle_photo`)
   runs its body inside `_run_with_recovery(update, original_text, work_fn)`.
   If `work_fn` raises a transient API error after the SDK retries are
   exhausted:

   - The user gets a friendly *"Anthropic is busy, I haven't lost what you
     sent — hang tight"* message that **echoes back their original text**
     (so they don't have to retype it).
   - A background task retries `work_fn` periodically (every 30 seconds, up
     to 6 attempts → ~3 more minutes of silent recovery).
   - When a retry succeeds, the user gets the normal reply they were
     waiting for.
   - If all background retries fail, the user gets a final *"I couldn't get
     through, here's what you sent…"* message with their original text.

   One pending retry per user — if the user sends a new message while a
   retry is pending, the old retry is cancelled (latest message wins).

**Permanent errors** (auth, billing, bad request, code bugs) propagate
normally; only the four transient categories above trigger the recovery
layer.

**Limitation:** pending retries live in process memory only. A bot
restart during an outage drops them. A persistent on-disk queue would
survive restarts; out of scope for issue #18 — considered for later.

### The integrity guard on corrections

The correction branch was a major source of bugs early on: Haiku would
sometimes return a `meal_id` that was NOT in the candidate set the prompt
actually showed it, causing an unrelated row to be silently overwritten.
The fix has two parts:

1. The candidate pool is widened to **today's full meal log** (not the
   last 10 rows) — so the resolver can always see this morning's cottage
   cheese when the user corrects it at 6 PM.
2. **Every action that targets a `meal_id` is checked against the set of
   ids the resolver was actually shown.** If the id is outside that set,
   the action is rejected with a clarifying message to the user instead
   of being applied. `delete_many` also has a `dish_name`-first path
   that's safe because `delete_by_dish_name` is always scoped to the
   user's own rows.

---

## 5. Module reference: `database.py`

SQLite foundation. Every other module imports from here — nothing else
touches the database directly.

### 5.1 Connection + schema

#### `get_conn()`
Opens a `sqlite3` connection. Sets `row_factory=Row` so result rows
behave like dicts, enables WAL journal mode for safer concurrent writes,
and turns on foreign-key enforcement. All other functions in this module
open a fresh connection per call. SQLite handles many concurrent readers
and serializes writers via WAL — there's no connection pool.
**Called from:** every `db.*` function.

#### `init_db()`
Creates all tables and indices if they don't exist (`CREATE TABLE IF
NOT EXISTS`), and runs the one-time `dish_name` column migration on
existing databases by checking `PRAGMA table_info` before issuing
`ALTER TABLE`. Idempotent — safe to call on every startup.
**Called from:** `main()` in `telegram_bot.py`.

### 5.2 User helpers

#### `ensure_user(user_id)`
Creates a row in `users` (`INSERT OR IGNORE`) and initializes the user's
wiki folder. Also runs two one-time SQL→wiki migrations: legacy SQL
profile text → `profile.md`, and legacy `users.daily_kcal` → `goals.md`
canonical bullet. Both migrations are stamped with markers so they're
idempotent — the SQL columns are left in place as a fall-back/audit but
never read after the migration. Called everywhere a user is touched, so
the wiki is always present by the time other code reads it. Cheap on the
hot path because the marker check short-circuits before any file work.
**Called from:** `cmd_*`, `handle_*`, `db.log_meal`, `db.log_message`,
`db.upsert_daily_stats`, `db.save_profile`, `db.set_daily_goal`,
`db.set_language`.

#### `get_user(user_id) → dict|None`
Returns the `users` row as a dict (or None if missing). Used by various
advisor functions for legacy lookups, but most code reads the calorie
goal via `wiki.get_daily_kcal` now.

#### `set_daily_goal(user_id, kcal)`
**Deprecated** — kept only so any lingering callers don't crash. Writes
to `users.daily_kcal`, which is no longer the source of truth. The
canonical write path is `wiki.set_daily_kcal` via `goals.md`.

#### `set_language(user_id, lang)`
Updates `users.language`. Currently informational — language matching at
LLM-prompt time is the actual reply-language source.

### 5.3 Meal classification

#### `get_last_meal_today(user_id) → dict|None`
Returns the most recent non-deleted meal logged today by this user
(server local calendar day), or None. Used by `classify_meal_type` to
decide between inheritance and time-of-day window.
**Called from:** `log_meal`, `log_meal_items`.

#### `_meal_window(now) → (meal_type, window_start)`
Pure helper. Returns the meal_type and start-datetime of the meal window
containing `now`. Handles the night window crossing midnight: when
`00:00 ≤ now < 04:00`, the window started at 23:00 the previous calendar
day. **Called from:** `classify_meal_type`.

#### `classify_meal_type(now, last_meal=None) → str`
Pure function. Implements Tier 2 of the meal-type rules — see §3.3 for
the full algorithm. No DB reads, no side effects. Returns one of
`breakfast` / `lunch` / `dinner` / `snack`. Robust to malformed input
(unparseable `logged_at`, unknown prior `meal_type`) — falls back to
the time-of-day window. **Called from:** `log_meal`, `log_meal_items`.

### 5.4 Logging meals

#### `log_meal(user_id, dish, kcal, ...) → meal_id`
Inserts one meal row and returns its id. Auto-classifies `meal_type` if
the caller didn't pass one (using `get_last_meal_today` +
`classify_meal_type`). `dish_name` defaults to `dish` for standalone
items. All other write helpers go through this — it's the single INSERT
path for the `meals` table. **Called from:** `log_meal_items`.

#### `log_meal_items(user_id, items, source='photo') → list[meal_id]`
Batch insert. Classifies `meal_type` **once per batch** and passes the
same `meal_type` to every `log_meal` call — preventing the first row
from becoming its own siblings' "prior meal" mid-insert.

- **Tier 1 wins:** if any item dict has `meal_type` set (from caption
  detection), that becomes the batch's `meal_type`. Otherwise
  `classify_meal_type` runs against state BEFORE the batch.
- **No `dish_name` post-processing.** `dish_name` and `meal_type` are
  independent fields with separate jobs (see §3.3 and the analyzer
  prompts in §7.1). The analyzer is the single source of truth for
  naming; `log_meal_items` does not adjust `dish_name` based on
  `meal_type`.

**Called from:** `telegram_bot.handle_photo`, `telegram_bot.handle_text`
(`log_text` and `add_items` branches).

### 5.5 Corrections + bulk edits

#### `update_meal(meal_id, updates, reason='') → bool`
Dynamic UPDATE on a `meals` row. The allowed columns are
`dish, kcal, protein_g, fat_g, carbs_g, sugar_g, confidence,
meal_type, notes` — anything else is silently dropped. Always writes a
`corrections` row capturing the BEFORE and AFTER, plus the
natural-language reason from the LLM. Never called for hallucinated
`meal_id`s — the integrity guard in `handle_text` rejects those before
this function is reached.
**Called from:** `telegram_bot.handle_text` (`update`, `update_many`).

#### `delete_meal(meal_id) → bool`
Soft-delete: marks `confidence='deleted'`. The row stays in the table
for audit/correction-trail purposes; every read query filters it out.
**Called from:** `telegram_bot.handle_text` (`delete`, `delete_many`
fallback path).

#### `scale_dish_items(user_id, dish_name, factor) → row_count`
Scales every non-deleted item of a dish by `factor`. Two updates in one
transaction: the macro columns AND the weight token inside each
ingredient's `dish` text. Both must move together — otherwise `/today`
shows mismatched rows like *"300g yogurt — 272 kcal"* (the bug that
motivated this helper). Uses the `_rescale_weight_in_dish` regex helper
to rewrite only the LAST g/ml number in a dish string, so the count
token in *"Fried egg × 2pcs 120g"* doesn't get touched. Case-insensitive
`dish_name` match. **Called from:** `telegram_bot.handle_text`
(`scale_dish`, `scale_dish_grams`).

#### `delete_duplicate_dishes(user_id, dish_name) → row_count`
Soft-deletes every item of `dish_name` logged today MORE than 2 minutes
after the earliest matching log. Useful when a photo got logged twice
in quick succession. **Called from:** `handle_text` (`delete_duplicates`).

#### `delete_by_dish_name(user_id, dish_name) → row_count`
Soft-deletes every non-deleted row with this `dish_name` (any date) for
the user. Case-insensitive. Safer fallback than a `meal_id` list because
it's scoped to the user's own rows by definition.
**Called from:** `handle_text` (`delete_many`).

#### `clear_today(user_id) → row_count`
Soft-deletes all of today's meals. Reserved — currently no caller uses
it (the test cleanup goes through `delete_today_meals`, which
hard-deletes).

### 5.6 Read queries

| Function | Returns | Used by |
| --- | --- | --- |
| `get_today_meals(user_id)` | All non-deleted meals logged today, ordered by `logged_at`. | correction branch (candidate set), `advisor.answer_question`, `advisor.evening_summary`. |
| `get_meals_for_date(user_id, date_str)` | Same shape, for an arbitrary YYYY-MM-DD. | `get_meals_grouped_for_date`, `advisor.day_summary`. |
| `get_meals_grouped_for_date` / `get_today_meals_grouped` | Group by `(dish_name, meal_type)` preserving order of first appearance. Each group has summed macros and ingredients list. | `advisor.day_summary`, `advisor.evening_summary`, `advisor.log_confirmation`. |
| `get_totals_for_date` / `get_today_totals` | Sum of kcal/protein/fat/carbs/sugar plus item count for a date or today. | `advisor.log_confirmation`, `advisor.day_summary`, `advisor.evening_summary`, `handle_text` correction branch. |
| `get_recent_meals(user_id, limit=5)` | Last N non-deleted meals, newest first. | `handle_text` correction branch fallback. |
| `get_month_totals` / `get_week_totals` | Daily-totals rollup for the past 30 / 7 days. | `advisor.weekly_review`, `advisor.evening_summary`. |
| `get_meal_by_id(meal_id)` | Single row lookup. | `handle_text` correction branch (after update/delete). |
| `get_dish_items_today(user_id, dish_name)` | All today's items belonging to a `dish_name`. | `handle_text` `scale_dish_grams` branch. |
| `get_last_meal_batch(user_id, window_seconds=120)` | All items logged within `window_seconds` of the most recent — i.e. the last photo's cluster. | `handle_text` correction branch (resolver hint). |

### 5.7 User profile (legacy SQL)

`get_profile`, `save_profile`, `get_profile_for_prompt` — read/write
helpers for `user_profile.profile`. Wrapped by the wiki migration in
`ensure_user`; after migration the wiki replaces these as the source of
truth. Kept for the migration and any rare fallback path.

### 5.8 Daily activity stats

#### `upsert_daily_stats(user_id, date_str, steps, weight_kg, workouts, kcal_burned_est)`
Insert-or-merge for the `daily_stats` table. Only updates the fields
that are passed; `None` means *"leave the existing value alone."*
`workouts` is JSON-encoded.
**Called from:** `handle_text` (`health_update`, `workout_log`).

`get_daily_stats`, `get_week_stats`, `get_latest_weight` — read helpers.
Workouts JSON is parsed back into a list automatically.
`get_latest_weight` returns the most recently recorded weight for
activity-calorie estimation.

### 5.9 Short-term conversation memory

#### `log_message(user_id, role, content)`
Appends one row to `conversation_messages`. `role` must be exactly
`'user'` or `'assistant'`. Empty content is silently skipped so we don't
pollute the window with blank sends. **Called from:** `_send` (assistant
turns), `handle_text` + `handle_photo` (user turns).

#### `get_recent_conversation(user_id, hours=16, min_messages=20) → list[{role, content}]`
Returns the rolling-window history shaped for Claude's API. Selection
rule: last `hours` hours OR last `min_messages` messages — whichever is
**larger**. Slow-day users still see ≥ `min_messages` of context;
busy-day users see everything recent. Consecutive same-role messages
are merged with a blank line between them, because Claude's API
requires strict alternation between user and assistant turns.
**Called from:** `handle_text` (intent / correction / question).

#### `purge_conversation_older_than(days=14) → row_count`
Hard-deletes conversation rows older than N days (global, not per-user).
Called once a week by the Saturday lint cron.

### 5.10 Test cleanup

#### `delete_today_meals(user_id) → row_count`
Hard-delete (not soft-delete) of every meal logged today. Drops
dependent `corrections` rows first to honor the FK with
`foreign_keys=ON`. All in one transaction.
**Called from:** `cmd_reset_today`.

`delete_today_stats` / `delete_today_conversation` — hard-deletes the
`daily_stats` row for today and every `conversation_messages` row
stamped with today's date.

---

## 6. Module reference: `wiki.py`

Per-user long-term memory. Each user has a folder of five markdown pages
on disk; this module is the read/write layer plus a few small helpers
(canonical calorie line, one-time migrations, today-stamp stripping). The
ingest pipeline that decides WHAT to write lives in `advisor.py`.

### 6.1 Paths + locks

#### `user_wiki_dir(user_id) → Path`
Returns the folder for this user (`WIKI_DIR` or `./wiki` + `user_<id>/`).

#### `get_lock(user_id) → asyncio.Lock`
Returns the per-user `asyncio.Lock`, creating it on first access.
Different users don't block each other; concurrent writes for the SAME
user are serialized. Held by every place that mutates the wiki:
`ingest_interaction`, `/goal`, `/reset_today`, `lint_user_wiki`,
`contradictions.resolve`.

### 6.2 Init

#### `ensure_user_wiki(user_id)`
Creates the user's wiki folder and copies the five template pages from
`wiki_templates/` if they don't exist. Idempotent — returns immediately
if already initialized. Called from `db.ensure_user`, so the wiki is
always present by the time any handler reads it.

### 6.3 Read

| Function | Returns |
| --- | --- |
| `read_page(user_id, page_name) → str` | Raw markdown of a page (empty string if missing). Used everywhere except the prompt-formatting helper below. |
| `read_all_pages(user_id) → dict[name → str]` | All five pages in one call. Used by `/profile` and `contradictions.detect`. |
| `read_wiki_for_prompt(user_id) → str` | Format every populated page as one block ready for an LLM prompt. Skips pages that are still the empty template placeholder. |

### 6.4 Write

#### `strip_empty_placeholder(content) → str`
Removes the `_(Empty — the bot will populate this…)_` line that the
templates ship with. Self-healing — runs on every append, no-op once
the line is gone.

#### `write_page(user_id, page_name, content)`
Overwrite a page. Caller is responsible for holding `get_lock(user_id)`
if concurrent writes are possible. Validates the page name against the
canonical `PAGES` list.

#### `append_log(user_id, summary, details='')`
Appends a `## [YYYY-MM-DD] summary` section to `log.md` (plus `details`
body if supplied). `log.md` is append-only — lint never rewrites it.
Every wiki append, remove, replace, lint pass, and contradiction
resolution leaves a breadcrumb here.

### 6.5 Calorie goal — the canonical line

The single bullet `- **Daily calorie goal**: N kcal` lives in `goals.md`
and is the only line the bot reads programmatically. The lenient
`_CALORIE_GOAL_RE` matches that exact shape AND tolerates legacy
phrasings (*"Target 1850 kcal/day"*, *"Goal: 2000 kcal"*) so the
consolidator can clean drift up. Goal-shape lines for OTHER targets
(kg, sugar, protein) never match because the regex requires the literal
`kcal` unit.

#### `get_daily_kcal(user_id, default=2000) → int`
Read the calorie goal from `goals.md`. Returns `default` if the line is
missing or malformed. Never crashes on hand-edits or LLM drift.

#### `set_daily_kcal(user_id, kcal)`
Upsert the canonical calorie bullet. Strips EVERY existing
calorie-goal-ish line (canonical or legacy) and inserts exactly one
canonical bullet right under the `# Goals` heading. Non-calorie bullets
(kg, sugar, protein) are preserved byte-for-byte. Caller holds
`get_lock(user_id)`.

#### `consolidate_goal_line(user_id) → {found, kept, rewrote}`
Run by `/lint` and the post-ingest lint. If `goals.md` ends up with 2+
calorie-goal-ish lines (Haiku occasionally produces *"Target 1850
kcal/day"* alongside the canonical bullet), this collapses them: keep
the LAST number (later wins), rewrite to canonical shape. Idempotent.

#### `_rewrite_goals_with_canonical(content, canonical) → str`
Shared helper used by both `set_daily_kcal` and `consolidate_goal_line`.

### 6.6 One-time SQL → wiki migrations

#### `migrate_sql_profile_if_needed(user_id, sql_profile) → bool`
Copies legacy `user_profile.profile` text into `profile.md`. Stamps an
HTML-comment marker into the file so it runs at most once per user.

#### `migrate_sql_goal_if_needed(user_id, sql_kcal) → bool`
Copies legacy `users.daily_kcal` into `goals.md` as the canonical
bullet via `set_daily_kcal`. Stamps a separate marker for idempotency.

After both migrations run once, `ensure_user` becomes essentially free
on the hot path.

### 6.7 Today-stamp stripping (test cleanup)

#### `strip_today_lines(user_id, today_str=None) → dict[page → count]`
Deletes every line stamped `[YYYY-MM-DD]` for `today_str` (defaults to
today) from every wiki page, plus continuation lines belonging to
today-stamped multi-line bullets. Called by `/reset_today` so a day's
worth of test-generated bullets can be wiped without touching the
user's real long-term memory. Caller holds `get_lock(user_id)`.

---

## 7. Module reference: `analyzer.py`

Every Claude API call EXCEPT the ones in `advisor.py` and `lint.py`.
Three shapes of work happen here: parsing input the user sent (vision
on photos, text-to-nutrition, intent classification, correction
resolution), parsing iPhone Shortcut messages, and one-off LLM helpers
(date extraction, profile updates, activity calorie estimates).

Model choice is consistent: vision uses `claude-opus-4-6` (best image
understanding); text food parsing, intent, correction, date extraction,
profile updates, and activity estimation use `claude-haiku-4-5` (cheap,
fast, narrow tasks). Sonnet 4.6 is reserved for `advisor.py`'s
open-ended reasoning.

### 7.1 System prompts

Three vision/text prompts live as module-level string constants. Each
spells out the JSON output schema and the rules the model must follow.
**The prompts are the most-edited part of the codebase** — any
nutrition/UX behaviour you want to change probably lives here.

- `FOOD_SYSTEM_PROMPT` — used by `analyze_food_photo`. Identifies every
  distinct component of a plate, names the dish, embeds gram weights,
  handles countable multi-piece items with the `× Npcs` marker. Two
  naming rules:
    - **Rule 1 (single standalone item):** one apple →
      `dish_name = "Apple"`, regardless of caption.
    - **Rule 2 (composed dish, 2+ items):** descriptive name where
      possible (`"Caesar Salad"`, `"Udon Bowl"`, `"Avocado Toast"`),
      otherwise size-prefixed `"Small Plate"` / `"Medium Plate"` /
      `"Big Plate"` by ingredient count. Applies regardless of caption.
  The prompt's anti-hallucination clause forbids the words
  `"Breakfast"`, `"Lunch"`, `"Dinner"`, or `"Snack"` inside `dish_name`
  — those words belong in the `meal_type` field, never the `dish_name`.
  `meal_type` comes ONLY from the user's caption; if the caption is
  silent, `meal_type` is `null` and `database.classify_meal_type`
  fills it in from time of day (see §3.3).
- `LABEL_SYSTEM_PROMPT` — used by `analyze_label_photo`. Reads the
  nutrition table, scales macros to the user's portion size (caption →
  label serving → realistic per-product default in that order), always
  embeds a portion weight in the dish string.
- `TEXT_SYSTEM_PROMPT` — used by `analyze_text`. Same naming rules,
  same anti-hallucination clause as the photo prompt, applied to
  plain-text descriptions instead of images.
- `CORRECTION_SYSTEM_PROMPT` — used by `resolve_correction`. The
  *"how to interpret a correction"* rulebook; lists eight action types
  (`update`, `update_many`, `delete`, `delete_many`, `scale_dish`,
  `scale_dish_grams`, `delete_duplicates`, `add_items`) with examples.
- `INTENT_SYSTEM_PROMPT` — used by `detect_intent`. Classifier prompt
  with seven primary intents plus three special tokens (`command`,
  `health_update`, `workout_log`) emitted before the LLM call.

### 7.2 JSON helpers

#### `_extract_json(text) → list[dict]`
Strips markdown code fences, finds the outermost `[...]` block, parses
it, and normalizes every item's keys (filling missing macro fields with
0, coercing numbers to float, defaulting confidence to medium). Returns
`[]` on parse failure. **Called from:** `analyze_food_photo`,
`analyze_label_photo`, `analyze_text`.

#### `_encode_image(image_bytes) → str`
Base64-encode for the Claude vision payload.

#### `_looks_like_label(caption) → bool`
Lightweight keyword check on the caption (`label`, `package`,
`per 100g`, multilingual variants). Used to route between food and
label vision prompts without an extra LLM call.

### 7.3 Public vision + text APIs

#### `analyze_food_photo(image_bytes, caption=None) → list[dict]`
Send the image + caption to Opus 4.6 with `FOOD_SYSTEM_PROMPT`. Returns
a list of items ready for `db.log_meal_items`.

#### `analyze_label_photo(image_bytes, caption=None) → list[dict]`
Same shape but with `LABEL_SYSTEM_PROMPT`. Output is always exactly ONE
item (the product) with macros scaled to the user's actual portion.

#### `analyze_photo(image_bytes, caption=None) → (items, source)`
Smart router. Calls `_looks_like_label` on the caption to decide
between the food and label prompts. Returns `(items, 'photo'|'label')`
so the caller can stamp the right `source` on the meal rows.
**Called from:** `telegram_bot.handle_photo`.

#### `analyze_text(text) → list[dict]`
Plain-text food descriptions parsed by Haiku. Uses `TEXT_SYSTEM_PROMPT`.
**Called from:** `handle_text` (`log_text` branch).

### 7.4 Correction resolver

#### `resolve_correction(user_message, recent_meals, last_batch=None, conversation=None) → list[dict]`
Given the user's correction text, today's meal candidates, and
(optionally) the last logged batch + the conversation transcript, ask
Haiku to emit a JSON ARRAY of action dicts — one per requested change.
Multiple corrections in one message are supported (*"change feta to 70g
and remove hummus"*).

The conversation transcript is rendered as plain text and dropped
**inside one user turn** — passing it as the raw `messages` field made
Haiku follow the assistant role pattern and continue the dialogue
(writing recipes, etc.) instead of emitting JSON. Same trick used in
`detect_intent`.

On JSON-parse failure, returns a single `action: none` entry and logs
the raw response.

### 7.5 Intent router

#### `_format_transcript(history) → str`
Render conversation history as a plain-text *"User: …"* / *"Bot: …"*
transcript. Assistant turns longer than 400 characters are truncated —
the router only needs the gist of what the bot just said.
**Called from:** `detect_intent`, `resolve_correction`.

#### `detect_intent(text, history=None) → str`
Three short-circuits before the LLM call: messages starting with `/`
return `"command"`; messages starting with the iPhone Shortcut prefixes
(`📊 health`, `🏋️ workouts`) return `health_update` / `workout_log`.
Otherwise asks Haiku for one of: `log_text`, `correction`, `question`,
`cmd_today`, `cmd_date_query`, `cmd_week`, `cmd_goal`, `cmd_lint`,
`remember`. Cleans the LLM reply tolerantly (strips quotes, backticks,
punctuation) and falls back to **keyword heuristics on API errors** —
including "change", "fix", "wrong", "actually", "remove", "delete"
(and Russian "поменя", "измени", "исправ", "удали") → `correction`;
"how", "what", "сколько" or trailing `?` → `question`; otherwise
`log_text`.

### 7.6 Date + goal + activity helpers

#### `extract_query_date(text, today_str) → (date_str, label)`
*"What did I eat on Tuesday?"* → `("2026-04-21", "Tuesday (April 21st)")`.
Haiku-driven; falls back to yesterday on parse failure.

#### `parse_health_message(text) → dict`
Pure regex parser for the iPhone Shortcut's health snapshot. No LLM call.

#### `parse_workout_message(text) → list[dict]`
Pure regex parser for the workout list. No LLM call.

#### `estimate_activity_calories(steps, workouts, weight_kg, user_profile) → dict`
BMR (Mifflin-St Jeor) × 1.2 sedentary multiplier + walking calories from
steps + per-workout estimates. The LLM extracts age/gender/height from
the wiki profile if available, otherwise uses defaults (35F 165cm) and
surfaces an `assumed` note.

#### `update_profile(current_profile, new_message) → dict`
Legacy free-text profile updater (Haiku). Returns either
`{understood: true, profile: …}` or `{understood: false, ask: …}`.
After the wiki migration this is no longer on the hot path.

#### `extract_goal_from_text(text) → int|None`
Regex pull of a 3–5 digit number bounded by 500–10000.
*"set my goal to 1800 calories"* → `1800`.

---

## 8. Module reference: `advisor.py`

Output. Every reply text the user sees comes from here, except the
contradiction prompts and the lint summary. Also owns the wiki ingest
pipeline — the LLM-driven *"file this fact"* decision after a
self-statement or self-question.

### 8.1 Formatting helpers

These power every reply with a meal/macro block. They're internal but
understanding them helps when tweaking the user-facing text.

- `_food_emoji(dish) → str` — `FOOD_EMOJI_MAP` is a long ordered list of
  `(keywords, emoji)` pairs covering English, Russian, German, and a few
  other languages. First match wins. Falls back to 🍽 if nothing matches.
- `_fmt_meal(m) → str` — One-line render of a single meal row.
- `_fmt_totals(totals, goal) → str` — Macro totals block with a
  10-segment progress bar against the calorie goal, plus *"X kcal left
  today"* / *"X over goal"*.
- `_parse_grams(dish)`, `_parse_pcs(dish)`, `_dish_stem(dish)`,
  `_consolidate_items(items)` — Regex helpers for the dish strings.
  `_consolidate_items` collapses identical ingredients (three
  *"Fried egg 60g"* rows → one *"Fried egg × 3pcs 180g"*) so corrections
  that add a piece don't show up as a stray duplicate.
- `_fmt_dish_group(items) → str` — Render one dish (potentially many
  ingredients) as either a single line or a header + indented ingredient
  list. Multi-ingredient blocks always run through `_consolidate_items`.

### 8.2 Post-log confirmation

#### `log_confirmation(items, user_id) → str`
The reply text shown immediately after a meal is logged. Three rendering
branches: single standalone item, single dish with multiple ingredients,
or multiple dishes. Always followed by a totals block. Adds a 🚫 alert
if the user crossed 100% of the goal, a ⚡ heads-up if they crossed 80%,
and a 🔍 *"low confidence"* note if any item was tagged low-confidence
by the analyzer.

### 8.3 Activity rendering

`_workout_emoji(name) → str` and `_fmt_activity(stats) → str` — emoji
map and a renderer for the activity block (weight + steps + per-workout
lines + total burn).

### 8.4 Day-level summaries

#### `day_summary(user_id, for_date, label, *, include_totals=True) → str`
On-demand log + macros + activity for any calendar date. Groups meals
by `meal_type` in the canonical breakfast→lunch→dinner→snack order.
`include_totals=False` is used by `evening_summary`, which builds its
own totals block at the TOP and doesn't want a duplicated one inside
the meals section.

#### `today_summary(user_id) → str`
Wrapper around `day_summary` for today. Backs both `/today` and the
natural-language *"show today"* intent.

#### `daily_morning_summary(user_id) → str`
Short motivational summary of yesterday + a nudge to log breakfast
today. Reserved for the morning push (currently the cron is wired only
to evening + Sunday + Saturday — see §12).

### 8.5 Evening + weekly review

#### `evening_summary(user_id) → str`
21:00 push. Three blocks in order: macro totals at the TOP, then a
3-paragraph Sonnet-written analysis (what went well today + a 5-day
pattern + one tip for tomorrow), then the meal-by-meal day summary at
the bottom.

#### `weekly_review(user_id) → str`
Sunday push. Header with computed stats (this-week avg vs past-month
avg, on-track day count, weight trend if recorded), then a 3-paragraph
Sonnet review (this-week verdict, one longer-arc pattern, one concrete
recommendation for next week). Reads up to 30 days of history for the
long-arc paragraph.

### 8.6 Q&A

#### `answer_question(user_id, question, conversation=None) → str`
Open-ended nutrition Q&A. Uses Sonnet with a long system prompt that
injects today's meals, the past week's totals, and the wiki content.
The system prompt encodes a HARD character budget (3500 chars) so the
answer fits in one Telegram message; for broad questions the model is
told to give a compact tour rather than deep-dive into one angle, and
to end with *"want me to go deeper on X?"*.

If supplied, `conversation` is passed as the `messages` list directly
so follow-ups (*"and what about cardio?"*, *"option 2"*, *"how many
grams?"*) retain context. A safety net appends a paused-here hint if
the model still hits `max_tokens` despite the budget.

### 8.7 Wiki ingest — the long-term-memory writer

Every self-statement (*"I'm cutting sugar"*) and self-question (*"am I
low on protein?"*) fires a fire-and-forget background task that asks
Haiku whether anything is worth filing into the wiki, and applies
whatever updates the LLM proposes. Errors are caught and logged to
`./ingest.log` — never raised — so a bad ingest can't break the user's
reply flow.

#### `_apply_wiki_update(user_id, upd)`
Apply one structured update to the wiki. Four supported actions:

- `append` — add a new bullet (`- [today] text`) to
  `patterns`/`profile`/`goals`/`wins`. Bullet markers are normalized to
  a single `- ` so we don't end up with double markers (`- • foo`). The
  empty-placeholder line is stripped if still present. Every append
  leaves a breadcrumb in `log.md`.
- `remove_line` — delete one or more bullet lines on a page by
  case-insensitive substring match against the user-supplied target.
  Only bullet lines are eligible — headers, comments, and blank lines
  are preserved. Multi-match removals are logged with a warning. Every
  removal leaves a breadcrumb in `log.md`.
- `replace_line` — surgical in-place rewrite. Used when the user
  retracts ONE fact from a multi-fact bullet (*"Does not eat olive oil;
  prefers healthy fats"* → keep the second half). `new_content`
  replaces the entire bullet; only the first match is rewritten to stay
  conservative.
- `log_entry` — append a dated section to `log.md` (only emitted when
  the LLM explicitly wants a custom audit note; the three actions above
  already write log breadcrumbs automatically).

#### `ingest_interaction(user_id, interaction_type, user_message, bot_reply='') → None`
The ingest pipeline. Holds the per-user wiki lock, builds an
`_INGEST_PROMPT` that includes the WHOLE wiki (so the LLM can scan for
duplicates), the wiki rulebook from `wiki_instructions.md`, and the
recent interaction. Asks Haiku to return JSON describing zero or more
updates. Each update is applied through `_apply_wiki_update`.
Fire-and-forget — never raises. After the lock is released, if anything
was applied, schedule a background lint pass via
`_schedule_post_change_lint`.

#### `_schedule_post_change_lint(user_id)` / `_run_post_change_lint(user_id)`
Debounced lint scheduler. After every successful ingest write, kick off
a background lint over the four lintable pages so dedup, supersede, and
stale-removal happen within seconds rather than only on the weekly
cron. If a lint for this user is already pending we skip — the running
pass already sees the latest state.

#### `schedule_ingest(user_id, interaction_type, user_message, bot_reply='')`
Public entry point — fire and forget. `asyncio.create_task` wraps
`ingest_interaction`. Strong reference held in a module-level set so
the GC can't kill an in-flight task.
**Called from:** `handle_text` (`remember` + `question` branches).

---

## 9. Module reference: `lint.py`

Periodic tidy-up over each user's wiki. Triggered both reactively
(debounced after every ingest write — see `_schedule_post_change_lint`
in `advisor.py`) and on a weekly cron (Saturday 10:05 Berlin).

**Scope:** rewrite `profile.md` / `goals.md` / `patterns.md` /
`wins.md` by deduping, superseding by date prefix, and dropping stale
time-bounded goals. **Never touches `log.md`** (audit trail). Always
backs up the previous version of each rewritten page (last 3 kept per
page in the user's `.backups/` folder).

**Model:** Haiku 4.5 — narrow text task, cheap, fast.

### 9.1 Per-page guidance

The `_PAGE_GUIDANCE` dict supplies a tailored sub-prompt for each
lintable page:

- **profile** — dedup + supersede on weight/age/dietary lines.
- **goals** — dedup, supersede, drop-stale-time-bounded (relative-phrase
  windows like *"for the next week"* → 7 days), and drop-expired-explicit-deadlines.
- **patterns** — dedup + supersede only.
- **wins** — dedup ONLY. Past achievements never expire.

Pre-convention lines (no `[YYYY-MM-DD]` prefix) are left alone unless
an explicit date in the bullet text has passed. Lint never invents a
date for an unprefixed line.

### 9.2 Helpers

- `_is_empty_page(content) → bool` — True if the page has no bullet
  lines. (We used to test for the `_(Empty` substring, which kept
  matching once ingest appended a real bullet underneath.)
- `_line_count(content) → int` — Count non-blank lines, for before/after
  stats.
- `_backup_page(user_id, page_name, content) → Path` — Save to
  `wiki/user_<id>/.backups/<page>.md.<timestamp>`. Keeps only the most
  recent `BACKUP_RETENTION` (=3) backups per page.

### 9.3 Per-page Claude call

#### `_lint_page(page_name, content) → str`
Send one page to Haiku with the page-specific guidance + the global
rules + today's date. Strips code fences if Haiku added them. Two
sanity checks before accepting the output:

- Empty result → keep original.
- Output longer than 1.1× the input (with a 50-char floor) → reject and
  keep original. Lint should shrink or hold steady; growth is a sign of
  hallucination.

On any API/parse failure, returns the original content unchanged.

### 9.4 Audit trail

#### `_append_log(user_id, per_page)`
Append a dated `## [YYYY-MM-DD] Lint pass` section to `log.md` with one
bullet per page. Only writes the section if at least one page was
actually rewritten — otherwise `log.md` would flood with noise from the
post-ingest lint that fires after every wiki change.

### 9.5 Main entry

#### `lint_user_wiki(user_id) → result dict`
Run a lint pass over every lintable page. Holds the per-user wiki lock
for the whole pass so a concurrent ingest can't write between our read
and our write. Each page goes through: empty-check, `_lint_page` call,
sanity check, backup, `write_page`. Results dict is
`{page: {before, after, rewritten}}` or `{page: {skipped: 'empty'}}`.

After all pages, runs `wiki.consolidate_goal_line` as a deterministic
safety pass on `goals.md` (collapsing any calorie-goal drift Haiku
didn't catch).

**Called from:** `cmd_lint`, `run_lint_cron`, `_run_post_change_lint`.

---

## 10. Module reference: `contradictions.py`

Step 4b of the long-term-memory plan. Catches cross-page conflicts in
a user's wiki (*"profile says 'does not eat fish' but log says you ate
salmon last week"*) and resolves them through natural-language dialog
with the user.

The contradiction lives in a separate file, `contradictions.md`, in
the user's wiki folder. It has OPEN sections (still awaiting input)
and RESOLVED sections (history). The bot DMs the user about ONE
oldest-open at a time so the inbox doesn't get spammed.

### 10.1 IO helpers

- `_contradictions_path(user_id)`, `_ensure_file(user_id)`,
  `_strip_html_comments(content)` — locate / create / preprocess
  `contradictions.md`. `_strip_html_comments` blanks out comment blocks
  with same-length whitespace so the section regex doesn't match the
  example headers inside the file-level documentation comment, while
  preserving byte offsets.
- `_parse_sections(content) → list[dict]` — split the file into
  section dicts: `{ts, status, action, page_a, line_a, page_b, line_b,
  why, ask, raw, start, end}`. Each section is `## [TS] OPEN` or
  `## [TS] RESOLVED → action`, with the A/B/Why/Ask bullets parsed out.
- `oldest_open(user_id) → dict|None` — the OPEN section with the
  earliest timestamp, or None.
- `list_open(user_id) → list[dict]` — every OPEN section. Reserved.

### 10.2 Detection

#### `detect(user_id) → list[dict]`
Reads `profile`/`goals`/`patterns`/`wins` (skipping empty pages) plus
the log tail, asks Haiku for real contradictions. Conservative — the
prompt explicitly tells it that returning `[]` is a perfectly good
answer, and includes positive AND negative examples.

Each returned conflict is `{page_a, line_a, page_b, line_b, why, ask}`.
`ask` is the user-facing question Haiku writes at detection time —
short, conversational, no file names, dates phrased naturally. The DM
code reads `ask` back verbatim so phrasing survives a bot restart.

### 10.3 Recording

#### `record(user_id, conflicts) → list[ts]`
Appends new OPEN sections to `contradictions.md`, skipping anything
that's already present in any state (OPEN or RESOLVED). Returns the
timestamps of newly-added sections. Order-agnostic dedup via
`_same_pair` (`page_a`/`page_b` can be swapped).

### 10.4 Classifier

#### `classify_user_reply(contradiction, user_message) → dict`
When a contradiction is OPEN and the user sends a text, this Haiku
call decides which of six actions the message corresponds to:

- `pick_a` / `pick_b` — keep one, discard the other.
- `keep_both` — *"both are valid, no real conflict."*
- `remove_both` — drop both.
- `custom` — user gave a new value to write in. Comes with `custom_text`
  + `target_page`.
- `unrelated` — the message is about something else; fall through to
  the normal intent router.

On any failure, returns `{action: "unrelated"}` so the user's message
routes normally.

### 10.5 Resolution

Internal write helpers:

- `_normalize_for_match(s)` — strip leading bullet chars + whitespace.
- `_remove_line_from_page` — refuses to write to `log.md` (audit-only).
- `_append_line_to_page` — adds a `- [today] text` bullet using the
  same date-prefix convention as ingest.
- `_update_section_to_resolved` — flips the `## [ts] OPEN` header to
  `## [ts] RESOLVED → action` and appends a `- **Resolution** (today): …`
  bullet.

#### `resolve(user_id, ts, action, custom_text=None, target_page=None) → result`
Apply the resolution under the per-user wiki lock — atomic across all
wiki edits and the `contradictions.md` update. Branches by action and
writes the appropriate user-facing summary, `log.md` breadcrumb, and
contradictions.md state flip. Returns `{ok, action, summary, log_entry}`.

---

## 11. Module reference: `telegram_bot.py`

Entry point. Long-poll Telegram bot, command/handler dispatch, and the
cron entry points triggered by systemd cron with `--evening-summary`,
`--weekly-review`, `--lint-cron` flags.

### 11.1 Helpers

#### `_safe_reply(text) → str`
Truncate to 4000 chars so Telegram doesn't reject the message.

#### `_typing(update)`
Send the *"…typing"* indicator. Called before every LLM-bound handler.

#### `_send(update, text, **kwargs)`
Replacement for `update.message.reply_text` that ALSO records the
assistant turn in `conversation_messages`. Same signature, same return
value — migration is a mechanical rename. Logging is best-effort: if
the DB write fails the reply still goes out. `parse_mode` and other
rendering kwargs are deliberately not stored.

#### Transient-error recovery helpers (issue #18)

A small layer that catches Anthropic overload / gateway / rate-limit
errors and gracefully recovers — see §4 *Transient-error recovery* for
the user-facing behaviour.

- `_is_transient_api_error(e) → bool` — true for HTTP 502/503/504/529,
  `RateLimitError`, `APIConnectionError`, `APITimeoutError`.
- `_truncate_for_echo(text, limit=200)` — shorten user text for inclusion
  in friendly retry messages.
- `_send_overload_ack(update, original_text)` — first friendly message;
  sent when the SDK's inline retry exhausts and we begin background
  retry. Echoes the user's original text.
- `_send_overload_giveup(update, original_text)` — final friendly
  message; sent when background retries also exhaust. Includes original
  text so the user can resend without retyping.
- `_background_retry(user_id, update, original_text, work_fn)` — runs
  in `asyncio.create_task`. Sleeps 30s between attempts, up to 6 attempts
  (~3 min total). On success, exits silently (`work_fn` already sent its
  own reply). On permanent error or attempt exhaustion, sends the
  giveup message.
- `_run_with_recovery(update, original_text, work_fn)` — wraps each
  LLM-using branch in `handle_text` / `handle_photo`. Runs `work_fn`
  inline first; if it raises a transient API error, sends the ack and
  schedules a background retry. Permanent errors propagate.

State: `_pending_retries: dict[user_id, asyncio.Task]` holds at most one
pending retry per user. A new user message cancels the previous task.

### 11.2 Slash command handlers

- `cmd_start` / `cmd_help` — welcome message + command list.
- `cmd_today`, `cmd_week`, `cmd_goal` — thin wrappers over
  `advisor.today_summary`, `advisor.weekly_review`, and the calorie-goal
  read/write path. `cmd_goal` validates the number is in `[500, 10000]`.
- `cmd_profile` — reads every wiki page via `wiki.read_all_pages`, runs
  each through `_clean_wiki_for_display` (strips HTML comments,
  headings, horizontal rules, the empty-placeholder line, ingest date
  prefixes, bold/italic markers, collapses blank lines between
  bullets), and renders one section per populated page with an emoji
  header (👤 Profile, 🎯 Goals, 📊 Patterns, 🏆 Wins).
- `cmd_lint` — three phases. **Phase 1:** run `lint.lint_user_wiki`
  under the per-user wiki lock; build a per-page user reply. **Phase 2:**
  run `contradictions.detect` on the cleaned wiki, record any new
  conflicts. **Phase 3:** if any OPEN contradiction remains, DM the
  user about the oldest one. Errors in phases 2/3 don't block phase 1.
- `cmd_reset_today` — test cleanup. Wipes today's meals (hard-delete +
  dependent corrections), today's `daily_stats`, today's
  `conversation_messages`, and every `[today]`-stamped wiki line — all
  under the per-user wiki lock.

### 11.3 Message handlers

- `handle_photo(update, ctx)` — album-caption caching → highest-res
  download → `analyzer.analyze_photo` → `db.log_meal_items` →
  synthetic conversation log line → `advisor.log_confirmation` reply.
- `handle_voice(update, ctx)` — download `.ogg` → OpenAI Whisper-1 →
  echo back the transcription → forward the text to `handle_text`.
- `handle_text(update, ctx)` — the big one. Records the user turn,
  fetches conversation history once, runs the contradiction pre-route,
  then asks `analyzer.detect_intent` for the routing word and branches
  accordingly. The correction branch in particular has the
  integrity-guard logic that rejects hallucinated `meal_id`s before
  they reach the database.

### 11.4 Cron entry points

- `_push_to_all_users(message_fn, bot)` — iterate over every `users`
  row, call `message_fn(user_id) → text`, send via Telegram. Silent on
  per-user failures.
- `run_daily_summary`, `run_evening_summary`, `run_weekly_review` —
  wrappers over `_push_to_all_users` that select the appropriate
  advisor function.
- `_format_contradiction_dm(c) → str` — friendly DM body for an OPEN
  contradiction. Uses Haiku's `ask` field; falls back to plain rendering
  for legacy sections.
- `run_lint_cron()` — Saturday 10:05 Berlin job. For every user: lint
  the wiki, detect new contradictions, record new ones, DM the oldest
  open. After the per-user loop, runs
  `db.purge_conversation_older_than(14)` once globally.

### 11.5 `main()`

`init_db()` first, then branch on CLI flags: `--daily-summary` /
`--evening-summary` / `--weekly-review` / `--lint-cron` each invokes
the matching cron entry point. Otherwise builds the python-telegram-bot
Application, registers commands and message filters, and starts
long-poll with `drop_pending_updates=True` so a freshly-restarted bot
doesn't reprocess old queued messages.

---

## 11A. Module reference: `withings_sync.py` + `setup_withings.py`

Two small scripts that handle the Withings Health API integration. Issue
#7 introduced these to pull weight readings into `weight_readings`
automatically.

### `setup_withings.py` — one-time OAuth bootstrap

Interactive script run once per user (typically just you, the owner of
the bot). Walks through the Withings OAuth 2.0 authorization flow:

1. Reads `WITHINGS_CLIENT_ID` / `WITHINGS_CLIENT_SECRET` /
   `WITHINGS_REDIRECT_URI` from `.env`.
2. Builds an authorization URL with the `user.metrics` scope (weight
   readings) and prints it.
3. You open the URL in your browser, log into Withings, click *Allow
   access*. Withings redirects to the redirect URI with a `?code=...`
   query parameter (the redirect page may not exist; the *code* in the
   URL bar is what matters).
4. You paste the code back into the terminal.
5. The script POSTs the code to Withings' token endpoint, gets back an
   access + refresh token, and stores them in the `withings_auth` table
   for the given Telegram user_id.

Run: `python setup_withings.py --user <telegram_user_id>`. Required env
vars must be set first.

### `withings_sync.py` — hourly polling

Run by cron. For every user in `withings_auth`:

1. **`ensure_fresh_token(user_id)`** — if the access token expires within
   ~10 minutes, refresh via `refresh_access_token`. Withings' refresh
   sometimes rotates the refresh token too; we store whichever comes
   back. Token expiry timestamps are ISO UTC.
2. **`fetch_recent_weights(user_id, days=2)`** — POST to
   `https://wbsapi.withings.net/measure` with `action=getmeas`,
   `meastype=1` (weight), and a window covering the last `days` days.
   Withings encodes measurements as `(value, unit)` where the real number
   is `value × 10^unit` (e.g. `(67230, -3)` = `67.230 kg`). The decoder
   handles that. Returns a list of `{measured_at, weight_kg}` dicts.
3. **`db.insert_weight_reading()`** for each. The `UNIQUE(user_id,
   measured_at)` constraint silently dedups the overlap with the previous
   hour's fetch.

CLI:
- `python withings_sync.py` — sync all connected users.
- `python withings_sync.py --user <id>` — sync one user.
- `python withings_sync.py --days N` — fetch the last N days (default 2).

Errors are logged to `withings.log` (gitignored). Anything that returns
non-zero `status` from Withings (bad token, rate limit, etc.) is logged
and the script moves on to the next user — no user gets stuck.

## 12. Cron triggers and proactive pushes

All times are **Berlin wall-clock**. The server's OS timezone is set to
`Europe/Berlin` (Ubuntu's cron daemon ignores the `CRON_TZ` directive —
the OS timezone is the actual source of truth).

| Cron schedule | Command | What it does |
| --- | --- | --- |
| `0 21 * * *` (every day 21:00) | `telegram_bot.py --evening-summary` | `run_evening_summary` → push `advisor.evening_summary` to every user (totals → AI analysis → meal-by-meal). |
| `0 9 * * 0` (Sunday 09:00) | `telegram_bot.py --weekly-review` | `run_weekly_review` → push `advisor.weekly_review` (3-paragraph Sonnet review covering up to 30 days). |
| `5 10 * * 6` (Saturday 10:05) | `telegram_bot.py --lint-cron` | `run_lint_cron` → for every user: lint the wiki, detect new contradictions, DM the oldest open one if any. Then global `purge_conversation_older_than(14)`. |
| `7 * * * *` (every hour at :07) | `withings_sync.py` | For each user in `withings_auth`: refresh access token if needed, pull last 2 days of weight readings from Withings, insert any new ones into `weight_readings`. `daily_stats.weight_kg` is automatically refreshed to the day's min. |

Inside the bot process, two more triggers run reactively rather than on
a schedule:

- **Post-ingest lint** (`advisor._schedule_post_change_lint`). After
  every wiki write — `append`, `remove_line`, `replace_line` — a
  background lint over the four lintable pages is scheduled. Debounced
  one-per-user. Errors swallowed.
- **80%/100% calorie-goal alert** (`advisor.log_confirmation`).
  Computed on every meal log; appended to the same reply that confirms
  the meal. Crosses 80% → ⚡ heads-up; crosses 100% → 🚫 alert.

### What the user sees vs what the user doesn't see

| User-visible | Internal-only |
| --- | --- |
| Evening summary (21:00 daily) | Post-ingest lint runs |
| Sunday weekly review (Sun 09:00) | Conversation-message purge runs |
| Saturday contradiction DM (Sat 10:05, only when something is open) | Lint backups under `.backups/` |
| 80%/100% goal alert (in-line with the meal-log reply) | `ingest.log`, `lint.log`, `contradictions.log` breadcrumbs |

---

## 13. Deployment and configuration

### Servers + processes

The bot runs on a single server (Hetzner VPS in production) under
systemd. The systemd unit `nutrition-bot.service` runs `telegram_bot.py`
as the long-poll bot. A sibling unit `nutrition-bot-test.service`
points at the same code with a different `.env` file (test bot,
separate Telegram token, separate DB if `NUTRITION_DB_PATH` is set) —
both are restarted by `deploy.sh` when present.

### `deploy.sh` flow

1. Locally: `git push origin main`.
2. ssh to the server, `cd /opt/nutrition-bot`, `git fetch origin main`,
   `git reset --hard origin/main` (server is a pure mirror — no local
   commits to protect).
3. `pip install -q -r requirements.txt` inside the venv.
4. `chown nutrition:nutrition` the working dir, `chmod 600 .env`.
5. `systemctl daemon-reload` + `systemctl restart nutrition-bot`. If
   `nutrition-bot-test.service` is enabled, restart that too.
6. `systemctl status nutrition-bot` for a final sanity check.

### Environment variables (`.env`)

| Variable | Purpose |
| --- | --- |
| `NUTRITION_BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN`) | Token from @BotFather. **Required.** |
| `ANTHROPIC_API_KEY` | **Required.** Used by analyzer, advisor, lint, contradictions. Loaded once via dotenv at module import. |
| `OPENAI_API_KEY` | Optional. Only used for Whisper voice transcription in `handle_voice`. Without it, voice messages return an error message. |
| `NUTRITION_DB_PATH` | Optional override of the SQLite file path (defaults to `./nutrition.db`). Used by the test instance to keep its data separate. |
| `WIKI_DIR` | Optional override of the wiki base directory (defaults to `./wiki`). |
| `WITHINGS_CLIENT_ID` / `WITHINGS_CLIENT_SECRET` | Optional. From your Withings developer app at developer.withings.com. Required for the weight-sync integration; without them the hourly `withings_sync.py` cron is a no-op. |
| `WITHINGS_REDIRECT_URI` | Optional. The callback URL registered with your Withings app. Defaults to `http://localhost:8765/callback`. Used during the one-time `setup_withings.py` bootstrap; never called by the bot at runtime. |

### crontab snippet

Three lines added to root's crontab on the server:

```
0 21 * * * cd /opt/nutrition-bot && /opt/nutrition-bot/venv/bin/python telegram_bot.py --evening-summary
0  9 * * 0 cd /opt/nutrition-bot && /opt/nutrition-bot/venv/bin/python telegram_bot.py --weekly-review
5 10 * * 6 /opt/nutrition-bot/venv/bin/python /opt/nutrition-bot/telegram_bot.py --lint-cron
```

---

## 14. Common cross-cutting flows

### "User logs a 6-component plate, then says 'remove the hummus'"

1. `handle_photo` runs the 6 items through `analyzer.analyze_photo` →
   `db.log_meal_items`. The batch shares one `meal_type` and one
   `dish_name`. `db.log_message` records a synthetic
   `[Photo logged …]` line.
2. On the next text turn, `handle_text` records the user turn in
   `conversation_messages`, fetches the rolling window.
3. `contradictions.oldest_open` returns None (no open conflict).
4. `analyzer.detect_intent` sees the recent photo log in the transcript
   + a deletion verb → emits `"correction"`.
5. Correction branch fetches today's full log + the last batch (the 6
   items via `db.get_last_meal_batch`).
6. `analyzer.resolve_correction` is shown the conversation transcript
   inside one user turn, the meal history JSON, AND the LAST LOGGED
   BATCH section telling Haiku exactly which 6 ids form *"this dish."*
7. Haiku returns one action — likely `action=delete` with the
   `meal_id` of the hummus row.
8. The integrity guard verifies the `meal_id` is in `candidate_ids`
   (i.e. visible in today's log).
9. `db.delete_meal` soft-deletes the row. Reply: *"🗑 Removed Hummus
   30g"* + updated totals.

### "User says 'I'm cutting sugar' as a fresh thought"

1. `handle_text` → conversation log → no open contradiction →
   `detect_intent` emits `"remember"`.
2. Reply: *"✅ Got it — updating my notes."* (neutral phrasing because
   we don't know yet whether this is an add, retract, or rewrite).
3. `schedule_ingest` fires a background task. `ingest_interaction`
   holds the wiki lock, builds a prompt with the wiki rules + current
   wiki content + the user statement, asks Haiku.
4. Haiku returns `updates=[{page: "goals", action: "append",
   content: "- [today] Cutting sugar"}]`. `_apply_wiki_update` writes
   the bullet, strips the empty placeholder if present, and appends a
   breadcrumb to `log.md`.
5. Lock released. `_schedule_post_change_lint` fires a debounced
   background lint over the four lintable pages — Haiku gets a chance
   to dedup with any existing *"reduce sweets"* bullet, etc.

### "User asks 'how much protein today?' with rich follow-up"

1. `handle_text` → conversation log → `detect_intent` emits
   `"question"`.
2. `advisor.answer_question` with the rolling-window history as
   `messages`. Sonnet sees today's meals, week totals, the wiki, AND
   the prior turns.
3. Reply sent. `schedule_ingest` fires (`interaction_type="question"`);
   the LLM may or may not file a self-question into `patterns.md`
   (*"User concerned about protein"*).
4. If the user follows up with *"and what should I aim for?"*,
   `detect_intent` reads the prior bot turn in the transcript and
   emits `"question"` again — the message is interpreted as a
   continuation, not a fresh thought. Sonnet sees both turns, references
   its own previous suggestion, and gives a numeric target.

### "Contradiction is open; user replies"

1. `handle_text` records the user turn, fetches history, then BEFORE
   running `detect_intent`, calls `contradictions.oldest_open`.
2. If a section is open, `contradictions.classify_user_reply` asks
   Haiku whether this text is a reply to that question (six possible
   actions: `pick_a` / `pick_b` / `keep_both` / `remove_both` / `custom`
   / `unrelated`).
3. If the action is anything but `unrelated`, `contradictions.resolve`
   applies the wiki edit + flips the section header + appends a
   `log.md` breadcrumb. Reply with the result summary. `handle_text`
   returns — the normal intent router never runs.
4. If the action is `unrelated`, `handle_text` falls through to
   `detect_intent` and routes the message normally.

---

## 15. Glossary

| Term | Meaning |
| --- | --- |
| **`dish` vs `dish_name`** | `dish` = the individual ingredient (*"Tofu 80g"*). `dish_name` = the parent plate (*"Udon Noodle Bowl"*). All ingredients of one plate share the `dish_name`. Standalone items have `dish == dish_name`. |
| **batch** (in `log_meal_items`) | All items inserted in one call. Share a single `meal_type`. Their `meal_id`s form a tight time cluster (well under 1 second apart) — used by `get_last_meal_batch` to know what *"this dish I just added"* refers to. |
| **soft delete** | `confidence='deleted'`. The row stays for audit; every read query filters it out. |
| **candidate set / integrity guard** | In the correction branch, the set of `meal_id`s the resolver was actually shown. Any action on an id outside this set is a hallucination and is rejected with a clarifying user message instead of being applied to the DB. |
| **`× Npcs` marker** | Embedded in the `dish` string for countable multi-piece items: *"Fried egg × 2pcs 120g"* = 2 eggs totalling 120g and the macros. Strips clean via the `_PCS_RE` regex. |
| **Tier 1 vs Tier 2 `meal_type`** | Tier 1 = caption-detected by the analyzer (*"for breakfast"* → `breakfast`). Tier 2 = auto-classified by `classify_meal_type` using the time-of-day window + 90-min inheritance rule. Tier 1 always wins. |
| **wiki ingest** | The fire-and-forget background task that asks Haiku whether a self-statement / self-question is worth filing into the user's wiki, and applies the suggested updates. |
| **lint** | The tidy-up pass over the wiki — dedup, supersede by date prefix, drop stale time-bounded goals. Runs reactively after every wiki write (debounced) and on Saturday cron. |
| **contradiction** | Cross-page conflict in the wiki (*"profile says X, log says NOT-X"*). Detected by Haiku, recorded as an OPEN section in `contradictions.md`, resolved through DM dialogue with the user. |
| **short-term memory** | `conversation_messages` table. Rolling window of last 16 hours OR 20 messages, whichever is larger. Passed to every conversational LLM call so terse follow-ups have referents. |
| **long-term memory** | The wiki — per-user folder of five markdown pages (`profile`/`goals`/`patterns`/`wins`/`log`) that the LLM curates over time. Replaces RAG over raw history with a compounding synthesized profile. |
| **the `[YYYY-MM-DD]` prefix** | Internal date stamp on every wiki bullet. Lets lint reason about recency for supersede / drop-stale decisions. Stripped from any user-facing output. |
| **per-user wiki lock** | `asyncio.Lock` per `user_id`. Held by ingest, `/goal`, `/reset_today`, lint, and contradiction resolution. Different users don't block each other; concurrent writes for the SAME user are serialized. |
| **fire-and-forget** | Pattern used for ingest, lint, and contradiction detection. The task is scheduled via `asyncio.create_task` and a strong reference is kept so the GC doesn't kill it; errors are caught and logged but never raised. |
