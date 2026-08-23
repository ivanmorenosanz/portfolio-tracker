#!/bin/bash
# Watches the Portfolio app directory for Python/template file changes and
# restarts the portfolio systemd service when they occur.
#
# Deployed as a systemd user service so it survives across reboots.

set -euo pipefail

WATCH_DIR="/srv/appdata/web-apps/Portfolio"
DEBOUNCE_SEC=3

echo "[watcher] Watching $WATCH_DIR for changes..."

last_restart=0

inotifywait -m -r -q \
    --exclude '/\.git/|/__pycache__/|/\.venv/|\.db$|\.db-|\.pyc$' \
    -e modify -e create -e delete -e move -e attrib \
    --format '%w%f' \
    "$WATCH_DIR" \
| while read -r changed; do
    # Only care about Python, templates, and env files.
    case "$changed" in
        *.py|*.html|*.env|*.txt|*.md) ;;
        *) continue ;;
    esac

    now=$(date +%s)
    if (( now - last_restart < DEBOUNCE_SEC )); then
        continue
    fi
    last_restart=$now

    echo "[watcher] $(date '+%H:%M:%S') Change in $(basename "$changed") → restarting portfolio.service"
    sudo /usr/bin/systemctl restart portfolio.service
done