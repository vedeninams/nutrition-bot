#!/bin/bash
# server_setup.sh — Run this ONCE on your Hetzner server to set everything up.
#
# Before running: make sure your GitHub repo URL is ready.
# Usage: bash server_setup.sh https://github.com/YOUR_USERNAME/nutrition-bot
#
# Example: bash server_setup.sh https://github.com/maria/nutrition-bot

set -e

GITHUB_REPO="${1}"
if [ -z "$GITHUB_REPO" ]; then
    echo "Usage: bash server_setup.sh https://github.com/YOUR_USERNAME/nutrition-bot"
    exit 1
fi

REMOTE_DIR="/opt/nutrition-bot"

echo "=== Nutrition Bot — Server Setup ==="
echo "Repo: $GITHUB_REPO"
echo ""

# ── 1. Check OS ───────────────────────────────────────────────────────────────
echo "[1/8] Checking system..."
lsb_release -a 2>/dev/null || cat /etc/os-release
echo ""

# ── 2. Install system packages ────────────────────────────────────────────────
echo "[2/8] Installing system packages..."
apt-get update -q
apt-get install -y -q python3-venv python3-pip git
echo "Done."
echo ""

# ── 3. Create a dedicated system user ────────────────────────────────────────
echo "[3/8] Creating 'nutrition' system user..."
if id "nutrition" &>/dev/null; then
    echo "User 'nutrition' already exists — skipping."
else
    useradd --system --no-create-home --shell /usr/sbin/nologin nutrition
    echo "Created user 'nutrition'."
fi
echo ""

# ── 4. Clone the GitHub repo ──────────────────────────────────────────────────
echo "[4/8] Cloning repo from GitHub..."
if [ -d "$REMOTE_DIR/.git" ]; then
    echo "Repo already exists at $REMOTE_DIR — pulling latest instead."
    git -C "$REMOTE_DIR" pull
else
    git clone "$GITHUB_REPO" "$REMOTE_DIR"
    echo "Cloned to $REMOTE_DIR."
fi
echo ""

# ── 5. Create Python virtualenv and install dependencies ─────────────────────
echo "[5/8] Setting up Python virtual environment..."
if [ ! -d "$REMOTE_DIR/venv" ]; then
    python3 -m venv "$REMOTE_DIR/venv"
    echo "Virtual environment created."
fi
"$REMOTE_DIR/venv/bin/pip" install -q --upgrade pip
"$REMOTE_DIR/venv/bin/pip" install -q -r "$REMOTE_DIR/requirements.txt"
echo "Dependencies installed."
echo ""

# ── 6. Create .env placeholder ───────────────────────────────────────────────
echo "[6/8] Checking .env file..."
if [ ! -f "$REMOTE_DIR/.env" ]; then
    cat > "$REMOTE_DIR/.env" <<'EOF'
# Fill in your real values below, then save and exit.
# Run: nano /opt/nutrition-bot/.env

TELEGRAM_BOT_TOKEN=REPLACE_WITH_YOUR_BOT_TOKEN
ANTHROPIC_API_KEY=REPLACE_WITH_YOUR_API_KEY
NUTRITION_DB_PATH=/opt/nutrition-bot/nutrition.db
EOF
    echo ".env placeholder created — you must fill it in before starting the bot."
    echo "Run: nano $REMOTE_DIR/.env"
else
    echo ".env already exists — skipping."
fi
echo ""

# ── 7. Fix file ownership ─────────────────────────────────────────────────────
echo "[7/8] Setting file permissions..."
chown -R nutrition:nutrition "$REMOTE_DIR"
chmod 600 "$REMOTE_DIR/.env"
echo "Done."
echo ""

# ── 8. Install and enable systemd service ────────────────────────────────────
echo "[8/8] Installing systemd service..."
cp "$REMOTE_DIR/nutrition-bot.service" /etc/systemd/system/nutrition-bot.service
systemctl daemon-reload
systemctl enable nutrition-bot
echo "Service installed and enabled (auto-starts on reboot)."
echo ""

echo "=== Setup complete! ==="
echo ""
echo "Next step: fill in your API keys:"
echo "  nano /opt/nutrition-bot/.env"
echo ""
echo "Then start the bot:"
echo "  systemctl start nutrition-bot"
echo ""
echo "Check it's running:"
echo "  systemctl status nutrition-bot"
echo ""
echo "Watch live logs:"
echo "  journalctl -u nutrition-bot -f"
