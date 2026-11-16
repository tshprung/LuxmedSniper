"""Translate private chat requests into read-only search criteria."""

import datetime as dt
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from telegram_preferences import Preferences, contains_words, name_words, parse_preferences, split_preferences


INTERNIST_PHONE_SERVICE = "Konsultacja internistyczna - telefoniczna"
INTERNIST_NFZ_SERVICE = "Konsultacja internistyczna - NFZ (POZ)"
ALLERGY_SERVICE = "Konsultacja alergologiczna"
PHONE_WORDS = re.compile(r"\b(?:call|phone|telephone|telefonicz\w*|teleporad\w*)\b")
REMOTE_WORDS = re.compile(r"\b(?:video|wideo|online)\b")
OFFICE_SUFFIX = re.compile(r"\s+(?:in[- ]person|in (?:the )?office|in (?:the )?clinic)$", re.IGNORECASE)
PHONE_SUFFIX = re.compile(r"\s+(?:by phone|over the phone|by telephone|phone(?: call| consultation)?|call)$", re.IGNORECASE)
PHONE_PREFIX = re.compile(r"^(?:phone consultation|(?:phone )?call|telephone consultation)\s+(?:with\s+(?:(?:a|an)\s+)?)?", re.IGNORECASE)
MODE_CHOICES = re.compile(
    r"\s+(?:(?:in[- ]person|in (?:the )?office) or (?:by )?(?:phone(?: call)?|telephone|call)|"
    r"(?:by )?(?:phone(?: call)?|telephone|call) or (?:in[- ]person|in (?:the )?office))$",
    re.IGNORECASE,
)
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
    "allergist": ALLERGY_SERVICE,
    "alergolog": ALLERGY_SERVICE,
    "alergologa": ALLERGY_SERVICE,
    "allergolog": ALLERGY_SERVICE,
    "allergy consultation": ALLERGY_SERVICE,
    "allergist phone consultation": "Konsultacja alergologiczna - telefoniczna",
    "allergist phone call": "Konsultacja alergologiczna - telefoniczna",
    "dermatologist": "Dermatolog",
    "dermatologa": "Dermatolog",
    "internist": "Internista",
    "interniste": "Internista",
    "internal medicine": "Internista",
    "internista nfz": INTERNIST_NFZ_SERVICE,
    "internist nfz": INTERNIST_NFZ_SERVICE,
    "internista poz": INTERNIST_NFZ_SERVICE,
    "internist poz": INTERNIST_NFZ_SERVICE,
    "internist phone consultation": INTERNIST_PHONE_SERVICE,
    "internist phone call": INTERNIST_PHONE_SERVICE,
    "phone consultation with an internist": INTERNIST_PHONE_SERVICE,
    "phone consultation with internist": INTERNIST_PHONE_SERVICE,
    "call with internist": INTERNIST_PHONE_SERVICE,
    "call with an internist": INTERNIST_PHONE_SERVICE,
    "phone call with internist": INTERNIST_PHONE_SERVICE,
    "phone call with an internist": INTERNIST_PHONE_SERVICE,
    "cardiologist": "Kardiolog",
    "kardiologa": "Kardiolog",
    "ophthalmologist": "Okulista",
    "okuliste": "Okulista",
    "ent": "Konsultacja laryngologiczna",
    "ent specialist": "Konsultacja laryngologiczna",
    "ent phone consultation": "Konsultacja laryngologiczna - telefoniczna",
    "ent phone call": "Konsultacja laryngologiczna - telefoniczna",
    "laryngologa": "Laryngolog",
    "orthopedist": "Konsultacja ortopedyczna",
    "orthopaedist": "Konsultacja ortopedyczna",
    "orthopedic consultation": "Konsultacja ortopedyczna",
    "neurologist": "Neurolog",
    "gynecologist": "Ginekolog",
    "gynaecologist": "Ginekolog",
    "urologist": "Urolog",
    "endocrinologist": "Endokrynolog",
    "endokrynologa": "Endokrynolog",
    "gastroenterologist": "Gastroenterolog (Gastrolog)",
    "gastroenterologa": "Gastroenterolog (Gastrolog)",
    "gastrolog": "Gastroenterolog (Gastrolog)",
    "gastrologa": "Gastroenterolog (Gastrolog)",
    "rheumatologist": "Reumatolog",
    "reumatologa": "Reumatolog",
    "nephrologist": "Nefrolog",
    "nefrologa": "Nefrolog",
    "pulmonologist": "Pulmonolog",
    "pulmonologa": "Pulmonolog",
    "diabetologist": "Diabetolog",
    "diabetologa": "Diabetolog",
    "hematologist": "Hematolog",
    "haematologist": "Hematolog",
    "hematologa": "Hematolog",
    "dentist": "Stomatolog",
    "ultrasound": "USG",
    "thyroid ultrasound": "USG tarczycy",
    "blood draw": "Pobranie krwi",
    "fasting blood draw": "Pobranie krwi - i innych materiałów - na czczo",
}
PEDIATRIC_REQUESTS = {
    "pediatrician", "paediatrician", "pediatric consultation", "pediatric visit",
    "pediatr", "pediatra", "pediatre", "pediatry",
}
PEDIATRIC_ALIASES = {
    "sick child": "sick visit",
    "sick kids": "sick visit",
    "sick child nfz": "sick visit NFZ",
    "sick kids nfz": "sick visit NFZ",
}
GENERAL_REQUESTS = {"doctor", "lekarz", "lekarza"}
SEARCH_PREFIX = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?(?:find|(?:look|search|check)\s+for)\s+",
    re.IGNORECASE,
)
APPOINTMENTS_REQUEST = re.compile(
    r"(?:(?:please )?(?:(?:can|could|would) you (?:please )?)?"
    r"(?:(?:show|list|check)(?: me)?|let me see|what are) )?"
    r"(?:all )?(?:my |the |(?P<possessor>[\w -]{1,80}?)['’]s )?"
    r"(?:(?:upcoming|next|scheduled|booked) )*(?:lux med )?(?:appointments?|visits?|bookings?)"
    r"(?: for (?P<patient>.+?))?(?: please)?"
)
COVERAGE_AFTER_SERVICE = re.compile(
    r"(?P<service>.+?) (?:covered|included|part of (?:my|the) (?:package|plan|subscription|abonament)|"
    r"in (?:my|the) (?:package|plan|subscription|abonament)|"
    r"paid|payable|free)(?: (?:by|in|with) (?:my|the) "
    r"(?:lux med )?(?:package|plan|subscription|abonament))?(?: for (?P<patient>.+))?"
)
COVERAGE_BEFORE_SERVICE = re.compile(
    r"(?:coverage for|does (?:lux med|(?:my|the) (?:lux med )?(?:package|plan|subscription|abonament)) cover|"
    r"(?:do|would) i (?:have to )?pay for|is there a charge for|how much (?:is|are)|"
    r"how much (?:does|do)) (?P<service>.+?)(?: cost)?(?: for (?P<patient>.+))?"
)
PACKAGE_SERVICE_ALIASES = {
    "cardiolog": "Konsultacja kardiologiczna",
    "cardiologist": "Konsultacja kardiologiczna",
    "cardiology": "Konsultacja kardiologiczna",
    "cardiology consultation": "Konsultacja kardiologiczna",
    "kardiolog": "Konsultacja kardiologiczna",
    "kardiologa": "Konsultacja kardiologiczna",
    "cardiologist phone consultation": "Konsultacja kardiologiczna - telefoniczna",
    "holter": "Założenie Holtera",
    "holter test": "Założenie Holtera",
    "heart holter": "Założenie Holtera EKG",
    "heart holter test": "Założenie Holtera EKG",
    "holter for heart": "Założenie Holtera EKG",
    "holter test for heart": "Założenie Holtera EKG",
    "holter ecg": "Założenie Holtera EKG",
    "ecg holter": "Założenie Holtera EKG",
    "holter ekg": "Założenie Holtera EKG",
    "ekg holter": "Założenie Holtera EKG",
    "blood pressure holter": "Założenie Holtera ciśnieniowego",
    "holter cisnieniowy": "Założenie Holtera ciśnieniowego",
}


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ł", "l"))
    return " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).split())


def appointments_request(text: str) -> str | None:
    key = normalize(text.strip().rstrip("?.!"))
    command, _, argument = key.partition(" ")
    if command in {"/appointments", "/visits", "/bookings"}:
        return argument.removeprefix("for ") or "me"
    if match := APPOINTMENTS_REQUEST.fullmatch(key):
        return match["patient"] or match["possessor"] or "me"
    return None


def coverage_request(text: str) -> tuple[str, tuple[str, ...]] | None:
    key = normalize(text.strip().rstrip("?.!"))
    command, _, rest = key.partition(" ")
    if command == "/coverage":
        patient, separator, services = rest.partition("|")
        patient, services = (patient.strip(), services.strip()) if separator else ("me", rest)
        return patient, (services,) if services else ()
    key = re.sub(
        r"^(?:please )?(?:(?:can|could|would) you (?:please )?)?(?:check (?:if |whether )?)?",
        "", key,
    ).removesuffix(" please")
    subject = re.sub(r"^(?:is|are) ", "", key)
    match = COVERAGE_AFTER_SERVICE.fullmatch(subject) or COVERAGE_BEFORE_SERVICE.fullmatch(key)
    if match is None:
        return None
    parts = tuple(
        re.sub(r"^(?:a|an|the) | (?:visit|appointment)$", "", part.strip())
        for part in re.split(r"\s*(?:, and |,|;|\band\b)\s*", match["service"])
    )
    return match["patient"] or "me", parts if all(parts) else ()


def matching_package_services(query: str, services: list[dict]) -> tuple[list[dict], bool]:
    alias = PACKAGE_SERVICE_ALIASES.get(query, GENERAL_SERVICES.get(query, query))
    names = [(variant, name_words(variant["name"])) for variant in services]
    candidates = dict.fromkeys((name_words(alias), name_words(query)))
    for words in candidates:
        exact = [variant for variant, name in names if words == name]
        if exact:
            return exact, True
    for words in candidates:
        partial = [variant for variant, name in names if contains_words(name, words)]
        if partial:
            return partial, False
    return [], False


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
    def __init__(self, patient: str, reason: str, *, pediatric_search: Criteria | None = None):
        super().__init__(reason)
        self.patient = patient
        self.pediatric_search = pediatric_search


class PatientChoiceNeeded(RequestError):
    def __init__(self, service: str, city: str, filters: str):
        self.parts = (service, city, filters)


class VisitModeChoiceNeeded(RequestError):
    def __init__(self, criteria: Criteria):
        self.criteria = criteria


class ServiceChoiceNeeded(RequestError):
    def __init__(self, criteria: Criteria):
        self.criteria = criteria


def requests_phone(text: str) -> bool:
    return bool(PHONE_WORDS.search(normalize(text)))


def requests_remote(text: str) -> bool:
    return requests_phone(text) or bool(REMOTE_WORDS.search(normalize(text)))


def phone_service(service: str) -> str:
    key = normalize(service)
    if key == "internista":
        return INTERNIST_PHONE_SERVICE
    return service if requests_remote(service) else f"{service} - telefoniczna"


def check_patient_service(criteria: Criteria, children: set[str]) -> None:
    patient, service = criteria.patient, criteria.service
    key = normalize(service)
    if patient not in children and (
        key in PEDIATRIC_REQUESTS or pediatric_choice(service, allow_number=False)
        or key.startswith(("konsultacja pediatryczna", "pediatra -"))
    ):
        raise PatientServiceMismatch(patient, "is configured as an adult, and pediatrics is for children.")
    if patient in children and re.match(r"^(?:internista|konsultacja internisty|konsultacja internistyczna)\b", key):
        raise PatientServiceMismatch(
            patient, "is configured as a child, and an internist is an adult service.",
            pediatric_search=replace(criteria, service="", telephone=False),
        )


def matching_catalog_variants(service: str, variants, *, telephone: bool) -> list[dict]:
    requested = name_words(service)
    exact = {}
    partial = {}
    for variant in variants:
        remote, service_id = variant.get("isTelemedicine"), variant.get("id")
        if remote is not telephone or type(service_id) is not int or service_id <= 0:
            continue
        words = name_words(variant["name"])
        if requested and words == requested:
            exact[service_id] = variant
        elif contains_words(words, requested):
            partial[service_id] = variant
    return sorted((exact or partial).values(), key=lambda variant: normalize(variant["name"]))


def _one_edit(left: str, right: str) -> bool:
    if len(left) == len(right):
        changed = [index for index, (first, second) in enumerate(zip(left, right)) if first != second]
        if len(changed) == 1:
            return True
        if len(changed) == 2:
            first, second = changed
            return second == first + 1 and left[first:second + 1] == right[first:second + 1][::-1]
        return False
    shorter, longer = sorted((left, right), key=len)
    if len(longer) != len(shorter) + 1:
        return False
    offset = next((index for index, (first, second) in enumerate(zip(shorter, longer)) if first != second), len(shorter))
    return shorter[offset:] == longer[offset + 1:]


def nearby_catalog_variants(service: str, variants, *, telephone: bool) -> list[dict]:
    requested = normalize(service)
    if len(requested) < 5:
        return []
    aliases = {normalize(label) for key, label in GENERAL_SERVICES.items() if _one_edit(requested, key)}
    matches = {}
    for variant in variants:
        remote, service_id = variant.get("isTelemedicine"), variant.get("id")
        if remote is not telephone or type(service_id) is not int or service_id <= 0:
            continue
        name = normalize(variant["name"])
        if name in aliases or _one_edit(requested, name):
            matches[name, remote] = variant
    return sorted(matches.values(), key=lambda variant: normalize(variant["name"]))


def parse_search(text: str, aliases: dict[str, str], children: set[str], cities: dict, *, today: dt.date | None = None) -> Criteria | None:
    preference_text = ""
    phone_after_patient = False
    choose_mode = False
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
        prefix = SEARCH_PREFIX.match(text)
        if prefix is None:
            return None
        phrase = text[prefix.end():].rstrip(" ?.!")
        # Only the part after the service can contain search filters.
        patient_separator = re.search(r"\s+for\s+", phrase, re.IGNORECASE)
        if patient_separator is None:
            standalone = re.sub(r"^(?:a|an)\s+", "", phrase, flags=re.IGNORECASE)
            if normalize(standalone) in GENERAL_SERVICES.keys() | GENERAL_REQUESTS | PEDIATRIC_REQUESTS:
                raise PatientChoiceNeeded(standalone, "Wrocław", "")
            return None
        service = phrase[:patient_separator.start()]
        patient_phrase = phrase[patient_separator.end():]
        if today is not None:
            patient_phrase, preference_text = split_preferences(patient_phrase)
        patient_phrase, option_requested = MODE_CHOICES.subn("", patient_phrase)
        choose_mode = bool(option_requested)
        patient_phrase, removed_phone = PHONE_SUFFIX.subn("", patient_phrase)
        phone_after_patient = bool(removed_phone)
        patient_phrase = OFFICE_SUFFIX.sub("", patient_phrase)
        patient_location = re.fullmatch(r"(.+?)(?:\s+in\s+(.+))?", patient_phrase, re.IGNORECASE)
        if patient_location is None:
            return None
        patient_name, city_name = patient_location.groups()
        service = re.sub(r"^(?:me\s+)?(?:(?:a|an)\s+)?", "", service, flags=re.IGNORECASE)
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
        if choose_mode:
            service += " by phone or in person"
        elif phone_after_patient:
            service += " by phone"
        raise PatientChoiceNeeded(service, city_name, preference_text)
    city = cities.get(normalize(city_name))
    if city is None:
        raise RequestError("Use the city name shown in the portal.")
    city_id, city_name = city
    preferences = parse_preferences(preference_text, today) if today is not None else Preferences()
    service, service_mode_choice = MODE_CHOICES.subn("", service)
    choose_mode = choose_mode or bool(service_mode_choice)
    key = normalize(service)
    selected = pediatric_choice(service, allow_number=False)
    service = GENERAL_SERVICES.get(key, service)
    if selected is None and key not in GENERAL_SERVICES and key not in GENERAL_REQUESTS | PEDIATRIC_REQUESTS:
        # Match explicit English phone wording around a known specialty.
        plain = OFFICE_SUFFIX.sub("", service)
        plain, removed_suffix = PHONE_SUFFIX.subn("", plain)
        plain, removed_prefix = PHONE_PREFIX.subn("", plain)
        if plain != service:
            service = GENERAL_SERVICES.get(normalize(plain), plain)
            if removed_suffix or removed_prefix:
                service = phone_service(service)
    if phone_after_patient and selected is None and key not in GENERAL_REQUESTS | PEDIATRIC_REQUESTS:
        service = phone_service(service)
    service = selected or service
    criteria = Criteria(patient, service, city_id, city_name, requests_remote(service), preferences)
    check_patient_service(criteria, children)
    if key in PEDIATRIC_REQUESTS or (patient in children and key in GENERAL_REQUESTS):
        return Criteria(patient, "", city_id, city_name, preferences=preferences)
    if key in GENERAL_REQUESTS:
        raise ServiceChoiceNeeded(Criteria(patient, "", city_id, city_name, phone_after_patient, preferences))
    if selected:
        return criteria
    if len(service) > 130 or not service.strip():
        raise RequestError("Use a service name from the portal.")
    if choose_mode and not criteria.telephone:
        raise VisitModeChoiceNeeded(criteria)
    return criteria


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
    title = f"Which type of pediatric visit should I search for {patient_name}? Tap a choice or reply with its number."
    options = [f"{index}. {choice[0]}" for index, choice in enumerate(PEDIATRIC_SERVICES, 1)]
    return "\n".join([title, *options])
