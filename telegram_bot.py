"""Private Telegram long polling and read-only LUX MED availability searches."""

import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import requests

from luxmed_sniper import WARSAW, DoctorLocator, Luxmed, LuxMedPortal, PortalError
from telegram_calendar import CalendarAvailability, CalendarUnavailable
from telegram_configuration import PROFILE_KEYS, booking_patients, calendar_settings, trusted_devices, validate, worker_settings
from telegram_free_text import FreeText
from telegram_preferences import PreferenceError, Preferences, matching_slots, parse_preferences, split_preferences
from telegram_requests import PEDIATRIC_PREFIX, PEDIATRIC_SERVICES, Criteria, PatientServiceMismatch, RequestError, city_catalog, normalize, parse_search, pediatric_choice, pediatric_menu
from telegram_state import SearchState, next_search_time

PORTAL_REUSE_SECONDS = 3600
RESULTS_PER_PAGE = 3
SHORT_NOTICE_SECONDS = 6 * 3600
PATIENT_PORTAL_URL = "https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Page/Reservation/Search"
SICK_CHILD_VISIT = normalize(PEDIATRIC_PREFIX + PEDIATRIC_SERVICES[0][2])
SUPPORTED_BOOKING_SERVICES = {"internista", normalize(PEDIATRIC_PREFIX + PEDIATRIC_SERVICES[3][2])}
QUESTIONNAIRE_NOTICE = "LUX MED requires a medical questionnaire for this visit. Complete it in the Patient Portal."
PREPARATION_NOTICE = "LUX MED lists preparation instructions for this visit. Open “Zobacz szczegóły” in the Patient Portal."
BOOKING_ACCESS_NOTICE = "LUX MED denied access to its questionnaire check. Direct booking is paused for this patient. Availability searches can continue."
BOOKING_FAILURES = {
    "slot-gone": "That exact slot is no longer available. Use More or Continue to find another.",
    "booking-details-missing": "I couldn't verify all the details for this slot. Check it in the Patient Portal.",
    "booking-limit": "LUX MED didn't clear its booking limit check. Check the Patient Portal.",
    "booking-eligibility": "LUX MED didn't confirm that this service is covered and available to book online.",
    "portal-questions": QUESTIONNAIRE_NOTICE,
    "questionnaire-response-unrecognized": "I couldn't verify LUX MED's questionnaire response. Check this visit in the Patient Portal.",
    "portal-preparation": PREPARATION_NOTICE,
    "booking-needs-portal": "LUX MED returned a warning, a conflict, a payment or referral choice, or an existing visit. Check the Patient Portal.",
    "booking-rejected": "LUX MED declined the reservation. Check the Patient Portal for details.",
    "booking-hold-unavailable": "LUX MED didn't return a usable slot hold. Check the Patient Portal.",
}


class TelegramError(Exception):
    def __init__(self, *, retry_after: int = 5, stop: bool = False):
        self.retry_after = retry_after
        self.stop = stop


class Telegram:
    def __init__(self, token: str, owner: int):
        self.base = f"https://api.telegram.org/bot{token}"
        self.owner = owner
        self.session = requests.Session()

    def call(self, method: str, data: dict):
        try:
            response = self.session.post(
                f"{self.base}/{method}", json=data, timeout=(5, 20), allow_redirects=False,
            )
            if response.status_code in (401, 403, 409):
                raise TelegramError(stop=True)
            body = response.json()
            if not isinstance(body, dict):
                raise TelegramError()
            if response.status_code == 429:
                parameters = body.get("parameters", {})
                delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
                raise TelegramError(retry_after=delay if type(delay) is int and delay > 0 else 5)
            if response.status_code != 200 or body.get("ok") is not True or "result" not in body:
                raise TelegramError()
            return body["result"]
        except (requests.RequestException, ValueError):
            raise TelegramError() from None

    def poll(self, offset: int) -> list[dict]:
        updates = self.call("getUpdates", {"offset": offset, "timeout": 8, "allowed_updates": ["message"]})
        if not isinstance(updates, list):
            raise TelegramError()
        return updates

    def send(self, message: str) -> int:
        result = self.call("sendMessage", {"chat_id": self.owner, "text": message, "protect_content": True})
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramError()
        return result["message_id"]


@dataclass(frozen=True)
class Profile:
    name: str
    credentials: Luxmed = field(repr=False)
    aliases: tuple[str, ...] = ()
    child: bool = False


def configured_profiles(settings: dict, metadata: dict) -> dict[str, Profile]:
    profiles = {}
    for key in PROFILE_KEYS:
        prefix = f"LUXMED_{key.upper()}_"
        credentials = Luxmed(login=settings[prefix + "LOGIN"], password=settings[prefix + "PASSWORD"])
        info = metadata[key]
        profiles[key] = Profile(info["name"].strip(), credentials, tuple(info.get("aliases", ())), info["child"])
    return profiles


def local_time(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, WARSAW).strftime("%d.%m %H:%M")


class SearchBot:
    def __init__(self, telegram: Telegram, state: SearchState, profiles: dict[str, Profile], *, clock=time.time,
                 free_text: FreeText | None = None, calendar: CalendarAvailability | None = None,
                 booking_enabled: bool = False, device_credentials: dict | None = None,
                 allowed_booking_patients: frozenset[str] | None = None):
        self.telegram = telegram
        self.state = state
        self.profiles = profiles
        self.clock = clock
        self.free_text = free_text
        self.calendar = calendar
        self.booking_enabled = booking_enabled
        self.allowed_booking_patients = (
            frozenset(profiles) if allowed_booking_patients is None else allowed_booking_patients
        )
        self.device_credentials = device_credentials or {}
        self.portals: dict[str, tuple[float, LuxMedPortal]] = {}
        self.cities = city_catalog()
        self.children = {key for key, profile in profiles.items() if profile.child}
        self.aliases = {}
        for key, profile in profiles.items():
            for alias in (key, profile.name, *profile.aliases):
                name = normalize(alias)
                if not name:
                    continue
                if self.aliases.setdefault(name, key) != key:
                    raise ValueError("Duplicate patient alias")

    def accept(self, update: dict) -> None:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            return
        update_id = update["update_id"]
        with self.state.db:
            self.state.db.execute("BEGIN IMMEDIATE")
            if update_id < int(self.state.get_setting("offset", "0")):
                return
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            if (
                chat.get("type") == "private"
                and chat.get("id") == self.telegram.owner
                and sender.get("id") == self.telegram.owner
                and isinstance(message.get("text"), str)
            ):
                self.handle(message)
            self.state.set_setting("offset", str(update_id + 1))

    def handle(self, message: dict) -> None:
        text = message["text"].strip()
        key = normalize(text)
        command, _, argument = key.partition(" ")
        reply = message.get("reply_to_message") or {}
        if command in {"stop", "/stop"}:
            self.stop(argument, reply)
        elif key in {"/active", "active", "show active searches", "show running searches"}:
            self.show_active()
        elif command in {"/change", "change"}:
            self.change(text, reply)
        elif command in {"book", "/book", "details", "/details"} or key in {
            "more", "/more", "continue", "/continue", "find something else",
        }:
            self.handle_results(command, argument, reply)
        elif command in {"approve", "/approve"}:
            if (
                self.booking_enabled and re.fullmatch(r"[1-9][0-9]{0,8}", argument)
                and self.state.approve_booking(int(argument), self.allowed_booking_patients)
            ):
                self.state.queue_message("Approved. I’ll recheck the selected appointment before booking.")
            else:
                self.state.queue_message("Use Approve followed by the booking request number shown in my message.")
        elif command in {"reviewed", "/reviewed"}:
            if re.fullmatch(r"[1-9][0-9]{0,8}", argument) and self.state.acknowledge_unverified_booking(int(argument)):
                self.state.queue_message("Marked as reviewed. You can request a different appointment for this patient.")
            else:
                self.state.queue_message("Use Reviewed followed by the request number after checking the Patient Portal.")
        elif command in {"/resume", "resume"}:
            patient = self.aliases.get(argument)
            booking_paused = patient is not None and self.state.booking_paused(patient)
            if patient and self.state.resume(patient):
                message = "Searches resumed for the next eligible hour."
                if booking_paused:
                    message = "Booking pause cleared. Start a fresh search to request a booking again."
                self.state.queue_message(message)
            else:
                self.state.queue_message("Use /resume followed by a paused patient's name.")
        elif len(text) <= 600 and key not in {"/start", "/help", "help"}:
            self.handle_search(text)
        else:
            self.help()

    def handle_search(self, text: str) -> None:
        error_message = None
        today = dt.datetime.fromtimestamp(self.clock(), WARSAW).date()
        try:
            criteria = parse_search(text, self.aliases, self.children, self.cities, today=today)
        except PatientServiceMismatch as error:
            self.reject_mismatch(error)
            return
        except PreferenceError as error:
            self.state.queue_message(str(error))
            return
        except RequestError as error:
            criteria = None
            error_message = str(error)
        pending = self.state.get_setting("pending")
        if criteria is None and pending and error_message is None:
            fields = json.loads(pending)
            fields.pop("polish", None)  # Pending choices saved by the bilingual worker.
            fields["preferences"] = Preferences(**fields.get("preferences", {}))
            candidate = Criteria(**fields)
            if service := pediatric_choice(text):
                criteria = replace(candidate, service=service, telephone="telefonicz" in normalize(service))
        if criteria is None and self.free_text is not None and not text.startswith("/"):
            try:
                request, filters = split_preferences(text)
                preferences = parse_preferences(filters, today)
                criteria = self.free_text.criteria(request, self.aliases, self.children, self.cities)
                if criteria is not None:
                    criteria = replace(criteria, preferences=preferences)
            except PatientServiceMismatch as error:
                self.reject_mismatch(error)
                return
            except PreferenceError as error:
                self.state.queue_message(str(error))
                return
            except RequestError as error:
                error_message = str(error)
        if criteria and not criteria.service:
            self.state.set_setting("pending", json.dumps(asdict(criteria)))
            self.state.queue_message(pediatric_menu(self.profiles[criteria.patient].name))
        elif criteria:
            self.state.set_setting("pending", "")
            self.start(criteria)
        elif error_message:
            self.state.queue_message(error_message)
        elif pending:
            self.state.queue_message(pediatric_menu(self.profiles[json.loads(pending)["patient"]].name))
        else:
            self.help()

    def reject_mismatch(self, error: PatientServiceMismatch) -> None:
        self.state.set_setting("pending", "")
        name = self.profiles[error.patient].name
        self.state.queue_message(f"{name} {error} Please send a new request with the patient and service you intended.")

    def help(self) -> None:
        booking_available = self.booking_enabled and bool(self.allowed_booking_patients)
        title = "LUX MED appointment assistant" if booking_available else "Read-only appointment search"
        self.state.queue_message(
            f"{title}\n"
            "Find an internist for <patient>\nFind a pediatrician for <patient>\n"
            "Find an internist for <patient> next week after 16:00\n"
            "/find patient | portal service | city | optional filters\n"
            "/active · Stop <number>\n"
            "Change 123 to after 16:00\n/change number | service | city | optional filters\n"
            "From a result list: Details 2 · More · Continue"
            + (" · Book 2\nFor booking approval: Approve <request number> · Stop booking <request number>"
               if booking_available else ""),
        )

    def start(self, criteria: Criteria, *, excluded_slots=(), not_before: int = 0) -> None:
        now = int(self.clock())
        search_id, number, due, paused = self.state.add(
            criteria, now, excluded_slots=excluded_slots, not_before=not_before,
        )
        self.confirm_search(search_id, number, criteria, due, paused, now)

    @staticmethod
    def slot_details(slot: dict) -> str:
        starts = dt.datetime.fromtimestamp(slot["start"], WARSAW).strftime("%d.%m %H:%M")
        return " · ".join(value for value in (starts, slot["clinic"], slot["doctor"]) if value)

    @staticmethod
    def slot_key(slot: dict) -> tuple[int, str, str]:
        return slot["start"], slot["clinic"], slot["doctor"]

    @staticmethod
    def portal_notices(service: str, slots: list[dict]) -> list[str]:
        notices = []
        if normalize(service) == SICK_CHILD_VISIT:
            notices.append(QUESTIONNAIRE_NOTICE)
        if any(slot.get("requires_preparation") is True for slot in slots):
            notices.append(PREPARATION_NOTICE)
        return notices

    def show_results(self, search_id: int) -> None:
        row = self.state.results(search_id)
        slots = json.loads(row["slots"])
        now = int(self.clock())
        page = []
        for index in range(row["shown"], len(slots)):
            if slots[index]["start"] > now:
                page.append((index + 1, slots[index]))
                if len(page) == RESULTS_PER_PAGE:
                    break
        if not page:
            self.state.queue_message("No more upcoming options in this list. Use Continue to look for other slots.", search_id)
            return
        self.state.mark_shown(search_id, page[-1][0])
        self.state.set_setting("latest_result", str(search_id))
        heading = self.search_heading(row["display_number"], self.state.criteria(row))
        notices = self.portal_notices(row["service"], [slot for _, slot in page])
        lines = [heading, *notices, "Available slots (Warsaw time)"]
        lines.extend(f"{number}. {self.slot_details(slot)}" for number, slot in page)
        if any(slot["start"] > now for slot in slots[page[-1][0]:]):
            lines.append("Reply More to see the next options.")
        can_book = (
            self.can_book_for(row["patient"]) and not notices and not row["telephone"]
            and normalize(row["service"]) in SUPPORTED_BOOKING_SERVICES
            and not self.state.booking_paused(row["patient"])
        )
        action = "Book <option number> to request that slot" if can_book else "Details <option number> for the Patient Portal link"
        lines.append(f"Reply {action}, or Continue to search for different slots.")
        self.state.queue_message("\n".join(lines), search_id)

    def handle_results(self, command: str, argument: str, reply: dict) -> None:
        action = command.lstrip("/")
        if action in {"book", "details"} and not re.fullmatch(r"[1-9][0-9]{0,8}", argument):
            label = action.capitalize()
            self.state.queue_message(f"Use {label} followed by an option number, for example {label} 2.")
            return
        original = self.replied_search(reply)
        row = self.state.results(original["id"]) if original else None
        if not reply:
            row = self.state.results()
        if row is None:
            self.state.queue_message("I don’t have a numbered list for that search. Start a search to get a fresh list.")
            return
        search_id = row["id"]
        slots = json.loads(row["slots"])
        now = int(self.clock())
        if action in {"book", "details"}:
            number = int(argument)
            if number > row["shown"]:
                self.state.queue_message("Choose an option number shown in this list, or use More.", search_id)
                return
            slot = slots[number - 1]
            if slot["start"] <= now:
                self.state.queue_message("That appointment time has passed. Use More or Continue for other options.", search_id)
                return
            if action == "details":
                heading = self.search_heading(row["display_number"], self.state.criteria(row))
                notices = self.portal_notices(row["service"], [slot])
                if slot["start"] - now < SHORT_NOTICE_SECONDS:
                    notices.append("This appointment starts within six hours. Check the cancellation terms before confirming.")
                lines = [heading, f"Option {number}. {self.slot_details(slot)}", *notices,
                         "Confirm availability in the Patient Portal.", PATIENT_PORTAL_URL]
                self.state.queue_message("\n".join(lines), search_id)
            else:
                self.request_booking(row, number, slot, now)
        elif action == "more":
            self.show_results(search_id)
        elif row["status"] == "continued":
            self.state.queue_message("This list already has a follow-up search. Use /active to see what’s running.", search_id)
        else:
            criteria = self.state.criteria(row)
            today = dt.datetime.fromtimestamp(now, WARSAW).date().isoformat()
            if criteria.preferences.last_date and criteria.preferences.last_date < today:
                self.state.queue_message("The original date window has ended. Start a search with new dates.", search_id)
                return
            exclusions = json.loads(row["excluded_slots"]) + slots[:row["shown"]]
            self.state.mark_continued(search_id)
            self.start(criteria, excluded_slots=[slot for slot in exclusions if slot["start"] > now], not_before=row["next_due"])

    def can_book_for(self, patient: str) -> bool:
        return self.booking_enabled and patient in self.allowed_booking_patients

    def request_booking(self, row, number: int, slot: dict, now: int) -> None:
        search_id = row["id"]
        heading = self.search_heading(row["display_number"], self.state.criteria(row))
        description = f"{heading}\nOption {number}. {self.slot_details(slot)}"
        notices = self.portal_notices(row["service"], [slot])
        if notices:
            self.state.queue_message("\n".join([description, *notices, "Book this visit in the Patient Portal.", PATIENT_PORTAL_URL]), search_id)
            return
        if not self.can_book_for(row["patient"]):
            self.state.queue_message(
                f"{description}\nDirect booking is unavailable. Confirm the appointment in the Patient Portal.\n{PATIENT_PORTAL_URL}",
                search_id,
            )
            return
        if self.state.booking_paused(row["patient"]):
            self.state.queue_message(f"{description}\n{BOOKING_ACCESS_NOTICE}\n{PATIENT_PORTAL_URL}", search_id)
            return
        if row["telephone"] or normalize(row["service"]) not in SUPPORTED_BOOKING_SERVICES:
            self.state.queue_message(f"This visit type needs to be booked in the Patient Portal.\n{PATIENT_PORTAL_URL}", search_id)
            return
        if any(type(slot.get(key)) is not int for key in ("clinic_id", "doctor_id", "service_id")):
            self.state.queue_message("This list lacks the exact slot ID needed to book. Start a fresh search.", search_id)
            return
        if self.state.unresolved_booking(row["patient"]):
            self.state.queue_message(f"A prior booking for this patient is unverified. Check the Patient Portal before proceeding.\n{PATIENT_PORTAL_URL}", search_id)
            return
        slot_key = json.dumps([slot[key] for key in ("start", "clinic_id", "doctor_id", "service_id")])
        booking = self.state.request_booking(search_id, number, row["patient"], slot_key)
        status = booking["status"]
        if status == "booked":
            self.state.queue_message(f"{description}\nAlready booked in LUX MED.", search_id)
        elif status == "approval":
            self.send_booking_approval(booking, description, booking["reason"])
        elif status == "queued":
            due = next_search_time(now)
            timing = "Checking the exact slot and booking conditions." if due == now else f"Queued for {local_time(due)} Warsaw time."
            self.state.queue_message(f"{description}\nBooking request {booking['id']}. {timing}", search_id)
        elif status in {"checking", "submitting", "verifying"}:
            self.state.queue_message(f"{description}\nThis booking is already being processed.", search_id)
        else:
            self.state.queue_message(f"{description}\nThis booking request has ended. Use More or Continue for another slot.", search_id)

    def send_booking_approval(self, booking, description: str, reason: str) -> None:
        self.state.queue_message(
            f"{description}\n{reason}\nReply Approve {booking['id']} to proceed or Stop booking {booking['id']} to drop this request.",
            booking["search_id"],
        )

    def confirm_search(self, search_id: int, number: int, criteria: Criteria, due: int, paused: bool, now: int, *, changed: bool = False) -> None:
        if paused:
            status = "Paused. Check the portal, then use /resume."
        elif due > now:
            status = f"Queued for {local_time(due)} Warsaw time."
        else:
            status = "Checking availability. I’ll send the first result when the check finishes."
        prefix = "Updated\n" if changed else ""
        self.state.queue_message(f"{prefix}{self.search_heading(number, criteria)}\n{status}", search_id)

    def search_heading(self, number: int, criteria: Criteria) -> str:
        heading = f"#{number} · {self.profiles[criteria.patient].name} · {criteria.service} · {criteria.city}"
        if filters := criteria.preferences.describe():
            heading += f"\n{filters}"
        return heading

    def change(self, text: str, reply: dict) -> None:
        parts = [part.strip() for part in text.split("|")]
        match = re.fullmatch(r"/?change\s+#?([0-9]{1,18})(?:\s+to\s+(.+))?", parts[0], re.IGNORECASE)
        if len(text) > 600 or match is None or len(parts) > 4 or (len(parts) == 1) != bool(match[2]):
            self.state.queue_message("Try Change 123 to after 16:00 or /change 123 | service | city | filters.")
            return
        number = int(match[1])
        row = self.state.active_by_number(number)
        if row is None:
            self.state.queue_message(f"Search #{number} is inactive.")
            return
        original = self.replied_search(reply)
        if original is not None and original["id"] != row["id"]:
            self.state.queue_message("That message refers to a different search. Use /active to see current searches.")
            return
        search_id = row["id"]
        current = self.state.criteria(row)
        filters = match[2] or (parts[3] if len(parts) == 4 else "")
        now = int(self.clock())
        today = dt.datetime.fromtimestamp(now, WARSAW).date()
        try:
            if len(parts) == 1:
                criteria = current
            else:
                service = parts[1] or current.service
                city = parts[2] if len(parts) >= 3 and parts[2] else current.city
                criteria = parse_search(f"/find {current.patient} | {service} | {city}", self.aliases, self.children, self.cities)
            criteria = replace(criteria, preferences=parse_preferences(filters, today, current.preferences))
        except PatientServiceMismatch as error:
            self.reject_mismatch(error)
            return
        except (RequestError, PreferenceError) as error:
            self.state.queue_message(str(error))
            return
        if not criteria.service:
            self.state.queue_message("Choose a specific pediatric visit type in /change, such as routine visit.")
            return
        if criteria == current:
            self.state.queue_message(f"Search #{number} already uses those settings.")
            return
        due, paused = self.state.revise(search_id, criteria, now)
        self.confirm_search(search_id, number, criteria, due, paused, now, changed=True)

    def show_active(self) -> None:
        rows = self.state.active()
        if not rows:
            self.state.queue_message("No active searches.")
            return
        for index in range(0, len(rows), 6):
            lines = []
            for row in rows[index:index + 6]:
                heading = self.search_heading(row["display_number"], self.state.criteria(row))
                if row["last_checked"] is None:
                    last = "Awaiting a recorded result"
                else:
                    result = {
                        "no-slots": "no matching slots",
                        "outside-preferred-clinic": "matches at other clinics only",
                        "ambiguous-doctor": "doctor name needs clarification",
                        "paused": "paused",
                        "temporary-error": "temporary error",
                    }.get(row["last_result"], "checked")
                    last = f"Last check {local_time(row['last_checked'])}: {result}"
                next_at = local_time(row["next_check"])
                upcoming = f"Next check {next_at} Warsaw time"
                if row["suspended"]:
                    upcoming = "Paused. Use /resume after checking the portal."
                lines.append(f"{heading}\n{last}\n{upcoming}")
            self.state.queue_message("\n\n".join(lines))

    def replied_search(self, reply: dict):
        message_id = reply.get("message_id")
        return self.state.search_for_message(message_id) if type(message_id) is int else None

    def stop(self, argument: str, reply: dict) -> None:
        if match := re.fullmatch(r"booking ([1-9][0-9]{0,8})", argument):
            stopped = self.state.stop_booking(int(match[1]))
            self.state.queue_message("Booking request stopped." if stopped else "There’s no pending booking with that number.")
            return
        row = self.replied_search(reply)
        number = None
        if row is not None:
            number = row["display_number"]
            if argument and argument not in {str(number), f"#{number}"}:
                self.state.queue_message("That message refers to a different search. Use /active to see current searches.")
                return
        elif argument:
            if re.fullmatch(r"#?[0-9]{1,18}", argument):
                number = int(argument.lstrip("#"))
                row = self.state.active_by_number(number)
        elif not reply and self.state.get_setting("pending"):
            self.state.set_setting("pending", "")
            self.state.queue_message("Pending selection cleared.")
            return
        elif not reply:
            rows = self.state.active()
            if len(rows) == 1:
                row = rows[0]
                number = row["display_number"]
        if number is None:
            self.state.queue_message("Use Stop with a search number from /active.")
        elif row is not None and self.state.finish(row["id"], "stopped"):
            self.state.queue_message(f"Stopped search #{number}.")
        else:
            self.state.queue_message(f"That search (#{number}) has already ended.")

    def flush(self) -> None:
        while (message := self.state.next_message()) is not None:
            sent = self.telegram.send(message["text"])
            self.state.sent(message, sent)

    def portal_for(self, patient: str) -> LuxMedPortal:
        now = self.clock()
        cached = self.portals.get(patient)
        if cached is not None and now < cached[0]:
            return cached[1]
        self.close_portal(patient)
        credentials = self.profiles[patient].credentials
        trusted_device = self.device_credentials.get(patient)
        if trusted_device is not None and trusted_device["expires"] > now:
            portal = LuxMedPortal(credentials, trusted_device=trusted_device)
        else:
            portal = LuxMedPortal(credentials)
        reuse_until = now + PORTAL_REUSE_SECONDS
        if type(portal.token_expires_at) is int:
            reuse_until = min(reuse_until, portal.token_expires_at)
            if reuse_until <= self.clock():
                portal.session.close()
                raise PortalError("session-expired", suspend=True)
        self.portals[patient] = (reuse_until, portal)
        return portal

    def close_portal(self, patient: str) -> None:
        cached = self.portals.pop(patient, None)
        if cached is not None:
            cached[1].session.close()

    def close_portals(self) -> None:
        for patient in tuple(self.portals):
            self.close_portal(patient)

    def booking_due(self) -> None:
        now = int(self.clock())
        if not self.booking_enabled or not self.allowed_booking_patients or next_search_time(now) != now:
            return
        booking = self.state.claim_booking(self.allowed_booking_patients)
        if booking is None:
            return
        row = self.state.results(booking["search_id"])
        slot = json.loads(row["slots"])[booking["option_number"] - 1]
        criteria = self.state.criteria(row)
        heading = self.search_heading(row["display_number"], criteria)
        description = f"{heading}\nOption {booking['option_number']}. {self.slot_details(slot)}"
        reservation_id = None
        try:
            if slot["start"] <= now:
                raise PortalError("slot-gone")
            if normalize(criteria.service) == SICK_CHILD_VISIT:
                raise PortalError("portal-questions")
            if slot.get("requires_preparation") is True:
                raise PortalError("portal-preparation")
            if self.state.unresolved_booking(criteria.patient):
                raise PortalError("prior-booking-unverified")
            if self.state.account_paused(criteria.patient):
                raise PortalError("booking-account-paused")
            if self.state.booking_paused(criteria.patient):
                raise PortalError("booking-access-paused")
            portal = self.portal_for(criteria.patient)
            if portal.is_booking_blocked_by_penalty(criteria.city_id, slot["service_id"]):
                raise PortalError("booking-penalty")
            portal.check_booking_eligibility(criteria.city_id, slot["service_id"])
            locator = DoctorLocator(id=f"{criteria.city_id}*{slot['service_id']}*-1*{slot['doctor_id']}")
            appointment = self.refresh_booking_slot(portal, locator, slot)
            start = appointment.AppointmentDate.astimezone(dt.UTC)
            end = appointment.booking_term["end"].astimezone(dt.UTC)
            reasons = self.booking_approval_reasons(portal, start, end)
            if slot["start"] <= int(self.clock()):
                raise PortalError("slot-gone")
            checked_at = int(self.clock())
            if next_search_time(checked_at) != checked_at:
                with self.state.db:
                    self.state.booking_status(booking["id"], "queued", reason=booking["reason"])
                return
            if reasons and (not booking["approved"] or not set(reasons) <= set((booking["reason"] or "").splitlines())):
                with self.state.db:
                    reason = "\n".join(reasons)
                    self.state.booking_status(booking["id"], "approval", reason=reason)
                    self.send_booking_approval(booking, description, reason)
                return
            with self.state.db:
                self.state.booking_status(booking["id"], "submitting")
            reservation_id = portal.reserve_standard_visit(appointment, criteria.service)
            with self.state.db:
                self.state.booking_status(booking["id"], "verifying", reservation_id=reservation_id)
            if reservation_id not in portal.booked_visit_ids():
                raise PortalError("booking-outcome-unknown")
        except PortalError as error:
            self.report_booking_failure(booking, description, error, reservation_id)
            return
        with self.state.db:
            self.state.booking_status(booking["id"], "booked", reservation_id=reservation_id)
            self.state.queue_message(f"{description}\nBooked and verified in LUX MED.", row["id"])

    @staticmethod
    def refresh_booking_slot(portal, locator: DoctorLocator, slot: dict):
        date = dt.datetime.fromtimestamp(slot["start"], WARSAW).date()
        matches = [
            item for item in portal.search(locator, date=date)
            if int(item.AppointmentDate.timestamp()) == slot["start"]
            and item.clinic_id == slot["clinic_id"] and item.doctor_id == slot["doctor_id"]
            and item.service_id == slot["service_id"]
        ]
        if len(matches) != 1:
            raise PortalError("slot-gone")
        appointment = matches[0]
        if appointment.requires_preparation:
            raise PortalError("portal-preparation")
        if appointment.booking_term is None or str(appointment.booking_term["cityId"]) != locator.city_id:
            raise PortalError("booking-details-missing")
        return appointment

    def booking_approval_reasons(self, portal, start: dt.datetime, end: dt.datetime) -> list[str]:
        if not start < end:
            raise PortalError("booking-details-missing")
        reasons = []
        try:
            if portal.has_booking_conflict(start, end):
                reasons.append("This overlaps with another LUX MED appointment.")
        except PortalError as error:
            if error.suspend:
                raise
            reasons.append("I couldn’t verify other LUX MED appointments. Check for conflicts before approving.")
        try:
            if self.calendar is None:
                raise CalendarUnavailable
            if self.calendar.busy(start, end):
                reasons.append("Your Google Calendar shows a conflict.")
        except CalendarUnavailable:
            reasons.append("I couldn’t check your Google Calendar. Check that the time works before approving.")
        if start.timestamp() - self.clock() < SHORT_NOTICE_SECONDS:
            reasons.append("This appointment starts within six hours.")
        return reasons

    def report_booking_failure(self, booking, description: str, error: PortalError, reservation_id: int | None) -> None:
        code = str(error)
        unknown = reservation_id is not None or code == "booking-outcome-unknown"
        booking_access_denied = error.stage == "questionnaire" and error.http_status == 401
        with self.state.db:
            if booking_access_denied and not unknown:
                self.close_portal(booking["patient"])
                self.state.pause_booking(booking["patient"])
            elif error.suspend:
                self.close_portal(booking["patient"])
                self.state.suspend(booking["patient"])
            self.state.booking_status(booking["id"], "unknown" if unknown else "ended", reservation_id=reservation_id)
            if unknown:
                message = (
                    "LUX MED’s booking outcome is unclear. Check your appointments in the Patient Portal. "
                    f"Once you've checked, reply Reviewed {booking['id']} to allow requests for other slots."
                )
            elif code == "booking-penalty":
                message = "LUX MED reported a no-show restriction. Check the Patient Portal before booking."
            elif code == "prior-booking-unverified":
                message = "Check the earlier unverified booking in the Patient Portal first."
            elif booking_access_denied or code == "booking-access-paused":
                message = BOOKING_ACCESS_NOTICE
            elif error.suspend or code == "booking-account-paused":
                message = "LUX MED access is paused. Check the Patient Portal and complete any verification there."
            else:
                message = BOOKING_FAILURES.get(code, "The booking check failed. Check the Patient Portal.")
            self.state.queue_message(f"{description}\n{message}\n{PATIENT_PORTAL_URL}", booking["search_id"])

    def recover_bookings(self) -> None:
        with self.state.db:
            for booking in self.state.booking_recovery():
                if booking["status"] == "checking":
                    self.state.booking_status(booking["id"], "queued", reason=booking["reason"])
                else:
                    self.report_booking_failure(
                        booking, f"Booking request {booking['id']}", PortalError("booking-outcome-unknown"),
                        booking["reservation_id"],
                    )

    def search_due(self) -> None:
        today = dt.datetime.fromtimestamp(self.clock(), WARSAW).date().isoformat()
        with self.state.db:
            for row in self.state.expired(today):
                if self.state.finish(row["id"], "date-window-ended"):
                    heading = self.search_heading(row["display_number"], self.state.criteria(row))
                    self.state.queue_message(f"{heading}\nThe requested date window has ended. Search finished.", row["id"])
        batch = self.state.claim_batch(int(self.clock()))
        if not batch:
            return
        first = batch[0]
        patient = first["patient"]
        profile = self.profiles[patient]
        index = 0
        try:
            portal = self.portal_for(patient)
            catalog = portal.get_services()
            for index, row in enumerate(batch):
                now = int(self.clock())
                if next_search_time(now) != now:
                    break
                self.search_one(portal, catalog, row)
        except PortalError as error:
            with self.state.db:
                result = "paused" if error.suspend else "temporary-error"
                for row in batch[index:]:
                    self.state.record_result(row["id"], int(self.clock()), result)
                if error.suspend:
                    self.close_portal(patient)
                    self.state.suspend(patient)
                    status = "Searches paused. Check the official portal before using /resume."
                else:
                    status = "LUX MED check failed. The next attempt will be in an eligible hour."
                self.state.queue_message(f"{profile.name} · {status}")

    def search_one(self, portal: LuxMedPortal, catalog: list, row) -> None:
        search_id = row["id"]
        criteria = self.state.criteria(row)
        heading = self.search_heading(row["display_number"], criteria)
        try:
            service_id = portal.resolve_service(row["service"], catalog, telephone=bool(row["telephone"]))
        except PortalError as error:
            if str(error) not in {"service-not-in-catalog", "ambiguous-service"}:
                raise
            with self.state.db:
                if self.state.finish(search_id, "service-unresolved"):
                    if str(error) == "service-not-in-catalog":
                        status = "I couldn't find that service in this patient's LUX MED catalog. Search finished."
                    else:
                        status = "More than one service matched. Try its exact portal label. Search finished."
                    self.state.queue_message(f"{heading}\n{status}", search_id)
            return

        locator = DoctorLocator(id=f"{row['city_id']}*{service_id}*-1*-1")
        try:
            slots, outside_clinic = matching_slots(portal.search(locator), criteria.preferences)
        except PreferenceError as error:
            with self.state.db:
                self.state.record_result(search_id, int(self.clock()), "ambiguous-doctor")
                if self.state.mark_first_result(search_id):
                    self.state.queue_message(f"{heading}\n{error}", search_id)
            return
        excluded = {self.slot_key(slot) for slot in json.loads(row["excluded_slots"])}
        options = []
        for slot in slots:
            option = {
                "start": int(slot.AppointmentDate.timestamp()),
                "clinic": str(slot.ClinicPublicName or "")[:160].strip(),
                "doctor": str(slot.DoctorName or "")[:160].strip(),
                "clinic_id": slot.clinic_id, "doctor_id": slot.doctor_id, "service_id": int(service_id),
            }
            if slot.requires_preparation:
                option["requires_preparation"] = True
            key = self.slot_key(option)
            if key not in excluded:
                excluded.add(key)
                options.append(option)
        if not options:
            with self.state.db:
                result = "outside-preferred-clinic" if outside_clinic else "no-slots"
                next_check = self.state.record_result(search_id, int(self.clock()), result)
                if next_check is not None and self.state.mark_first_result(search_id):
                    due = local_time(next_check)
                    window = "the requested dates" if criteria.preferences.first_date else "the next 14 days"
                    kind = "new matching" if row["excluded_slots"] != "[]" else "matching"
                    status = f"No {kind} slots in {window}."
                    if outside_clinic:
                        status += " Other clinics have matches. Use /change to adjust the clinic filter."
                    status += f" I’ll check hourly from 06:00 to 22:00 Warsaw time and message you if I find one. Next check: {due}."
                    self.state.queue_message(f"{heading}\n{status}", search_id)
            return
        with self.state.db:
            if not self.state.finish(search_id, "found"):
                return
            self.state.save_results(search_id, options)
            self.show_results(search_id)


def main() -> int:
    os.umask(0o077)
    try:
        settings = worker_settings()
        if "LUXMED_DIRECT_BOOKING_PATIENTS" in os.environ:
            settings["LUXMED_DIRECT_BOOKING_PATIENTS"] = os.environ["LUXMED_DIRECT_BOOKING_PATIENTS"]
        profiles = configured_profiles(settings, validate(settings))
        allowed_booking_patients = booking_patients(settings)
        token = settings["TELEGRAM_BOT_TOKEN"]
        owner = int(settings["TELEGRAM_OWNER_ID"])
        path = Path(os.environ.get("LUXMED_STATE_DIRECTORY", "./data"))
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = settings.get("OPENAI_API_KEY", "").strip()
        interpreter = FreeText(key) if key else None
        calendar_config = calendar_settings(settings)
        calendar = CalendarAvailability(calendar_config) if calendar_config else None
        bot = SearchBot(Telegram(token, owner), SearchState(path / "telegram.sqlite3"), profiles,
                        free_text=interpreter, calendar=calendar,
                        booking_enabled=bool(allowed_booking_patients),
                        device_credentials=trusted_devices(profiles),
                        allowed_booking_patients=allowed_booking_patients)
    except (KeyError, ValueError, OSError):
        print("Telegram bot configuration is incomplete.", file=sys.stderr)
        return 2

    try:
        return run_worker(bot)
    finally:
        bot.close_portals()


def run_worker(bot: SearchBot) -> int:
    bot.recover_bookings()
    while True:
        try:
            updates = bot.telegram.poll(int(bot.state.get_setting("offset", "0")))
            for update in updates:
                bot.accept(update)
            bot.flush()
            bot.booking_due()
            bot.flush()
            bot.search_due()
            bot.flush()
        except TelegramError as error:
            if error.stop:
                print("Telegram authorization or polling conflict. Check the bot configuration.", file=sys.stderr)
                return 2
            print("Telegram request failed. Retrying after a pause.", file=sys.stderr)
            time.sleep(max(5, error.retry_after))
        except KeyboardInterrupt:
            return 0
        except Exception:
            print("Telegram worker stopped unexpectedly.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
