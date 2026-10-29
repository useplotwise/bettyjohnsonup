#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu DigitalOcean droplet. Run as root:
#   bash setup_droplet.sh https://github.com/useplotwise/bettyjohnsonup.git
set -euo pipefail
REPO_URL="${1:?usage: setup_droplet.sh <git-repo-url>}"

apt-get update
apt-get install -y python3 python3-venv git

id tradebot >/dev/null 2>&1 || useradd --create-home --shell /bin/bash tradebot
sudo -u tradebot bash -c "
  cd ~ && [ -d bettyjohnsonup ] || git clone '$REPO_URL' bettyjohnsonup
  cd bettyjohnsonup && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  mkdir -p ~/secrets && chmod 700 ~/secrets
  [ -f .env ] || cp .env.example .env
"

cp /home/tradebot/bettyjohnsonup/deploy/tradebot.service /etc/systemd/system/tradebot.service
systemctl daemon-reload
echo "Next: put your Kalshi key in /home/tradebot/secrets/kalshi.pem (chmod 600),"
echo "edit /home/tradebot/bettyjohnsonup/.env, then: systemctl enable --now tradebot"
