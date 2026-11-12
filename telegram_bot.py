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
from telegram_requests import Criteria, RequestError, city_catalog, normalize, parse_search, pediatric_choice, pediatric_menu
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


def localized(polish: bool, english: str, translated: str) -> str:
    return translated if polish else english


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
        polish = command in {"/szukaj", "znajdz", "szukaj", "poszukaj", "zatrzymaj", "/zatrzymaj", "aktywne", "/aktywne", "wznow", "/wznow", "/zmien", "zmien"}
        if command in {"stop", "/stop", "zatrzymaj", "/zatrzymaj"}:
            self.stop(argument, polish, message.get("reply_to_message") or {})
        elif key in {"/active", "active", "show active searches", "show running searches", "/aktywne", "aktywne", "pokaz aktywne"}:
            self.show_active(polish or key == "pokaz aktywne")
        elif command in {"/change", "change", "/zmien", "zmien"}:
            self.change(text, polish)
        elif command in {"/resume", "resume", "/wznow", "wznow"}:
            patient = self.aliases.get(argument)
            if patient and self.state.resume(patient):
                self.state.queue_message(localized(polish, "Searches resumed for the next eligible hour.", "Wznowiono wyszukiwania na najbliższą dostępną godzinę."))
            else:
                self.state.queue_message(localized(polish, "Use /resume followed by a paused patient's name.", "Użyj /wznow i podaj pacjenta ze wstrzymanym wyszukiwaniem."))
        elif len(text) <= 600 and key not in {"/start", "/help", "help", "/pomoc", "pomoc"}:
            self.handle_search(text)
        else:
            self.help()

    def handle_search(self, text: str) -> None:
        error_message = None
        try:
            criteria = parse_search(text, self.aliases, self.children, self.cities)
        except RequestError as error:
            criteria = None
            error_message = str(error)
        pending = self.state.get_setting("pending")
        if criteria is None and pending and error_message is None:
            candidate = Criteria(**json.loads(pending))
            if service := pediatric_choice(text, candidate.polish):
                criteria = replace(candidate, service=service, telephone="telefonicz" in normalize(service))
        if criteria is None and self.free_text is not None and not text.startswith("/"):
            try:
                criteria = self.free_text.criteria(text, self.aliases, self.children, self.cities)
            except RequestError as error:
                error_message = str(error)
        if criteria and not criteria.service:
            self.state.set_setting("pending", json.dumps(asdict(criteria)))
            self.state.queue_message(pediatric_menu(criteria.polish))
        elif criteria:
            self.state.set_setting("pending", "")
            self.start(criteria)
        elif error_message:
            self.state.queue_message(error_message)
        elif pending:
            self.state.queue_message(pediatric_menu(Criteria(**json.loads(pending)).polish))
        else:
            self.help()

    def help(self) -> None:
        self.state.queue_message(
            "Read-only availability / Wyszukiwanie terminów\n"
            "Find an internist for <patient>\nZnajdź pediatrę dla <pacjent>\n"
            "/find patient | portal service | city\n/szukaj pacjent | usługa | miasto\n"
            "/active /aktywne · Stop <number> / Zatrzymaj <numer>\n"
            "/change number | service | city\n/zmień numer | usługa | miasto",
        )

    def start(self, criteria: Criteria) -> None:
        now = int(self.clock())
        search_id, due, paused = self.state.add(criteria, now)
        self.confirm_search(search_id, criteria, due, paused, now)

    def confirm_search(self, search_id: int, criteria: Criteria, due: int, paused: bool, now: int, *, changed: bool = False) -> None:
        profile = self.profiles[criteria.patient]
        if paused:
            status = localized(criteria.polish, "Paused. Check the portal, then use /resume.", "Wstrzymano. Sprawdź Portal Pacjenta, a potem użyj /wznow.")
        elif due > now:
            status = localized(criteria.polish, f"Queued for {local_time(due)} Warsaw time.", f"W kolejce na {local_time(due)} czasu polskiego.")
        else:
            status = localized(criteria.polish, "Checking availability. I’ll send the first result when the check finishes.", "Sprawdzam dostępność. Wyślę pierwszy wynik po zakończeniu sprawdzania.")
        prefix = localized(criteria.polish, "Updated\n", "Zmieniono\n") if changed else ""
        self.state.queue_message(f"{prefix}#{search_id} · {profile.name} · {criteria.service} · {criteria.city}\n{status}", search_id)

    def change(self, text: str, polish: bool) -> None:
        parts = [part.strip() for part in text.split("|")]
        number = parts[0].split(maxsplit=1)
        if len(text) > 600 or len(parts) not in (2, 3) or len(number) != 2 or not re.fullmatch(r"#?[0-9]{1,18}", number[1]):
            self.state.queue_message(localized(polish, "Use /change number | service | city.", "Użyj /zmień numer | usługa | miasto."))
            return
        search_id = int(number[1].lstrip("#"))
        row = next((item for item in self.state.active() if item["id"] == search_id), None)
        if row is None:
            self.state.queue_message(localized(polish, f"Search #{search_id} is inactive.", f"Wyszukiwanie #{search_id} jest nieaktywne."))
            return
        service = parts[1] or row["service"]
        city = parts[2] if len(parts) == 3 and parts[2] else row["city"]
        command = "/szukaj" if polish else "/find"
        try:
            criteria = parse_search(f"{command} {row['patient']} | {service} | {city}", self.aliases, self.children, self.cities)
        except RequestError as error:
            self.state.queue_message(str(error))
            return
        if not criteria.service:
            self.state.queue_message(localized(polish, "Choose a specific pediatric visit type in /change, such as routine visit.", "Podaj konkretny rodzaj wizyty pediatrycznej w /zmień, np. bilans."))
            return
        if (criteria.service, criteria.city_id, criteria.telephone) == (row["service"], row["city_id"], bool(row["telephone"])):
            self.state.queue_message(localized(polish, f"Search #{search_id} already uses those settings.", f"Wyszukiwanie #{search_id} ma już te ustawienia."))
            return
        now = int(self.clock())
        due, paused = self.state.revise(search_id, criteria, now)
        self.confirm_search(search_id, criteria, due, paused, now, changed=True)

    def show_active(self, polish: bool) -> None:
        rows = self.state.active()
        if not rows:
            self.state.queue_message(localized(polish, "No active searches.", "Brak aktywnych wyszukiwań."))
            return
        for index in range(0, len(rows), 10):
            lines = []
            for row in rows[index:index + 10]:
                heading = f"#{row['id']} · {self.profiles[row['patient']].name} · {row['service']} · {row['city']}"
                if row["last_checked"] is None:
                    last = localized(polish, "Awaiting a recorded result", "Oczekuję na zapisany wynik")
                else:
                    result = {
                        "no-slots": localized(polish, "no matching slots", "brak pasujących terminów"),
                        "paused": localized(polish, "paused", "wstrzymano"),
                        "temporary-error": localized(polish, "temporary error", "błąd tymczasowy"),
                    }.get(row["last_result"], localized(polish, "checked", "sprawdzono"))
                    last = localized(polish, f"Last check {local_time(row['last_checked'])}: {result}", f"Ostatnie sprawdzenie {local_time(row['last_checked'])}: {result}")
                next_at = local_time(row["next_check"])
                upcoming = localized(polish, f"Next check {next_at} Warsaw time", f"Następne sprawdzenie {next_at} czasu polskiego")
                if row["suspended"]:
                    upcoming = localized(polish, "Paused. Use /resume after checking the portal.", "Wstrzymano. Sprawdź portal i użyj /wznow.")
                lines.append(f"{heading}\n{last}\n{upcoming}")
            self.state.queue_message("\n\n".join(lines))

    def stop(self, argument: str, polish: bool, reply: dict) -> None:
        search_id = None
        if argument:
            if re.fullmatch(r"#?[0-9]{1,18}", argument):
                search_id = int(argument.lstrip("#"))
        elif type(reply.get("message_id")) is int:
            search_id = self.state.search_for_message(reply["message_id"])
        elif self.state.get_setting("pending"):
            self.state.set_setting("pending", "")
            self.state.queue_message(localized(polish, "Pending selection cleared.", "Anulowano wybór rodzaju wizyty."))
            return
        else:
            rows = self.state.active()
            if len(rows) == 1:
                search_id = rows[0]["id"]
        if search_id is None:
            self.state.queue_message(localized(polish, "Use Stop with a search number from /active.", "Użyj Zatrzymaj z numerem z /aktywne."))
        elif self.state.finish(search_id, "stopped"):
            self.state.queue_message(localized(polish, f"Stopped search #{search_id}.", f"Zatrzymano wyszukiwanie #{search_id}."))
        else:
            self.state.queue_message(localized(polish, f"Search #{search_id} is inactive.", f"Wyszukiwanie #{search_id} jest nieaktywne."))

    def flush(self) -> None:
        while (message := self.state.next_message()) is not None:
            sent = self.telegram.send(message["text"])
            self.state.sent(message, sent)

    def search_due(self) -> None:
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
                    status = localized(first["polish"], "Searches paused. Check the official portal before using /resume.", "Wstrzymano wyszukiwania. Sprawdź Portal Pacjenta przed użyciem /wznow.")
                else:
                    status = localized(first["polish"], "LUX MED check failed. The next attempt will be in an eligible hour.", "Wyszukiwanie w LUX MED nie powiodło się. Ponowię próbę w najbliższym dostępnym oknie.")
                self.state.queue_message(f"{profile.name} · {status}")

    def search_one(self, portal: LuxMedPortal, catalog: list, row) -> None:
        search_id = row["id"]
        try:
            service_id = portal.resolve_service(row["service"], catalog, telephone=bool(row["telephone"]))
        except PortalError as error:
            if str(error) not in {"service-not-in-catalog", "ambiguous-service"}:
                raise
            with self.state.db:
                if self.state.finish(search_id, "service-unresolved"):
                    status = localized(row["polish"], "I couldn't match a unique service. Try its exact portal label.", "Nie udało się dopasować jednej usługi. Podaj dokładną nazwę z Portalu Pacjenta.")
                    self.state.queue_message(f"#{search_id} · {status}", search_id)
            return

        locator = DoctorLocator(id=f"{row['city_id']}*{service_id}*-1*-1")
        slots = portal.search(locator)
        if not slots:
            with self.state.db:
                next_check = self.state.record_result(search_id, int(self.clock()), "no-slots")
                if next_check is not None and self.state.mark_first_result(search_id):
                    name = self.profiles[row["patient"]].name
                    due = local_time(next_check)
                    status = localized(
                        row["polish"],
                        f"No matching slots in the next 14 days. I’ll check hourly from 06:00 to 22:00 Warsaw time and message you if I find one. Next check: {due}.",
                        f"Brak pasujących terminów w ciągu 14 dni. Będę sprawdzać co godzinę od 06:00 do 22:00 czasu polskiego i dam znać, gdy coś znajdę. Następne sprawdzenie: {due}.",
                    )
                    self.state.queue_message(f"#{search_id} · {name} · {row['service']} · {row['city']}\n{status}", search_id)
            return
        with self.state.db:
            if not self.state.finish(search_id, "found"):
                return
            name = self.profiles[row["patient"]].name
            heading = localized(row["polish"], "Available slots", "Dostępne terminy")
            lines = [f"#{search_id} · {name} · {row['service']} · {row['city']}", heading]
            for slot in sorted(slots, key=lambda item: item.AppointmentDate)[:3]:
                start = slot.AppointmentDate.astimezone(WARSAW).strftime("%d.%m %H:%M")
                details = [start, slot.ClinicPublicName, slot.DoctorName]
                lines.append(" · ".join(str(value)[:160].strip() for value in details if value))
            lines.append(localized(row["polish"], "Search finished. Use the Patient Portal to book.", "Wyszukiwanie zakończone. Rezerwację wykonasz w Portalu Pacjenta."))
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
