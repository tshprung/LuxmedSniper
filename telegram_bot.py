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
from telegram_configuration import PROFILE_KEYS, validate, worker_settings
from telegram_free_text import FreeText
from telegram_preferences import PreferenceError, Preferences, matching_slots, parse_preferences, split_preferences
from telegram_requests import Criteria, PatientServiceMismatch, RequestError, city_catalog, normalize, parse_search, pediatric_choice, pediatric_menu
from telegram_state import SearchState, next_search_time


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
    def __init__(self, telegram: Telegram, state: SearchState, profiles: dict[str, Profile], *, clock=time.time, free_text: FreeText | None = None):
        self.telegram = telegram
        self.state = state
        self.profiles = profiles
        self.clock = clock
        self.free_text = free_text
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
        elif command in {"/resume", "resume"}:
            patient = self.aliases.get(argument)
            if patient and self.state.resume(patient):
                self.state.queue_message("Searches resumed for the next eligible hour.")
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
        self.state.queue_message(
            "Read-only appointment search\n"
            "Find an internist for <patient>\nFind a pediatrician for <patient>\n"
            "Find an internist for <patient> next week after 16:00\n"
            "/find patient | portal service | city | optional filters\n"
            "/active · Stop <number>\n"
            "Change 123 to after 16:00\n/change number | service | city | optional filters",
        )

    def start(self, criteria: Criteria) -> None:
        now = int(self.clock())
        search_id, number, due, paused = self.state.add(criteria, now)
        self.confirm_search(search_id, number, criteria, due, paused, now)

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
        profile = self.profiles[first["patient"]]
        index = 0
        try:
            portal = LuxMedPortal(profile.credentials)
            with portal.session:
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
                    self.state.suspend(first["patient"])
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
        if not slots:
            with self.state.db:
                result = "outside-preferred-clinic" if outside_clinic else "no-slots"
                next_check = self.state.record_result(search_id, int(self.clock()), result)
                if next_check is not None and self.state.mark_first_result(search_id):
                    due = local_time(next_check)
                    window = "the requested dates" if criteria.preferences.first_date else "the next 14 days"
                    status = f"No matching slots in {window}."
                    if outside_clinic:
                        status += " Other clinics have matches. Use /change to adjust the clinic filter."
                    status += f" I’ll check hourly from 06:00 to 22:00 Warsaw time and message you if I find one. Next check: {due}."
                    self.state.queue_message(f"{heading}\n{status}", search_id)
            return
        with self.state.db:
            if not self.state.finish(search_id, "found"):
                return
            lines = [heading, "Available slots"]
            for slot in slots[:3]:
                start = slot.AppointmentDate.astimezone(WARSAW).strftime("%d.%m %H:%M")
                details = [start, slot.ClinicPublicName, slot.DoctorName]
                lines.append(" · ".join(str(value)[:160].strip() for value in details if value))
            lines.append("Search finished. Use the Patient Portal to book.")
            self.state.queue_message("\n".join(lines), search_id)


def main() -> int:
    os.umask(0o077)
    try:
        settings = worker_settings()
        profiles = configured_profiles(settings, validate(settings))
        token = settings["TELEGRAM_BOT_TOKEN"]
        owner = int(settings["TELEGRAM_OWNER_ID"])
        path = Path(os.environ.get("LUXMED_STATE_DIRECTORY", "./data"))
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = settings.get("OPENAI_API_KEY", "").strip()
        interpreter = FreeText(key) if key else None
        bot = SearchBot(Telegram(token, owner), SearchState(path / "telegram.sqlite3"), profiles, free_text=interpreter)
    except (KeyError, ValueError, OSError):
        print("Telegram bot configuration is incomplete.", file=sys.stderr)
        return 2

    while True:
        try:
            updates = bot.telegram.poll(int(bot.state.get_setting("offset", "0")))
            for update in updates:
                bot.accept(update)
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
