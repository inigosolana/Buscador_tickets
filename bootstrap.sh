#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/data

touch /app/data/telegram_cars_store.json
touch /app/data/sent_car_ids.json
touch /app/data/reader_markdown_cache.json

ln -sf /app/data/telegram_cars_store.json /app/telegram_cars_store.json
ln -sf /app/data/sent_car_ids.json /app/sent_car_ids.json
ln -sf /app/data/reader_markdown_cache.json /app/reader_markdown_cache.json

exec "$@"
