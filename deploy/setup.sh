#!/usr/bin/env bash
# One-time setup of the ORB bot on a Debian/Ubuntu server (Google Cloud e2-micro).
# Run from inside the Orb_bot folder:   bash deploy/setup.sh
# Safe to run again: it only adds what is missing. It never starts the bot; you do that with "orbctl start".
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ENVD="$HOME/.config/orb-bot"; ENVF="$ENVD/env"
UNITD="$HOME/.config/systemd/user"
say() { printf "\n== %s\n" "$*"; }

[[ -f "$DIR/orb_bot.py" ]] || { echo "Run this from the Orb_bot folder (orb_bot.py not found in $DIR)"; exit 1; }
[[ $EUID -ne 0 ]] || { echo "Run as your normal user, not root (it uses sudo where needed)."; exit 1; }

say "System packages (python3-venv, nano)"
if ! python3 -m venv --help >/dev/null 2>&1 || ! command -v nano >/dev/null; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip nano
fi
python3 - <<'PY' || { echo "Python 3.11 or newer is needed. Pick Debian 12 or Ubuntu 24.04 for the VM."; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

say "1 GB swap file (the free VM has only 1 GB of memory)"
if [[ ! -f /swapfile ]] && ! swapon --show | grep -q .; then
  if sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile; then
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  else
    sudo rm -f /swapfile; echo "WARNING: could not add swap. The bot should still fit in 1 GB."
  fi
else
  echo "swap already present"
fi

say "Clock in India time, so bot.log times match what you see on your PC"
sudo timedatectl set-timezone Asia/Kolkata || echo "(could not set the time zone; the bot still trades on New York time)"

say "Python packages (into $DIR/.venv)"
[[ -x "$DIR/.venv/bin/python" ]] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$DIR/.venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"

say "Running the test suite"
(cd "$DIR" && "$DIR/.venv/bin/python" -m pytest -q -p no:cacheprovider)

say "Keys file ($ENVF)"
mkdir -p "$ENVD"; chmod 700 "$ENVD"
if [[ ! -f "$ENVF" ]]; then
  cat > "$ENVF" <<'ENV'
# ORB bot keys. Only this account can read this file. One KEY=value per line, no quotes, no spaces around =.
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENV
  echo "created (empty). Fill it in with: orbctl keys"
else
  echo "already there, left as it is"
fi
chmod 600 "$ENVF"
# Windows line endings in an uploaded file would break the values
sed -i 's/\r$//' "$ENVF" "$DIR"/deploy/orbctl "$DIR"/deploy/systemd/*.service

say "Services (systemd, user level)"
mkdir -p "$UNITD"
for f in "$DIR"/deploy/systemd/*.service; do
  sed "s#@DIR@#$DIR#g" "$f" > "$UNITD/$(basename "$f")"
done
# keep the services running when you log out of SSH
sudo loginctl enable-linger "$USER" || echo "WARNING: could not enable linger: the bot would stop when you close SSH."
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
for _ in 1 2 3 4 5; do systemctl --user daemon-reload 2>/dev/null && break; sleep 1; done
systemctl --user enable --now orb-dashboard.service

say "The orbctl command"
chmod +x "$DIR/deploy/orbctl"
mkdir -p "$HOME/.local/bin"; ln -sf "$DIR/deploy/orbctl" "$HOME/.local/bin/orbctl"
grep -q 'HOME/.local/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"

cat <<DONE

Setup finished. Next:
  1. source ~/.bashrc          (once, so the orbctl command is found)
  2. orbctl keys               paste your keys, then Ctrl+O, Enter, Ctrl+X
  3. orbctl test-alerts        a Telegram message should arrive
  4. orbctl start              start trading with paper orders
  5. orbctl status             check it
DONE
