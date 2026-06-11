#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "").strip())
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_PATH = Path(__file__).with_name("telegram_cars_store.json")
MODELS_PATH = Path(__file__).with_name("search_models.json")
MADRID_TZ = ZoneInfo("Europe/Madrid")


def default_state() -> Dict[str, Any]:
    return {
        "last_update_id": 0,
        "cars": {},
        "saved_ids": [],
        "discarded_ids": [],
        "sent_messages": {},
        "sort_by": "price",
        "filter_brand": "",
        "filter_model": "",
    }


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    base = default_state()
    base.update(state)
    return base


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_models() -> Dict[str, Any]:
    if not MODELS_PATH.exists():
        return {"models": []}
    return json.loads(MODELS_PATH.read_text(encoding="utf-8"))


def save_models(payload: Dict[str, Any]) -> None:
    MODELS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def tg_call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(f"{BASE_URL}/{method}", data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def normalize_slug(text: str) -> str:
    return text.strip().lower().replace("_", "-").replace(" ", "-")


def reply_keyboard() -> str:
    keyboard = {
        "keyboard": [
            [{"text": "Guardados"}, {"text": "Ordenar"}],
            [{"text": "Filtros"}, {"text": "Modelos"}],
            [{"text": "Ayuda"}],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }
    return json.dumps(keyboard, ensure_ascii=False)


def send_text(text: str, inline_keyboard: list | None = None) -> None:
    payload: Dict[str, Any] = {"chat_id": CHAT_ID, "text": text}
    if inline_keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard}, ensure_ascii=False)
    else:
        payload["reply_markup"] = reply_keyboard()
    tg_call("sendMessage", payload)


def sort_cars(cars: List[Dict[str, Any]], sort_by: str) -> List[Dict[str, Any]]:
    key_map = {
        "precio": "price_value",
        "price": "price_value",
        "distancia": "distance_km",
        "distance": "distance_km",
        "km": "mileage_value",
    }
    if sort_by in {"published_newest", "publicado", "recientes"}:
        return sorted(
            cars,
            key=lambda car: car.get("published_at") or car.get("first_seen_at") or "",
            reverse=True,
        )
    if sort_by in {"published_oldest", "antiguos"}:
        return sorted(
            cars,
            key=lambda car: car.get("published_at") or car.get("first_seen_at") or "9999",
        )
    key_name = key_map.get(sort_by, "price_value")
    return sorted(cars, key=lambda car: car.get(key_name, 0))


def car_matches_filters(car: Dict[str, Any], state: Dict[str, Any]) -> bool:
    title = normalize_slug(car.get("title", ""))
    brand_filter = normalize_slug(state.get("filter_brand", ""))
    model_filter = normalize_slug(state.get("filter_model", ""))
    if brand_filter and brand_filter not in title:
        return False
    if model_filter:
        parts = [part for part in re.split(r"[^a-z0-9]+", model_filter) if part]
        pattern = r"(?<![a-z0-9])" + r"[-_]*".join(map(re.escape, parts)) + r"(?![a-z0-9])"
        if not re.search(pattern, title):
            return False
    return True


def build_price_block(car: Dict[str, Any]) -> str:
    financed = car.get("financed_price", "").strip()
    cash = car.get("cash_price", "").strip()
    generic = car.get("price", "").strip() or "No disponible"
    financed = financed or generic
    cash = cash or generic
    return f"*Precio financiado*\n{financed}\n\n*Precio al contado*\n{cash}"


def build_seller_type(car: Dict[str, Any]) -> str:
    explicit = str(car.get("seller_type", "")).strip()
    if explicit:
        return explicit
    source = str(car.get("source", "")).lower()
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


def build_publication_block(car: Dict[str, Any]) -> str:
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


def build_text(car: Dict[str, Any]) -> str:
    return (
        "*Coche guardado*\n\n"
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


def send_saved_cars(state: Dict[str, Any]) -> None:
    discarded_ids = set(state.get("discarded_ids", []))
    cars = [
        state["cars"][car_id]
        for car_id in state["saved_ids"]
        if car_id in state["cars"] and car_id not in discarded_ids
    ]
    cars = [car for car in cars if car_matches_filters(car, state)]
    cars = sort_cars(cars, state.get("sort_by", "price"))
    if not cars:
        send_text("No tienes coches guardados todavia.")
        return

    filter_bits = []
    if state.get("filter_brand"):
        filter_bits.append(f"marca={state['filter_brand']}")
    if state.get("filter_model"):
        filter_bits.append(f"modelo={state['filter_model']}")
    filter_text = f" | Filtros: {', '.join(filter_bits)}" if filter_bits else ""
    send_text(f"Guardados: {len(cars)} | Orden: {state.get('sort_by', 'price')}{filter_text}")
    for car in cars[:20]:
        if not car.get("image_url"):
            continue
        payload = {
            "chat_id": CHAT_ID,
            "caption": build_text(car),
            "parse_mode": "Markdown",
            "reply_markup": reply_keyboard(),
        }
        payload["photo"] = car["image_url"]
        tg_call("sendPhoto", payload)


def list_models_text() -> str:
    models = load_models().get("models", [])
    if not models:
        return "No hay modelos configurados."
    lines = []
    for item in models:
        details = [f"desde {item.get('min_year', 2022)}"]
        if item.get("fuel"):
            details.append(item["fuel"])
        lines.append(f"- {item['brand']} {item['model']} ({', '.join(details)})")
    return "\n".join(lines)


def add_model_command(text: str) -> str:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return "Usa: /addmodelo marca modelo"
    brand = normalize_slug(parts[1])
    model = normalize_slug(parts[2])
    payload = load_models()
    models = payload.setdefault("models", [])
    if any(item["brand"] == brand and item["model"] == model for item in models):
        return f"Ya estaba: {brand} {model}"
    new_model = {"brand": brand, "model": model, "min_year": 2023 if brand == "kia" else 2022}
    if brand == "mazda":
        new_model["fuel"] = "gasolina"
    models.append(new_model)
    save_models(payload)
    return f"Añadido: {brand} {model}"


def del_model_command(text: str) -> str:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return "Usa: /delmodelo marca modelo"
    brand = normalize_slug(parts[1])
    model = normalize_slug(parts[2])
    payload = load_models()
    models = payload.get("models", [])
    filtered = [item for item in models if not (item["brand"] == brand and item["model"] == model)]
    if len(filtered) == len(models):
        return f"No encontre: {brand} {model}"
    payload["models"] = filtered
    save_models(payload)
    return f"Quitado: {brand} {model}"


def order_menu() -> None:
    send_text(
        "Como quieres ordenarlos:",
        inline_keyboard=[
            [{"text": "Por precio", "callback_data": "sort:price"}],
            [{"text": "Por distancia", "callback_data": "sort:distance"}],
            [{"text": "Por kilometros", "callback_data": "sort:km"}],
            [{"text": "Mas recientes", "callback_data": "sort:published_newest"}],
            [{"text": "Mas antiguos", "callback_data": "sort:published_oldest"}],
            [{"text": "Ver guardados", "callback_data": "show_saved:now"}],
        ],
    )


def filters_menu() -> None:
    send_text(
        "Filtros rapidos:",
        inline_keyboard=[
            [{"text": "Marca Kia", "callback_data": "filter_brand:kia"}, {"text": "Marca Mazda", "callback_data": "filter_brand:mazda"}],
            [{"text": "Sin marca", "callback_data": "filter_brand:"}],
            [{"text": "Niro", "callback_data": "filter_model:niro"}, {"text": "Sportage", "callback_data": "filter_model:sportage"}],
            [{"text": "CX-3", "callback_data": "filter_model:cx-3"}, {"text": "CX-30", "callback_data": "filter_model:cx-30"}],
            [{"text": "CX-5", "callback_data": "filter_model:cx-5"}, {"text": "Sin modelo", "callback_data": "filter_model:"}],
            [{"text": "Ver filtros", "callback_data": "show_filters:now"}, {"text": "Borrar filtros", "callback_data": "clear_filters:now"}],
        ],
    )


def models_menu() -> None:
    payload = load_models()
    rows = []
    for item in payload.get("models", []):
        label = f"Quitar {item['brand']} {item['model']}"
        rows.append([{"text": label[:30], "callback_data": f"drop_model:{item['brand']}:{item['model']}"}])
    rows.append([{"text": "Ver modelos", "callback_data": "show_models:now"}])
    send_text("Modelos activos:", inline_keyboard=rows[:20] if rows else [[{"text": "Ver modelos", "callback_data": "show_models:now"}]])


def help_text() -> str:
    return (
        "Botones rapidos:\n"
        "- Guardados\n"
        "- Ordenar\n"
        "- Filtros\n"
        "- Modelos\n\n"
        "Comandos:\n"
        "/guardados\n"
        "/limpiar\n"
        "/orden precio\n"
        "/orden distancia\n"
        "/orden km\n"
        "/orden recientes\n"
        "/orden antiguos\n"
        "/fmarca kia\n"
        "/fmodelo sportage\n"
        "/modelos\n"
        "/addmodelo mazda cx-30\n"
        "/delmodelo mazda cx-30"
    )


def delete_tracked_messages(state: Dict[str, Any]) -> int:
    sent_messages = state.get("sent_messages", {})
    deleted = 0
    for _, message_id in list(sent_messages.items()):
        try:
            tg_call("deleteMessage", {"chat_id": CHAT_ID, "message_id": message_id})
            deleted += 1
        except requests.RequestException:
            pass
    state["sent_messages"] = {}
    return deleted


def handle_command(state: Dict[str, Any], text: str) -> None:
    stripped = text.strip()
    lower = stripped.lower()

    if lower in {"guardados", "/guardados"}:
        send_saved_cars(state)
        return
    if lower in {"ordenar", "/ordenar"}:
        order_menu()
        return
    if lower in {"filtros", "/filtros-menu"}:
        filters_menu()
        return
    if lower in {"modelos", "/modelos-menu"}:
        models_menu()
        return
    if lower in {"ayuda", "/ayuda", "/start", "/menu"}:
        send_text(help_text())
        return
    if lower in {"limpiar", "/limpiar"}:
        deleted = delete_tracked_messages(state)
        save_state(state)
        send_text(f"Mensajes borrados: {deleted}")
        return

    if lower.startswith("/orden"):
        parts = lower.split(maxsplit=1)
        sort_by = parts[1] if len(parts) > 1 else "price"
        state["sort_by"] = sort_by
        save_state(state)
        send_text(f"Orden actualizado a: {sort_by}")
        return
    if lower.startswith("/fmarca"):
        parts = stripped.split(maxsplit=1)
        state["filter_brand"] = normalize_slug(parts[1]) if len(parts) > 1 else ""
        save_state(state)
        send_text(f"Filtro marca: {state['filter_brand'] or 'ninguno'}")
        return
    if lower.startswith("/fmodelo"):
        parts = stripped.split(maxsplit=1)
        state["filter_model"] = normalize_slug(parts[1]) if len(parts) > 1 else ""
        save_state(state)
        send_text(f"Filtro modelo: {state['filter_model'] or 'ninguno'}")
        return
    if lower.startswith("/filtros"):
        send_text(f"Marca: {state.get('filter_brand') or 'ninguna'} | Modelo: {state.get('filter_model') or 'ninguno'}")
        return
    if lower.startswith("/clearfiltros"):
        state["filter_brand"] = ""
        state["filter_model"] = ""
        save_state(state)
        send_text("Filtros borrados")
        return
    if lower.startswith("/modelos"):
        send_text(list_models_text())
        return
    if lower.startswith("/addmodelo"):
        send_text(add_model_command(stripped))
        return
    if lower.startswith("/delmodelo"):
        send_text(del_model_command(stripped))
        return


def remove_from_lists(state: Dict[str, Any], car_id: str) -> None:
    state["saved_ids"] = [item for item in state.get("saved_ids", []) if item != car_id]
    discarded = set(state.get("discarded_ids", []))
    discarded.add(car_id)
    state["discarded_ids"] = sorted(discarded)
    state.get("cars", {}).pop(car_id, None)
    state.get("sent_messages", {}).pop(car_id, None)


def handle_callback(state: Dict[str, Any], callback_query: Dict[str, Any]) -> None:
    data = callback_query.get("data", "")
    callback_id = callback_query["id"]
    message = callback_query.get("message", {})
    message_id = message.get("message_id")

    if ":" not in data:
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id})
        return

    action, payload = data.split(":", 1)

    if action == "sort":
        state["sort_by"] = payload or "price"
        save_state(state)
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": f"Orden: {state['sort_by']}"})
        return
    if action == "filter_brand":
        state["filter_brand"] = normalize_slug(payload)
        save_state(state)
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": f"Marca: {state['filter_brand'] or 'ninguna'}"})
        return
    if action == "filter_model":
        state["filter_model"] = normalize_slug(payload)
        save_state(state)
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": f"Modelo: {state['filter_model'] or 'ninguno'}"})
        return
    if action == "show_filters":
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id})
        send_text(f"Marca: {state.get('filter_brand') or 'ninguna'} | Modelo: {state.get('filter_model') or 'ninguno'}")
        return
    if action == "clear_filters":
        state["filter_brand"] = ""
        state["filter_model"] = ""
        save_state(state)
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Filtros borrados"})
        return
    if action == "show_models":
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id})
        send_text(list_models_text())
        return
    if action == "show_saved":
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id})
        send_saved_cars(state)
        return
    if action == "drop_model":
        brand, model = (payload.split(":", 1) + [""])[:2]
        result = del_model_command(f"/delmodelo {brand} {model}")
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": result[:180]})
        return

    car_id = payload
    if car_id not in state.get("cars", {}):
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "No encuentro ese coche"})
        return

    saved_ids = set(state.get("saved_ids", []))
    if action == "save":
        if car_id in saved_ids:
            saved_ids.remove(car_id)
            state["saved_ids"] = sorted(saved_ids)
            save_state(state)
            tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Coche quitado de guardados"})
            return
        saved_ids.add(car_id)
        state["saved_ids"] = sorted(saved_ids)
        save_state(state)
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Coche guardado"})
        return

    if action == "discard":
        remove_from_lists(state, car_id)
        save_state(state)
        if message_id is not None:
            try:
                tg_call("deleteMessage", {"chat_id": CHAT_ID, "message_id": message_id})
            except requests.RequestException:
                pass
        tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Coche descartado"})
        return

    tg_call("answerCallbackQuery", {"callback_query_id": callback_id})


def main() -> int:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en el entorno")
    while True:
        state = load_state()
        response = requests.get(
            f"{BASE_URL}/getUpdates",
            params={"offset": state.get("last_update_id", 0) + 1, "timeout": 30},
            timeout=40,
        )
        response.raise_for_status()
        updates = response.json().get("result", [])

        for update in updates:
            state["last_update_id"] = update["update_id"]
            if "message" in update:
                message = update["message"]
                if str(message.get("chat", {}).get("id")) == CHAT_ID:
                    handle_command(state, message.get("text", ""))
            if "callback_query" in update:
                callback_query = update["callback_query"]
                if str(callback_query.get("message", {}).get("chat", {}).get("id")) == CHAT_ID:
                    handle_callback(state, callback_query)
            save_state(state)

        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
