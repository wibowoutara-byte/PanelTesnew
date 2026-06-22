#!/bin/bash
# ============================================================
# Pterodactyl Installation Script — AnzNokosFree Bot
# Script ini dijalankan sekali saat server dibuat di panel
# ============================================================

echo "=== Installing AnzNokosFree Bot ==="

# Update pip
pip install --upgrade pip --quiet

# Install dependencies
pip install \
    "python-telegram-bot==22.1" \
    "requests>=2.34.2" \
    "beautifulsoup4>=4.15.0" \
    --quiet

echo "=== Dependencies installed ==="
echo "=== Bot siap dijalankan! ==="
