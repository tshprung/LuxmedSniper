"""Translate private chat requests into read-only search criteria."""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


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


class RequestError(ValueError):
    pass


def parse_search(text: str, aliases: dict[str, str], children: set[str], cities: dict) -> Criteria | None:
    if re.match(r"^/find(?:\s|$)", text, re.IGNORECASE):
        _, *tail = text.split(maxsplit=1)
        arguments = tail[0] if tail else ""
        parts = [part.strip() for part in arguments.split("|")]
        if len(parts) not in (2, 3) or not all(parts):
            raise RequestError("Use /find patient | service | city.")
        patient_name, service = parts[:2]
        city_name = parts[2] if len(parts) == 3 else "Wrocław"
    else:
        phrase = re.fullmatch(
            r"(?:find|search for)\s+(.+?)\s+for\s+(.+?)(?:\s+in\s+(.+))?",
            text, re.IGNORECASE,
        )
        if phrase is None:
            return None
        service, patient_name, city_name = phrase.groups()
        service = re.sub(r"^(?:a|an)\s+", "", service, flags=re.IGNORECASE)
        city_name = city_name or "Wrocław"

    patient = aliases.get(normalize(patient_name))
    if patient is None:
        raise RequestError("Which of the three patients is this for?")
    city = cities.get(normalize(city_name))
    if city is None:
        raise RequestError("Use the city name shown in the portal.")
    city_id, city_name = city
    key = normalize(service)
    if key in PEDIATRIC_REQUESTS or (patient in children and key in GENERAL_REQUESTS):
        if patient not in children:
            raise RequestError("Which child is this for?")
        return Criteria(patient, "", city_id, city_name)
    if key in GENERAL_REQUESTS:
        raise RequestError("Which service or specialty?")
    if patient in children:
        if GENERAL_SERVICES.get(key, service).casefold() == "internista":
            return Criteria(patient, "", city_id, city_name)
        selected = pediatric_choice(service, allow_number=False)
        if selected:
            return Criteria(patient, selected, city_id, city_name, "telefonicz" in normalize(selected))
    service = GENERAL_SERVICES.get(key, service)
    if len(service) > 130 or not service.strip():
        raise RequestError("Use a service name from the portal.")
    return Criteria(patient, service, city_id, city_name, "telefonicz" in normalize(service))


def pediatric_choice(text: str, *, allow_number: bool = True) -> str | None:
    key = normalize(text)
    if allow_number and key.isascii() and key.isdecimal() and len(key) <= 2:
        index = int(key) - 1
        if 0 <= index < len(PEDIATRIC_SERVICES):
            return PEDIATRIC_PREFIX + PEDIATRIC_SERVICES[index][2]
    for english, localized, suffix in PEDIATRIC_SERVICES:
        label = PEDIATRIC_PREFIX + suffix
        if key in {normalize(english), normalize(localized), normalize(label), normalize(suffix)}:
            return label
    return None


def pediatric_menu() -> str:
    title = "Which type of pediatric visit? Reply with a number."
    options = [f"{index}. {choice[0]}" for index, choice in enumerate(PEDIATRIC_SERVICES, 1)]
    return "\n".join([title, *options])
