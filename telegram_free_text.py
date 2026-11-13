"""Optional interpretation of short scheduling requests using redacted text."""

import json
import re

import requests

from telegram_requests import (
    GENERAL_REQUESTS,
    GENERAL_SERVICES,
    PEDIATRIC_ALIASES,
    PEDIATRIC_PREFIX,
    PEDIATRIC_REQUESTS,
    PEDIATRIC_SERVICES,
    RequestError,
    normalize,
    parse_search,
)


# Only these words and the three placeholders can leave the worker.
SCHEDULING_WORDS = frozenset("""
    a an am any are at available availability appointments appointment
    can check could d do does find for get have help i if in is just look looking
    me my need of please request s search see slots slot some the there to visit
    want what with would you able show like
    whether
""".split())

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["search", "clarify"]},
    },
    "required": ["intent"],
    "additionalProperties": False,
}
INSTRUCTIONS = (
    "Classify a redacted English Telegram request. PERSON, SERVICE and CITY stand for "
    "a patient, portal service and city already validated locally. Return search only when the "
    "user clearly wants to check appointment availability. Return clarify for statements, unclear "
    "requests or any request to book, cancel or change appointments."
)


def clean(text: str) -> str:
    return " ".join(re.sub(r"[.,!?…'’]", " ", normalize(text)).split())


def redacted_request(text: str, aliases: dict, cities: dict):
    services = {normalize(service): service for service in GENERAL_SERVICES.values()}
    services.update(GENERAL_SERVICES)
    services.update({key: key for key in PEDIATRIC_REQUESTS | GENERAL_REQUESTS})
    for english, polish, suffix in PEDIATRIC_SERVICES:
        label = PEDIATRIC_PREFIX + suffix
        for alias in (english, polish, label):
            services[normalize(alias)] = label
    for alias, service in PEDIATRIC_ALIASES.items():
        services[alias] = services[normalize(service)]

    locations = {name: city[1] for name, city in cities.items()}
    terms = {
        **{clean(name): ("CITY", city) for name, city in locations.items()},
        **{clean(name): ("SERVICE", service) for name, service in services.items()},
        **{clean(name): ("PERSON", patient) for name, patient in aliases.items()},
    }
    terms.pop("", None)
    known = "|".join(re.escape(name) for name in sorted(terms, key=lambda name: (-len(name), name)))
    matches = {"PERSON": set(), "SERVICE": set(), "CITY": set()}

    def replace_term(match: re.Match) -> str:
        kind, value = terms[match.group(0)]
        matches[kind].add(value)
        return kind

    redacted = re.sub(rf"(?<!\w)(?:{known})(?!\w)", replace_term, clean(text))
    # A generic pediatrician mention can accompany one specific visit type.
    if len(matches["SERVICE"]) > 1 and any(service.startswith(PEDIATRIC_PREFIX) for service in matches["SERVICE"]):
        matches["SERVICE"].difference_update(PEDIATRIC_REQUESTS)
    if len(matches["PERSON"]) != 1 or len(matches["SERVICE"]) != 1 or len(matches["CITY"]) > 1:
        return None
    if any(word not in SCHEDULING_WORDS and word not in matches for word in redacted.split()):
        return None
    patient = next(iter(matches["PERSON"]))
    service = next(iter(matches["SERVICE"]))
    city = next(iter(matches["CITY"]), "Wrocław")
    return redacted, patient, service, city


class FreeText:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def criteria(self, text: str, aliases: dict, children: set, cities: dict):
        prepared = redacted_request(text, aliases, cities)
        if prepared is None:
            return None
        redacted, patient, service, city = prepared
        try:
            response = self.session.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "gpt-5.4-nano",
                    "instructions": INSTRUCTIONS,
                    "input": redacted,
                    "store": False,
                    "reasoning": {"effort": "none"},
                    "max_output_tokens": 96,
                    "text": {"format": {"type": "json_schema", "name": "telegram_search_intent", "strict": True, "schema": SCHEMA}},
                },
                timeout=(4, 8),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError("API status")
            body = response.json()
            if body["status"] != "completed":
                return None
            items = [
                content["text"]
                for message in body["output"] if message.get("type") == "message"
                for content in message.get("content", []) if content.get("type") == "output_text"
            ]
            if len(items) != 1:
                return None
            parsed = json.loads(items[0])
            if parsed["intent"] != "search":
                return None
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            raise RequestError("Free-text interpretation is unavailable. Try /find patient | service | city.") from None

        return parse_search(f"/find {patient} | {service} | {city}", aliases, children, cities)
