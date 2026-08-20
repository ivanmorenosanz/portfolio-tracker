#!/bin/bash
# Start portfolio app with Finnhub API key

cd /srv/appdata/web-apps/Portfolio

# Load environment variables from .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Activate venv
source .venv/bin/activate

# Start the app
echo "[$(date)] Starting Portfolio app..."
echo "[$(date)] Finnhub API Key: ${FINNHUB_API_KEY:0:20}..."

exec python3 main.py
