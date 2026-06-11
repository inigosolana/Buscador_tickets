#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from autoscout_scraper import (
    extract_price_value,
    fetch_autoscout_seller_type,
    get_search_urls,
    listing_matches_search_rules,
    scrape_multiple_searches,
)


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "").strip())
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_PATH = Path(__file__).with_name("telegram_cars_store.json")
SENT_IDS_PATH = Path(__file__).with_name("sent_car_ids.json")
MADRID_TZ = ZoneInfo("Europe/Madrid")


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def build_price_block(car: dict) -> str:
    financed = car.get("financed_price", "").strip()
    cash = car.get("cash_price", "").strip()
    generic = car.get("price", "").strip() or "No disponible"
    financed = financed or generic
    cash = cash or generic
    return f"*Precio financiado*\n{financed}\n\n*Precio al contado*\n{cash}"


def build_seller_type(car: dict) -> str:
    explicit = str(car.get("seller_type", "")).strip()
    if explicit:
        return explicit
    url = str(car.get("url", "")).lower()
    if "autoscout24." in url:
        resolved = fetch_autoscout_seller_type(car.get("url", ""))
        if resolved:
            car["seller_type"] = resolved
            return resolved
    source = str(car.get("source", "")).lower()
    location = str(car.get("location", ""))
    if "autoscout24" in source and "ES-" in location:
        prefix = location.split("ES-", 1)[0].strip()
        if prefix:
            car["seller_type"] = "Concesionario"
            return "Concesionario"
    if "particular" in source:
        return "Particular"
    if "concesionario" in source or "selected" in source:
        return "Concesionario"
    return "No indicado"


def parse_tracking_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MADRID_TZ)
        return parsed.astimezone(MADRID_TZ)
    except ValueError:
        return None


def format_elapsed(value: datetime) -> str:
    seconds = max(0, int((datetime.now(MADRID_TZ) - value).total_seconds()))
    if seconds < 3600:
        amount = max(1, seconds // 60)
        return f"{amount} minuto" if amount == 1 else f"{amount} minutos"
    if seconds < 86400:
        amount = seconds // 3600
        return f"{amount} hora" if amount == 1 else f"{amount} horas"
    if seconds < 30 * 86400:
        amount = seconds // 86400
        return f"{amount} dia" if amount == 1 else f"{amount} dias"
    amount = seconds // (30 * 86400)
    return f"{amount} mes" if amount == 1 else f"{amount} meses"


def build_publication_block(car: dict) -> str:
    published = parse_tracking_datetime(str(car.get("published_at", "")))
    first_seen = parse_tracking_datetime(str(car.get("first_seen_at", "")))
    if published:
        is_wallapop = str(car.get("source", "")).lower() == "wallapop"
        date_label = "Publicado/actualizado aprox." if is_wallapop else "Publicado"
        age_prefix = "Al menos " if is_wallapop else ""
        return (
            f"*{date_label}*\n{published.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"*Lleva publicado*\n{age_prefix}{format_elapsed(published)}"
        )
    if first_seen:
        return (
            f"*Publicado*\nNo indicado por la web; detectado el {first_seen.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"*Lleva publicado*\nAl menos {format_elapsed(first_seen)}"
        )
    return "*Publicado*\nNo indicado por la web\n\n*Lleva publicado*\nNo disponible"


def build_caption(car: dict) -> str:
    return (
        "*Nuevo coche detectado*\n\n"
        f"*Modelo*\n{car['title']}\n\n"
        f"{build_price_block(car)}\n\n"
        f"*Vendedor*\n{build_seller_type(car)}\n\n"
        f"*Kilometros*\n{car['mileage_km']}\n\n"
        f"*Ano*\n{car['year']}\n\n"
        f"{build_publication_block(car)}\n\n"
        f"*Ubicacion*\n{car['location']}\n\n"
        f"*Fuente*\n{car['source']}\n\n"
        f"[Ver anuncio]({car['url']})"
    )


def default_state() -> dict:
    return {
        "last_update_id": 0,
        "cars": {},
        "saved_ids": [],
        "discarded_ids": [],
        "sent_messages": {},
        "sort_by": "price",
    }


def main() -> int:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en el entorno")
    store = load_json(STATE_PATH, default_state())
    store.setdefault("cars", {})
    store.setdefault("saved_ids", [])
    store.setdefault("discarded_ids", [])
    store.setdefault("sent_messages", {})
    store.setdefault("sort_by", "price")
    tracking_started_at = datetime.now(MADRID_TZ).isoformat(timespec="seconds")
    for stored_car in store["cars"].values():
        stored_car.setdefault("first_seen_at", tracking_started_at)
        stored_car.setdefault("published_at", "")
        stored_car.setdefault("publication_text", "")

    sent_ids = set(load_json(SENT_IDS_PATH, []))
    discarded_ids = set(store.get("discarded_ids", []))
    force_resend = os.getenv("FORCE_RESEND", "").strip() == "1"
    listings = scrape_multiple_searches(get_search_urls())

    fresh_count = 0
    for listing in listings:
        car = asdict(listing)
        unique_id = f"{car['source']}::{car['lead_id']}"
        previous_car = store["cars"].get(unique_id, {})
        car["first_seen_at"] = previous_car.get("first_seen_at") or datetime.now(MADRID_TZ).isoformat(timespec="seconds")
        if not car.get("published_at"):
            car["published_at"] = previous_car.get("published_at", "")
        price_value = extract_price_value(car["price"])
        if price_value is None or price_value > 22000:
            continue
        if not listing_matches_search_rules(
            car.get("title", ""),
            str(car.get("year", "")),
            car.get("fuel_type", ""),
            car.get("url", ""),
            car.get("url", ""),
        ):
            continue
        if not car.get("image_url"):
            continue

        car["seller_type"] = build_seller_type(car)
        if unique_id in discarded_ids:
            continue
        store["cars"][unique_id] = car
        if not force_resend and unique_id in sent_ids:
            continue

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Guardar", "callback_data": f"save:{unique_id}"},
                    {"text": "Descartar", "callback_data": f"discard:{unique_id}"},
                    {"text": "Ver anuncio", "url": car["url"]},
                ]
            ]
        }
        payload = {
            "chat_id": CHAT_ID,
            "caption": build_caption(car),
            "parse_mode": "Markdown",
            "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
        }
        response = requests.post(
            f"{BASE_URL}/sendPhoto",
            data={**payload, "photo": car["image_url"]},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json().get("result", {})
        if "message_id" in result:
            store["sent_messages"][unique_id] = result["message_id"]
        sent_ids.add(unique_id)
        fresh_count += 1

    save_json(STATE_PATH, store)
    save_json(SENT_IDS_PATH, sorted(sent_ids))
    print(f"Enviados nuevos: {fresh_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
