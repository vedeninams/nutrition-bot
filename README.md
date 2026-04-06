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
├── telegram_bot.py   ← Entry point. Routes messages.
├── analyzer.py       ← Claude vision: food/label/text → nutrition data
├── advisor.py        ← Smart replies, summaries, weekly review
├── database.py       ← SQLite: all data storage
├── .env.example      ← Template for secrets
├── requirements.txt
└── README.md
```

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
