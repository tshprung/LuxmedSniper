"""Private Telegram long polling and read-only LUX MED availability searches."""

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import requests

from luxmed_prices import PublicPriceList
from luxmed_questionnaire import Questionnaire, QuestionnaireError, QuestionnaireReceipt, visible_text
from luxmed_sniper import (
    WARSAW, BookingQuestionnaireRequired, CancellationTerms, DoctorLocator, Luxmed, LuxMedPortal,
    PackageCoverage, PortalError, ScheduledVisit, service_variants,
)
from telegram_calendar import CalendarAvailability, CalendarUnavailable
from telegram_configuration import PROFILE_KEYS, booking_patients, calendar_settings, trusted_devices, validate, worker_settings
from telegram_free_text import FreeText
from telegram_preferences import PreferenceError, Preferences, UnrecognizedPreferences, matching_slots, parse_preferences
from telegram_requests import (
    ALLERGY_SERVICE, GENERAL_SERVICES, INTERNIST_PHONE_SERVICE, PEDIATRIC_PREFIX, PEDIATRIC_SERVICES, Criteria,
    PatientChoiceNeeded, PatientServiceMismatch, RequestError, ServiceChoiceNeeded, VisitModeChoiceNeeded,
    appointments_request, check_patient_service, city_catalog, coverage_request,
    matching_catalog_variants, matching_package_services, nearby_catalog_variants, normalize, parse_search,
    pediatric_choice, pediatric_menu,
    phone_service, requests_phone, requests_remote, split_clinician_first_search,
)
from telegram_state import SearchState, next_search_time

PORTAL_REUSE_SECONDS = 3600
RESULTS_PER_PAGE = 3
SHORT_NOTICE_SECONDS = 6 * 3600
APPROVAL_CUTOFF_SECONDS = 3600
CALENDAR_RETRY_SECONDS = 15 * 60
PATIENT_PORTAL_URL = "https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Page/Reservation/Search"
APPOINTMENTS_PORTAL_URL = "https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Page/Timeline"
COVERAGE_HELP = "Try “Is a cardiologist visit covered for me?” or /coverage patient | exact service name."
SICK_CHILD_SERVICES = {
    normalize(PEDIATRIC_PREFIX + PEDIATRIC_SERVICES[index][2]) for index in (0, 1, 2)
}
ALLERGY_SERVICES = {"alergolog", normalize(ALLERGY_SERVICE)}
QUESTIONNAIRE_PROMPT = "LUX MED questionnaire"
QUESTIONNAIRE_APPROVAL = "Please confirm this allergy appointment."
INTERNIST_QUESTIONNAIRE_APPROVAL = "Please confirm this internist appointment."
QUESTIONNAIRE_EXPIRED = "This questionnaire session has ended. Start a fresh search to try again."
SUPPORTED_BOOKING_SERVICES = {"internista", normalize(PEDIATRIC_PREFIX + PEDIATRIC_SERVICES[3][2])}
QUESTIONNAIRE_NOTICE = "LUX MED requires a medical questionnaire for this visit. Complete it in the Patient Portal."
PREPARATION_NOTICE = "LUX MED lists preparation instructions for this visit. Open “Zobacz szczegóły” in the Patient Portal."
ADDITIONAL_SLOT_NOTICE = "LUX MED lists this as an additional slot. Check its booking options in the Patient Portal."
BOOKING_ACCESS_NOTICE = "Direct booking is paused for this patient. Availability searches can continue. Use /resume with the patient name to clear the pause."
OPTION_NUMBER = re.compile(r"[1-9][0-9]{0,8}")
QUESTIONNAIRE_ANSWER = re.compile(r"(?:/?answer |option )?([0-9]{1,2})")
CANCEL_APPOINTMENT = re.compile(r"/?cancel(?: appointment| visit)? #?([1-9][0-9]{0,8})")
SEARCH_CHANGE_PROMPT = "Change an active search"
SEARCH_FILTER_CHOICES = ("Tomorrow", "Next week", "After 16:00", "Any time", "Clear filters")
CALLBACK_COMMAND = re.compile(
    r"(?:select|book|approve|stop booking) [1-9][0-9]{0,8}|more|continue|options|"
    r"active|pick patient|pick visits|"
    r"/appointments patient[123]|services patient[123]"
)
BOOKING_CHECK_LABELS = {
    "login": "sign-in", "service-catalog": "service catalog",
    "availability": "slot availability", "phone-availability": "phone slot availability",
    "slot-recheck": "exact slot check",
    "phone-doctors": "phone clinician lookup", "booking-penalty": "no-show restriction check",
    "booking-limits": "booking limits", "referrals": "referral check",
    "booking-location": "clinic location check", "facility-directory": "clinic directory",
    "questionnaire": "questionnaire check", "service-eligibility": "service eligibility",
    "questionnaire-submission": "questionnaire submission",
    "booking-conflicts": "LUX MED conflict check", "booking-request-token": "booking request verification",
    "booking-hold": "slot hold", "booking-confirm": "booking confirmation",
    "booking-release": "slot hold release", "booking-verification": "reservation verification",
}
BOOKING_FAILURES = {
    "slot-gone": "That exact slot is no longer available. Use More or Continue to find another.",
    "booking-details-missing": "I couldn't verify all the details for this slot. Check it in the Patient Portal.",
    "booking-location-unverified": "I couldn’t verify that this clinic belongs to your requested city. Use More or Continue to try another slot.",
    "booking-limit": "LUX MED didn't clear its booking limit check. Check the Patient Portal.",
    "booking-eligibility": "LUX MED didn't confirm that this service is covered and available to book online.",
    "booking-referrals-unconfirmed": "I couldn’t verify the referral requirements. Check this visit in the Patient Portal.",
    "booking-referral-review": "LUX MED returned a referral for this service. Review it in the Patient Portal to book.",
    "portal-questions": QUESTIONNAIRE_NOTICE,
    "questionnaire-response-unrecognized": "I couldn't verify LUX MED's questionnaire response. Check this visit in the Patient Portal.",
    "questionnaire-submission-unconfirmed": "I couldn’t verify that LUX MED accepted the answers. Check the Patient Portal.",
    "questionnaire-expired": QUESTIONNAIRE_EXPIRED,
    "questionnaire-open": "Finish the current questionnaire first, or send Stop questionnaire.",
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
        updates = self.call("getUpdates", {
            "offset": offset, "timeout": 8, "allowed_updates": ["message", "callback_query"],
        })
        if not isinstance(updates, list):
            raise TelegramError()
        return updates

    def send(self, message: str, *, force_reply: bool = False,
             buttons: list[list[tuple[str, str]]] | None = None) -> int:
        payload = {"chat_id": self.owner, "text": message, "protect_content": True}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": action} for label, action in row] for row in buttons
                ],
            }
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True}
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramError()
        return result["message_id"]

    def acknowledge(self, callback_id: str) -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id})
        except TelegramError as error:
            if error.stop:
                raise


@dataclass(frozen=True)
class Profile:
    name: str
    credentials: Luxmed = field(repr=False)
    aliases: tuple[str, ...] = ()
    child: bool = False


@dataclass(repr=False)
class QuestionnaireBooking:
    booking_id: int
    search_id: int
    patient: str
    service: str
    approval_reason: str
    portal: LuxMedPortal
    expires_at: float
    form: Questionnaire | None
    receipt: QuestionnaireReceipt | None = None
    prompt: str | None = None
    message_id: int | None = None


@dataclass(repr=False)
class AppointmentList:
    patient: str
    token: str
    visits: list[ScheduledVisit]
    messages: set[int] = field(default_factory=set)


@dataclass(repr=False)
class CancellationReview:
    token: str
    patient: str
    visit: ScheduledVisit
    terms: CancellationTerms | None
    message_id: int | None = None


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
                 public_prices: PublicPriceList | None = None,
                 booking_enabled: bool = False, device_credentials: dict | None = None,
                 allowed_booking_patients: frozenset[str] | None = None, calendar_destination: str | None = None,
                 calendar_detail_patients: frozenset[str] = frozenset()):
        self.telegram = telegram
        self.state = state
        self.profiles = profiles
        self.clock = clock
        self.free_text = free_text
        self.public_prices = public_prices
        self.calendar = calendar
        self.calendar_destination = calendar_destination
        self.calendar_detail_patients = calendar_detail_patients
        self.booking_enabled = booking_enabled
        self.allowed_booking_patients = (
            frozenset(profiles) if allowed_booking_patients is None else allowed_booking_patients
        )
        self.device_credentials = device_credentials or {}
        self.portals: dict[str, tuple[float, LuxMedPortal]] = {}
        self.questionnaire_booking: QuestionnaireBooking | None = None
        self.appointment_list: AppointmentList | None = None
        self.cancellation_review: CancellationReview | None = None
        self.cities = city_catalog()
        self.children = {key for key, profile in profiles.items() if profile.child}
        self.aliases = {}
        for key, profile in profiles.items():
            owner_aliases = ("me", "myself") if key == "patient1" else ()
            for alias in (key, profile.name, *profile.aliases, *owner_aliases):
                name = normalize(alias)
                if not name:
                    continue
                if self.aliases.setdefault(name, key) != key:
                    raise ValueError("Duplicate patient alias")

    def accept(self, update: dict) -> None:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            return
        update_id = update["update_id"]
        callback = update.get("callback_query") or {}
        callback_id = None
        with self.state.db:
            self.state.db.execute("BEGIN IMMEDIATE")
            if update_id < int(self.state.get_setting("offset", "0")):
                return
            message = callback.get("message") if callback else update.get("message")
            message = message or {}
            chat = message.get("chat") or {}
            sender = (callback if callback else message).get("from") or {}
            if (
                chat.get("type") == "private"
                and chat.get("id") == self.telegram.owner
                and sender.get("id") == self.telegram.owner
            ):
                if callback and isinstance(callback.get("id"), str):
                    callback_id = callback["id"]
                    self.handle_callback(callback.get("data"), message)
                elif not callback and isinstance(message.get("text"), str):
                    self.handle(message)
            self.state.set_setting("offset", str(update_id + 1))
        if callback_id is not None:
            self.telegram.acknowledge(callback_id)

    def handle_callback(self, action, message: dict) -> None:
        if not isinstance(action, str):
            return
        if match := re.fullmatch(r"answer ([1-9][0-9]?)", action):
            self.answer_questionnaire(match[1], message.get("message_id"))
        elif match := re.fullmatch(r"cancel visit ([a-f0-9]{12}) ([1-9][0-9]{0,8})", action):
            self.show_visit_cancellation(int(match[2]), message, token=match[1])
        elif match := re.fullmatch(r"cancel review ([a-f0-9]{12}) (yes|keep)", action):
            self.respond_to_cancellation(match[2] == "yes", message, token=match[1])
        elif match := re.fullmatch(r"coverage (patient[123]) ([1-9][0-9]{0,9})", action):
            self.show_coverage(match[1], (), service_id=int(match[2]))
        elif action == "stop questionnaire":
            self.stop_questionnaire(message)
        elif match := re.fullmatch(r"patient (patient[123]) ([a-f0-9]{12})", action):
            self.choose_patient_button(match[1], match[2])
        elif match := re.fullmatch(r"(service|pediatric) ([1-9][0-9]?) ([a-f0-9]{12})", action):
            if match[1] == "service":
                self.choose_catalog_service(match[2], menu_id=match[3])
            else:
                self.choose_pediatric_button(match[2], match[3])
        elif match := re.fullmatch(r"(change|stop) search ([1-9][0-9]{0,17})", action):
            row = self.active_search_for_action(int(match[2]))
            if row is not None:
                if match[1] == "change":
                    self.show_search_change(row)
                else:
                    self.stop_search(row["id"], row["display_number"])
        elif match := re.fullmatch(r"filter search ([1-9][0-9]{0,17}) (.+)", action):
            if match[2] in SEARCH_FILTER_CHOICES and (row := self.active_search_for_action(int(match[1]))) is not None:
                self.apply_search_change(row, match[2])
        elif CALLBACK_COMMAND.fullmatch(action):
            self.handle({"text": action, "reply_to_message": message})

    def handle(self, message: dict) -> None:
        text = message["text"].strip()
        key = normalize(text)
        command, _, argument = key.partition(" ")
        reply = message.get("reply_to_message") or {}
        flow = self.questionnaire_booking
        answer = QUESTIONNAIRE_ANSWER.fullmatch(key)
        if key in {"stop questionnaire", "/stop questionnaire", "cancel questionnaire"}:
            self.stop_questionnaire()
        elif key in {"options", "/options", "give me options", "show me options", "show options"}:
            self.show_options(reply)
        elif match := CANCEL_APPOINTMENT.fullmatch(key):
            self.show_visit_cancellation(int(match[1]), reply)
        elif key in {"confirm cancellation", "keep appointment"}:
            self.respond_to_cancellation(key == "confirm cancellation", reply)
        elif (
            (flow is not None and flow.message_id is not None and reply.get("message_id") == flow.message_id)
            or (flow is not None and flow.form is not None and not reply and answer is not None)
            or str(reply.get("text") or "").startswith(QUESTIONNAIRE_PROMPT + "\n")
            or command in {"answer", "/answer"}
        ):
            message_id = reply.get("message_id") if reply else (flow.message_id if flow is not None else None)
            self.answer_questionnaire(answer[1] if answer else text, message_id)
        elif command in {"stop", "/stop"}:
            self.stop(argument, reply)
        elif key in {"/active", "active", "show active searches", "show running searches"}:
            self.show_active()
        elif command in {"service", "/service"}:
            self.choose_catalog_service(argument)
        elif OPTION_NUMBER.fullmatch(key):
            if not reply and self.state.get_setting("pending_service"):
                self.choose_catalog_service(key)
            elif not reply and self.state.get_setting("pending"):
                self.handle_search(key)
            else:
                self.handle_results("select", key, reply)
        elif key in {"pick patient", "pick visits"}:
            self.show_patient_buttons(appointments=key == "pick visits")
        elif command == "services" and argument in self.profiles:
            self.handle_search(f"/find {argument} | doctor")
        elif len(text) <= 600 and (patient_name := appointments_request(text)) is not None:
            self.show_appointments(patient_name)
        elif command in {"/change", "change"}:
            self.change(text, reply)
        elif command in {"book", "/book", "details", "/details", "select", "option"} or key in {
            "more", "/more", "continue", "/continue", "find something else",
        }:
            self.handle_results(command, argument, reply)
        elif command in {"approve", "/approve"}:
            if re.fullmatch(r"[1-9][0-9]{0,8}", argument):
                self.approve_booking(int(argument))
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
        elif not text.startswith("/") and str(reply.get("text") or "").startswith(f"{SEARCH_CHANGE_PROMPT}\n"):
            original = self.replied_search(reply)
            if original is not None:
                number = original["display_number"]
                command = f"/change {number} | {text}" if "|" in text else f"Change {number} to {text}"
                self.change(command, reply)
            else:
                self.state.queue_message("Open Active searches and choose Change again.",
                                         buttons=[[("Active searches", "active")]])
        elif len(text) <= 600 and (coverage := coverage_request(text)) is not None:
            self.show_coverage(*coverage)
        elif len(text) <= 600 and key not in {"/start", "/help", "help"}:
            if not self.handle_chat_text(text, reply):
                self.handle_search(text)
        else:
            self.help()

    def handle_chat_text(self, text: str, reply: dict) -> bool:
        if self.free_text is None or text.startswith("/"):
            return False
        try:
            action = self.free_text.chat_action(text, self.aliases)
        except RequestError:
            return False
        if action is None:
            return False
        intent, patient, number = action
        if intent == "appointments":
            self.show_appointments(patient or "")
        elif intent == "active":
            self.show_active()
        elif intent == "cancel":
            if number is not None and (
                patient is None or (self.appointment_list is not None and patient == self.appointment_list.patient)
            ):
                self.show_visit_cancellation(number, reply)
            else:
                self.show_appointments(patient or "")
        else:
            self.handle_results(intent, "", reply)
        return True

    def show_coverage(self, patient_name: str, services: tuple[str, ...], *, service_id: int | None = None) -> None:
        if service_id is None and not (1 <= len(services) <= 4 and all(len(service) <= 180 for service in services)):
            self.state.queue_message(f"Ask about up to four services at a time. {COVERAGE_HELP}")
            return
        patient = self.aliases.get(patient_name)
        if patient is None:
            names = ", ".join(profile.name for profile in self.profiles.values())
            self.state.queue_message(f"Use one of these patient names: {names}. {COVERAGE_HELP}")
            return
        name = self.profiles[patient].name
        paused = f"LUX MED access for {name} is paused. Once access is restored, send /resume {name} and ask again."
        if self.state.account_paused(patient):
            self.state.queue_message(paused)
            return
        heading = f"LUX MED subscription coverage for {name}"
        results = []
        choices = []
        try:
            portal = self.portal_for(patient)
            packages = portal.medical_package_ids()
            if not packages:
                self.state.queue_message(f"LUX MED shows no active subscription for {name}.")
                return
            catalog = portal.medical_package_services()
            matches = (
                [([variant for variant in catalog if variant["id"] == service_id], True)]
                if service_id is not None else
                [matching_package_services(service, catalog) for service in services]
            )
            for number, (variants, exact) in enumerate(matches, 1):
                if not variants:
                    results.append(f"Service {number} · No match in the package catalog. Try the exact LUX MED name.")
                elif len(variants) == 1 and exact:
                    variant = variants[0]
                    coverage = [portal.medical_package_coverage(package, variant["id"]) for package in packages]
                    quote = None
                    if self.public_prices is not None and all(item.status in {"paid", "outside"} for item in coverage):
                        quote = self.public_prices.lookup(variant["name"])
                    summaries = [self.coverage_summary(item, has_listed_price=quote is not None) for item in coverage]
                    if len(summaries) > 1:
                        summaries = [f"Package {i} · {summary}" for i, summary in enumerate(summaries, 1)]
                    if quote is not None:
                        summaries.append(quote.describe())
                    results.append("\n".join([variant["name"], *summaries]))
                else:
                    choices.append((number, variants))
        except PortalError as error:
            status = f"I couldn’t verify subscription coverage for {name} right now. Try again in a moment."
            if error.suspend:
                self.close_portal(patient)
                if error.stage == "login" or error.http_status == 429:
                    self.state.suspend(patient)
                    status = paused
            self.state.queue_message(status)
            return
        if results:
            for start in range(0, len(results), 2):
                self.state.queue_message("\n\n".join([heading, *results[start:start + 2]]))
        for number, variants in choices:
            shown = sorted(variants, key=lambda variant: normalize(variant["name"]))[:5]
            lines = [f"{i}. {variant['name']}" for i, variant in enumerate(shown, 1)]
            buttons = [
                [(f"Check {i}", f"coverage {patient} {variant['id']}")]
                for i, variant in enumerate(shown, 1)
            ]
            if len(variants) > len(shown):
                lines.append("For more matches, ask with a more specific service name.")
            self.state.queue_message(
                "\n".join([heading, f"Which variant should I check for service {number}?", *lines]),
                buttons=buttons,
            )

    @staticmethod
    def coverage_summary(coverage: PackageCoverage, *, has_listed_price: bool = False) -> str:
        status = {
            "covered": "Included in the subscription",
            "paid": "Payable" if has_listed_price else "Payable · price unavailable from this lookup",
            "outside": "Outside the subscription",
            "copay": "LUX MED lists a co-payment · amount unavailable from this lookup",
            "unknown": "LUX MED coverage could not be confirmed",
        }[coverage.status]
        conditions = [status]
        if coverage.referral_required is not None:
            conditions.append("Referral required" if coverage.referral_required else "No referral required")
        if coverage.usage_limit is not None:
            conditions.append("Usage limit applies" if coverage.usage_limit else "No usage limit")
        return " · ".join(conditions)

    def show_appointments(self, patient_name: str) -> None:
        self.appointment_list = None
        self.cancellation_review = None
        patient = self.aliases.get(patient_name)
        if patient is None:
            self.show_patient_buttons(appointments=True)
            return
        name = self.profiles[patient].name
        paused = f"LUX MED access for {name} is paused. Once access is restored, send /resume {name} and ask again."
        if self.state.account_paused(patient):
            self.state.queue_message(paused)
            return
        try:
            portal = self.portal_for(patient)
            visits = portal.scheduled_visits(dt.datetime.fromtimestamp(self.clock(), WARSAW))
        except PortalError as error:
            status = f"I couldn’t load the LUX MED appointments for {name}. Try again in a moment."
            if error.suspend:
                self.close_portal(patient)
                if error.stage == "login" or error.http_status == 429:
                    self.state.suspend(patient)
                    status = paused
            self.state.queue_message(status)
            return
        if not visits:
            self.state.queue_message(f"LUX MED shows no upcoming appointments for {name}.")
            return
        heading = f"Upcoming LUX MED appointments for {name} (Warsaw time)"
        snapshot = AppointmentList(patient, uuid.uuid4().hex[:12], visits)
        self.appointment_list = snapshot
        entries = [f"{number}. {self.upcoming_visit_details(visit)}" for number, visit in enumerate(visits, 1)]
        instructions = "Tap a cancellation button or type Cancel followed by the visit number to see how to cancel it."
        # Five bounded entries fit Telegram's message limit even with Unicode labels.
        for first in range(0, len(entries), 5):
            numbers = range(first + 1, min(first + 5, len(entries)) + 1)
            buttons = [[(f"Cancel {number}", f"cancel visit {snapshot.token} {number}")] for number in numbers]
            self.state.queue_message("\n\n".join([heading, *entries[first:first + 5], instructions]), buttons=buttons)

    @staticmethod
    def upcoming_visit_details(visit: ScheduledVisit) -> str:
        date = visit.starts_at.strftime("%d %b %Y, %H:%M")
        details = " · ".join(part for part in (visit.mode, visit.clinic, visit.clinician) if part)
        return f"{date} · {visit.service}" + (f"\n  {details}" if details else "")

    def show_visit_cancellation(self, number: int, reply: dict, *, token: str | None = None) -> None:
        snapshot = self.appointment_list
        message_id = reply.get("message_id")
        if (snapshot is None or (token is not None and token != snapshot.token)
                or (reply and (type(message_id) is not int or message_id not in snapshot.messages))):
            self.state.queue_message("Open Upcoming appointments to get the current visit numbers.",
                                     buttons=[[("Upcoming appointments", "pick visits")]])
            return
        if number > len(snapshot.visits):
            self.state.queue_message("Choose a number from the current upcoming appointments list.")
            return
        visit = snapshot.visits[number - 1]
        if visit.starts_at.timestamp() <= self.clock():
            self.state.queue_message("That appointment time has passed. Open Upcoming appointments for a fresh list.",
                                     buttons=[[("Upcoming appointments", "pick visits")]])
            return
        name = self.profiles[snapshot.patient].name
        terms = None
        if self.can_cancel_visit(snapshot.patient, visit):
            try:
                terms = self.portal_for(snapshot.patient).cancellation_terms(visit)
            except PortalError as error:
                self.handle_cancellation_access_error(snapshot.patient, error)
        review = CancellationReview(uuid.uuid4().hex[:12], snapshot.patient, visit, terms)
        self.cancellation_review = review
        yes = "Yes, cancel this visit" if terms is not None else "Continue to portal steps"
        instructions = (
            "Reply Confirm cancellation to this message or tap a button." if terms is not None else
            "Finish this cancellation in the Patient Portal."
        )
        warning = ""
        if terms is not None and terms.late_hours is not None:
            warning = (
                f"LUX MED marks this as late, within {terms.late_hours:g} hours of the visit. "
                "Another patient may miss the chance to use this slot.\n"
            )
            if terms.no_show:
                warning += "LUX MED flagged its no-show process, which may affect future booking.\n"
        self.state.queue_message(
            f"Appointment {number} for {name}\n{self.upcoming_visit_details(visit)}\n\n"
            f"{warning}Are you sure you want to cancel this visit?\n{instructions}",
            buttons=[[(yes, f"cancel review {review.token} yes"),
                      ("Keep appointment", f"cancel review {review.token} keep")]],
        )

    def can_cancel_visit(self, patient: str, visit: ScheduledVisit) -> bool:
        return (
            patient == "patient1" and self.can_book_for(patient)
            and visit.cancellation_instance is not None
            and visit.event_id is not None and not self.state.account_paused(patient)
        )

    @staticmethod
    def visit_signature(visit: ScheduledVisit) -> str:
        identity = [int(visit.starts_at.timestamp()), visit.service, visit.clinic, visit.clinician,
                    visit.mode, visit.cancellation_instance]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def respond_to_cancellation(self, confirm: bool, reply: dict, *, token: str | None = None) -> None:
        review = self.cancellation_review
        if (review is None or review.message_id is None or reply.get("message_id") != review.message_id
                or (token is not None and token != review.token)):
            self.state.queue_message("Use the current cancellation prompt to confirm or keep the visit.",
                                     buttons=[[("Upcoming appointments", "pick visits")]])
            return
        self.cancellation_review = None
        if not confirm:
            self.state.queue_message("Appointment kept.")
        elif review.visit.starts_at.timestamp() <= self.clock():
            self.state.queue_message("The appointment time has passed. Open Upcoming appointments for a fresh list.")
        elif review.terms is None:
            self.state.queue_message(
                "Open the selected visit under Leczenie in the Patient Portal, tap Odwołaj and review any conditions.\n"
                f"{APPOINTMENTS_PORTAL_URL}",
            )
        elif self.state.queue_cancellation(
            review.patient, review.visit.event_id, review.visit.event_type,
            int(review.visit.starts_at.timestamp()), self.visit_signature(review.visit),
            late_hours=review.terms.late_hours, no_show=review.terms.no_show,
        ):
            self.state.queue_message("Confirmed. I’ll recheck the exact visit before cancelling it.")
        else:
            self.state.queue_message(
                f"There's already a cancellation request for this visit. Check its status in the Patient Portal.\n"
                f"{APPOINTMENTS_PORTAL_URL}",
            )

    def handle_cancellation_access_error(self, patient: str, error: PortalError) -> None:
        if error.suspend:
            self.close_portal(patient)
            if error.stage == "login" or error.http_status == 429:
                self.state.suspend(patient)

    def handle_search(self, text: str) -> None:
        error_message = None
        menu_selection = False
        needs_filter_interpretation = False
        today = dt.datetime.fromtimestamp(self.clock(), WARSAW).date()
        text, doctor_filter = split_clinician_first_search(text)
        doctor = None
        try:
            if doctor_filter:
                doctor = parse_preferences(doctor_filter, today).doctor
            local_text = f"{text}, {doctor_filter}" if doctor else text
            criteria = parse_search(local_text, self.aliases, self.children, self.cities, today=today)
        except PatientChoiceNeeded as error:
            self.show_search_patient_choices({"parts": error.parts})
            return
        except VisitModeChoiceNeeded as error:
            criteria = error.criteria
            self.show_catalog_service_menu({
                "criteria": asdict(criteria),
                "choices": [
                    {"service": criteria.service, "telephone": False},
                    {"service": phone_service(criteria.service), "telephone": True},
                ],
                "prompt": "Would you like an in-person visit or a phone call?",
            })
            return
        except ServiceChoiceNeeded as error:
            self.show_specialty_choices(error.criteria)
            return
        except PatientServiceMismatch as error:
            self.reject_mismatch(error)
            return
        except UnrecognizedPreferences as error:
            criteria = None
            error_message = str(error)
            needs_filter_interpretation = True
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
            fields.pop("menu", None)
            fields.pop("explanation", None)
            fields["preferences"] = Preferences(**fields.get("preferences", {}))
            candidate = Criteria(**fields)
            if service := pediatric_choice(text):
                criteria = replace(candidate, service=service, telephone=requests_remote(service))
                menu_selection = True
        if self.free_text is not None and not menu_selection and not text.startswith("/"):
            try:
                interpreted = self.free_text.criteria(
                    text, self.aliases, self.children, self.cities, today=today, local=criteria,
                )
                if interpreted is not None and (
                    not needs_filter_interpretation or interpreted.preferences != Preferences()
                ):
                    if doctor:
                        interpreted = replace(interpreted, preferences=replace(interpreted.preferences, doctor=doctor))
                    criteria = interpreted
            except PatientServiceMismatch as error:
                self.reject_mismatch(error)
                return
            except ServiceChoiceNeeded as error:
                self.show_specialty_choices(error.criteria)
                return
            except PreferenceError as error:
                self.state.queue_message(str(error))
                return
            except RequestError as error:
                error_message = str(error)
        if criteria and not criteria.service:
            self.offer_pediatric_choices(criteria)
        elif criteria:
            self.state.set_setting("pending", "")
            self.start(criteria)
        elif error_message:
            self.state.queue_message(error_message)
        elif pending:
            self.show_pediatric_menu(json.loads(pending))
        else:
            self.state.queue_message(
                "What would you like to do?",
                buttons=[[("Find a visit", "pick patient")],
                         [("Upcoming appointments", "pick visits"), ("Active searches", "active")]],
            )

    def show_patient_buttons(self, *, appointments: bool = False) -> None:
        if appointments:
            self.appointment_list = None
            self.cancellation_review = None
        prefix = "/appointments" if appointments else "services"
        prompt = "Whose appointments should I show?" if appointments else "Who is the appointment for?"
        self.state.queue_message(prompt, buttons=[[
            (profile.name, f"{prefix} {patient}") for patient, profile in self.profiles.items()
        ]])

    def show_specialty_choices(self, criteria: Criteria) -> None:
        services = [GENERAL_SERVICES[key] for key in ("internist", "allergist", "ent", "orthopedist")]
        self.show_catalog_service_menu({
            "criteria": asdict(criteria),
            "choices": [{"service": phone_service(service) if criteria.telephone else service,
                         "telephone": criteria.telephone} for service in services],
            "prompt": "Which specialty should I search for? You can also type a different one.",
        })

    def show_search_patient_choices(self, choice: dict) -> None:
        if "menu" not in choice:
            choice["menu"] = uuid.uuid4().hex[:12]
            self.state.set_setting("pending_patient", json.dumps(choice))
        self.state.queue_message("Who is the appointment for?", buttons=[[
            (profile.name, f"patient {patient} {choice['menu']}") for patient, profile in self.profiles.items()
        ]])

    def choose_patient_button(self, patient: str, menu_id: str) -> None:
        choice = json.loads(self.state.get_setting("pending_patient") or "{}")
        if patient not in self.profiles or choice.get("menu") != menu_id:
            self.state.queue_message("That patient choice has expired. Ask for a fresh search.")
            return
        self.state.set_setting("pending_patient", "")
        self.handle_search("/find " + " | ".join([patient, *filter(None, choice["parts"])]))

    def show_pediatric_menu(self, fields: dict) -> None:
        menu = fields.get("menu")
        if not menu:
            menu = fields["menu"] = uuid.uuid4().hex[:12]
            self.state.set_setting("pending", json.dumps(fields))
        prompt = pediatric_menu(self.profiles[fields["patient"]].name)
        if explanation := fields.get("explanation"):
            prompt = f"{explanation}\n\n{prompt}"
        self.state.queue_message(
            prompt,
            buttons=[[(choice[0], f"pediatric {number} {menu}")]
                     for number, choice in enumerate(PEDIATRIC_SERVICES, 1)],
        )

    def choose_pediatric_button(self, number: str, menu_id: str) -> None:
        fields = json.loads(self.state.get_setting("pending") or "{}")
        if fields.get("menu") != menu_id:
            self.state.queue_message("That pediatric menu has expired. Ask for a fresh search.")
            return
        self.handle_search(number)

    def offer_pediatric_choices(self, criteria: Criteria, *, explanation: str = "") -> None:
        fields = {**asdict(criteria), "menu": uuid.uuid4().hex[:12], "explanation": explanation}
        self.state.set_setting("pending_service", "")
        self.state.set_setting("pending_patient", "")
        self.state.set_setting("pending", json.dumps(fields))
        self.show_pediatric_menu(fields)

    def reject_mismatch(self, error: PatientServiceMismatch, search_id: int | None = None, *,
                        offer_pediatrics: bool = True) -> None:
        if search_id is None:
            self.state.set_setting("pending", "")
        name = self.profiles[error.patient].name
        if offer_pediatrics and error.pediatric_search is not None:
            self.offer_pediatric_choices(error.pediatric_search, explanation=f"{name} {error}")
            return
        self.state.queue_message(
            f"{name} {error} Please send a new request with the patient and service you intended.", search_id,
        )

    def help(self) -> None:
        booking_available = self.booking_enabled and bool(self.allowed_booking_patients)
        title = "LUX MED appointment assistant" if booking_available else "Read-only appointment search"
        self.state.queue_message(
            f"{title}\n"
            "Find an internist phone call for me\nFind a pediatrician for <patient>\n"
            "Find an internist for <patient> next week after 16:00\n"
            "Show my next appointments or /appointments <patient>\n"
            "Is a cardiologist visit covered for me? · /coverage <patient> | <service>\n"
            "/find patient | portal service | city | optional filters\n"
            "/active · Change or stop a search\n"
            "Change 123 to after 16:00\n/change number | service | city | optional filters\n"
            "From a result list: Details 2 · More · Continue"
            + (" · Book 2\nFor booking approval: Approve <request number> · Stop booking <request number>"
               if booking_available else ""),
        )

    def start(self, criteria: Criteria, *, excluded_slots=(), not_before: int = 0) -> None:
        self.state.set_setting("pending_service", "")
        self.state.set_setting("pending_patient", "")
        now = int(self.clock())
        search_id, number, due, paused = self.state.add(
            criteria, now, excluded_slots=excluded_slots, not_before=not_before,
        )
        self.confirm_search(search_id, number, criteria, due, paused, now)

    @staticmethod
    def slot_details(slot: dict) -> str:
        starts = dt.datetime.fromtimestamp(slot["start"], WARSAW).strftime("%d.%m %H:%M")
        _, clinic, doctor = SearchBot.slot_key(slot)
        return " · ".join(value for value in (starts, clinic, doctor) if value)

    @staticmethod
    def slot_key(slot: dict) -> tuple[int, str, str]:
        doctor = slot["doctor"]
        # Older result lists stored this placeholder for an unnamed clinician.
        return slot["start"], slot["clinic"], "" if doctor == "None None None" else doctor

    @staticmethod
    def portal_notices(service: str, slots: list[dict]) -> list[str]:
        notices = []
        if normalize(service) in SICK_CHILD_SERVICES:
            notices.append(QUESTIONNAIRE_NOTICE)
        if any(slot.get("requires_preparation") is True for slot in slots):
            notices.append(PREPARATION_NOTICE)
        if slots and all(slot.get("is_additional") is True for slot in slots):
            notices.append(ADDITIONAL_SLOT_NOTICE if len(slots) == 1 else
                           "These are additional LUX MED slots. Check their booking options in the Patient Portal.")
        return notices

    @staticmethod
    def supports_booking_service(service: str, telephone: bool, patient: str) -> bool:
        key = normalize(service)
        if telephone:
            return key == normalize(INTERNIST_PHONE_SERVICE)
        return key in SUPPORTED_BOOKING_SERVICES or (patient == "patient1" and key in ALLERGY_SERVICES)

    @staticmethod
    def is_nationwide_phone_search(criteria: Criteria) -> bool:
        return criteria.telephone and normalize(criteria.service) == normalize(INTERNIST_PHONE_SERVICE)

    def show_options(self, reply: dict) -> None:
        original = self.replied_search(reply)
        if original and self.state.results(original["id"]) is not None:
            self.show_results(original["id"], current=True)
        elif self.questionnaire_booking is not None and self.questionnaire_booking.form is not None:
            self.queue_question(self.questionnaire_booking)
        elif pending_patient := self.state.get_setting("pending_patient"):
            self.show_search_patient_choices(json.loads(pending_patient))
        elif pending_service := self.state.get_setting("pending_service"):
            self.show_catalog_service_menu(json.loads(pending_service))
        elif pending := self.state.get_setting("pending"):
            self.show_pediatric_menu(json.loads(pending))
        elif row := self.state.results():
            self.show_results(row["id"], current=True)
        else:
            self.show_patient_buttons()

    def show_results(self, search_id: int, *, current: bool = False) -> None:
        row = self.state.results(search_id)
        slots = json.loads(row["slots"])
        now = int(self.clock())
        page = [(index + 1, slot) for index, slot in enumerate(slots[:row["shown"]])
                if slot["start"] > now][-RESULTS_PER_PAGE:] if current else []
        if not page:
            for index in range(row["shown"], len(slots)):
                if slots[index]["start"] > now:
                    page.append((index + 1, slots[index]))
                    if len(page) == RESULTS_PER_PAGE:
                        break
        if not page:
            self.state.queue_message("No more upcoming options in this list. Use Continue to look for other slots.",
                                     search_id, buttons=[[("Continue", "continue")]])
            return
        self.state.mark_shown(search_id, max(row["shown"], page[-1][0]))
        self.state.set_setting("latest_result", str(search_id))
        heading = self.search_heading(row["display_number"], self.state.criteria(row))
        notices = self.portal_notices(row["service"], [slot for _, slot in page])
        lines = [heading, *notices, "Available slots (Warsaw time)"]
        for number, slot in page:
            portal_only = slot.get("is_additional") is True or slot.get("requires_preparation") is True
            suffix = " · Patient Portal" if portal_only else ""
            lines.append(f"{number}. {self.slot_details(slot)}{suffix}")
        more = any(slot["start"] > now for slot in slots[max(row["shown"], page[-1][0]):])
        if more:
            lines.append("Reply More to see the next options.")
        can_book = (
            self.can_book_for(row["patient"]) and not notices
            and self.supports_booking_service(row["service"], bool(row["telephone"]), row["patient"])
            and not self.state.booking_paused(row["patient"])
        )
        action = "Book <option number> to request that slot" if can_book else "Details <option number> for the Patient Portal link"
        lines.append(f"Tap an option or reply with its number. Reply {action}, or Continue to search for different slots.")
        buttons = [[(f"Option {number}", f"select {number}") for number, _ in page]]
        buttons.append(([("More", "more")] if more else []) + [("Continue", "continue")])
        self.state.queue_message("\n".join(lines), search_id, buttons=buttons)

    def handle_results(self, command: str, argument: str, reply: dict) -> None:
        action = "select" if command == "option" else command.lstrip("/")
        if action in {"book", "details", "select"} and not OPTION_NUMBER.fullmatch(argument):
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
        if action in {"book", "details", "select"}:
            number = int(argument)
            if number > row["shown"]:
                self.state.queue_message("Choose an option from this list.", search_id,
                                         buttons=[[("See options", "options"), ("More", "more")]])
                return
            slot = slots[number - 1]
            if slot["start"] <= now:
                self.state.queue_message("That appointment time has passed.", search_id,
                                         buttons=[[("See options", "options"), ("Continue search", "continue")]])
                return
            if action in {"details", "select"}:
                heading = self.search_heading(row["display_number"], self.state.criteria(row))
                notices = self.portal_notices(row["service"], [slot])
                can_book = (
                    action == "select" and self.can_book_for(row["patient"])
                    and self.supports_booking_service(row["service"], bool(row["telephone"]), row["patient"])
                    and not self.state.booking_paused(row["patient"]) and not notices
                )
                if slot["start"] - now < SHORT_NOTICE_SECONDS:
                    notices.append("This appointment starts within six hours. Check the cancellation terms before confirming.")
                lines = [heading, f"Option {number}. {self.slot_details(slot)}", *notices]
                if can_book:
                    lines.append(f"Want this time? Tap Book option {number}.")
                else:
                    lines.extend(["Confirm availability in the Patient Portal.", PATIENT_PORTAL_URL])
                buttons = [[("Back to options", "options"), ("Continue", "continue")]]
                if can_book:
                    buttons.insert(0, [(f"Book option {number}", f"book {number}")])
                self.state.queue_message("\n".join(lines), search_id, buttons=buttons)
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
            handoff = [*notices, PATIENT_PORTAL_URL] if slot.get("is_additional") is True else [
                *notices, "Book this visit in the Patient Portal.", PATIENT_PORTAL_URL,
            ]
            self.state.queue_message("\n".join([description, *handoff]), search_id,
                                     buttons=[[("Back to options", "options"), ("Continue search", "continue")]])
            return
        if not self.can_book_for(row["patient"]):
            self.state.queue_message(
                f"{description}\nDirect booking is unavailable. Confirm the appointment in the Patient Portal.\n{PATIENT_PORTAL_URL}",
                search_id,
            )
            return
        if self.state.booking_paused(row["patient"]):
            self.state.queue_message(f"{description}\n{BOOKING_ACCESS_NOTICE}", search_id)
            return
        if not self.supports_booking_service(row["service"], bool(row["telephone"]), row["patient"]):
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
            if slot["start"] <= now + APPROVAL_CUTOFF_SECONDS:
                self.expire_booking_offer(booking, self.state.results(booking["search_id"]), now)
            else:
                self.send_booking_approval(booking, description, booking["reason"], slot["start"])
        elif status == "queued":
            due = next_search_time(now)
            timing = "Checking the exact slot and booking conditions." if due == now else f"Queued for {local_time(due)} Warsaw time."
            self.state.queue_message(f"{description}\n{timing}", search_id)
        elif status == "questionnaire":
            self.state.queue_message("Reply to the current LUX MED question, or send Stop questionnaire.", search_id)
        elif status in {"checking", "submitting", "verifying"}:
            self.state.queue_message(f"{description}\nThis booking is already being processed.", search_id)
        else:
            self.state.queue_message(f"{description}\nThis booking request has ended. Use More or Continue for another slot.", search_id)

    def send_booking_approval(self, booking, description: str, reason: str, starts_at: int) -> None:
        deadline = local_time(starts_at - APPROVAL_CUTOFF_SECONDS)
        self.state.queue_message(
            f"{description}\n{reason}\nApproval code: {booking['id']}\n"
            f"Approve by {deadline} Warsaw time.\n"
            f"Reply Approve {booking['id']} to book this option or Stop booking {booking['id']} to drop this request.",
            booking["search_id"],
            buttons=[[("Approve booking", f"approve {booking['id']}"),
                      ("Stop booking", f"stop booking {booking['id']}")]],
        )

    def approve_booking(self, booking_id: int) -> None:
        if booking_id in self.expire_due_approvals(int(self.clock())):
            return
        if self.booking_enabled and self.state.approve_booking(booking_id, self.allowed_booking_patients):
            self.state.queue_message("Approved. I’ll recheck the selected appointment before booking.")
        elif self.state.booking_approval_expired(booking_id):
            self.state.queue_message(f"Approval {booking_id} has expired.", buttons=[[("Active searches", "active")]])
        else:
            self.state.queue_message("Use Approve followed by the booking request number shown in my message.")

    def expire_due_approvals(self, now: int) -> set[int]:
        due = self.state.due_booking_approvals(now + APPROVAL_CUTOFF_SECONDS)
        for booking in due:
            self.expire_booking_offer(booking, self.state.results(booking["search_id"]), now)
        return {booking["id"] for booking in due}

    def expire_booking_offer(self, booking, row, now: int) -> None:
        booking_id, search_id = booking["id"], booking["search_id"]
        if not self.state.expire_booking_approval(booking_id, booking["status"]):
            return
        if self.questionnaire_booking is not None and self.questionnaire_booking.booking_id == booking_id:
            self.questionnaire_booking = None
        message = f"Approval {booking_id} has expired."
        if row is None or row["status"] != "found" or self.state.search_has_live_booking(search_id):
            self.state.queue_message(message, search_id)
            return
        criteria = self.state.criteria(row)
        next_due = next_search_time(max(now, row["next_due"]))
        next_date = dt.datetime.fromtimestamp(next_due, WARSAW).date().isoformat()
        if criteria.preferences.last_date and criteria.preferences.last_date < next_date:
            self.state.queue_message(f"{message} The requested date window has ended.", search_id)
            return
        slots = json.loads(row["slots"])
        excluded = json.loads(row["excluded_slots"])
        excluded.extend(slots[number - 1] for number in self.state.expired_booking_options(search_id))
        self.state.mark_continued(search_id)
        followup, number, due, paused = self.state.add(
            criteria, now, excluded_slots=[slot for slot in excluded if slot["start"] > now], not_before=next_due,
        )
        self.state.queue_message(f"{message} I’ll keep looking with the same filters.", followup)
        self.confirm_search(followup, number, criteria, due, paused, now)

    def confirm_search(self, search_id: int, number: int, criteria: Criteria, due: int, paused: bool, now: int, *, changed: bool = False) -> None:
        if paused:
            status = "Paused. Check the portal, then use /resume."
        elif due > now:
            status = f"I’ll start looking at {local_time(due)} Warsaw time."
        else:
            status = "I’ll check LUX MED now and let you know what I find."
        prefix = "Updated\n" if changed else ""
        self.state.queue_message(f"{prefix}{self.search_heading(number, criteria)}\n{status}", search_id)

    def search_heading(self, number: int, criteria: Criteria) -> str:
        location = "Nationwide (phone)" if self.is_nationwide_phone_search(criteria) else criteria.city
        heading = f"#{number} · {self.profiles[criteria.patient].name} · {criteria.service} · {location}"
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
        filters = match[2] or (parts[3] if len(parts) == 4 else "")
        service = parts[1] if len(parts) >= 2 else ""
        city = parts[2] if len(parts) >= 3 else ""
        self.apply_search_change(row, filters, service=service, city=city)

    def apply_search_change(self, row, filters: str, *, service: str = "", city: str = "") -> None:
        current = self.state.criteria(row)
        now = int(self.clock())
        today = dt.datetime.fromtimestamp(now, WARSAW).date()
        try:
            criteria = current
            if service or city:
                criteria = parse_search(
                    f"/find {current.patient} | {service or current.service} | {city or current.city}",
                    self.aliases, self.children, self.cities,
                )
            criteria = replace(criteria, preferences=parse_preferences(filters, today, current.preferences))
        except PatientServiceMismatch as error:
            self.reject_mismatch(error, offer_pediatrics=False)
            return
        except (ServiceChoiceNeeded, VisitModeChoiceNeeded):
            self.state.queue_message("Choose a specific service and visit type for this search.")
            return
        except (RequestError, PreferenceError) as error:
            self.state.queue_message(str(error))
            return
        if not criteria.service:
            self.state.queue_message("Choose a specific pediatric visit type in /change, such as routine visit.")
            return
        number = row["display_number"]
        if criteria == current:
            self.state.queue_message(f"Search #{number} already uses those settings.")
            return
        due, paused = self.state.revise(row["id"], criteria, now)
        self.confirm_search(row["id"], number, criteria, due, paused, now, changed=True)

    def active_search_for_action(self, search_id: int):
        row = self.state.active_by_id(search_id)
        if row is None:
            self.state.queue_message("That search has ended.", buttons=[[("Active searches", "active")]])
        return row

    def show_search_change(self, row) -> None:
        heading = self.search_heading(row["display_number"], self.state.criteria(row))
        choices = [(label, f"filter search {row['id']} {label}") for label in SEARCH_FILTER_CHOICES]
        self.state.queue_message(
            f"{SEARCH_CHANGE_PROMPT}\n{heading}\n\n"
            "Tap a filter or reply to this message with a change like “tomorrow after 17:00”. "
            "To change the service or city, reply with service | city | filters.",
            row["id"], buttons=[choices[:2], choices[2:4], choices[4:]],
        )

    def show_active(self) -> None:
        rows = self.state.active()
        if not rows:
            self.state.queue_message("No active searches.")
            return
        for index in range(0, len(rows), 6):
            lines = []
            buttons = []
            for row in rows[index:index + 6]:
                number = row["display_number"]
                heading = self.search_heading(number, self.state.criteria(row))
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
                buttons.append([(f"Change #{number}", f"change search {row['id']}"),
                                (f"Stop #{number}", f"stop search {row['id']}")])
            self.state.queue_message("\n\n".join(lines), buttons=buttons)

    def replied_search(self, reply: dict):
        message_id = reply.get("message_id")
        return self.state.search_for_message(message_id) if type(message_id) is int else None

    def stop(self, argument: str, reply: dict) -> None:
        if match := re.fullmatch(r"booking ([1-9][0-9]{0,8})", argument):
            stopped = self.state.stop_booking(int(match[1]))
            if stopped and self.questionnaire_booking and self.questionnaire_booking.booking_id == int(match[1]):
                self.questionnaire_booking = None
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
        elif not reply and self.state.get_setting("pending_service"):
            self.state.set_setting("pending_service", "")
            self.state.queue_message("Service selection cleared.")
            return
        elif not reply and self.state.get_setting("pending_patient"):
            self.state.set_setting("pending_patient", "")
            self.state.queue_message("Patient selection cleared.")
            return
        elif not reply:
            rows = self.state.active()
            if len(rows) == 1:
                row = rows[0]
                number = row["display_number"]
        if number is None:
            self.state.queue_message("Use Stop with a search number from /active.")
            return
        self.stop_search(row["id"] if row is not None else None, number)

    def stop_search(self, search_id: int | None, number: int) -> None:
        if search_id is not None and self.state.finish(search_id, "stopped"):
            self.state.queue_message(f"Stopped search #{number}.")
        else:
            self.state.queue_message(f"That search (#{number}) has already ended.")

    def flush(self) -> None:
        while (message := self.state.next_message()) is not None:
            if message["buttons"]:
                buttons = json.loads(message["buttons"])
                sent = self.telegram.send(message["text"], buttons=buttons)
                if self.appointment_list is not None:
                    prefix = f"cancel visit {self.appointment_list.token} "
                    if any(action.startswith(prefix) for row in buttons for _, action in row):
                        self.appointment_list.messages.add(sent)
                if self.cancellation_review is not None:
                    confirm = f"cancel review {self.cancellation_review.token} yes"
                    if any(action == confirm for row in buttons for _, action in row):
                        self.cancellation_review.message_id = sent
            else:
                sent = self.telegram.send(message["text"])
            self.state.sent(message, sent)
        flow = self.questionnaire_booking
        if flow is not None and flow.prompt is not None:
            choices = [
                [(f"{number}. {visible_text(choice['name'])[:55]}", f"answer {number}")]
                for number, choice in enumerate(flow.form.choices, 1)
            ]
            choices.append([("Stop questionnaire", "stop questionnaire")])
            flow.message_id = self.telegram.send(flow.prompt, buttons=choices)
            flow.prompt = None

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
                raise PortalError("session-expired", suspend=True, stage="login")
        self.portals[patient] = (reuse_until, portal)
        return portal

    def close_portal(self, patient: str) -> None:
        cached = self.portals.pop(patient, None)
        if cached is not None:
            cached[1].session.close()

    def close_portals(self) -> None:
        for patient in tuple(self.portals):
            self.close_portal(patient)

    def queue_question(self, flow: QuestionnaireBooking) -> None:
        question = flow.form.prompt()
        name = self.profiles[flow.patient].name
        flow.message_id = None
        flow.prompt = (
            f"{QUESTIONNAIRE_PROMPT}\n{name} · {flow.service}\n\n{question}\n\n"
            "Type the number shown, such as 1 or Option 1, or tap an answer. "
            "I’ll send your choices to LUX MED when the form is complete. "
            "Send Stop questionnaire to end this request."
        )

    def stop_questionnaire(self, message: dict | None = None) -> None:
        flow = self.questionnaire_booking
        if message is not None and (flow is None or flow.message_id is None
                                    or message.get("message_id") != flow.message_id):
            self.state.queue_message("That questionnaire button has expired.")
            return
        if flow is not None and self.state.stop_booking(flow.booking_id):
            self.questionnaire_booking = None
            self.state.queue_message("Questionnaire stopped.")
        else:
            self.state.queue_message("There’s no active questionnaire.")

    def answer_questionnaire(self, text: str, message_id: int | None) -> None:
        flow = self.questionnaire_booking
        if flow is None or flow.expires_at <= self.clock():
            self.state.queue_message(QUESTIONNAIRE_EXPIRED)
            return
        if flow.form is None or flow.message_id is None or message_id != flow.message_id:
            self.state.queue_message("Use the latest LUX MED question. You can type its number or tap an answer.")
            return
        if not re.fullmatch(r"[1-9][0-9]?", text) or int(text) > len(flow.form.choices):
            self.state.queue_message("Type one of the numbers shown on the current LUX MED question, or tap its button.")
            return
        try:
            flow.form.choose(int(text))
            if flow.form.result is None:
                self.queue_question(flow)
                return
            flow.message_id = None
            if flow.form.permits_original_service():
                info = flow.form.information
                if len(info.encode("utf-16-le")) > 6800:
                    raise QuestionnaireError()
                if info:
                    self.state.queue_message(f"LUX MED\n{info}", flow.search_id)
                self.state.booking_status(flow.booking_id, "queued", reason=flow.approval_reason)
                return
            result = flow.form.result
            service = visible_text(result.get("name"))[:160] if result.get("discriminator") == "ServiceVariantResultDto" else ""
            notice = flow.form.information or service
            message = "LUX MED recommends a different next step for your answers."
            if service:
                message += f" You can start a new search for {service} if that’s what you want."
            else:
                message += f" Review the result in the Patient Portal.\n{PATIENT_PORTAL_URL}"
            if notice and notice != service and len(notice.encode("utf-16-le")) <= 6800:
                message = f"{notice}\n{message}"
        except QuestionnaireError:
            message = BOOKING_FAILURES["questionnaire-response-unrecognized"] + f"\n{PATIENT_PORTAL_URL}"
        self.state.booking_status(flow.booking_id, "ended")
        self.questionnaire_booking = None
        self.state.queue_message(message, flow.search_id)

    def start_questionnaire(self, booking, criteria: Criteria, slot: dict, portal: LuxMedPortal,
                            service: str, approval_reason: str, raw: dict | None) -> QuestionnaireBooking:
        if self.questionnaire_booking is not None:
            raise PortalError("questionnaire-open")
        try:
            form = Questionnaire(raw, slot["service_id"], criteria.city_id, self.clock()) if raw is not None else None
            flow = QuestionnaireBooking(
                booking["id"], booking["search_id"], criteria.patient, service, approval_reason, portal,
                min(self.portals[criteria.patient][0], slot["start"]), form,
            )
            if form is not None:
                self.queue_question(flow)
                with self.state.db:
                    self.state.booking_status(booking["id"], "questionnaire", reason=approval_reason)
        except QuestionnaireError:
            raise PortalError("questionnaire-response-unrecognized", stage="questionnaire") from None
        self.questionnaire_booking = flow
        return flow

    def questionnaire_preflight(self, booking, criteria: Criteria, slot: dict, portal: LuxMedPortal,
                                service: str, approval_reason: str) -> QuestionnaireBooking:
        flow = self.questionnaire_booking
        service_id = slot["service_id"]
        if flow is not None and flow.booking_id != booking["id"]:
            raise PortalError("questionnaire-open")
        if flow is not None and (flow.portal is not portal or flow.expires_at <= self.clock()):
            raise PortalError("questionnaire-expired")
        if flow is None and approval_reason in (booking["reason"] or "").splitlines():
            raise PortalError("questionnaire-expired")
        variant = portal.resolve_in_person_service(service)
        if variant != service_id:
            raise PortalError("booking-details-missing")
        portal.check_booking_requirements(variant)
        if flow is None:
            raw = portal.booking_questionnaire(criteria.city_id, variant)
            flow = self.start_questionnaire(booking, criteria, slot, portal, service, approval_reason, raw)
            if flow.form is not None:
                return flow
        portal.check_booking_coverage(criteria.city_id, variant)
        if flow.form is not None:
            # A queued questionnaire is complete only after the user chose a terminal answer.
            try:
                occurrence = flow.form.submission(self.clock())
            except QuestionnaireError:
                raise PortalError("questionnaire-response-unrecognized", stage="questionnaire") from None
            portal.submit_booking_questionnaire(occurrence)
            flow.receipt = flow.form.receipt
            flow.form = None
        return flow

    def booking_due(self) -> None:
        now = int(self.clock())
        with self.state.db:
            self.expire_due_approvals(now)
        flow = self.questionnaire_booking
        if flow is not None and flow.expires_at <= now:
            with self.state.db:
                if self.state.stop_booking(flow.booking_id):
                    self.state.queue_message(QUESTIONNAIRE_EXPIRED, flow.search_id)
            self.questionnaire_booking = None
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
        allergy = criteria.patient == "patient1" and not criteria.telephone and normalize(criteria.service) in ALLERGY_SERVICES
        internist = criteria.patient == "patient1" and not criteria.telephone and normalize(criteria.service) == "internista"
        service_name = ALLERGY_SERVICE if allergy else criteria.service
        approval_reason = QUESTIONNAIRE_APPROVAL if allergy else INTERNIST_QUESTIONNAIRE_APPROVAL
        resuming_questionnaire = approval_reason in (booking["reason"] or "").splitlines()
        reservation_id = None
        try:
            if slot["start"] <= now:
                raise PortalError("slot-gone")
            if not booking["approved"] and slot["start"] <= now + APPROVAL_CUTOFF_SECONDS:
                with self.state.db:
                    self.expire_booking_offer(booking, row, now)
                return
            if normalize(criteria.service) in SICK_CHILD_SERVICES:
                raise PortalError("portal-questions")
            if slot.get("requires_preparation") is True:
                raise PortalError("portal-preparation")
            if slot.get("is_additional") is True:
                raise PortalError("portal-additional")
            if not self.supports_booking_service(criteria.service, criteria.telephone, criteria.patient):
                raise PortalError("booking-needs-portal")
            if self.state.unresolved_booking(criteria.patient):
                raise PortalError("prior-booking-unverified")
            if self.state.account_paused(criteria.patient):
                raise PortalError("booking-account-paused")
            if self.state.booking_paused(criteria.patient):
                raise PortalError("booking-access-paused")
            portal = self.portal_for(criteria.patient)
            if criteria.telephone:
                variant = portal.resolve_service(
                    INTERNIST_PHONE_SERVICE, portal.get_services(), telephone=True, regular_phone=True,
                )
                if variant != slot["service_id"]:
                    raise PortalError("booking-details-missing")
            if portal.is_booking_blocked_by_penalty(criteria.city_id, slot["service_id"]):
                raise PortalError("booking-penalty")
            questionnaire = None
            if allergy or (internist and resuming_questionnaire):
                questionnaire = self.questionnaire_preflight(booking, criteria, slot, portal, service_name, approval_reason)
            else:
                try:
                    portal.check_booking_eligibility(criteria.city_id, slot["service_id"])
                except BookingQuestionnaireRequired as error:
                    if not internist:
                        raise
                    questionnaire = self.start_questionnaire(
                        booking, criteria, slot, portal, service_name, approval_reason, error.form,
                    )
            if questionnaire is not None and questionnaire.form is not None:
                return
            receipt = questionnaire.receipt if questionnaire is not None else None
            locator = DoctorLocator(id=f"{criteria.city_id}*{slot['service_id']}*-1*{slot['doctor_id']}")
            appointment = self.refresh_booking_slot(
                portal, locator, slot, nationwide_from=criteria.city if criteria.telephone else None,
                questionnaire=receipt,
            )
            start = appointment.AppointmentDate.astimezone(dt.UTC)
            end = appointment.booking_term["end"].astimezone(dt.UTC)
            reasons = self.booking_approval_reasons(portal, start, end)
            if questionnaire is not None:
                reasons.append(questionnaire.approval_reason)
            if slot["start"] <= int(self.clock()):
                raise PortalError("slot-gone")
            checked_at = int(self.clock())
            if next_search_time(checked_at) != checked_at:
                with self.state.db:
                    self.state.booking_status(booking["id"], "queued", reason=booking["reason"])
                return
            if reasons and (not booking["approved"] or not set(reasons) <= set((booking["reason"] or "").splitlines())):
                with self.state.db:
                    if slot["start"] <= checked_at + APPROVAL_CUTOFF_SECONDS:
                        self.expire_booking_offer(booking, row, checked_at)
                    else:
                        reason = "\n".join(reasons)
                        self.state.booking_status(booking["id"], "approval", reason=reason)
                        self.send_booking_approval(booking, description, reason, slot["start"])
                return
            with self.state.db:
                self.state.booking_status(booking["id"], "submitting")
            reservation_id = (
                portal.reserve_standard_visit(appointment, service_name, questionnaire=receipt)
                if receipt else portal.reserve_standard_visit(appointment, service_name)
            )
            with self.state.db:
                self.state.booking_status(booking["id"], "verifying", reservation_id=reservation_id)
            if reservation_id not in portal.booked_visit_ids(telephone=criteria.telephone):
                raise PortalError("booking-outcome-unknown")
        except PortalError as error:
            if self.questionnaire_booking and self.questionnaire_booking.booking_id == booking["id"]:
                self.questionnaire_booking = None
            self.report_booking_failure(booking, description, error, reservation_id)
            return
        if questionnaire is not None:
            self.questionnaire_booking = None
        with self.state.db:
            self.state.booking_status(booking["id"], "booked", reservation_id=reservation_id)
            self.state.queue_message(f"{description}\nBooked and verified in LUX MED.", row["id"])
            if self.calendar_destination:
                self.state.queue_calendar_event(
                    booking["id"], uuid.uuid4().hex, self.calendar_destination,
                    int(start.timestamp()), int(end.timestamp()), int(self.clock()),
                )

    def cancellation_due(self) -> None:
        request = self.state.claim_cancellation()
        if request is None:
            return
        request_id, patient = request["id"], request["patient"]
        name = self.profiles[patient].name
        description = f"{name} · {local_time(request['starts_at'])} (Warsaw time)"
        submitting = False
        status = "blocked"
        buttons = None
        try:
            now = dt.datetime.fromtimestamp(self.clock(), WARSAW)
            if self.state.account_paused(patient) or not self.can_book_for(patient):
                raise PortalError("cancellation-access-paused")
            portal = self.portal_for(patient)
            identity = (request["reservation_id"], request["event_type"])
            visit = next((item for item in portal.scheduled_visits(now)
                          if (item.event_id, item.event_type) == identity), None)
            if (visit is None or not self.can_cancel_visit(patient, visit)
                    or self.visit_signature(visit) != request["signature"]):
                raise PortalError("cancellation-visit-changed")
            description = f"{name}\n{self.upcoming_visit_details(visit)}"
            approved_terms = CancellationTerms(request["late_hours"], bool(request["no_show"]))
            if portal.cancellation_terms(visit) != approved_terms:
                raise PortalError("cancellation-terms-changed")
            if visit.starts_at.timestamp() <= self.clock():
                raise PortalError("cancellation-visit-changed")
            with self.state.db:
                self.state.cancellation_status(request_id, "submitting")
            submitting = True
            portal.cancel_standard_visit(visit)
            if any(
                (item.event_id, item.event_type) == identity
                for item in portal.scheduled_visits(dt.datetime.fromtimestamp(self.clock(), WARSAW))
            ):
                raise PortalError("cancellation-outcome-unverified")
            status = "cancelled"
            message = "Cancelled and verified in LUX MED."
            self.appointment_list = None
            self.cancellation_review = None
        except PortalError as error:
            code = str(error)
            with self.state.db:
                self.handle_cancellation_access_error(patient, error)
            if submitting and code != "cancellation-refused":
                status = "unknown"
                message = "I couldn’t verify the cancellation. Check this visit in the Patient Portal."
            elif code == "cancellation-terms-changed":
                message = "LUX MED’s cancellation conditions changed. Select the visit again to review them."
                buttons = [[("Upcoming appointments", "pick visits")]]
            elif code == "cancellation-visit-changed":
                message = "This visit or its cancellation options changed. Open Upcoming appointments to check it again."
                buttons = [[("Upcoming appointments", "pick visits")]]
            elif code == "cancellation-refused":
                message = "LUX MED declined the cancellation. Check the reason in the Patient Portal."
            else:
                message = "I couldn’t verify the cancellation conditions. Check this visit in the Patient Portal."
            if buttons is None:
                message += f"\n{APPOINTMENTS_PORTAL_URL}"
        with self.state.db:
            self.state.cancellation_status(request_id, status)
            if status == "cancelled":
                self.state.mark_cancelled_booking(
                    patient, request["reservation_id"], int(self.clock()), telephone=request["event_type"] == 7,
                )
            self.state.queue_message(f"{description}\n{message}", buttons=buttons)

    def recover_cancellations(self) -> None:
        with self.state.db:
            for request in self.state.recover_cancellations():
                name = self.profiles[request["patient"]].name
                starts = local_time(request["starts_at"])
                self.state.queue_message(
                    f"{name} · {starts} (Warsaw time)\nI couldn’t verify the cancellation. "
                    f"Check this visit in the Patient Portal.\n{APPOINTMENTS_PORTAL_URL}",
                )

    def sync_calendar_due(self) -> None:
        if self.calendar is None or not self.calendar_destination:
            return
        now = int(self.clock())
        removal = self.state.due_calendar_removal(self.calendar_destination, now)
        if removal is not None:
            self.sync_calendar_removal(removal)
            return
        if next_search_time(now) != now:
            return
        event = self.state.due_calendar_event(self.calendar_destination, now)
        if event is None:
            return
        profile = self.profiles[event["patient"]].name
        appointment = f"{profile} · {local_time(event['starts_at'])} (Warsaw time)"
        failure = "I couldn’t recheck this booking with LUX MED."
        try:
            if self.state.account_paused(event["patient"]):
                raise CalendarUnavailable
            portal = self.portal_for(event["patient"])
            if event["reservation_id"] not in portal.booked_visit_ids(telephone=bool(event["telephone"])):
                raise CalendarUnavailable
            failure = "I couldn’t confirm the Google Calendar event yet."
            details = {}
            if event["patient"] in self.calendar_detail_patients:
                result = self.state.results(event["search_id"])
                if result is not None:
                    slot = json.loads(result["slots"])[event["option_number"] - 1]
                    _, clinic, clinician = self.slot_key(slot)
                    details = {
                        "service": result["service"], "clinic": clinic, "clinician": clinician,
                        "telephone": bool(event["telephone"]),
                    }
            self.calendar.add_booking(
                self.calendar_destination, event["event_id"],
                dt.datetime.fromtimestamp(event["starts_at"], WARSAW),
                dt.datetime.fromtimestamp(event["ends_at"], WARSAW),
                patient_name=profile, **details,
            )
        except (CalendarUnavailable, PortalError) as error:
            retry = next_search_time(now + CALENDAR_RETRY_SECONDS)
            with self.state.db:
                if isinstance(error, PortalError) and error.suspend:
                    self.close_portal(event["patient"])
                    if error.stage == "login" or str(error) == "rate-limited":
                        self.state.suspend(event["patient"])
                self.state.finish_calendar_attempt(event["booking_id"], retry, False)
                if retry >= event["ends_at"]:
                    followup = "Check Google Calendar for this appointment."
                elif event["attempts"] == 0:
                    followup = "I’ll retry automatically."
                else:
                    return
                self.state.queue_message(f"{appointment}\n{failure} {followup}", event["search_id"])
            return
        with self.state.db:
            self.state.finish_calendar_attempt(event["booking_id"], now, True)
            self.state.queue_message(f"{appointment}\nAdded the appointment to Google Calendar.", event["search_id"])

    def sync_calendar_removal(self, event) -> None:
        profile = self.profiles[event["patient"]].name
        appointment = f"{profile} · {local_time(event['starts_at'])} (Warsaw time)"
        try:
            self.calendar.remove_booking(event["calendar_id"], event["event_id"])
        except CalendarUnavailable:
            retry = int(self.clock()) + CALENDAR_RETRY_SECONDS
            with self.state.db:
                self.state.finish_calendar_removal(event["booking_id"], retry, False)
                if event["attempts"] == 0:
                    self.state.queue_message(
                        f"{appointment}\nThe visit was cancelled. I’ll retry removing its Google Calendar event."
                    )
            return
        with self.state.db:
            self.state.finish_calendar_removal(event["booking_id"], int(self.clock()), True)
            self.state.queue_message(f"{appointment}\nCleared the matching entry from Google Calendar.")

    @staticmethod
    def refresh_booking_slot(portal, locator: DoctorLocator, slot: dict, *, nationwide_from: str | None = None,
                             questionnaire: QuestionnaireReceipt | None = None):
        date = dt.datetime.fromtimestamp(slot["start"], WARSAW).date()
        telephone = nationwide_from is not None
        parameters = {"date": date, "nationwide_from": nationwide_from}
        if questionnaire is not None:
            parameters["questionnaire"] = questionnaire
        availability = portal.search(locator, **parameters)
        matches = [
            item for item in availability
            if int(item.AppointmentDate.timestamp()) == slot["start"]
            and item.clinic_id == slot["clinic_id"] and item.doctor_id == slot["doctor_id"]
            and item.service_id == slot["service_id"]
        ]
        if len(matches) != 1:
            raise PortalError("slot-gone")
        appointment = matches[0]
        if appointment.requires_preparation:
            raise PortalError("portal-preparation")
        if appointment.is_additional:
            raise PortalError("portal-additional", stage="slot-recheck")
        term = appointment.booking_term
        if term is None or term.get("isTelemedicine", False) is not telephone:
            raise PortalError("booking-details-missing", stage="slot-recheck")
        if not telephone:
            portal.check_booking_city(appointment, int(locator.city_id))
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
        account_blocked = error.suspend and (error.stage == "login" or error.http_status == 429 or code == "rate-limited")
        booking_access_denied = error.suspend and not account_blocked and (
            code in {"access-denied", "session-unconfirmed"} or error.http_status in (401, 403)
        )
        step = BOOKING_CHECK_LABELS.get(error.stage, "booking check")
        status = f" (HTTP {error.http_status})" if error.http_status is not None else ""
        if error.stage is not None:
            print(f"LUX MED booking request {booking['id']} stopped at {step}{status}.", file=sys.stderr)
        with self.state.db:
            if error.suspend:
                self.close_portal(booking["patient"])
            if account_blocked:
                self.state.suspend(booking["patient"])
            elif booking_access_denied:
                self.state.pause_booking(booking["patient"])
            self.state.booking_status(booking["id"], "unknown" if unknown else "ended", reservation_id=reservation_id)
            portal_link = True
            buttons = None
            if unknown:
                message = (
                    "LUX MED’s booking outcome is unclear. Check your appointments in the Patient Portal. "
                    f"Once you've checked, reply Reviewed {booking['id']} to allow requests for other slots."
                )
            elif code == "booking-penalty":
                message = "LUX MED reported a no-show restriction. Check the Patient Portal before booking."
            elif code == "prior-booking-unverified":
                message = "Check the earlier unverified booking in the Patient Portal first."
            elif booking_access_denied:
                message = f"LUX MED denied the {step}{status}. {BOOKING_ACCESS_NOTICE}"
                portal_link = False
            elif code == "booking-access-paused":
                message = BOOKING_ACCESS_NOTICE
                portal_link = False
            elif code in {"questionnaire-expired", "questionnaire-open", "booking-location-unverified"}:
                message = BOOKING_FAILURES[code]
                portal_link = False
            elif code == "portal-additional":
                message = ADDITIONAL_SLOT_NOTICE
                buttons = [[("More", "more"), ("Continue search", "continue")]]
            elif code == "rate-limited":
                message = "LUX MED rate-limited this account. Try /resume later."
                portal_link = False
            elif account_blocked or code == "booking-account-paused":
                message = "LUX MED access is paused. Check the Patient Portal and complete any verification there."
            elif code in {"slot-gone", "booking-details-missing", "booking-hold-unavailable", "network-error", "portal-error"} or error.suspend:
                message = BOOKING_FAILURES.get(code, f"The {step} failed{status}. Availability searches can continue.")
                if code in {"booking-details-missing", "booking-hold-unavailable"}:
                    message = f"The {step} could not verify the selected slot{status}. Use More or Continue to try another slot."
                if not error.suspend and code in {"slot-gone", "booking-details-missing", "booking-hold-unavailable"}:
                    buttons = [[("More", "more"), ("Continue search", "continue")]]
                portal_link = False
            else:
                message = BOOKING_FAILURES.get(code, "The booking check failed. Check the Patient Portal.")
            link = f"\n{PATIENT_PORTAL_URL}" if portal_link else ""
            self.state.queue_message(f"{description}\n{message}{link}", booking["search_id"], buttons=buttons)

    def recover_bookings(self) -> None:
        with self.state.db:
            for booking in self.state.booking_recovery():
                if booking["status"] == "questionnaire":
                    self.state.booking_status(booking["id"], "ended")
                    self.state.queue_message(QUESTIONNAIRE_EXPIRED, booking["search_id"])
                elif booking["status"] == "checking":
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
        regular_phone = self.is_nationwide_phone_search(criteria)
        try:
            service_id = portal.resolve_service(
                criteria.service, catalog, telephone=criteria.telephone, regular_phone=regular_phone,
            )
        except PortalError as error:
            if str(error) not in {"service-not-in-catalog", "ambiguous-service"}:
                raise
            variants = list(service_variants(catalog))
            if regular_phone:
                variants = [variant for variant in variants
                            if variant.get("isVideoConsultation") is False and variant.get("isPoz") is False]
            matches = matching_catalog_variants(criteria.service, variants, telephone=criteria.telephone)
            if not matches and str(error) == "service-not-in-catalog":
                choices = nearby_catalog_variants(criteria.service, variants, telephone=criteria.telephone)
                if choices:
                    self.offer_catalog_service(row, criteria, choices)
                    return
                alternatives = matching_catalog_variants(criteria.service, variants, telephone=not criteria.telephone)
                if alternatives:
                    self.offer_catalog_service(
                        row, criteria, alternatives,
                        prompt="I found another visit type for this service. Would you like one of these options?",
                    )
                    return
            if len({item["name"] for item in matches}) > 1:
                self.offer_catalog_service(row, criteria, matches, prompt="Several LUX MED services match. Which one did you mean?")
                return
            if len(matches) != 1:
                heading = self.search_heading(row["display_number"], criteria)
                self.report_catalog_choices(search_id, heading, matches, ambiguous=str(error) == "ambiguous-service")
                return
            service, = matches
            service_id = service["id"]
            criteria = replace(criteria, service=service["name"], telephone=service["isTelemedicine"])

        try:
            check_patient_service(criteria, self.children)
        except PatientServiceMismatch as error:
            with self.state.db:
                if self.state.finish(search_id, "service-mismatch"):
                    self.reject_mismatch(error, search_id)
            return
        if (criteria.service, criteria.telephone) != (row["service"], bool(row["telephone"])):
            with self.state.db:
                self.state.resolve_search_service(search_id, criteria.service, criteria.telephone)

        heading = self.search_heading(row["display_number"], criteria)
        locator = DoctorLocator(id=f"{row['city_id']}*{service_id}*-1*-1")
        try:
            if self.is_nationwide_phone_search(criteria):
                appointments = portal.search(locator, nationwide_from=criteria.city)
            else:
                appointments = portal.search(locator)
            slots, outside_clinic = matching_slots(appointments, criteria.preferences)
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
            if slot.is_additional:
                option["is_additional"] = True
            key = self.slot_key(option)
            if key not in excluded:
                excluded.add(key)
                options.append(option)
        if (self.can_book_for(criteria.patient)
                and self.supports_booking_service(criteria.service, criteria.telephone, criteria.patient)
                and not self.state.booking_paused(criteria.patient)):
            options.sort(key=lambda option: option.get("is_additional") is True)
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

    def offer_catalog_service(self, row, criteria: Criteria, matches: list[dict], *, prompt: str = "") -> None:
        choices = [{"service": item["name"], "telephone": item["isTelemedicine"]} for item in matches[:5]]
        heading = self.search_heading(row["display_number"], criteria)
        with self.state.db:
            if self.state.finish(row["id"], "service-unresolved"):
                self.show_catalog_service_menu({
                    "criteria": asdict(criteria), "choices": choices, "heading": heading,
                    "search_id": row["id"], "prompt": prompt,
                })

    def show_catalog_service_menu(self, suggestion: dict) -> None:
        if "menu" not in suggestion:
            suggestion["menu"] = uuid.uuid4().hex[:12]
            self.state.set_setting("pending_service", json.dumps(suggestion))
        menu = suggestion["menu"]
        choices = suggestion["choices"]
        prompt = suggestion.get("prompt")
        if not prompt and len(choices) == 1:
            prompt = f"Did you mean {choices[0]['service']}? Reply Service 1 to search for it."
        lines = [suggestion.get("heading", ""), prompt or "Which LUX MED service did you mean?"]
        buttons = []
        for number, choice in enumerate(choices, 1):
            service = choice["service"]
            kind = "In person"
            if choice["telephone"]:
                kind = "Phone" if requests_phone(service) else "Remote"
            lines.append(f"{number}. {service} · {kind}")
            buttons.append([(f"{number}. {kind} · {service}"[:64], f"service {number} {menu}")])
        self.state.queue_message("\n".join(filter(None, lines)), suggestion.get("search_id"), buttons=buttons)

    def choose_catalog_service(self, argument: str, *, menu_id: str | None = None) -> None:
        pending = self.state.get_setting("pending_service")
        if not pending:
            self.state.queue_message("I don’t have a service choice open. Tell me what appointment to look for.")
            return
        suggestion = json.loads(pending)
        if menu_id is not None and suggestion.get("menu") != menu_id:
            self.state.queue_message("That service choice has expired. Ask for a fresh search.")
            return
        choices = suggestion["choices"]
        if not re.fullmatch(r"[1-9]", argument) or int(argument) > len(choices):
            self.state.queue_message("Reply Service followed by one of the numbers shown.")
            return
        fields = suggestion["criteria"]
        fields.update(choices[int(argument) - 1])
        fields["preferences"] = Preferences(**fields["preferences"])
        criteria = Criteria(**fields)
        try:
            check_patient_service(criteria, self.children)
        except PatientServiceMismatch as error:
            self.state.set_setting("pending_service", "")
            self.reject_mismatch(error)
            return
        self.state.set_setting("pending", "")
        self.start(criteria)

    def report_catalog_choices(self, search_id: int, heading: str, matches: list[dict], *, ambiguous: bool) -> None:
        labels = list(dict.fromkeys(variant["name"] for variant in matches))
        if len(labels) > 1:
            status = "\n".join([
                "Several LUX MED services match. Use one of these names in your request.",
                *(f"• {label}" for label in labels[:5]),
            ])
            if len(labels) > 5:
                status += "\nAdd more words to narrow the list."
        elif ambiguous or matches:
            status = "LUX MED lists variants with the same name. Use a more specific service name or choose in the Patient Portal."
        else:
            status = "I couldn't find that service in this patient's LUX MED catalog. Try a specialty or the portal service name."
        with self.state.db:
            if self.state.finish(search_id, "service-unresolved"):
                self.state.queue_message(f"{heading}\n{status}", search_id)


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
        calendar_detail_patients = frozenset(
            value.strip() for value in os.environ.get("LUXMED_CALENDAR_DETAILS_PATIENTS", "").split(",")
            if value.strip()
        )
        if not calendar_detail_patients.issubset(PROFILE_KEYS):
            raise ValueError("Invalid calendar details patient list")
        bot = SearchBot(Telegram(token, owner), SearchState(path / "telegram.sqlite3"), profiles,
                        free_text=interpreter, calendar=calendar,
                        public_prices=PublicPriceList(),
                        booking_enabled=bool(allowed_booking_patients),
                        device_credentials=trusted_devices(profiles),
                        allowed_booking_patients=allowed_booking_patients,
                        calendar_destination=calendar_config.get("destination_calendar_id") if calendar_config else None,
                        calendar_detail_patients=calendar_detail_patients)
    except (KeyError, ValueError, OSError):
        print("Telegram bot configuration is incomplete.", file=sys.stderr)
        return 2

    try:
        return run_worker(bot)
    finally:
        bot.close_portals()


def run_worker(bot: SearchBot) -> int:
    bot.recover_bookings()
    bot.recover_cancellations()
    while True:
        try:
            updates = bot.telegram.poll(int(bot.state.get_setting("offset", "0")))
            for update in updates:
                bot.accept(update)
            bot.flush()
            bot.cancellation_due()
            bot.flush()
            bot.booking_due()
            bot.flush()
            bot.sync_calendar_due()
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
