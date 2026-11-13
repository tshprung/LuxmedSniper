"""Parse and apply date, time, doctor, and clinic filters for Telegram searches."""

import datetime as dt
import re
from dataclasses import dataclass, replace

from luxmed_sniper import WARSAW, Appointment


TIME = r"[0-9]{1,2}(?::[0-9]{2})?(?:\s*(?:am|pm))?"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DATE = r"(?:today|tomorrow|this week|next week|(?:on\s+)?(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|" + "|".join(WEEKDAYS) + "))"
FILTER = re.compile(
    rf"\s*(?:,\s*)?(?:(?P<date>{DATE})|"
    rf"(?P<between>between\s+{TIME}\s+and\s+{TIME})|"
    rf"(?P<time>(?:at|around|after|before)\s+{TIME})|"
    r"(?P<doctor>with\s+(?:dr\.?\s+|doctor\s+)?[^,]+?)|"
    r"(?P<clinic>(?:clinic|near|preferably\s+at|only\s+at|at)\s+[^,]+?))"
    rf"(?=\s*(?:,|$)|\s+(?:{DATE}|between\b|after\b|before\b|around\b|at\b|with\b|clinic\b|near\b|preferably\b|only\b))",
    re.IGNORECASE,
)
FILTER_START = re.compile(
    rf"(?:,\s*|\s+)(?={DATE}(?:\s|,|$)|(?:between|after|before|around|at|with|clinic|near|preferably|only)\s)",
    re.IGNORECASE,
)


class PreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class Preferences:
    first_date: str | None = None
    last_date: str | None = None
    time_rule: str | None = None
    start_minute: int | None = None
    end_minute: int | None = None
    doctor: str | None = None
    clinic: str | None = None
    preferred_clinic: bool = False

    def describe(self) -> str:
        parts = []
        if self.first_date:
            parts.append(self.first_date if self.first_date == self.last_date else f"{self.first_date} to {self.last_date}")
        if self.time_rule == "between":
            parts.append(f"between {clock_label(self.start_minute)} and {clock_label(self.end_minute)}")
        elif self.time_rule:
            mode = "around" if self.time_rule == "at" else self.time_rule
            parts.append(f"{mode} {clock_label(self.start_minute)}" + (" (±1 hour)" if mode == "around" else ""))
        if self.doctor:
            parts.append(f"doctor {self.doctor}")
        if self.clinic:
            parts.append(f"{'preferred clinic' if self.preferred_clinic else 'clinic'} {self.clinic}")
        return " · ".join(parts)


def clock_label(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clock_minutes(value: str) -> int:
    text = value.strip().casefold()
    meridiem = text[-2:] if text.endswith(("am", "pm")) else ""
    if meridiem:
        text = text[:-2].strip()
    hours, _, minutes = text.partition(":")
    hour, minute = int(hours), int(minutes or "0")
    if not 0 <= minute <= 59 or (meridiem and not 1 <= hour <= 12) or (not meridiem and not 0 <= hour <= 23):
        raise PreferenceError("Use a valid appointment time, such as 16:00.")
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return hour * 60 + minute


def date_range(value: str, today: dt.date) -> tuple[str, str]:
    day = " ".join(value.casefold().split()).removeprefix("on ")
    if day in ("this week", "next week"):
        monday = today - dt.timedelta(days=today.weekday())
        if day == "next week":
            monday += dt.timedelta(days=7)
        return max(today, monday).isoformat(), (monday + dt.timedelta(days=6)).isoformat()
    if day == "today":
        date = today
    elif day == "tomorrow":
        date = today + dt.timedelta(days=1)
    elif day in WEEKDAYS:
        date = today + dt.timedelta(days=(WEEKDAYS.index(day) - today.weekday()) % 7)
    else:
        try:
            date = dt.date.fromisoformat(day)
        except ValueError:
            raise PreferenceError("Use a valid date in YYYY-MM-DD format.") from None
    if date < today:
        raise PreferenceError("That appointment date has passed.")
    if date > today + dt.timedelta(days=14):
        raise PreferenceError("Choose a date within the next 14 days.")
    return date.isoformat(), date.isoformat()


def split_preferences(text: str) -> tuple[str, str]:
    boundary = FILTER_START.search(text)
    if boundary is None:
        return text, ""
    return text[:boundary.start()].strip(), text[boundary.end():].strip().rstrip("?.")


def parse_preferences(text: str, today: dt.date, base: Preferences = Preferences()) -> Preferences:
    if text.casefold().strip() == "clear filters":
        return Preferences()
    seen = set()
    while text.strip():
        match = FILTER.match(text)
        if match is None:
            raise PreferenceError("I couldn't read those filters. Try next week, after 16:00, with Dr Smith, or clinic followed by its name.")
        kind = match.lastgroup
        field = "time" if kind == "between" else kind
        if field in seen:
            raise PreferenceError("Use one date range, time window, doctor, and clinic per search.")
        seen.add(field)
        value = match[kind].strip()
        if kind == "date":
            first, last = date_range(value, today)
            base = replace(base, first_date=first, last_date=last)
        elif kind == "between":
            begin, end = re.split(r"\s+and\s+", value[8:], flags=re.IGNORECASE)
            lower, upper = clock_minutes(begin), clock_minutes(end)
            if lower >= upper:
                raise PreferenceError("The end of the time window must be later than its start.")
            base = replace(base, time_rule="between", start_minute=lower, end_minute=upper)
        elif kind == "time":
            rule, time = value.split(maxsplit=1)
            rule = "at" if rule.casefold() == "around" else rule.casefold()
            minute = clock_minutes(time)
            if (rule == "before" and minute == 0) or (rule == "after" and minute == 1439):
                raise PreferenceError("Choose a time window with at least one matching time of day.")
            base = replace(base, time_rule=rule, start_minute=minute, end_minute=None)
        else:
            prefix = r"with\s+(?:(?:dr\.?|doctor)\s+)?" if kind == "doctor" else r"(?:clinic|near|preferably\s+at|only\s+at|at)\s+"
            name = re.sub("^" + prefix, "", value, flags=re.IGNORECASE).strip()
            if not 2 <= len(name) <= 80 or not any(char.isalpha() for char in name):
                raise PreferenceError(f"Use a {kind} name as shown in the portal.")
            if kind == "doctor":
                base = replace(base, doctor=name)
            else:
                preferred = value.casefold().startswith(("preferably ", "near "))
                base = replace(base, clinic=name, preferred_clinic=preferred)
        text = text[match.end():]
    return base


def name_words(value: str) -> tuple[str, ...]:
    from telegram_requests import normalize

    return tuple(re.findall(r"[a-z0-9]+", normalize(value)))


def contains_words(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return bool(needle) and any(
        haystack[index:index + len(needle)] == needle for index in range(len(haystack) - len(needle) + 1)
    )


def matching_slots(slots: list[Appointment], preferences: Preferences) -> tuple[list[Appointment], bool]:
    eligible = []
    outside_clinic = False
    doctor_names = set()
    requested_doctor = name_words(preferences.doctor) if preferences.doctor else ()
    clinic = name_words(preferences.clinic) if preferences.clinic else ()
    clinic_labels = (clinic,)
    if clinic in (("dominikanska",), ("dominikanski",)):
        clinic_labels = (("dominikanska",), ("dominikanski",))
    for slot in slots:
        local = slot.AppointmentDate.astimezone(WARSAW)
        day = local.date().isoformat()
        if preferences.first_date and not preferences.first_date <= day <= preferences.last_date:
            continue
        minute = local.hour * 60 + local.minute
        start = preferences.start_minute
        rule = preferences.time_rule
        if (
            (rule == "at" and abs(minute - start) > 60)
            or (rule == "after" and minute <= start)
            or (rule == "before" and minute >= start)
            or (rule == "between" and not start <= minute <= preferences.end_minute)
        ):
            continue
        if requested_doctor:
            doctor = name_words(slot.doctor_search_name or slot.DoctorName)
            if not contains_words(doctor, requested_doctor):
                continue
            doctor_names.add(doctor)
        if preferences.clinic:
            clinic_name = name_words(slot.ClinicPublicName)
            if not any(contains_words(clinic_name, label) for label in clinic_labels):
                outside_clinic = outside_clinic or preferences.preferred_clinic
                continue
        eligible.append(slot)
    if len(doctor_names) > 1:
        raise PreferenceError("More than one doctor matches that name. Use the full name with /change.")
    def rank(slot: Appointment) -> tuple:
        local = slot.AppointmentDate.astimezone(WARSAW)
        distance = abs(local.hour * 60 + local.minute - preferences.start_minute) if preferences.time_rule == "at" else 0
        return local.date(), distance, slot.AppointmentDate.astimezone(dt.UTC)

    return sorted(eligible, key=rank), outside_clinic
