#!/usr/bin/env bash
set -euo pipefail

cd /app

while true; do
  python send_cars_to_telegram.py
  sleep "${SCRAPE_INTERVAL_SECONDS:-900}"
done
