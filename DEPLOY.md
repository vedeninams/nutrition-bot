# Deploying the Nutrition Bot to Hetzner

The deployment flow is:

```
Your Mac  ──git push──►  GitHub  ──git pull──►  Hetzner Server
```

You never copy files directly to the server. GitHub is the single source of truth.
The `.env` file and the database (`nutrition.db`) live only on the server — never in git.

---

## One-time setup (do this once, then never again)

### Step 1 — Create the GitHub repository

1. Go to [github.com](https://github.com) → **New repository**
2. Name it `nutrition-bot`
3. Set it to **Private** (keeps your code to yourself)
4. Do **not** add a README or .gitignore (you already have one)
5. Click **Create repository** — leave the page open, you'll need the URL

### Step 2 — Push the code to GitHub

Open Terminal on your Mac, go to the bot folder:

```bash
cd ~/path/to/nutrition-bot

# Initialise git (skip if already done)
git init
git add .
git commit -m "Initial commit"

# Connect to your GitHub repo and push
git remote add origin git@github.com:YOUR_USERNAME/nutrition-bot.git
git branch -M main
git push -u origin main
```

Verify it worked: refresh your GitHub repo page — you should see all the Python files.
You should NOT see `.env` or `nutrition.db` — the `.gitignore` keeps those out.

### Step 3 — Find your server IP

Log in to [console.hetzner.cloud](https://console.hetzner.cloud) → click your server → copy the **Public IP**.

### Step 4 — Connect to your server

```bash
ssh root@YOUR_SERVER_IP
```

Type `yes` if asked about the fingerprint.

### Step 5 — Set up an SSH deploy key (needed for private repos)

The server needs permission to pull from your private GitHub repo. You give it that by creating an SSH key pair on the server and adding the public key to GitHub.

**5a. Generate the key on the server:**
```bash
ssh root@YOUR_SERVER_IP 'ssh-keygen -t ed25519 -C "nutrition-bot-hetzner" -f /root/.ssh/nutrition-bot-deploy -N "" && cat /root/.ssh/nutrition-bot-deploy.pub'
```
Copy the entire line that's printed (starts with `ssh-ed25519 AAAA...`).

**5b. Add it to GitHub:**
1. Go to your repo → **Settings** → **Deploy keys** → **Add deploy key**
2. Title: `Hetzner server`
3. Key: paste the line you copied
4. Leave "Allow write access" unchecked
5. Click **Add key**

**5c. Configure the server to use this key for GitHub:**
```bash
ssh root@YOUR_SERVER_IP 'cat >> /root/.ssh/config << EOF

Host github.com
  HostName github.com
  User git
  IdentityFile /root/.ssh/nutrition-bot-deploy
  StrictHostKeyChecking no
EOF'
```

### Step 6 — Run the server setup script

Still on your Mac, from the `nutrition-bot` folder:

```bash
# Upload the setup script to the server
scp server_setup.sh root@YOUR_SERVER_IP:/root/

# Run it — use the SSH URL (git@github.com:...), NOT the https:// URL
ssh root@YOUR_SERVER_IP 'bash /root/server_setup.sh git@github.com:YOUR_USERNAME/nutrition-bot.git'
```

This takes about 1–2 minutes. It will:
- Install Python and git
- Create a dedicated `nutrition` system user
- Clone your repo from GitHub to `/opt/nutrition-bot`
- Create a Python virtual environment and install dependencies
- Create a placeholder `.env` file
- Install and enable the systemd service

### Step 7 — Fill in your API keys on the server

```bash
ssh root@YOUR_SERVER_IP 'nano /opt/nutrition-bot/.env'
```

Replace the placeholder values:

```
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...    ← from @BotFather on Telegram
ANTHROPIC_API_KEY=sk-ant-...               ← from console.anthropic.com
NUTRITION_DB_PATH=/opt/nutrition-bot/nutrition.db
```

Save: `Ctrl+X` → `Y` → `Enter`

### Step 8 — Start the bot

```bash
ssh root@YOUR_SERVER_IP 'systemctl start nutrition-bot'
ssh root@YOUR_SERVER_IP 'systemctl status nutrition-bot'
```

You should see `Active: active (running)` in green.

Send `/start` to your bot in Telegram — it should respond immediately. 🎉

### Step 9 — Save your server address (optional convenience)

To avoid typing your server address every time you deploy:

```bash
echo 'root@YOUR_SERVER_IP' > .deploy_config
```

---

## Deploying updates (every time you change code)

This is your normal workflow after the one-time setup above:

```bash
cd ~/path/to/nutrition-bot

# 1. Stage and commit your changes
git add .
git commit -m "describe what you changed"

# 2. Push to GitHub AND deploy to server in one command
./deploy.sh root@YOUR_SERVER_IP

# Or if you saved your server address in .deploy_config:
./deploy.sh
```

`deploy.sh` does everything: pushes to GitHub, SSHs into the server, pulls the latest code, installs any new dependencies, and restarts the bot. Takes about 20–30 seconds.

---

## Useful commands

Run these from your Mac via SSH, or directly on the server:

```bash
# Check if bot is running
ssh root@YOUR_SERVER_IP 'systemctl status nutrition-bot'

# Watch live logs (Ctrl+C to stop)
ssh root@YOUR_SERVER_IP 'journalctl -u nutrition-bot -f'

# View last 50 log lines
ssh root@YOUR_SERVER_IP 'journalctl -u nutrition-bot -n 50'

# Restart the bot manually
ssh root@YOUR_SERVER_IP 'systemctl restart nutrition-bot'

# Check which version of the code is running
ssh root@YOUR_SERVER_IP 'git -C /opt/nutrition-bot log --oneline -5'
```

---

## What lives where

```
Your Mac (git repo)          GitHub (private repo)       Hetzner Server
────────────────────         ─────────────────────       ──────────────────────────
database.py             →    database.py             →   /opt/nutrition-bot/database.py
analyzer.py             →    analyzer.py             →   /opt/nutrition-bot/analyzer.py
advisor.py              →    advisor.py              →   /opt/nutrition-bot/advisor.py
telegram_bot.py         →    telegram_bot.py         →   /opt/nutrition-bot/telegram_bot.py
requirements.txt        →    requirements.txt        →   /opt/nutrition-bot/requirements.txt
.gitignore              →    .gitignore              →   /opt/nutrition-bot/.gitignore
                                                         /opt/nutrition-bot/venv/   (created on server)
.env  ← stays on Mac        (never in GitHub)            /opt/nutrition-bot/.env    (created on server)
nutrition.db ← local copy  (never in GitHub)            /opt/nutrition-bot/nutrition.db  (the real one)
```

---

## Rollback (if something breaks)

```bash
# On the server: revert to the previous commit
ssh root@YOUR_SERVER_IP '
    cd /opt/nutrition-bot &&
    git log --oneline -5 &&
    git checkout COMMIT_HASH &&
    systemctl restart nutrition-bot
'
```

Replace `COMMIT_HASH` with the hash from `git log` (e.g. `a1b2c3d`).

---

## Troubleshooting

**"Permission denied" when pushing to GitHub**
Make sure your SSH key is added to GitHub. Check: `ssh -T git@github.com`

**Bot doesn't respond after deploy**
```bash
ssh root@YOUR_SERVER_IP 'journalctl -u nutrition-bot -n 30'
```
Look for lines starting with `ERROR` — usually a missing or wrong API key.

**Server says "not a git repository"**
The server setup script may not have run successfully. SSH in and check:
```bash
ls /opt/nutrition-bot/
git -C /opt/nutrition-bot status
```

**`deploy.sh` says "no remote named origin"**
```bash
git remote add origin git@github.com:YOUR_USERNAME/nutrition-bot.git
```
