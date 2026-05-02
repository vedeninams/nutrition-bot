# 🥗 Nutrition Bot

A personal Telegram nutritionist. Send photos of food or nutrition labels and it logs calories + macros automatically.

## Introduction

Winter 2026: I stepped on a cactus in Tenerife. Two months without proper training and a few extra kilos later, I wanted to come back into shape by being mindful and gentle about what I eat, not exhausting myself with strict diets.

Classic buy-vs-build moment. Plenty of nutrition apps exist, but I felt something big was changing in AI roughly since December 2025 and wanted to experience the new tools first-hand, not just read about them. As a Technical Product Manager in a Platform team I work with engineers every day; keeping my hands on the craft helps me speak their language. So I am building it myself and using it as a playground to experiment — so here we are.

## Features

- 📸 **Food photo** → AI identifies every item on the plate and estimates calories
- 🏷 **Nutrition label photo** → reads the label and calculates your portion
- 💬 **Text description** → "I had oatmeal with banana" → logged
- ✏️ **Natural language corrections** → "it was a whole avocado not half" → auto-updated
- 📊 **Daily and weekly summaries** pushed automatically
- ⚡ **Limit alerts** when you hit 80% of your daily goal

## When the bot will ping you

The bot runs on Berlin time, so all the times below are Berlin wall-clock — no timezone math needed.

You don't have to do anything to get these; they just show up:

- **Every evening at 21:00** — a daily wrap-up of what you ate, your totals for the day, and a short note on how it went versus your goal.
- **Every Sunday at 09:00** — a weekly review: trends, patterns the bot noticed, and what to try next week.
- **Occasionally on Saturday mornings (around 10:05)** — if the bot spots something in its notes about you that contradicts itself (for example, two different stated goals), it will send a short message asking you to clarify. If everything looks consistent, you won't hear from it.

On top of that, if you cross 80% of your daily calorie goal during the day, the bot will send a quick heads-up so you can plan the rest of the day.

Everything else is on-demand — you send a photo or a message, the bot replies. No silent tracking, no surprise pings outside the times above.

## Architecture reference

**[`ARCHITECTURE.md`](./ARCHITECTURE.md)** is the source of truth for how
the codebase is organized — every script, every public function, when it's
called, and how the modules fit together. Read it before changing anything;
update it in the same commit when behaviour, schema, prompts, triggers, file
layout, or cron change.

## Project structure

```
nutrition-bot/
├── telegram_bot.py        ← Entry point. Routes messages.
├── analyzer.py            ← Claude vision: food/label/text → nutrition data
├── advisor.py             ← Smart replies, summaries, weekly review
├── database.py            ← SQLite: all data storage
├── wiki.py                ← Per-user long-term memory (markdown wiki)
├── lint.py                ← Background tidy pass over the wiki
├── contradictions.py      ← Cross-page conflict detection + resolution
├── wiki_instructions.md   ← Schema/rulebook for the LLM wiki maintainer
├── wiki_templates/        ← Template pages copied when a user first joins
├── ARCHITECTURE.md        ← Detailed architecture reference (read me first)
├── .env.example           ← Template for secrets
├── requirements.txt
└── README.md
```

## Memory architecture

The bot has two layers of memory:

**Short-term memory** — a rolling window of the most recent turns gets passed to the model on every reply, so the bot can follow natural dialogue ("Yes" after a question still makes sense) and resolve ambiguous messages in context (e.g. "there are two eggs" shortly after a photo is correctly treated as a correction, not a new log). Every incoming user message (including a text stand-in for photos) and every outgoing bot reply is stored in a `conversation_messages` SQL table. The reader returns **whichever is longer: the last 16 hours OR the last 20 messages** — the message floor prevents a midnight / timezone cliff from erasing mid-conversation context. The history is fed into intent classification, correction resolution, and Q&A. A weekly housekeeping job drops rows older than 14 days.

**Long-term memory (the wiki)** — each user has a small folder of markdown pages (`wiki/user_<id>/profile.md`, `goals.md`, `patterns.md`, `wins.md`, `log.md`) that the LLM incrementally maintains. Instead of re-deriving patterns from raw data every time, observations get *synthesized* into the wiki as they happen. Queries (questions, summaries, Sunday reviews) read from the synthesized wiki rather than raw history. Once a week the bot "lints" the wiki — consolidates redundancy, flags contradictions, updates progress.

This memory architecture is directly inspired by Andrej Karpathy's essay [**"LLM Wiki"**](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Karpathy's key argument: most LLM applications use raw retrieval (RAG), which means the LLM rediscovers knowledge from scratch on every question. A better pattern is an LLM-maintained persistent wiki — a compounding artifact where cross-references, synthesis, and reflections build up over time. This project is an experiment in applying that pattern to a personal nutrition coach.

The rules the LLM follows to maintain the wiki live in [`wiki_instructions.md`](./wiki_instructions.md).

The per-user wiki folder (`wiki/`) contains personal data and is gitignored — it never leaves the server.

## Setup

### 1. Create a Telegram bot

Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, follow instructions. Copy the token.

### 2. Install dependencies

```bash
cd nutrition-bot
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure secrets

```bash
cp .env.example .env
nano .env     # or open in any editor
```

Fill in:
- `NUTRITION_BOT_TOKEN` — from BotFather
- `ANTHROPIC_API_KEY` — from console.anthropic.com

### 4. Run

```bash
python telegram_bot.py
```

## Deploy to server (same as Vabali bot)

### systemd service

```ini
[Unit]
Description=Nutrition Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/nutrition-bot
ExecStart=/root/nutrition-bot/venv/bin/python telegram_bot.py
Restart=always
RestartSec=10
EnvironmentFile=/root/nutrition-bot/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nutrition-bot

[Install]
WantedBy=multi-user.target
```

Save to `/etc/systemd/system/nutrition-bot.service`, then:

```bash
systemctl daemon-reload
systemctl enable nutrition-bot
systemctl start nutrition-bot
systemctl status nutrition-bot
```

### Cron jobs for proactive push

The server's OS timezone should be set to `Europe/Berlin` so all times
below are literal Berlin wall-clock times (no `CRON_TZ` needed — Ubuntu
cron ignores that directive):

```bash
timedatectl set-timezone Europe/Berlin
systemctl restart cron
systemctl restart nutrition-bot
crontab -e
```

Add:
```
# Evening summary — every day at 21:00 Berlin
0 21 * * * cd /opt/nutrition-bot && /opt/nutrition-bot/venv/bin/python telegram_bot.py --evening-summary >> /var/log/nutrition-bot-cron.log 2>&1

# Weekly review — every Sunday at 09:00 Berlin
0 9 * * 0 cd /opt/nutrition-bot && /opt/nutrition-bot/venv/bin/python telegram_bot.py --weekly-review >> /var/log/nutrition-bot-cron.log 2>&1

# Weekly memory tidy + contradiction check — every Saturday at 10:05 Berlin
# (One day before the weekly review so the wiki is clean and any flagged
# conflicts are surfaced to the user before Sunday's recap lands.)
5 10 * * 6 /opt/nutrition-bot/venv/bin/python /opt/nutrition-bot/telegram_bot.py --lint-cron >> /opt/nutrition-bot/cron.log 2>&1
```

## Commands

| Command | What it does |
|---------|-------------|
| `/today` | Today's food log + running totals |
| `/week` | Weekly review with AI analysis |
| `/goal 1800` | Set daily calorie target |
| `/help` | Quick reference |
