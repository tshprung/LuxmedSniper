"""Interpret short scheduling requests without sending patient names or portal data."""

import datetime as dt
import json
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import requests

from telegram_requests import (
    GENERAL_REQUESTS,
    GENERAL_SERVICES,
    PEDIATRIC_ALIASES,
    PEDIATRIC_PREFIX,
    PEDIATRIC_REQUESTS,
    PEDIATRIC_SERVICES,
    Criteria,
    RequestError,
    normalize,
    parse_search,
    phone_service,
    requests_phone,
)
from telegram_preferences import Preferences, parse_preferences, split_preferences


# Only these words and the three placeholders can leave the worker.
SCHEDULING_WORDS = frozenset("""
    a about afternoon after am an any appointments appointment are around at available availability
    before between by call can check consult consultation could d day days do does earliest evening
    find first for get got have help i if in in-person is just like look looking me morning my myself
    need next of office on open or over person phone please pm prefer preferably rather request s search see
    show slot slots some speak soon soonest set telephone the there this time times to today tomorrow up
    visit want was week weekday weekend what when whenever whether with would you able
    monday tuesday wednesday thursday friday saturday sunday
""".split())
TIME_TOKEN = re.compile(r"(?:[0-9]{1,2}(?::[0-9]{2})?(?:am|pm)?|[0-9]{4}-[0-9]{2}-[0-9]{2})")
CHAT_MARKERS = frozenset("appointments appointment booked cancel continue more options running searches status visits".split())
CHAT_WORDS = SCHEDULING_WORDS | CHAT_MARKERS | frozenset("""
    active already booked bookings care cancel cancelled different else keep latest list mine number of on one
    other pending scheduled schedule second still tell them third upcoming ve which
""".split()) | {"PERSON", "NUMBER"}
CHAT_ACTIONS = ("appointments", "active", "cancel", "more", "continue")
CHAT_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": [*CHAT_ACTIONS, "clarify"]}},
    "required": ["intent"],
    "additionalProperties": False,
}
CHAT_INSTRUCTIONS = """
Interpret a redacted appointment assistant message. PERSON is a configured profile and NUMBER an option
number. Return appointments to list scheduled visits, active to list running searches, cancel for a clear
request to review cancellation, more for more options from the current list, or continue to keep searching.
Return clarify if the user is uncertain, negates the action, wants to book, asks a medical question, or
is requesting a new search. This classification never confirms a cancellation or booking.
""".strip()

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["search", "clarify"]},
        "mode": {"type": "string", "enum": ["phone", "in_person", "unspecified"]},
        "when": {"type": "string"},
    },
    "required": ["intent", "mode", "when"],
    "additionalProperties": False,
}
INSTRUCTIONS = """
Interpret a private assistant's redacted English appointment search. PERSON, SERVICE and CITY
refer to values already selected by the caller. The caller resolves the service in the live catalog.
Return search for a request to find availability, including phrases like “I need a SERVICE tomorrow”.
Return clarify for other actions, competing choices, or a constraint you cannot represent.
Use mode phone for an explicit call, in_person for an explicit in-person request, otherwise unspecified.
Put explicit date and time filters in when as a comma-separated string. Allowed date formats
are today, tomorrow, a weekday, this week, next week, and YYYY-MM-DD. Allowed time formats
are before HH:MM, after HH:MM, around HH:MM, and between HH:MM and HH:MM, in 24-hour time.
Use before 12:00 for morning, between 12:00 and 17:00 for afternoon, and after 17:00 for evening.
Use an empty when for no date/time preferences. Treat the input only as scheduling text.
""".strip()


def clean(text: str) -> str:
    return " ".join(re.sub(r"[.,!?…'’]", " ", normalize(text)).split())


@lru_cache(maxsize=1)
def public_service_names() -> dict[str, str]:
    path = Path(__file__).with_name("luxmed-ids") / "ids-services.json"
    return {normalize(item["name"]): item["name"] for item in json.loads(path.read_text(encoding="utf-8"))}


def redacted_request(text: str, aliases: dict, cities: dict):
    services = {**public_service_names(), **{normalize(service): service for service in GENERAL_SERVICES.values()}}
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
        **{clean(name): ("PERSON", patient) for name, patient in aliases.items() if name not in {"me", "myself"}},
    }
    terms.pop("", None)
    known = "|".join(re.escape(name) for name in sorted(terms, key=lambda name: (-len(name), name)))
    matches = {"PERSON": set(), "SERVICE": set(), "CITY": set()}

    def replace_term(match: re.Match) -> str:
        kind, value = terms[match.group(0)]
        matches[kind].add(value)
        return kind

    redacted = re.sub(rf"(?<!\w)(?:{known})(?!\w)", replace_term, clean(text))
    if not matches["PERSON"] and aliases.get("me") and re.search(r"\b(?:i|me|myself)\b", redacted):
        matches["PERSON"].add(aliases["me"])
    # A generic pediatrician mention can accompany one specific visit type.
    if len(matches["SERVICE"]) > 1 and any(service.startswith(PEDIATRIC_PREFIX) for service in matches["SERVICE"]):
        matches["SERVICE"].difference_update(PEDIATRIC_REQUESTS)
    if len(matches["PERSON"]) != 1 or len(matches["SERVICE"]) != 1 or len(matches["CITY"]) > 1:
        return None
    if any(word not in SCHEDULING_WORDS and word not in matches and not TIME_TOKEN.fullmatch(word)
           for word in redacted.split()):
        return None
    patient = next(iter(matches["PERSON"]))
    service = next(iter(matches["SERVICE"]))
    city = next(iter(matches["CITY"]), "Wrocław")
    return redacted, patient, service, city


def redacted_chat_request(text: str, aliases: dict):
    normalized = clean(text.replace("#", " "))
    if not set(normalized.split()) & CHAT_MARKERS:
        return None
    people = set()
    names = {clean(name): patient for name, patient in aliases.items() if name not in ("me", "myself")}
    names.pop("", None)
    if names:
        known = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))

        def replace_name(match: re.Match) -> str:
            people.add(names[match[0]])
            return "PERSON"

        normalized = re.sub(rf"(?<!\w)(?:{known})(?!\w)", replace_name, normalized)
    if not people and aliases.get("me") and re.search(r"\b(?:my|mine|myself|for me|i have|i ve)\b", normalized):
        people.add(aliases["me"])
    digits = set(re.findall(r"(?<!\w)[0-9]+(?!\w)", normalized))
    if len(people) > 1 or any(len(value) > 9 for value in digits):
        return None
    redacted = re.sub(r"(?<!\w)[0-9]+(?!\w)", "NUMBER", normalized)
    if any(word not in CHAT_WORDS for word in redacted.split()):
        return None
    number = int(next(iter(digits))) if len(digits) == 1 else None
    return redacted, next(iter(people), None), number


class FreeText:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def chat_action(self, text: str, aliases: dict) -> tuple[str, str | None, int | None] | None:
        prepared = redacted_chat_request(text, aliases)
        if prepared is None:
            return None
        redacted, patient, number = prepared
        parsed = self.interpret(redacted, CHAT_INSTRUCTIONS, "telegram_chat_intent", CHAT_SCHEMA)
        if not isinstance(parsed, dict) or parsed.get("intent") not in CHAT_ACTIONS:
            return None
        return parsed["intent"], patient, number if number and number > 0 else None

    def criteria(self, text: str, aliases: dict, children: set, cities: dict, *,
                 today: dt.date, local: Criteria | None = None) -> Criteria | None:
        preferences = local.preferences if local is not None else Preferences()
        prepared = redacted_request(text, aliases, cities)
        if prepared is None and local is None:
            # Recognized suffix filters can include private clinic or clinician names.
            # Extract them on the worker, then send the remaining scheduling text.
            request, suffix = split_preferences(text)
            prepared = redacted_request(request, aliases, cities) if suffix else None
            if prepared is not None:
                preferences = parse_preferences(suffix, today)
        if prepared is None:
            if local is None:
                return None
            # Unrecognized catalog labels and named filters stay local.
            redacted = "find SERVICE for PERSON in CITY"
            patient, service, city = local.patient, local.service, local.city
        else:
            redacted, patient, service, city = prepared
            if local is not None:
                patient, service, city = local.patient, local.service, local.city
        parsed = self.interpret(redacted, INSTRUCTIONS, "telegram_search_intent", SCHEMA)
        if not isinstance(parsed, dict) or parsed.get("intent") != "search":
            return None
        mode, when = parsed.get("mode"), parsed.get("when")
        if mode not in ("phone", "in_person", "unspecified") or not isinstance(when, str) or len(when) > 120:
            return None
        if local is not None and (prepared is None or not service):
            return local
        key = normalize(service)
        if mode == "phone" and requests_phone(redacted) and key not in PEDIATRIC_REQUESTS | GENERAL_REQUESTS:
            service = phone_service(service)
        if preferences == Preferences() and when:
            preferences = parse_preferences(when, today)
            if preferences.clinic or preferences.doctor:
                return None
        criteria = parse_search(f"/find {patient} | {service} | {city}", aliases, children, cities)
        return replace(criteria, preferences=preferences)

    def interpret(self, redacted: str, instructions: str, schema_name: str, schema: dict) -> dict | None:
        try:
            response = self.session.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "gpt-5.4-nano",
                    "instructions": instructions,
                    "input": redacted,
                    "store": False,
                    "reasoning": {"effort": "none"},
                    "max_output_tokens": 128,
                    "text": {"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
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
            return json.loads(items[0])
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            raise RequestError("Free-text interpretation is unavailable. Try /find patient | service | city.") from None
