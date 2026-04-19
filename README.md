# 🥗 Nutrition Bot

A personal Telegram nutritionist. Send photos of food or nutrition labels and it logs calories + macros automatically.

## Features

- 📸 **Food photo** → AI identifies every item on the plate and estimates calories
- 🏷 **Nutrition label photo** → reads the label and calculates your portion
- 💬 **Text description** → "I had oatmeal with banana" → logged
- ✏️ **Natural language corrections** → "it was a whole avocado not half" → auto-updated
- 📊 **Daily and weekly summaries** pushed automatically
- ⚡ **Limit alerts** when you hit 80% of your daily goal

## Project structure

```
nutrition-bot/
├── telegram_bot.py        ← Entry point. Routes messages.
├── analyzer.py            ← Claude vision: food/label/text → nutrition data
├── advisor.py             ← Smart replies, summaries, weekly review
├── database.py            ← SQLite: all data storage
├── wiki.py                ← Per-user long-term memory (markdown wiki)
├── wiki_instructions.md   ← Schema/rulebook for the LLM wiki maintainer
├── wiki_templates/        ← Template pages copied when a user first joins
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

```bash
crontab -e
```

Add:
```
# Daily morning summary at 08:00
0 8 * * * /root/nutrition-bot/venv/bin/python /root/nutrition-bot/telegram_bot.py --daily-summary >> /var/log/nutrition-daily.log 2>&1

# Weekly review every Sunday at 08:30
30 8 * * 0 /root/nutrition-bot/venv/bin/python /root/nutrition-bot/telegram_bot.py --weekly-review >> /var/log/nutrition-weekly.log 2>&1
```

## Commands

| Command | What it does |
|---------|-------------|
| `/today` | Today's food log + running totals |
| `/week` | Weekly review with AI analysis |
| `/goal 1800` | Set daily calorie target |
| `/help` | Quick reference |
