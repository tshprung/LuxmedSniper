"""Read published Wrocław clinic prices from LUX MED's public price list."""

import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal

import requests

from telegram_requests import normalize


PUBLIC_PRICE_URL = "https://www.luxmed.pl/dla-pacjenta/uslugi/cennik"
PUBLIC_PRICE_CITY = "Wrocław"
MAX_PUBLIC_PAGE_BYTES = 40 * 1024 * 1024
PRICE_MARKER = '"priceList":'


@dataclass(frozen=True)
class ListedPrice:
    amount: Decimal
    clinics: tuple[str, ...]

    def describe(self) -> str:
        amount = format(self.amount.normalize(), "f").replace(".", ",")
        clinic = self.clinics[0] if len(self.clinics) == 1 else f"{len(self.clinics)} clinics at this price"
        return f"Lowest LUX MED public price in {PUBLIC_PRICE_CITY} · {amount} zł · {clinic}"


def wroclaw_prices(page: str) -> dict[str, ListedPrice]:
    if (marker := page.find(PRICE_MARKER)) < 0:
        return {}
    catalog, _ = json.JSONDecoder().raw_decode(page, marker + len(PRICE_MARKER))
    if not isinstance(catalog, dict):
        return {}
    matches: dict[str, dict[str, ListedPrice]] = {}
    for category in catalog.values():
        if not isinstance(category, dict):
            continue
        for group in category.values():
            if not isinstance(group, dict):
                continue
            for service_id, service in group.items():
                if not isinstance(service, dict) or not isinstance(service.get("Nazwa_na_www"), str):
                    continue
                name = normalize(service["Nazwa_na_www"])
                if price := listed_price(service.get("cities"), name):
                    matches.setdefault(name, {})[service_id] = price
    return {name: next(iter(prices.values())) for name, prices in matches.items() if len(prices) == 1}


def listed_price(cities, service_name: str) -> ListedPrice | None:
    clinics = cities.get(PUBLIC_PRICE_CITY) if isinstance(cities, dict) else None
    if not isinstance(clinics, dict):
        return None
    prices = {}
    for clinic in clinics.values():
        if not isinstance(clinic, dict) or clinic.get("city") != PUBLIC_PRICE_CITY:
            continue
        name, price, service = (clinic.get(field) for field in ("centerName", "cena", "Nazwa_na_www"))
        if not isinstance(service, str) or normalize(service) != service_name:
            continue
        if not isinstance(name, str) or not isinstance(price, str) or not re.fullmatch(r"[0-9]+(?:[,.][0-9]{1,2})?", price):
            continue
        amount = Decimal(price.replace(",", "."))
        if amount > 0:
            label = " ".join(name.replace("_~s~", "/").split())[:130]
            if label:
                prices[label] = min(amount, prices.get(label, amount))
    if prices:
        lowest = min(prices.values())
        return ListedPrice(lowest, tuple(sorted(name for name, amount in prices.items() if amount == lowest)))
    return None


class PublicPriceList:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.expires_at = 0
        self.prices: dict[str, ListedPrice] = {}

    def lookup(self, service_name: str) -> ListedPrice | None:
        now = self.clock()
        if now >= self.expires_at:
            try:
                self.prices = self.load()
            except (requests.RequestException, ValueError):
                self.prices = {}
            self.expires_at = now + (6 * 3600 if self.prices else 5 * 60)
        return self.prices.get(normalize(service_name))

    @staticmethod
    def load() -> dict[str, ListedPrice]:
        # Use a separate public request with no Patient Portal session or auth headers.
        with requests.get(PUBLIC_PRICE_URL, timeout=(4, 8), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return {}
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(data) + len(chunk) > MAX_PUBLIC_PAGE_BYTES:
                    return {}
                data.extend(chunk)
            return wroclaw_prices(data.decode("utf-8"))
