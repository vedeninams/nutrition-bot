#!/bin/bash
# deploy.sh — Deploy the bot via GitHub.
#
# Flow: commit locally → push to GitHub → server pulls → pip install → restart
#
# Usage:   ./deploy.sh user@your-server-ip
# Example: ./deploy.sh root@123.456.789.0
#
# You can also save your server address in .deploy_config to avoid typing it every time.

set -e

# ── Load saved server address if available ────────────────────────────────────
CONFIG_FILE="$(dirname "$0")/.deploy_config"
if [ -f "$CONFIG_FILE" ]; then
    SAVED_SERVER=$(cat "$CONFIG_FILE")
fi

SERVER="${1:-$SAVED_SERVER}"
if [ -z "$SERVER" ]; then
    echo "Usage: ./deploy.sh user@server-ip"
    echo "Example: ./deploy.sh root@123.456.789.0"
    echo ""
    echo "Tip: to avoid typing it every time, run:"
    echo "  echo 'root@123.456.789.0' > .deploy_config"
    exit 1
fi

REMOTE_DIR="/opt/nutrition-bot"

echo "=== Deploying Nutrition Bot ==="
echo "Server: $SERVER"
echo ""

# ── 1. Check we are in a git repo with a remote ───────────────────────────────
echo "[1/4] Checking git status..."
if ! git -C "$(dirname "$0")" rev-parse --git-dir > /dev/null 2>&1; then
    echo "ERROR: Not a git repository. Run 'git init' and push to GitHub first."
    exit 1
fi

BRANCH=$(git -C "$(dirname "$0")" rev-parse --abbrev-ref HEAD)
REMOTE=$(git -C "$(dirname "$0")" remote get-url origin 2>/dev/null || echo "")
if [ -z "$REMOTE" ]; then
    echo "ERROR: No git remote named 'origin'. Add your GitHub repo first:"
    echo "  git remote add origin git@github.com:YOUR_USERNAME/nutrition-bot.git"
    exit 1
fi
echo "Branch: $BRANCH"
echo "Remote: $REMOTE"
echo ""

# ── 2. Push to GitHub ─────────────────────────────────────────────────────────
echo "[2/4] Pushing to GitHub..."
git -C "$(dirname "$0")" push origin "$BRANCH"
echo ""

# ── 3. Pull on the server and install dependencies ────────────────────────────
echo "[3/4] Pulling on server and installing dependencies..."
ssh "$SERVER" "
    set -e
    cd $REMOTE_DIR
    git pull origin $BRANCH
    $REMOTE_DIR/venv/bin/pip install -q --upgrade pip
    $REMOTE_DIR/venv/bin/pip install -q -r requirements.txt
    chown -R nutrition:nutrition $REMOTE_DIR
    chmod 600 $REMOTE_DIR/.env 2>/dev/null || true
    echo 'Pull and install done.'
"
echo ""

# ── 4. Restart the bot ────────────────────────────────────────────────────────
echo "[4/4] Restarting the bot..."
ssh "$SERVER" "
    systemctl daemon-reload
    systemctl restart nutrition-bot
    sleep 2
    systemctl status nutrition-bot --no-pager
"
echo ""
echo "=== Deploy complete! ==="
echo ""
echo "To watch live logs:"
echo "  ssh $SERVER 'journalctl -u nutrition-bot -f'"
