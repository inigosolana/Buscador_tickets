#!/usr/bin/env python3
"""
Scraper multi-fuente para buscar coches y enviarlos a n8n/Telegram.

Fuentes actuales:
    - AutoScout24
    - Coches.net (si no bloquea por anti-bot)
    - Kia Seminuevos Certificados (concesionarios cercanos)
    - Mazda Selected
    - Mazda Norkar
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import pgeocode
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.autoscout24.es"
MODELS_PATH = Path(__file__).with_name("search_models.json")
READER_CACHE_PATH = Path(__file__).with_name("reader_markdown_cache.json")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "100"))
REQUEST_TIMEOUT = 30
READER_BASE_URL = "https://r.jina.ai/"
TARGET_POSTAL_CODE = os.getenv("TARGET_POSTAL_CODE", "39700")
MAX_DISTANCE_KM = float(os.getenv("MAX_DISTANCE_KM", "150"))
POSTAL_GEOCODER = pgeocode.Nominatim("es")
LOCATION_DISTANCE_CACHE: dict[str, Optional[float]] = {}
MADRID_TZ = ZoneInfo("Europe/Madrid")
READER_CACHE_TTL_SECONDS = 45 * 60
READER_MARKDOWN_CACHE: dict[str, tuple[float, str]] = {}
READER_REQUEST_LOCK = threading.Lock()
READER_LAST_REQUEST_AT = 0.0
NEARBY_PROVINCE_SLUGS = ("cantabria", "vizcaya", "alava", "asturias", "burgos", "la-rioja")
NEARBY_CITY_SLUGS = ("santander", "bilbao", "vitoria", "oviedo", "burgos", "logrono")
SOURCE_DEFAULT_POSTAL_CODES = {
    "https://www.kia.com/es/concesionarios/numarmotor/kia-seminuevos-certificados/buscador/": "39700",
    "https://www.kia.com/es/concesionarios/masmotorsa/kia-seminuevos-certificados/buscador/": "48950",
    "https://www.mazdaselected.es/concesionario/mouromotor": "39009",
    "https://www.mazdaselected.es/concesionario/norkar": "48950",
    "https://www.mazdanorkar.com/coches-ocasion/": "48950",
}
SOURCE_DEFAULT_LOCATIONS = {
    "39009": "Santander",
    "39700": "Laredo",
    "48950": "Erandio",
}
SOURCE_KIA_DEALER_IDS = {
    "https://www.kia.com/es/concesionarios/numarmotor/kia-seminuevos-certificados/buscador/": "1052",
    "https://www.kia.com/es/concesionarios/masmotorsa/kia-seminuevos-certificados/buscador/": "916",
}


@dataclass
class CarListing:
    lead_id: str
    title: str
    price: str
    price_value: int
    cash_price: str
    financed_price: str
    mileage_km: str
    mileage_value: int
    year: str
    url: str
    image_url: str
    location: str
    distance_km: float
    source: str
    seller_type: str
    published_at: str
    publication_text: str
    fuel_type: str


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_model_slug(value: str) -> str:
    return normalize_text(value).lower().replace("_", "-").replace(" ", "-")


def normalize_search_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", normalize_text(value).lower())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def iso_datetime(value: datetime) -> str:
    return value.astimezone(MADRID_TZ).isoformat(timespec="seconds")


def parse_iso_publication(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MADRID_TZ)
        return iso_datetime(parsed)
    except ValueError:
        return ""


def parse_reader_publication(text: str, source: str) -> tuple[str, str]:
    now = datetime.now(MADRID_TZ)
    if source == "Milanuncios":
        match = re.search(
            r"Publicado el\s+(\d{2})/(\d{2})/(\d{4})\s+a las\s+(\d{2}):(\d{2})",
            text,
            re.IGNORECASE,
        )
        if match:
            day, month, year, hour, minute = map(int, match.groups())
            value = datetime(year, month, day, hour, minute, tzinfo=MADRID_TZ)
            return iso_datetime(value), match.group(0)

    if source == "Coches.net":
        match = re.search(r"Publicado:\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", text, re.IGNORECASE)
        if match:
            day, month, hour, minute = map(int, match.groups())
            year = now.year
            value = datetime(year, month, day, hour, minute, tzinfo=MADRID_TZ)
            if value > now + timedelta(days=1):
                value = value.replace(year=year - 1)
            return iso_datetime(value), match.group(0)

    if source == "Wallapop":
        match = re.search(
            r"hace\s+(\d+|un|una)\s+(minuto|minutos|hora|horas|d[ií]a|d[ií]as|semana|semanas|mes|meses)",
            text,
            re.IGNORECASE,
        )
        if match:
            amount = 1 if match.group(1).lower() in {"un", "una"} else int(match.group(1))
            unit = match.group(2).lower()
            if unit.startswith("minuto"):
                value = now - timedelta(minutes=amount)
            elif unit.startswith("hora"):
                value = now - timedelta(hours=amount)
            elif unit.startswith(("día", "dia")):
                value = now - timedelta(days=amount)
            elif unit.startswith("semana"):
                value = now - timedelta(weeks=amount)
            else:
                value = now - timedelta(days=amount * 30)
            return iso_datetime(value), match.group(0)

    return "", ""


def load_models() -> List[dict]:
    if not MODELS_PATH.exists():
        return []
    payload = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    models = payload.get("models", [])
    normalized: List[dict] = []
    for item in models:
        brand = normalize_model_slug(item.get("brand", ""))
        model = normalize_model_slug(item.get("model", ""))
        if brand and model:
            normalized.append(
                {
                    "brand": brand,
                    "model": model,
                    "min_year": int(item.get("min_year", 2022)),
                    "fuel": normalize_search_text(item.get("fuel", "")),
                }
            )
    return normalized


def model_matches_text(model: str, value: str) -> bool:
    parts = [part for part in re.split(r"[^a-z0-9]+", normalize_search_text(model)) if part]
    if not parts:
        return False
    pattern = r"(?<![a-z0-9])" + r"[\s_-]*".join(map(re.escape, parts)) + r"(?![a-z0-9])"
    return bool(re.search(pattern, normalize_search_text(value)))


def find_model_rule(title: str, source_url: str = "") -> Optional[dict]:
    searchable = f"{title} {source_url}"
    for rule in load_models():
        if model_matches_text(rule["brand"], searchable) and model_matches_text(rule["model"], searchable):
            return rule
    return None


def fuel_matches_rule(required_fuel: str, value: str) -> bool:
    if not required_fuel:
        return True
    searchable = normalize_search_text(value)
    if required_fuel == "gasolina":
        if any(marker in searchable for marker in ("diesel", "skyactiv-d", "electrico", "electric", "phev")):
            return False
        gasoline_markers = ("gasolina", "petrol", "skyactiv-g", "e-skyactiv-g")
        if any(marker in searchable for marker in gasoline_markers):
            return True
        return False
    return required_fuel in searchable


def listing_matches_search_rules(
    title: str,
    year: str,
    fuel_type: str = "",
    raw_text: str = "",
    source_url: str = "",
) -> bool:
    rule = find_model_rule(title, source_url)
    if not rule or not str(year).isdigit():
        return False
    if int(year) < int(rule.get("min_year", 2022)):
        return False
    return fuel_matches_rule(rule.get("fuel", ""), f"{fuel_type} {title} {raw_text}")


def build_default_search_urls() -> List[str]:
    urls: List[str] = []
    default_region_slot = int(datetime.now(MADRID_TZ).timestamp() // 600)
    region_index = int(os.getenv("SOURCE_REGION_INDEX", str(default_region_slot))) % len(NEARBY_PROVINCE_SLUGS)
    province = NEARBY_PROVINCE_SLUGS[region_index]
    city = NEARBY_CITY_SLUGS[region_index]
    for item in load_models():
        brand = item["brand"]
        model = item["model"]
        min_year = item["min_year"]
        coches_model = model.replace("-", "")
        urls.extend(
            [
                f"https://www.autoscout24.es/lst/{brand}/{model}?atype=C&cy=E&desc=1&fregfrom={min_year}&kmto=50000&sort=standard&source=listpage_search-results",
                f"https://www.coches.com/coches-segunda-mano/{brand}-{model}.htm",
            ]
        )
        urls.extend(
            [
                f"https://www.coches.net/{brand}/{coches_model}/segunda-mano/{province}/",
                f"https://www.milanuncios.com/coches-de-segunda-mano-en-{province}/{brand}-{model}.htm",
                f"https://es.wallapop.com/coches-segunda-mano/{brand}-{model}/{city}",
            ]
        )
    urls.extend(
        [
            "https://www.kia.com/es/concesionarios/numarmotor/kia-seminuevos-certificados/buscador/",
            "https://www.kia.com/es/concesionarios/masmotorsa/kia-seminuevos-certificados/buscador/",
            "https://www.mazdaselected.es/concesionario/mouromotor",
            "https://www.mazdaselected.es/concesionario/norkar",
            "https://www.mazdanorkar.com/coches-ocasion/",
        ]
    )
    return list(dict.fromkeys(urls))


def get_allowed_models() -> tuple[str, ...]:
    models = [item["model"] for item in load_models()]
    return tuple(dict.fromkeys(models))


def get_host(page_url: str) -> str:
    return urlparse(page_url).netloc.lower()


def get_source_name(page_url: str) -> str:
    host = get_host(page_url)
    if "coches.net" in host:
        return "Coches.net"
    if "coches.com" in host:
        return "Coches.com"
    if "wallapop.com" in host:
        return "Wallapop"
    if "milanuncios.com" in host:
        return "Milanuncios"
    if "kia.com" in host:
        return "Kia Concesionario"
    if "mazdaselected.es" in host:
        return "Mazda Selected"
    if "mazdanorkar.com" in host:
        return "Mazda Concesionario"
    return "AutoScout24"


def infer_seller_type_from_source(source_name: str) -> str:
    normalized = normalize_text(source_name).lower()
    if "particular" in normalized:
        return "Particular"
    if "concesionario" in normalized or "selected" in normalized or "dealer" in normalized:
        return "Concesionario"
    return ""


def fetch_reader_markdown(page_url: str) -> str:
    global READER_LAST_REQUEST_AT
    if not READER_MARKDOWN_CACHE and READER_CACHE_PATH.exists():
        try:
            cached_payload = json.loads(READER_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached_payload, dict):
                for key, value in cached_payload.items():
                    if isinstance(value, dict) and isinstance(value.get("text"), str):
                        READER_MARKDOWN_CACHE[str(key)] = (
                            float(value.get("fetched_at", 0)),
                            value["text"],
                        )
        except (OSError, json.JSONDecodeError):
            pass
    cached = READER_MARKDOWN_CACHE.get(page_url)
    if cached and is_valid_reader_content(cached[1]) and time.time() - cached[0] < READER_CACHE_TTL_SECONDS:
        return cached[1]
    READER_MARKDOWN_CACHE.pop(page_url, None)
    last_error: Optional[Exception] = None
    reader_urls = [f"{READER_BASE_URL}{page_url}"]
    if page_url.startswith("https://"):
        reader_urls.append(f"{READER_BASE_URL}http://{page_url.removeprefix('https://')}")
        reader_urls.append(f"{READER_BASE_URL}http://{page_url}")
    for attempt, reader_url in enumerate(reader_urls):
        try:
            with READER_REQUEST_LOCK:
                delay = 5.0 - (time.monotonic() - READER_LAST_REQUEST_AT)
                if delay > 0:
                    time.sleep(delay)
                response = requests.get(
                    reader_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=max(REQUEST_TIMEOUT, 120),
                )
                READER_LAST_REQUEST_AT = time.monotonic()
            response.raise_for_status()
            if not is_valid_reader_content(response.text):
                raise requests.RequestException(f"Contenido bloqueado por la fuente: {page_url}")
            READER_MARKDOWN_CACHE[page_url] = (time.time(), response.text)
            try:
                READER_CACHE_PATH.write_text(
                    json.dumps(
                        {
                            key: {"fetched_at": fetched_at, "text": text}
                            for key, (fetched_at, text) in READER_MARKDOWN_CACHE.items()
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < len(reader_urls) - 1:
                time.sleep(2 * (attempt + 1))
    if last_error:
        raise last_error
    return ""


def is_valid_reader_content(text: str) -> bool:
    lowered = text.lower()
    blocked_markers = (
        "warning: target url returned error 403",
        "## 403 error",
        "request blocked. we can't connect",
        "pardon our interruption",
    )
    return bool(text.strip()) and not any(marker in lowered for marker in blocked_markers)


def extract_reader_image(text: str, host_fragment: str = "") -> str:
    pattern = r"!\[[^\]]*\]\((https?://[^)\s]+)\)"
    for match in re.finditer(pattern, text):
        image_url = match.group(1)
        if host_fragment and host_fragment not in image_url:
            continue
        if any(marker in image_url.lower() for marker in ("logo", "label-icons", "/logos/")):
            continue
        return image_url
    return ""


def extract_reader_price_values(text: str) -> List[int]:
    values: List[int] = []
    for raw in re.findall(r"(\d{1,3}(?:[.,]\d{3})+|\d{4,6})\s*\u20ac", text):
        normalized = raw.replace(".", "").replace(",", "")
        if normalized.isdigit():
            values.append(int(normalized))
    return values


def format_euro(value: int) -> str:
    return f"\u20ac {value:,}".replace(",", ".")


def extract_year(text: str) -> str:
    match = re.search(r"\b(20\d{2})\b", text)
    return match.group(1) if match else ""


def extract_mileage(text: str) -> str:
    match = re.search(r"([\d\.\,]+)\s*km", text, re.IGNORECASE)
    if not match:
        return ""
    return f"{match.group(1).replace('.', '').replace(',', '.')} km"


def extract_mileage_value(text: str) -> Optional[int]:
    match = re.search(r"([\d\.\,]+)\s*km", text, re.IGNORECASE)
    if not match:
        return None
    raw = match.group(1).replace(".", "").replace(",", "")
    return int(raw) if raw.isdigit() else None


def extract_price(text: str) -> str:
    for pattern in (
        r"€\s*([\d\.\,]{4,})",
        r"([\d\.\,]{4,})\s*€",
        r"â‚¬\s*([\d\.\,]{4,})",
        r"([\d\.\,]{4,})\s*â‚¬",
    ):
        match = re.search(pattern, text)
        if match:
            return f"€ {match.group(1)}"
    return ""


def extract_price_value(price_text: str) -> Optional[int]:
    match = re.search(r"([\d\.\,]+)", price_text)
    if not match:
        return None
    raw = match.group(1).replace(".", "").replace(",", "")
    return int(raw) if raw.isdigit() else None


def extract_postal_code(location_text: str) -> str:
    match = re.search(r"ES-(\d{4,5})", location_text, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).zfill(5)


def distance_from_target_km(postal_code: str) -> Optional[float]:
    if not postal_code:
        return None

    target = POSTAL_GEOCODER.query_postal_code(TARGET_POSTAL_CODE)
    current = POSTAL_GEOCODER.query_postal_code(postal_code)
    if any(
        value is None or (isinstance(value, float) and value != value)
        for value in (target.latitude, target.longitude, current.latitude, current.longitude)
    ):
        return None

    distance = pgeocode.haversine_distance(
        [[target.latitude, target.longitude]],
        [[current.latitude, current.longitude]],
    )
    return float(distance[0])


def distance_from_location_name(location_name: str) -> Optional[float]:
    normalized = normalize_text(location_name).lower()
    if not normalized:
        return None
    if normalized in LOCATION_DISTANCE_CACHE:
        return LOCATION_DISTANCE_CACHE[normalized]

    try:
        target = POSTAL_GEOCODER.query_postal_code(TARGET_POSTAL_CODE)
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{location_name}, Spain", "format": "jsonv2", "limit": 1},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        items = response.json()
        if not items:
            LOCATION_DISTANCE_CACHE[normalized] = None
            return None
        lat = float(items[0]["lat"])
        lon = float(items[0]["lon"])
        if any(value != value for value in (target.latitude, target.longitude, lat, lon)):
            LOCATION_DISTANCE_CACHE[normalized] = None
            return None
        distance = pgeocode.haversine_distance(
            [[target.latitude, target.longitude]],
            [[lat, lon]],
        )
        LOCATION_DISTANCE_CACHE[normalized] = float(distance[0])
        return LOCATION_DISTANCE_CACHE[normalized]
    except Exception:
        LOCATION_DISTANCE_CACHE[normalized] = None
        return None


def safe_attr(element, attr_name: str) -> str:
    if not element:
        return ""
    value = element.get(attr_name, "")
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return normalize_text(value)


def discover_listing_cards(soup: BeautifulSoup) -> List:
    selectors = [
        '[data-testid="search-list-container"] article',
        'article[data-testid*="listing"]',
        'article',
        '[class*="ListItem"]',
        '[class*="listing"]',
    ]
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            return cards
    return []


def discover_cards_for_url(soup: BeautifulSoup, search_url: str) -> List:
    host = get_host(search_url)
    if "mazdaselected.es" in host:
        return soup.select("li.vehicle")
    if "mazdanorkar.com" in host:
        return soup.select("article")
    return discover_listing_cards(soup)


def extract_direct_link(card, page_url: str) -> str:
    host = get_host(page_url)
    if "mazdaselected.es" in host:
        link = card.select_one('a[href*="/ficha/"]') or card.select_one('a[href*="../ficha/"]')
    else:
        link = card.select_one('a[href*="/oferta/"]') or card.select_one("a[href]")
    href = safe_attr(link, "href")
    if not href:
        return ""
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    return href if href.startswith("http") else urljoin(base, href)


def extract_lead_id(card, url: str) -> str:
    for attr_name in ("id", "data-guid", "data-listing-id", "data-id"):
        value = safe_attr(card, attr_name)
        if value:
            return value

    data_attrs = " ".join(
        safe_attr(card, attr_name)
        for attr_name in ("data-testid", "data-guid", "data-id", "class")
    )
    match = re.search(r"([a-f0-9]{8,}|listing-\d+|\d{6,})", data_attrs, re.IGNORECASE)
    if match:
        return match.group(1)

    for pattern in (r"/oferta/[^/]+-([a-f0-9]{8,})", r"/ficha/(\d+)", r"(\d{6,})"):
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def extract_title(card) -> str:
    selectors = [
        '[data-testid="title"]',
        "h2",
        "h3",
        '[class*="title"]',
    ]
    for selector in selectors:
        element = card.select_one(selector)
        if element:
            text = normalize_text(element.get_text(" ", strip=True))
            if text:
                return text
    return ""


def extract_image_url(card, page_url: str) -> str:
    image = card.select_one("img[src]") or card.select_one("img[data-src]")
    if not image:
        return ""
    src = safe_attr(image, "src") or safe_attr(image, "data-src")
    if not src:
        return ""
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    return src if src.startswith("http") else urljoin(base, src)


def extract_location(raw_text: str) -> str:
    match = re.search(r"([A-Z0-9\-\.\s]+ES-\d{4,5}\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .\-]+)", raw_text)
    return normalize_text(match.group(1)) if match else "Ubicacion no disponible"


def infer_location_from_source(page_url: str) -> str:
    postal_code = SOURCE_DEFAULT_POSTAL_CODES.get(page_url, "")
    if not postal_code:
        return "Ubicacion no disponible"
    city = SOURCE_DEFAULT_LOCATIONS.get(postal_code, "")
    return f"ES-{postal_code} {city}".strip()


def build_listing(
    *,
    lead_id: str,
    title: str,
    price: str,
    cash_price: str = "",
    financed_price: str = "",
    mileage: str,
    year: str,
    url: str,
    image_url: str,
    location: str,
    source_url: str,
    seller_type: str = "",
    published_at: str = "",
    publication_text: str = "",
    fuel_type: str = "",
    raw_text: str = "",
) -> Optional[CarListing]:
    price_value = extract_price_value(price)
    mileage_value = extract_mileage_value(mileage)
    postal_code = extract_postal_code(location) or SOURCE_DEFAULT_POSTAL_CODES.get(source_url, "")
    distance_km = distance_from_target_km(postal_code) if postal_code else distance_from_location_name(location)

    if not lead_id:
        return None
    if price_value is None or price_value > 22000:
        return None
    if distance_km is None or distance_km > MAX_DISTANCE_KM:
        return None
    if mileage_value is None or mileage_value >= 50000:
        return None
    if not listing_matches_search_rules(title, year, fuel_type, raw_text, source_url):
        return None

    normalized_location = location
    if normalized_location == "Ubicacion no disponible" and postal_code:
        normalized_location = infer_location_from_source(source_url)

    return CarListing(
        lead_id=lead_id,
        title=title or "Sin titulo",
        price=price or "No disponible",
        price_value=price_value,
        cash_price=cash_price or "",
        financed_price=financed_price or "",
        mileage_km=mileage or "No disponible",
        mileage_value=mileage_value or 0,
        year=year or "No disponible",
        url=url,
        image_url=image_url,
        location=f"{normalized_location} ({distance_km:.1f} km)",
        distance_km=distance_km or 0.0,
        source=get_source_name(source_url),
        seller_type=seller_type or infer_seller_type_from_source(get_source_name(source_url)),
        published_at=published_at,
        publication_text=publication_text,
        fuel_type=normalize_text(fuel_type),
    )


def fetch_autoscout_metadata(detail_url: str) -> tuple[str, str, str]:
    try:
        response = requests.get(detail_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text, re.S)
        if next_data_match:
            payload = json.loads(next_data_match.group(1))
            serialized = json.dumps(payload)
            seller_match = re.search(r'"isDealer"\s*:\s*(true|false)', serialized)
            date_match = re.search(r'"createdTimestampWithOffset"\s*:\s*"([^"]+)"', serialized)
            seller_type = ""
            if seller_match:
                seller_type = "Concesionario" if seller_match.group(1) == "true" else "Particular"
            published_at = parse_iso_publication(date_match.group(1)) if date_match else ""
            return seller_type, published_at, date_match.group(1) if date_match else ""
        if "VehicleOverview_itemText__AI4dA\">Profesional<" in response.text:
            return "Concesionario", "", ""
        if "VehicleOverview_itemText__AI4dA\">Particular<" in response.text:
            return "Particular", "", ""
    except Exception:
        return "", "", ""
    return "", "", ""


def fetch_autoscout_seller_type(detail_url: str) -> str:
    return fetch_autoscout_metadata(detail_url)[0]


def parse_mazdaselected_card(card, page_url: str) -> Optional[CarListing]:
    link = extract_direct_link(card, page_url)
    if not link:
        return None

    title_main = normalize_text(card.select_one(".v_title").get_text(" ", strip=True)) if card.select_one(".v_title") else ""
    title_trim = normalize_text(card.select_one(".v_equip").get_text(" ", strip=True)) if card.select_one(".v_equip") else ""
    title = normalize_text(f"{title_main} {title_trim}".strip())
    price = normalize_text(card.select_one(".buy_euro").get_text(" ", strip=True)) if card.select_one(".buy_euro") else ""
    mileage = normalize_text(card.select_one(".v_km").get_text(" ", strip=True)) if card.select_one(".v_km") else ""
    year = normalize_text(card.select_one(".v_1").get_text(" ", strip=True)) if card.select_one(".v_1") else ""
    city = normalize_text(card.select_one(".v_3").get_text(" ", strip=True)) if card.select_one(".v_3") else ""
    postal_code = SOURCE_DEFAULT_POSTAL_CODES.get(page_url, "")
    location = f"ES-{postal_code} {city}".strip() if postal_code and city else infer_location_from_source(page_url)

    return build_listing(
        lead_id=extract_lead_id(card, link),
        title=title,
        price=price,
        cash_price=price,
        mileage=mileage,
        year=year,
        url=link,
        image_url=extract_image_url(card, page_url),
        location=location,
        source_url=page_url,
        seller_type="Concesionario",
        raw_text=normalize_text(card.get_text(" ", strip=True)),
    )


def parse_mazdanorkar_card(card, page_url: str) -> Optional[CarListing]:
    link = extract_direct_link(card, page_url)
    if not link:
        return None

    raw_text = normalize_text(card.get_text(" ", strip=True))
    cleaned_text = re.sub(
        r"Ver ficha completa|Agregar al comparador|Agregar a favoritos|Eliminar favorito",
        " ",
        raw_text,
        flags=re.IGNORECASE,
    )
    title = normalize_text(cleaned_text)
    price_match = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d{4,6})\s*€", cleaned_text)
    mileage_match = re.search(r"([\d\.\,]+)\s*km\.?", cleaned_text, re.IGNORECASE)

    return build_listing(
        lead_id=extract_lead_id(card, link),
        title=title,
        price=f"€ {price_match.group(1)}" if price_match else "",
        mileage=f"{mileage_match.group(1)} km" if mileage_match else "",
        year=extract_year(cleaned_text),
        url=link,
        image_url=extract_image_url(card, page_url),
        location=infer_location_from_source(page_url),
        source_url=page_url,
        seller_type="Concesionario",
        raw_text=cleaned_text,
    )


def parse_generic_card(card, page_url: str) -> Optional[CarListing]:
    link = extract_direct_link(card, page_url)
    if not link:
        return None

    raw_text = normalize_text(card.get_text(" ", strip=True))
    seller_type = ""
    published_at = ""
    publication_text = ""
    if "autoscout24." in get_host(page_url):
        seller_type, published_at, publication_text = fetch_autoscout_metadata(link)
    return build_listing(
        lead_id=extract_lead_id(card, link),
        title=extract_title(card),
        price=extract_price(raw_text),
        mileage=extract_mileage(raw_text),
        year=extract_year(raw_text),
        url=link,
        image_url=extract_image_url(card, page_url),
        location=extract_location(raw_text),
        source_url=page_url,
        seller_type=seller_type,
        published_at=published_at,
        publication_text=publication_text,
        raw_text=raw_text,
    )


def parse_card(card, page_url: str) -> Optional[CarListing]:
    host = get_host(page_url)
    if "mazdaselected.es" in host:
        return parse_mazdaselected_card(card, page_url)
    if "mazdanorkar.com" in host:
        return parse_mazdanorkar_card(card, page_url)
    return parse_generic_card(card, page_url)


def fetch_coches_com_listings(search_url: str) -> List[CarListing]:
    response = requests.get(search_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        return []

    payload = json.loads(match.group(1))
    classifieds = payload.get("props", {}).get("pageProps", {}).get("classifieds", {}).get("classifiedList", [])
    listings: List[CarListing] = []
    for vehicle in classifieds:
        make_info = vehicle.get("make") or {}
        model_info = vehicle.get("model") or {}
        version_info = vehicle.get("version") or {}
        title = normalize_text(
            f"{make_info.get('name', '')} {model_info.get('name', '')} {version_info.get('name', '')}"
        )
        registration = vehicle.get("registration") or {}
        mileage_info = vehicle.get("mileage") or {}
        price_info = vehicle.get("price") or {}
        price_offer_info = vehicle.get("priceOffer") or {}
        year = normalize_text(str(registration.get("year", "")))
        mileage_amount = mileage_info.get("amount", "")
        mileage = f"{mileage_amount} km" if mileage_amount != "" else ""
        cash_value = normalize_text(str(price_info.get("amount", "")))
        financed_value = normalize_text(str(price_offer_info.get("amount", "")))
        price = f"€ {financed_value}" if financed_value else f"€ {cash_value}"
        cash_price = f"€ {cash_value}" if cash_value else ""
        financed_price = f"€ {financed_value}" if financed_value else ""
        showroom_list = vehicle.get("showroomList") or []
        city = normalize_text(str(showroom_list[0].get("city", ""))) if showroom_list else ""
        province_info = vehicle.get("currentProvince") or {}
        province = normalize_text(str(province_info.get("name", "")))
        location = ", ".join(item for item in (city, province) if item)
        visible_id = normalize_text(str(vehicle.get("visibleId", "")))
        link = f"https://www.coches.com/coches-segunda-mano/{visible_id}.htm" if visible_id else search_url
        source_label = "Coches.com Particular" if vehicle.get("category") == 3 else "Coches.com"
        published_at = parse_iso_publication(normalize_text(str(vehicle.get("createdAt", ""))))
        fuel_info = vehicle.get("fuel") or {}
        fuel_type = normalize_text(
            str(fuel_info.get("name", "")) if isinstance(fuel_info, dict) else str(fuel_info)
        )
        listing = build_listing(
            lead_id=normalize_text(str(vehicle.get("id", ""))),
            title=title,
            price=price,
            cash_price=cash_price,
            financed_price=financed_price,
            mileage=mileage,
            year=year,
            url=link,
            image_url=normalize_text(str(vehicle.get("image", ""))),
            location=location or province or "Ubicacion no disponible",
            source_url=search_url,
            seller_type="Particular" if vehicle.get("category") == 3 else "Concesionario",
            published_at=published_at,
            publication_text=normalize_text(str(vehicle.get("createdAt", ""))),
            fuel_type=fuel_type,
            raw_text=json.dumps(vehicle, ensure_ascii=False),
        )
        if listing:
            listing.source = source_label
            listings.append(listing)
    return listings


def fetch_kia_api_listings(search_url: str) -> List[CarListing]:
    dealer_id = SOURCE_KIA_DEALER_IDS.get(search_url)
    if not dealer_id:
        return []
    kia_models = [f"&{item['model']}-" for item in load_models() if item["brand"] == "kia"]
    if not kia_models:
        return []
    kia_min_year = min(item["min_year"] for item in load_models() if item["brand"] == "kia")

    response = requests.post(
        "https://kiaokasion.net/kia/async/metodos.aspx",
        data={
            "accion": "actualizarCoches",
            "modelos": "".join(kia_models),
            "carrocerias": "",
            "motores": "",
            "cambios": "",
            "combustibles": "",
            "colores": "",
            "kilometros": "50000",
            "preciominimo": "0",
            "preciomaximo": "22000",
            "cp": "",
            "km": "nacional",
            "orden": "1",
            "pagina": "1",
            "anyminimo": str(kia_min_year),
            "anymaximo": "2026",
            "longitud": "",
            "latitud": "",
            "kmsdistancia": str(int(MAX_DISTANCE_KM)),
            "idconcesionario": dealer_id,
        },
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": search_url,
            "Origin": "https://www.kia.com",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    listings: List[CarListing] = []
    for vehicle in payload.get("vehiculos", []):
        title = normalize_text(f"{vehicle.get('marca', '')} {vehicle.get('modelo', '')} {vehicle.get('version', '')}")
        price = f"€ {vehicle.get('precio', '')}"
        mileage = f"{vehicle.get('kilometros', '')} km"
        year = normalize_text(str(vehicle.get("any", "")))
        image_url = normalize_text(vehicle.get("imagen", ""))
        town = normalize_text(vehicle.get("poblacion", "")) or SOURCE_DEFAULT_LOCATIONS.get(
            SOURCE_DEFAULT_POSTAL_CODES.get(search_url, ""),
            "",
        )
        postal_code = SOURCE_DEFAULT_POSTAL_CODES.get(search_url, "")
        location = f"ES-{postal_code} {town}".strip() if postal_code else town
        listing = build_listing(
            lead_id=normalize_text(str(vehicle.get("id", ""))),
            title=title,
            price=price,
            cash_price=f"€ {vehicle.get('precio_alcontado', '')}" if vehicle.get("precio_alcontado") else price,
            financed_price=f"€ {vehicle.get('precio', '')}" if vehicle.get("precio") else "",
            mileage=mileage,
            year=year,
            url=search_url,
            image_url=image_url,
            location=location or infer_location_from_source(search_url),
            source_url=search_url,
            seller_type="Concesionario",
            fuel_type=normalize_text(str(vehicle.get("combustible", ""))),
            raw_text=json.dumps(vehicle, ensure_ascii=False),
        )
        if listing:
            listings.append(listing)
    return listings


def fetch_coches_net_reader_listings(search_url: str) -> List[CarListing]:
    markdown = fetch_reader_markdown(search_url)
    blocks = re.split(r"(?=^## \[)", markdown, flags=re.MULTILINE)
    listings: List[CarListing] = []

    for block in blocks:
        heading = re.match(r"^## \[([^\]]+)\]\((https://www\.coches\.net/[^)]+)\)", block)
        if not heading:
            continue

        title, detail_url = heading.groups()
        year_match = re.search(r"^\*\s+(20\d{2})\s*$", block, re.MULTILINE)
        mileage_match = re.search(r"^\*\s+([\d.,]+)\s*km\s*$", block, re.MULTILINE | re.IGNORECASE)
        if not year_match or not mileage_match:
            continue

        price_values = extract_reader_price_values(block)
        if not price_values:
            continue
        financed_match = re.search(
            r"Precio financiado:\s*\*\*(\d{1,3}(?:[.,]\d{3})+|\d{4,6})\s*\u20ac\*\*",
            block,
            re.IGNORECASE,
        )
        financed_value = None
        if financed_match:
            financed_value = int(financed_match.group(1).replace(".", "").replace(",", ""))
        cash_value = price_values[0]

        lines = [normalize_text(line.lstrip("* ").strip()) for line in block.splitlines()]
        location = ""
        mileage_index = next(
            (index for index, line in enumerate(lines) if re.fullmatch(r"[\d.,]+\s*km", line, re.IGNORECASE)),
            -1,
        )
        if mileage_index >= 0:
            for candidate in lines[mileage_index + 1 : mileage_index + 5]:
                if not candidate or re.fullmatch(r"\d+\s*cv", candidate, re.IGNORECASE):
                    continue
                if candidate.lower().startswith(("reservable", "profesional", "particular")):
                    continue
                location = candidate
                break

        seller_type = "Particular" if re.search(r"\bParticular\b", block, re.IGNORECASE) else "Concesionario"
        listing = build_listing(
            lead_id=extract_lead_id(None, detail_url),
            title=normalize_text(title),
            price=format_euro(financed_value or cash_value),
            cash_price=format_euro(cash_value),
            financed_price=format_euro(financed_value) if financed_value else "",
            mileage=f"{mileage_match.group(1)} km",
            year=year_match.group(1),
            url=detail_url,
            image_url=extract_reader_image(block, "ccdn.es/cnet/vehicles"),
            location=location or "Ubicacion no disponible",
            source_url=search_url,
            seller_type=seller_type,
            raw_text=block,
        )
        if listing:
            try:
                detail_markdown = fetch_reader_markdown(detail_url)
                listing.published_at, listing.publication_text = parse_reader_publication(
                    detail_markdown,
                    "Coches.net",
                )
            except requests.RequestException:
                pass
            listings.append(listing)
    return listings


def fetch_milanuncios_reader_listings(search_url: str) -> List[CarListing]:
    markdown = fetch_reader_markdown(search_url)
    blocks = re.split(r"(?=^## \[)", markdown, flags=re.MULTILINE)
    listings: List[CarListing] = []

    for block in blocks:
        heading = re.match(
            r'^## \[([^\]]+)\]\((https://www\.milanuncios\.com/[^ )]+)(?:\s+"[^"]*")?\)',
            block,
        )
        if not heading:
            continue
        title, detail_url = heading.groups()
        mileage_match = re.search(r"\*\s*([\d.,]+)\s*kms?\s*\*\s*(20\d{2})", block, re.IGNORECASE)
        if not mileage_match:
            continue

        price_values = extract_reader_price_values(block)
        if not price_values:
            continue
        cash_value = price_values[0]
        financed_value = price_values[1] if len(price_values) > 1 else None
        location_match = re.search(r"\]\[([^\]]+?)(?:\s+Garant[ií]a|\s+\*\s*[\d.,]+\s*kms?)", block, re.IGNORECASE)
        location = normalize_text(location_match.group(1)) if location_match else "Ubicacion no disponible"
        seller_type = "Concesionario" if re.search(
            r"Precio financiado|Garant[ií]a|financiaci[oó]n|concesionario|taller propio",
            block,
            re.IGNORECASE,
        ) else "Particular"

        detail_markdown = ""
        try:
            detail_markdown = fetch_reader_markdown(detail_url)
        except requests.RequestException:
            pass
        published_at, publication_text = parse_reader_publication(detail_markdown, "Milanuncios")

        listing = build_listing(
            lead_id=extract_lead_id(None, detail_url),
            title=normalize_text(title),
            price=format_euro(financed_value or cash_value),
            cash_price=format_euro(cash_value),
            financed_price=format_euro(financed_value) if financed_value else "",
            mileage=f"{mileage_match.group(1)} km",
            year=mileage_match.group(2),
            url=detail_url,
            image_url=extract_reader_image(detail_markdown or block, "images.milanuncios.com"),
            location=location,
            source_url=search_url,
            seller_type=seller_type,
            published_at=published_at,
            publication_text=publication_text,
            raw_text=f"{block} {detail_markdown}",
        )
        if listing:
            listings.append(listing)
    return listings


def parse_wallapop_listing_cards(markdown: str) -> List[tuple[int, str, str, str]]:
    cards: List[tuple[int, str, str, str]] = []
    link_pattern = re.compile(
        r"\]\((https://es\.wallapop\.com/item/[^ )]+)(?:\s+\"[^\"]*\")?\)",
        re.DOTALL,
    )
    for match in link_pattern.finditer(markdown):
        block_start = markdown.rfind("\n\n[", 0, match.start())
        if block_start < 0:
            block_start = 0
        raw_block = markdown[block_start : match.start()]
        content = normalize_text(raw_block)
        detail_url = match.group(1)
        price_match = re.search(r"\*\*([\d.]+)\u20ac\*\*\s*###\s*(.+)", content)
        if not price_match:
            continue
        price_value = int(price_match.group(1).replace(".", ""))
        cards.append(
            (
                price_value,
                price_match.group(2),
                detail_url,
                extract_reader_image(raw_block, "cdn.wallapop.com"),
            )
        )
    return cards


def extract_wallapop_location(content: str, city_slug: str, city_fallbacks: dict[str, str]) -> str:
    postal_match = re.search(
        r"\b(\d{5})\s+([^,.]+)(?:,\s*([^,.]+))?",
        content,
        re.IGNORECASE,
    )
    if postal_match:
        postal_code, city, province = postal_match.groups()
        place = ", ".join(part for part in (normalize_text(city), normalize_text(province or "")) if part)
        return f"ES-{postal_code} {place}".strip()

    city_match = re.search(
        r"(?:ubicad[oa] en|disponible en)\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]+)",
        content,
        re.IGNORECASE,
    )
    if city_match:
        return normalize_text(city_match.group(1))
    return city_fallbacks.get(city_slug, "Ubicacion no disponible")


def fetch_wallapop_reader_listings(search_url: str) -> List[CarListing]:
    markdown = fetch_reader_markdown(search_url)
    listings: List[CarListing] = []

    city_slug = urlparse(search_url).path.rstrip("/").split("/")[-1]
    city_fallbacks = {
        "santander": "Santander",
        "bilbao": "Bilbao",
        "vitoria": "Vitoria-Gasteiz",
        "oviedo": "Oviedo",
        "burgos": "Burgos",
        "logrono": "Logrono",
    }

    for displayed_price, content, detail_url, card_image in parse_wallapop_listing_cards(markdown):
        year_match = re.search(r"\b(20\d{2})\b", content)
        mileage_match = re.search(r"\b([\d.]+)\s*km\b", content, re.IGNORECASE)
        if not year_match or not mileage_match:
            continue
        year = int(year_match.group(1))
        mileage_value = int(mileage_match.group(1).replace(".", ""))
        if displayed_price > 22000 or year < 2022 or mileage_value >= 50000:
            continue

        combined = content

        title = content
        title_split = re.split(
            r"\s+(?:H[ií]brido|Gasolina|Di[eé]sel|El[eé]ctrico|H[ií]brido enchufable)\s+\u00b7",
            content,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        if title_split:
            title = title_split[0]

        cash_match = re.search(
            r"Precio (?:al )?contado(?:\s+de|\s*:)?\s*([\d.]+)\s*\u20ac",
            combined,
            re.IGNORECASE,
        )
        financed_match = re.search(
            r"Precio (?:oferta )?financiad[oa](?:\s*:)?\s*([\d.]+)\s*\u20ac",
            combined,
            re.IGNORECASE,
        )
        cash_value = int(cash_match.group(1).replace(".", "")) if cash_match else displayed_price
        financed_value = int(financed_match.group(1).replace(".", "")) if financed_match else displayed_price

        location_match = re.search(
            r"(?:ubicad[oa] en|concesionario\s+[^,.]+,\s*)([^.\n]+?(?:\d{5}\s+)?[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]+)",
            combined,
            re.IGNORECASE,
        )
        location = normalize_text(location_match.group(1)) if location_match else city_fallbacks.get(
            city_slug,
            "Ubicacion no disponible",
        )
        location = extract_wallapop_location(combined, city_slug, city_fallbacks)
        seller_type = "Concesionario" if re.search(
            r"concesionario|garant[ií]a|stock|financiaci[oó]n|IVA deducible|empresa",
            combined,
            re.IGNORECASE,
        ) else "Particular"

        listing = build_listing(
            lead_id=extract_lead_id(None, detail_url),
            title=normalize_text(title),
            price=format_euro(financed_value),
            cash_price=format_euro(cash_value),
            financed_price=format_euro(financed_value),
            mileage=f"{mileage_value} km",
            year=str(year),
            url=detail_url,
            image_url=card_image,
            location=location,
            source_url=search_url,
            seller_type=seller_type,
            raw_text=combined,
        )
        if listing:
            try:
                detail_markdown = fetch_reader_markdown(detail_url)
                listing.published_at, listing.publication_text = parse_reader_publication(
                    detail_markdown,
                    "Wallapop",
                )
                if not listing.image_url:
                    listing.image_url = extract_reader_image(detail_markdown, "wallapop.com")
            except requests.RequestException:
                pass
            listings.append(listing)
    return listings


def scrape_listings(search_url: str) -> List[CarListing]:
    if "kia.com" in get_host(search_url):
        return fetch_kia_api_listings(search_url)
    if "coches.com" in get_host(search_url):
        return fetch_coches_com_listings(search_url)
    if "coches.net" in get_host(search_url):
        return fetch_coches_net_reader_listings(search_url)
    if "milanuncios.com" in get_host(search_url):
        return fetch_milanuncios_reader_listings(search_url)
    if "wallapop.com" in get_host(search_url):
        return fetch_wallapop_reader_listings(search_url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=True)
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
            page.wait_for_timeout(4000)

            for button_text in ("Aceptar", "Accept", "Estoy de acuerdo"):
                button = page.get_by_role("button", name=button_text)
                try:
                    if button.count() > 0:
                        button.first.click(timeout=1500)
                        page.wait_for_timeout(1000)
                        break
                except PlaywrightTimeoutError:
                    pass

            page_content = page.content()
        finally:
            browser.close()

    lowered = page_content.lower()
    if "algo no va bien" in lowered and "bot" in lowered:
        return []

    soup = BeautifulSoup(page_content, "html.parser")
    cards = discover_cards_for_url(soup, search_url)

    listings: List[CarListing] = []
    seen_ids = set()
    for card in cards:
        listing = parse_card(card, search_url)
        if not listing or listing.lead_id in seen_ids:
            continue
        seen_ids.add(listing.lead_id)
        listings.append(listing)
        if len(listings) >= MAX_RESULTS:
            break

    return listings


def get_search_urls() -> List[str]:
    raw_urls = os.getenv("SEARCH_URLS", "").strip()
    if raw_urls:
        urls = [item.strip() for item in raw_urls.split(",") if item.strip()]
        if urls:
            return urls

    single_url = os.getenv("SEARCH_URL", "").strip()
    if single_url:
        return [single_url]

    return build_default_search_urls()


def scrape_multiple_searches(search_urls: List[str]) -> List[CarListing]:
    merged: List[CarListing] = []
    seen_keys = set()
    results_by_url: dict[str, List[CarListing]] = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_urls = {executor.submit(scrape_listings, url): url for url in search_urls}
        for future in as_completed(future_urls):
            search_url = future_urls[future]
            try:
                results_by_url[search_url] = future.result()
            except Exception as exc:
                print(f"Saltando fuente por error: {search_url} -> {exc}", file=sys.stderr)

    for search_url in search_urls:
        for listing in results_by_url.get(search_url, []):
            dedupe_key = f"{listing.source}:{listing.lead_id}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            merged.append(listing)

    return merged[:MAX_RESULTS]


def post_to_n8n(webhook_url: str, listings: List[CarListing]) -> requests.Response:
    payload = {
        "source": "multi-source",
        "search_urls": get_search_urls(),
        "items": [asdict(item) for item in listings],
    }
    return requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)


def main() -> int:
    search_urls = get_search_urls()
    webhook_url = os.getenv("N8N_WEBHOOK_URL", "").strip()

    if not webhook_url:
        print("Falta la variable de entorno N8N_WEBHOOK_URL", file=sys.stderr)
        return 1

    try:
        listings = scrape_multiple_searches(search_urls)
    except Exception as exc:
        print(f"Error durante el scraping: {exc}", file=sys.stderr)
        return 1

    if not listings:
        print("No se encontraron anuncios con los filtros actuales.", file=sys.stderr)
        return 2

    print(json.dumps([asdict(item) for item in listings], ensure_ascii=False, indent=2))

    try:
        response = post_to_n8n(webhook_url, listings)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Error enviando datos a n8n: {exc}", file=sys.stderr)
        return 1

    print(f"Enviados {len(listings)} anuncios a n8n. Respuesta HTTP: {response.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
