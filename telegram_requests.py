"""Translate private chat requests into read-only search criteria."""

import datetime as dt
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from telegram_preferences import Preferences, parse_preferences, split_preferences


PEDIATRIC_PREFIX = "Konsultacja pediatryczna - "
PEDIATRIC_SERVICES = (
    ("sick visit", "wizyta dla chorego dziecka", "gabinet dzieci chorych"),
    ("sick visit by phone", "telefoniczna dla chorego dziecka", "gabinet dzieci chorych - telefoniczna"),
    ("sick visit NFZ", "wizyta dla chorego dziecka NFZ", "gabinet dzieci chorych NFZ (POZ)"),
    ("routine visit", "wizyta dla zdrowego dziecka", "gabinet dzieci zdrowych"),
    ("routine visit by phone", "telefoniczna dla zdrowego dziecka", "gabinet dzieci zdrowych - telefoniczna"),
    ("routine visit NFZ", "wizyta dla zdrowego dziecka NFZ", "gabinet dzieci zdrowych NFZ (POZ)"),
    ("health check", "bilans", "gabinet dzieci zdrowych - bilans"),
    ("health check NFZ", "bilans NFZ", "gabinet dzieci zdrowych - bilans NFZ (POZ)"),
    ("vaccination assessment", "kwalifikacja do szczepienia", "gabinet dzieci zdrowych - kwalifikacja do szczepienia (inne niż p/grypie)"),
    ("flu vaccination assessment", "kwalifikacja do szczepienia na grypę", "gabinet dzieci zdrowych - kwalifikacja do szczepienia p/grypie"),
    ("vaccination NFZ", "szczepienie NFZ", "gabinet dzieci zdrowych szczepienia (inne niż p/grypie) NFZ (POZ)"),
    ("flu vaccination NFZ with own vaccine", "szczepienie na grypę NFZ z własną szczepionką", "gabinet dzieci zdrowych szczepienia p/grypie (szczepionka własna Pacjenta) NFZ (POZ)"),
    ("newborn follow-up", "patronaż noworodka", "gabinet dzieci zdrowych - patronaż noworodka"),
    ("newborn follow-up NFZ", "patronaż noworodka NFZ", "gabinet dzieci zdrowych - patronaż noworodka - NFZ (POZ)"),
    ("on-duty pediatrician", "lekarz dyżurny", "lekarz dyżurny"),
    ("phone consultation NFZ", "konsultacja telefoniczna NFZ", "telefoniczna NFZ (POZ)"),
)

GENERAL_SERVICES = {
    "allergist": "Alergolog",
    "dermatologist": "Dermatolog",
    "dermatologa": "Dermatolog",
    "internist": "Internista",
    "interniste": "Internista",
    "internal medicine": "Internista",
    "cardiologist": "Kardiolog",
    "kardiologa": "Kardiolog",
    "ophthalmologist": "Okulista",
    "okuliste": "Okulista",
    "ent": "Laryngolog",
    "laryngologa": "Laryngolog",
}
PEDIATRIC_REQUESTS = {"pediatrician", "paediatrician", "pediatric consultation", "pediatric visit", "pediatra", "pediatre", "pediatry"}
PEDIATRIC_ALIASES = {
    "sick child": "sick visit",
    "sick kids": "sick visit",
    "sick child nfz": "sick visit NFZ",
    "sick kids nfz": "sick visit NFZ",
}
GENERAL_REQUESTS = {"doctor", "lekarz", "lekarza"}


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ł", "l"))
    return " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).split())


def city_catalog() -> dict[str, tuple[int, str]]:
    path = Path(__file__).with_name("luxmed-ids") / "ids-cities.json"
    return {
        normalize(city["name"]): (city["id"], city["name"])
        for city in json.loads(path.read_text(encoding="utf-8"))
    }


@dataclass(frozen=True)
class Criteria:
    patient: str
    service: str
    city_id: int
    city: str
    telephone: bool = False
    preferences: Preferences = Preferences()


class RequestError(ValueError):
    pass


class PatientServiceMismatch(RequestError):
    def __init__(self, patient: str, reason: str):
        super().__init__(reason)
        self.patient = patient


def parse_search(text: str, aliases: dict[str, str], children: set[str], cities: dict, *, today: dt.date | None = None) -> Criteria | None:
    preference_text = ""
    if re.match(r"^/find(?:\s|$)", text, re.IGNORECASE):
        _, *tail = text.split(maxsplit=1)
        arguments = tail[0] if tail else ""
        parts = [part.strip() for part in arguments.split("|")]
        if len(parts) not in (2, 3, 4) or not all(parts):
            raise RequestError("Use /find patient | service | city | filters.")
        patient_name, service = parts[:2]
        city_name = parts[2] if len(parts) >= 3 else "Wrocław"
        preference_text = parts[3] if len(parts) == 4 else ""
    else:
        if today is not None:
            text, preference_text = split_preferences(text)
        phrase = re.fullmatch(
            r"(?:find|search for)\s+(.+?)\s+for\s+(.+?)(?:\s+in\s+(.+))?",
            text, re.IGNORECASE,
        )
        if phrase is None:
            return None
        service, patient_name, city_name = phrase.groups()
        service = re.sub(r"^(?:a|an)\s+", "", service, flags=re.IGNORECASE)
        city_name = city_name or "Wrocław"
        if normalize(service) in PEDIATRIC_REQUESTS and normalize(patient_name) not in aliases:
            patient_phrase = normalize(patient_name)
            for alias in sorted(aliases, key=len, reverse=True):
                if patient_phrase.startswith(alias + " "):
                    visit_type = patient_phrase[len(alias):].strip(" ,")
                    service = pediatric_choice(visit_type, allow_number=False)
                    if service is None:
                        raise RequestError("I couldn't match that pediatric visit type. Try “sick kids NFZ” or use the exact portal label.")
                    patient_name = alias
                    break

    patient = aliases.get(normalize(patient_name))
    if patient is None:
        raise RequestError("Which of the three patients is this for?")
    city = cities.get(normalize(city_name))
    if city is None:
        raise RequestError("Use the city name shown in the portal.")
    city_id, city_name = city
    preferences = parse_preferences(preference_text, today) if today is not None else Preferences()
    key = normalize(service)
    selected = pediatric_choice(service, allow_number=False)
    if patient not in children and (key in PEDIATRIC_REQUESTS or selected or key.startswith("konsultacja pediatryczna")):
        raise PatientServiceMismatch(patient, "is configured as an adult, and pediatrics is for children.")
    if patient in children and normalize(GENERAL_SERVICES.get(key, service)) in {
        "internista", "konsultacja internisty", "konsultacja internistyczna",
    }:
        raise PatientServiceMismatch(patient, "is configured as a child, and an internist is an adult service.")
    if key in PEDIATRIC_REQUESTS or (patient in children and key in GENERAL_REQUESTS):
        return Criteria(patient, "", city_id, city_name, preferences=preferences)
    if key in GENERAL_REQUESTS:
        raise RequestError("Which service or specialty?")
    if selected:
        return Criteria(patient, selected, city_id, city_name, "telefonicz" in normalize(selected), preferences)
    service = GENERAL_SERVICES.get(key, service)
    if len(service) > 130 or not service.strip():
        raise RequestError("Use a service name from the portal.")
    return Criteria(patient, service, city_id, city_name, "telefonicz" in normalize(service), preferences)


def pediatric_choice(text: str, *, allow_number: bool = True) -> str | None:
    key = normalize(text)
    for prefix in sorted(PEDIATRIC_REQUESTS, key=len, reverse=True):
        if key.startswith(prefix + " "):
            key = key[len(prefix):].strip()
            break
    key = normalize(PEDIATRIC_ALIASES.get(key, key))
    if allow_number and key.isascii() and key.isdecimal() and len(key) <= 2:
        index = int(key) - 1
        if 0 <= index < len(PEDIATRIC_SERVICES):
            return PEDIATRIC_PREFIX + PEDIATRIC_SERVICES[index][2]
    for english, localized, suffix in PEDIATRIC_SERVICES:
        label = PEDIATRIC_PREFIX + suffix
        if key in {normalize(english), normalize(localized), normalize(label), normalize(suffix)}:
            return label
    return None


def pediatric_menu(patient_name: str) -> str:
    title = f"Which type of pediatric visit should I search for {patient_name}? Reply with a number."
    options = [f"{index}. {choice[0]}" for index, choice in enumerate(PEDIATRIC_SERVICES, 1)]
    return "\n".join([title, *options])
