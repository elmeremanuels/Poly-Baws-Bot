#!/bin/bash
# deploy.sh — pull latest + restart services
# config.yaml wordt ALTIJD uit de repo genomen.
# Server-specifieke secrets staan in backend/config/.env (nooit in git).
set -e
cd /opt/poly-baws-bot

# Haal de remote versie op en overschrijf config.yaml zonder te vragen
git fetch origin claude/polymarket-trading-bot-GEypG
git checkout origin/claude/polymarket-trading-bot-GEypG -- backend/config/config.yaml
git merge --ff-only origin/claude/polymarket-trading-bot-GEypG 2>/dev/null || \
    git reset --hard origin/claude/polymarket-trading-bot-GEypG

sudo chmod 664 backend/config/config.yaml
sudo systemctl restart poly-baws-bot
sudo systemctl restart poly-baws-streamlit
echo "Deploy klaar — $(date)"
