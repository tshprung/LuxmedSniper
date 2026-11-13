import argparse
import asyncio
import base64
import copy
import datetime
import json
import logging
import os
import subprocess
import sys
import time
import typing
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import schedule
import yaml
from loguru import logger
from pydantic import BaseModel, Field, model_validator

WARSAW = ZoneInfo("Europe/Warsaw")
PORTAL_TIMEOUT = (5, 15)
IN_PERSON_SERVICE_NAMES = (
    {"internista", "konsultacja internisty", "konsultacja internistyczna"},
    {
        "pediatra - gabinet dzieci zdrowych",
        "konsultacja w gabinecie dzieci zdrowych",
        "konsultacja pediatry w gabinecie dzieci zdrowych",
        "konsultacja pediatryczna - gabinet dzieci zdrowych",
        "konsultacja pediatryczna - w gabinecie dzieci zdrowych",
    },
)


class DoctorLocator(BaseModel):
    id: str
    name: str | None = None
    enabled: bool = True

    city_id: str
    service_id: str
    clinic_ids: list[int] = Field(default_factory=list)
    doctor_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _parse_from_id_and_config(cls, data: Any, info):
        if not isinstance(data, dict):
            return data

        raw_id = str(data.get("id", "")).strip()
        try:
            city_id, service_id, clinic_ids_str, doctor_ids_str = raw_id.split("*")
        except ValueError as e:
            raise ValueError(
                "doctor_locator.id must have format: cityId*serviceId*facilitiesIds*doctorsIds (example: 1*7409*-1*-1)",
            ) from e

        def _parse_ids(s: str) -> list[int]:
            return [i for i in map(int, s.split(",")) if i != -1]

        clinic_ids = _parse_ids(clinic_ids_str)
        doctor_ids = _parse_ids(doctor_ids_str)

        luxmedsniper_config = (info.context or {}).get("luxmedsniper_config", {}) if info else {}
        clinic_ids += list(luxmedsniper_config.get("facilities_ids", []) or [])

        return {
            **data,
            "city_id": city_id,
            "service_id": service_id,
            "clinic_ids": clinic_ids,
            "doctor_ids": doctor_ids,
            "name": data.get("name") or raw_id,
            "enabled": data.get("enabled", True),
        }


@dataclass
class Appointment:
    AppointmentDate: datetime.datetime
    clinic_id: int
    ClinicPublicName: str
    DoctorName: str
    service_id: int
    doctor_search_name: str = ""
    doctor_id: int | None = None
    booking_term: dict | None = field(default=None, repr=False)
    requires_preparation: bool = False


class Luxmed(BaseModel):
    password: str
    login: str | None = None
    email: str | None = None

    model_config = {"validate_assignment": True, "hide_input_in_errors": True}

    @model_validator(mode="before")
    @classmethod
    def _validate_login_or_email(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        login = data.get("login")
        email = data.get("email")

        # exactly one of login/email must be provided
        if bool(login) == bool(email):
            raise ValueError("Provide exactly one of: luxmed.login or luxmed.email")

        # email is deprecated but still supported
        if email and not login:
            logger.warning("Config field luxmed.email is deprecated; use luxmed.login instead.")
            data["login"] = email

        return data


class LuxmedSniper(BaseModel):
    doctor_locators: list[DoctorLocator]
    lookup_time_days: int
    facilities_ids: list[int]
    notification_provider: list[str]


class LuxMedPortal:
    NEW_PORTAL_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal'
    LUXMED_LOGIN_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/Account/LogIn'
    NEW_PORTAL_RESERVATION_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/terms/index'

    DICTIONARY_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Dictionary'
    DICTIONARY_CITIES_URL = f'{DICTIONARY_URL}/cities'
    DICTIONARY_SERVICES_URL = f'{DICTIONARY_URL}/serviceVariantsGroups'
    DICTIONARY_FACILITIES_AND_DOCTORS = f'{DICTIONARY_URL}/facilitiesAndDoctors'
    UPCOMING_VISITS_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/DashboardMedicalSection/GetUpcomingVisits'
    NON_SHOW_PENALTIES_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Reservation/CheckUserNonShowPenalties'

    def __init__(self, credentials: Luxmed):
        self.session = requests.Session()
        try:
            data = self._portal_json(
                "POST",
                self.LUXMED_LOGIN_URL,
                json={"login": credentials.login, "password": credentials.password},
                authenticating=True,
            )
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
                raise PortalError("login-unconfirmed", suspend=True)
            self.token_expires_at = self._token_expiry(token)
            self.session.headers["authorization-token"] = f"Bearer {token}"
        except BaseException:
            self.session.close()
            raise

    @staticmethod
    def _token_expiry(token: str) -> int | None:
        # This hint only shortens reuse. The portal still verifies the session.
        if token.count(".") != 2:
            return None
        _, payload, _ = token.split(".")
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, RecursionError):
            return None
        expires_at = claims.get("exp") if isinstance(claims, dict) else None
        return expires_at if type(expires_at) is int and expires_at > 0 else None

    def _portal_json(self, method: str, url: str, *, authenticating: bool = False,
                     stage: str | None = None, **kwargs: Any) -> Any:
        try:
            response = self.session.request(
                method, url, timeout=PORTAL_TIMEOUT, allow_redirects=False, **kwargs,
            )
        except requests.RequestException:
            raise PortalError("network-error", stage=stage) from None
        status = response.status_code
        error_context = {"stage": stage, "http_status": status}
        if status == 429:
            raise PortalError("rate-limited", suspend=True, **error_context)
        if status in (401, 403):
            raise PortalError("access-denied", suspend=True, **error_context)
        if 300 <= status < 400:
            raise PortalError("login-unconfirmed" if authenticating else "session-unconfirmed", suspend=True, **error_context)
        if status != 200:
            raise PortalError("portal-error", suspend=authenticating and status < 500, **error_context)
        try:
            return response.json()
        except ValueError:
            raise PortalError(
                "login-unconfirmed" if authenticating else "unexpected-response",
                suspend=True,
                **error_context,
            ) from None

    def search(self, locator: DoctorLocator, *, days: int = 14, date: datetime.date | None = None) -> list[Appointment]:
        now = datetime.datetime.now(tz=datetime.UTC)
        end = now + datetime.timedelta(days=days)
        params = {
            "searchPlace.id": locator.city_id,
            "searchPlace.type": 0,
            "serviceVariantId": locator.service_id.split(",")[0],
            "languageId": 10,
            "searchDateFrom": (date or now.astimezone(WARSAW).date()).isoformat(),
            "searchDateTo": (date or end.astimezone(WARSAW).date()).isoformat(),
            "searchDatePreset": 1 if date else days,
            "delocalized": "false",
            "processId": str(uuid.uuid4()),
        }
        if locator.clinic_ids:
            params["facilitiesIds"] = locator.clinic_ids
        if locator.doctor_ids:
            params["doctorsIds"] = locator.doctor_ids
        content = self._portal_json("GET", self.NEW_PORTAL_RESERVATION_URL, params=params)
        try:
            appointments = self._parse_visits_new_portal(content, locator)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise PortalError("unexpected-response", suspend=True) from None
        return [
            appointment for appointment in appointments
            if now <= appointment.AppointmentDate <= end
            and (date is None or appointment.AppointmentDate.date() == date)
        ]

    @staticmethod
    def _parse_visits_new_portal(content: Any, locator: DoctorLocator) -> list[Appointment]:
        appointments = []
        terms_for_service = content["termsForService"]
        days = terms_for_service["termsForDays"]
        if not isinstance(days, list):
            raise ValueError("Expected a list of days")
        additional = terms_for_service.get("additionalData")
        requires_preparation = isinstance(additional, dict) and additional.get("isPreparationRequired") is True
        for day in days:
            terms = day["terms"]
            if not isinstance(terms, list):
                raise ValueError("Expected a list of slots")
            for term in terms:
                doctor = term["doctor"]
                clinic_id = int(term["clinicGroupId"])
                doctor_id = int(doctor["id"])
                if locator.doctor_ids and doctor_id not in locator.doctor_ids:
                    continue
                if locator.clinic_ids and clinic_id not in locator.clinic_ids:
                    continue
                starts_at = datetime.datetime.fromisoformat(term["dateTimeFrom"])
                if starts_at.tzinfo is None:
                    starts_at = starts_at.replace(tzinfo=WARSAW)
                appointments.append(
                    Appointment(
                        starts_at.astimezone(WARSAW),
                        term["clinicId"],
                        term["clinic"],
                        f"{doctor['academicTitle']} {doctor['firstName']} {doctor['lastName']}",
                        term["serviceId"],
                        doctor_search_name=f"{doctor['firstName']} {doctor['lastName']}",
                        doctor_id=doctor_id,
                        booking_term=LuxMedPortal._booking_term(term, terms_for_service, content.get("correlationId")),
                        requires_preparation=requires_preparation,
                    ),
                )
        return appointments

    @staticmethod
    def _booking_term(term: dict, service: dict, correlation_id) -> dict | None:
        """Keep the fields the standard in-person reservation flow needs in memory."""
        if (
            not isinstance(correlation_id, str) or not correlation_id
            or term.get("isTelemedicine") is not False
            or any(term.get(key) is not False for key in ("isAdditional", "isImpediment"))
            or any(type(term.get(key)) is not int or term[key] <= 0
                   for key in ("cityId", "serviceId", "clinicId", "roomId", "scheduleId"))
        ):
            return None
        additional = service.get("additionalData") or {}
        if not isinstance(additional, dict) or additional.get("isPreparationRequired", False) is not False:
            return None
        try:
            start, end = (datetime.datetime.fromisoformat(term[key]) for key in ("dateTimeFrom", "dateTimeTo"))
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "correlationId": correlation_id, "roomId": term["roomId"], "scheduleId": term["scheduleId"],
            "cityId": term["cityId"], "cityName": term.get("city"),
            "doctor": {key: term["doctor"][key] for key in ("id", "academicTitle", "firstName", "lastName")},
            "start": start.astimezone(WARSAW) if start.tzinfo else start.replace(tzinfo=WARSAW),
            "end": end.astimezone(WARSAW) if end.tzinfo else end.replace(tzinfo=WARSAW),
        }

    def get_cities(self) -> list[dict]:
        return self._portal_json("GET", self.DICTIONARY_CITIES_URL)

    def booked_visit_ids(self) -> set[int]:
        """Return IDs of reserved or confirmed in-person visits shown on the dashboard."""
        content = self._portal_json("GET", self.UPCOMING_VISITS_URL)
        events = content.get("events") if isinstance(content, dict) else None
        if not isinstance(events, list):
            raise PortalError("unexpected-upcoming-visits", suspend=True)

        booked = set()
        for event in events:
            if not isinstance(event, dict) or type(event.get("eventType")) is not int:
                raise PortalError("unexpected-upcoming-visits", suspend=True)
            if event["eventType"] != 1:  # Visit in the portal's event type enum.
                continue
            status = event.get("status")
            if type(status) is not int or not 0 <= status <= 6:
                raise PortalError("unexpected-upcoming-visits", suspend=True)
            if status in (1, 2):  # Reserved, Confirmed.
                visit_id = event.get("eventId")
                if type(visit_id) is not int or visit_id <= 0:
                    raise PortalError("unexpected-upcoming-visits", suspend=True)
                booked.add(visit_id)
        return booked

    def has_booking_conflict(self, start: datetime.datetime, end: datetime.datetime) -> bool:
        """Check overlaps against the upcoming events used on the portal search page."""
        content = self._portal_json("GET", f"{self.NEW_PORTAL_URL}/terms/getevents")
        events = content.get("events") if isinstance(content, dict) else None
        if not isinstance(events, list):
            raise PortalError("booking-conflicts-unknown")
        try:
            for event in events:
                begins = datetime.datetime.fromisoformat(event["date"])
                begins = begins if begins.tzinfo else begins.replace(tzinfo=WARSAW)
                if begins >= end:
                    continue
                if start <= begins:
                    return True
                if begins.astimezone(WARSAW).date() != start.astimezone(WARSAW).date():
                    continue
                if event.get("dateTo") is None:
                    raise PortalError("booking-conflicts-unknown")
                finishes = datetime.datetime.fromisoformat(event["dateTo"])
                finishes = finishes if finishes.tzinfo else finishes.replace(tzinfo=WARSAW)
                if finishes <= begins:
                    raise PortalError("booking-conflicts-unknown")
                if finishes > start:
                    return True
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PortalError("booking-conflicts-unknown") from None
        return False

    def is_booking_blocked_by_penalty(self, city_id: int, service_variant_id: int) -> bool:
        """Check the portal's no-show restriction for a search without a referral."""
        content = self._portal_json(
            "GET",
            self.NON_SHOW_PENALTIES_URL,
            params={"cityId": city_id, "serviceVariantId": service_variant_id, "referralId": ""},
        )
        can_reserve = content.get("canReserve") if isinstance(content, dict) else None
        if type(can_reserve) is not bool:
            raise PortalError("unexpected-penalty-status", suspend=True)
        return not can_reserve

    def check_booking_eligibility(self, city_id: int, service_variant_id: int) -> None:
        """Check the normal portal prerequisites for an in-person visit without a referral."""
        limits = self._portal_json("GET", f"{self.NEW_PORTAL_URL}/Reservation/GetLimits", stage="booking-limits")
        if not isinstance(limits, dict) or limits.get("canReserve") is not True:
            raise PortalError("booking-limit")
        referrals = self._portal_json(
            "GET", f"{self.NEW_PORTAL_URL}/Reservation/GetReferralsForReservation", stage="referrals",
        )
        if not isinstance(referrals, dict) or not isinstance(referrals.get("referrals"), list) or any(
            not isinstance(referral, dict) or referral.get("serviceVariantId") == service_variant_id
            for referral in referrals["referrals"]
        ):
            raise PortalError("booking-needs-portal")
        params = {"locationId": city_id, "serviceVariantId": service_variant_id, "process": 4}
        triage = self._portal_json("GET", f"{self.NEW_PORTAL_URL}/PNM/Index", params=params, stage="questionnaire")
        if not isinstance(triage, dict) or triage.get("questions") not in (None, []):
            raise PortalError("portal-questions")
        availability = self._portal_json(
            "GET", f"{self.NEW_PORTAL_URL}/Reservation/serviceVariantAvailabilityDetails",
            params={"cityId": city_id, "serviceVariantId": service_variant_id, "reservationId": ""},
            stage="service-eligibility",
        )
        if not isinstance(availability, dict) or any(
            availability.get(key) is not False for key in
            ("isNotAvailableInCity", "isNotAvailableInContract", "isNotAvailableInAreaOfCare", "blockedByAccountLevel")
        ):
            raise PortalError("booking-eligibility")

    def reserve_standard_visit(self, appointment: Appointment, service_name: str) -> int:
        """Hold and confirm one routine covered visit. Return its reservation ID."""
        term = appointment.booking_term
        if term is None or appointment.doctor_id is None:
            raise PortalError("booking-details-missing")
        payload = {
            "cityId": term["cityId"], "cityName": term["cityName"], "serviceVariantId": appointment.service_id,
            "serviceVariantName": service_name,
            "facilityId": appointment.clinic_id, "facilityName": appointment.ClinicPublicName,
            "doctorId": appointment.doctor_id, "doctor": term["doctor"],
            "roomId": term["roomId"], "scheduleId": term["scheduleId"],
            "date": term["start"].astimezone(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "timeFrom": term["start"].strftime("%H:%M"),
            "timeTo": term["end"].strftime("%H:%M"), "correlationId": term["correlationId"],
            "isTelemedicine": False, "isPoz": False, "isRehabilitation": False, "isVideoConsultation": False,
            "isAdditional": False, "isImpediment": False, "isPreparationRequired": False,
            "parentReservationId": None, "referralId": None, "eReferralId": None, "executedTriageId": None,
        }
        base = f"{self.NEW_PORTAL_URL}/Reservation"
        hold = self._portal_json("POST", f"{base}/LockTerm", json=payload)
        value = hold.get("value") if isinstance(hold, dict) else None
        if not isinstance(value, dict):
            raise PortalError("booking-hold-unavailable")
        hold_id = value.get("temporaryReservationId")
        if type(hold_id) is not int or hold_id <= 0:
            raise PortalError("booking-hold-unavailable")
        valuations = value.get("valuations")
        valuation = valuations[0] if isinstance(valuations, list) and len(valuations) == 1 else None
        alternative_price = valuation.get("alternativePrice") if isinstance(valuation, dict) else None
        can_confirm = (
            hold.get("hasErrors") is False and hold.get("hasWarnings") is False
            and hold.get("errors", []) == [] and hold.get("warnings", []) == []
            and value.get("askForReferral") is False and value.get("relatedVisits") in (None, [])
            and isinstance(valuation, dict)
            and type(valuation.get("valuationType")) is int and valuation["valuationType"] == 1  # InPackage.
            and type(valuation.get("price")) in (int, float) and valuation["price"] == 0
            and (alternative_price is None or (type(alternative_price) in (int, float) and alternative_price == 0))
            and valuation.get("isReferralRequired") is False
        )
        if not can_confirm:
            self.release_booking_hold(hold_id)
            raise PortalError("booking-needs-portal")
        confirmation = {key: payload[key] for key in (
            "cityId", "serviceVariantId", "doctorId", "facilityId", "roomId", "scheduleId",
            "date", "timeFrom", "parentReservationId", "referralId", "eReferralId", "executedTriageId",
        )}
        confirmation.update(temporaryReservationId=hold_id, referralRequired=False, valuationId=None, valuation=valuation)
        try:
            result = self._portal_json("POST", f"{base}/Confirm", json=confirmation)
        except PortalError as error:
            raise PortalError("booking-outcome-unknown", suspend=error.suspend) from None
        if isinstance(result, dict) and result.get("hasErrors") is True:
            self.release_booking_hold(hold_id)
            raise PortalError("booking-rejected")
        value = result.get("value") if isinstance(result, dict) and result.get("hasErrors") is False else None
        reservation_id = value.get("reservationId") if isinstance(value, dict) else None
        if type(reservation_id) is not int or reservation_id <= 0:
            raise PortalError("booking-outcome-unknown")
        return reservation_id

    def release_booking_hold(self, hold_id: int) -> None:
        try:
            self._portal_json(
                "POST", f"{self.NEW_PORTAL_URL}/Reservation/ReleaseTerm",
                params={"reservationId": hold_id}, json={},
            )
        except PortalError as error:
            if error.suspend:
                raise

    def get_services(self) -> list[dict]:
        return self._portal_json("GET", self.DICTIONARY_SERVICES_URL)

    def resolve_in_person_service(self, service_name: str) -> int:
        return self.resolve_service(service_name, self.get_services())

    @staticmethod
    def resolve_service(service_name: str, catalog: Any, *, telephone: bool = False) -> int:
        target = " ".join(service_name.casefold().split())
        names = {target}
        if not telephone:
            names = next((aliases for aliases in IN_PERSON_SERVICE_NAMES if target in aliases), names)
        matches = set()

        def collect(nodes: Any) -> None:
            if not isinstance(nodes, list):
                raise PortalError("unexpected-catalog", suspend=True)
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(node.get("name"), str):
                    raise PortalError("unexpected-catalog", suspend=True)
                children = node.get("children", [])
                if children:
                    collect(children)
                    continue
                name = " ".join(node["name"].casefold().split())
                if name in names and node.get("isTelemedicine") is telephone:
                    service_id = node.get("id")
                    if type(service_id) is not int or service_id <= 0:
                        raise PortalError("unexpected-catalog", suspend=True)
                    matches.add(service_id)

        collect(catalog)
        if not matches:
            raise PortalError("service-not-in-catalog", suspend=True)
        if len(matches) > 1:
            raise PortalError("ambiguous-service", suspend=True)
        return matches.pop()

    def get_facilities_and_doctors(self, city_id: int, service_variant_id: int) -> dict[str, Any]:
        params = {"cityId": city_id, "serviceVariantId": service_variant_id}
        return self._portal_json("GET", self.DICTIONARY_FACILITIES_AND_DOCTORS, params=params)


class LuxMedSniper(LuxMedPortal):
    def __init__(self, configuration_files: typing.Iterable[str] = ("luxmed_sniper.yaml",)):
        self.config = self._load_configuration(configuration_files)
        self.notification_providers = self._setup_providers()
        super().__init__(Luxmed(**self.config["luxmed"]))

    @staticmethod
    def _load_configuration(configuration_files: Iterable[str]) -> dict[str, Any]:
        def merge(a: dict[str, Any], b: dict[str, Any]) -> None:
            for key, value in b.items():
                if key not in a or a[key] == value:
                    a[key] = value
                elif isinstance(a[key], dict) and isinstance(value, dict):
                    merge(a[key], value)
                else:
                    raise LuxmedSniperError("Conflicting configuration values")

        config: dict[str, Any] = {}
        for configuration_file in configuration_files:
            configuration_path = Path(configuration_file).expanduser()
            with configuration_path.open(encoding="utf-8") as stream:
                merge(config, yaml.safe_load(stream))
        return config
    @staticmethod
    def _format_message(message_template: str, doctor_locator: DoctorLocator, appointment_data: Appointment) -> str:
        message_data = copy.deepcopy(appointment_data.__dict__)
        message_data.update(doctor_locator.__dict__)
        return message_template.format(**message_data)

    def _setup_providers(self) -> list[Callable[[DoctorLocator, Appointment], Any]]:  # noqa: C901
        notification_providers: list[Callable[[DoctorLocator, Appointment], Any]] = []

        providers = self.config["luxmedsniper"]["notification_provider"]

        if "pushover" in providers:
            pushover_user_key = self.config["pushover"]["user_key"]
            pushover_api_token = self.config["pushover"]["api_token"]

            def pushover_callback(doctor_locator: DoctorLocator, appointment: Appointment) -> None:
                message = self.config["pushover"]["message_template"].format(**appointment.__dict__)
                data = {
                    "token": pushover_api_token,
                    "user": pushover_user_key,
                    "message": message,
                    "title": self.config["pushover"]["title"],
                }
                response = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=10)
                if response.status_code != requests.codes["ok"]:
                    raise LuxmedSniperError("Pushover rejected the notification")

            notification_providers.append(pushover_callback)
        if "slack" in providers:
            from slack_sdk import WebClient  # noqa: PLC0415

            client = WebClient(token=self.config["slack"]["api_token"])
            channel = self.config["slack"]["channel"]
            notification_providers.append(
                lambda doctor_locator, appointment: client.chat_postMessage(
                    channel=channel,
                    text=self.config["slack"]["message_template"].format(vars(appointment)),
                ),
            )
        if "pushbullet" in providers:
            from pushbullet import Pushbullet  # noqa: PLC0415

            pb = Pushbullet(self.config["pushbullet"]["access_token"])

            notification_providers.append(
                lambda doctor_locator, appointment: pb.push_note(
                    title=self.config["pushbullet"]["title"],
                    body=self.config["pushbullet"]["message_template"].format_map(vars(appointment)),
                ),
            )
        if "ntfy" in providers:
            def ntfy_callback(doctor_locator, appointment):
                requests.post(
                    f"https://ntfy.sh/{self.config['ntfy']['topic']}",
                    data=self.config["ntfy"]["message_template"].format_map(vars(appointment)),
                    headers={"Tags": "hospital,pill,syringe", "Title": "Nowa wizyta"},
                    timeout=10,
                )

            notification_providers.append(ntfy_callback)
        if "gi" in providers:
            import gi  # noqa: PLC0415

            gi.require_version("Notify", "0.7")
            from gi.repository import Notify  # noqa: PLC0415

            # One time initialization of libnotify
            Notify.init("Luxmed Sniper")
            notification_providers.append(
                lambda doctor_locator, appointment: Notify.Notification.new(
                    self.config["gi"]["message_template"].format_map(vars(appointment)),
                    None,
                ).show(),
            )
        if "telegram" in providers:
            from telegram_send import send as t_send  # noqa: PLC0415

            notification_providers.append(
                lambda doctor_locator, appointment: t_send(
                    messages=[self.config["telegram"]["message_template"].format_map(vars(appointment))],
                    conf=self.config["telegram"]["tele_conf_path"],
                ),
            )
        if "sound" in providers:
            audio_file = Path(self.config["sound"]["audio"])

            def cb(_, __):
                subprocess.check_output(["/usr/bin/aplay", audio_file], shell=False, stderr=subprocess.STDOUT)  # noqa: S603

            notification_providers.append(cb)
        if "console" in providers:
            notification_providers.append(
                lambda doctor_locator, appointment: print(
                    self._format_message(self.config["console"]["message_template"], doctor_locator, appointment),
                ),
            )
        if "console_async" in providers:
            async def async_console_notification(doctor_locator, appointment):
                print(
                    self._format_message(
                        self.config["console_async"]["message_template"], doctor_locator, appointment
                    ),
                )

            notification_providers.append(async_console_notification)

        return notification_providers

    def _add_to_database(self, appointment: Appointment) -> None:
        path = Path(self.config["misc"]["notifydb"])
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    db: dict[str, list[str]] = json.load(f)
            except json.JSONDecodeError:
                db = {}  # corrupted file → reset
        else:
            db = {}

        # 2. Update DB
        notifications = db.get(appointment.DoctorName, [])
        notifications.append(appointment.AppointmentDate.isoformat())
        db[appointment.DoctorName] = notifications

        # class DateTimeEncoder(json.JSONEncoder):
        #     def default(self, obj):
        #         if isinstance(obj, datetime.datetime):
        #             return obj.isoformat()
        #         return super().default(obj)

        # 3. Save back to JSON (atomic write)
        with path.open("w", encoding="utf-8") as f:
            # json.dump(db, f, cls=DateTimeEncoder, indent=2, ensure_ascii=False)
            json.dump(db, f, indent=2, ensure_ascii=False)

    def _is_already_known(self, appointment: Appointment) -> bool:
        path = Path(self.config["misc"]["notifydb"])

        # If file doesn't exist → nothing known yet
        if not path.exists():
            return False

        try:
            with path.open(encoding="utf-8") as f:
                db: dict[str, list[str]] = json.load(f)
        except json.JSONDecodeError:
            logger.warning("Notification state file is invalid")
            return False

        notifications = map(datetime.datetime.fromisoformat, db.get(appointment.DoctorName, []))
        return appointment.AppointmentDate in notifications

    def _send_notification(self, doctor_locator: DoctorLocator, appointment: Appointment) -> None:
        for provider in self.notification_providers:
            try:
                result = provider(doctor_locator, appointment)
                if result is not None and asyncio.iscoroutine(result) is True:
                    asyncio.run(result)
            except Exception:
                logger.error("Sending notification failed")

    def check(self) -> None:
        doctor_locator_dict: dict[str, Any]
        for doctor_locator_dict in self.config["luxmedsniper"]["doctor_locators"]:
            doctor_locator = DoctorLocator.model_validate(
                doctor_locator_dict,
                context={"luxmedsniper_config": self.config["luxmedsniper"]},
            )

            if not doctor_locator.enabled:
                continue
            try:
                appointments = self.search(doctor_locator, days=self.config["luxmedsniper"]["lookup_time_days"])
                if not appointments:
                    logger.info("No matching slots")
                    continue
                for appointment in appointments:
                    if not self._is_already_known(appointment):
                        self._add_to_database(appointment)
                        self._send_notification(doctor_locator, appointment)
            except PortalError as error:
                if error.suspend:
                    raise
                logger.error("Portal search failed ({})", error)
            except Exception:
                logger.error("Search or notification state update failed")

class LuxmedSniperError(Exception):
    pass


class PortalError(LuxmedSniperError):
    def __init__(self, status: str, *, suspend: bool = False,
                 stage: str | None = None, http_status: int | None = None):
        super().__init__(status)
        self.suspend = suspend
        self.stage = stage
        self.http_status = http_status


def setup_logging() -> None:
    # Third-party HTTP logs can include request URLs and authentication data.
    logging.basicConfig(handlers=[logging.NullHandler()], force=True)
    logger.configure(handlers=[
        dict(sink=sys.stdout, level=os.environ.get("LOGURU_LEVEL", "INFO"), diagnose=False),
    ])


# noinspection PyTypeChecker
def dump_current_ids(config, city_wildcard: str | None, dump_ids_doctors: bool) -> None:
    luxmed_sniper = LuxMedSniper(config)
    cities: list[dict[str, Any]] = luxmed_sniper.get_cities()
    logger.info(f'Found: {len(cities)} cities')
    services: list[dict[str, Any]] = []
    for s in luxmed_sniper.get_services():
        services.append(
            dict(id=s['id'],name=s['name'], telemedicine=s['isTelemedicine'])
        )
        for c in s['children']:
            services.append(
                dict(id=c['id'], name=c['name'], telemedicine=c['isTelemedicine'])
            )
            for c2 in c['children']:
                services.append(
                    dict(id=c2['id'], name=c2['name'], telemedicine=c2['isTelemedicine'])
                )
    logger.info(f'Found: {len(services)} services')
    facilities_and_doctors = {}  # per city, per service
    for city in cities:
        if city_wildcard is not None:
            if not fnmatch(city['name'], city_wildcard):
                logger.info(f'{city["name"]} - skipping facilities and doctors')
                continue
        if dump_ids_doctors is False:
            continue
        logger.info(f'{city["name"]} - looking for facilities and doctors')
        facilities_and_doctors[city['id']] = {}
        for service in services:
            facilities_and_doctors[city['id']][service['id']] = {}
            fac_and_doc: dict[str, Any] = luxmed_sniper.get_facilities_and_doctors(city['id'], service['id'])
            facilities_and_doctors[city['id']][service['id']]['facilities'] = copy.deepcopy(fac_and_doc['facilities'])
            facilities_and_doctors[city['id']][service['id']]['doctors'] = [
                dict(id=d['id'], name=f'{d["academicTitle"]} {d["lastName"]} {d["firstName"]}')
                for d in fac_and_doc['doctors']
            ]

    json.dump(cities, open('luxmed-ids/ids-cities.json', 'w', encoding='utf-8'), indent=4, ensure_ascii=False)
    json.dump(services, open('luxmed-ids/ids-services.json', 'w', encoding='utf-8'), indent=4, ensure_ascii=False)
    json.dump(facilities_and_doctors, open('luxmed-ids/ids-facilities-doctors.json', 'w', encoding='utf-8'), indent=4, ensure_ascii=False)


def work(config: list[str]) -> None:
    try:
        luxmed_sniper = LuxMedSniper(config)
        with luxmed_sniper.session:
            luxmed_sniper.check()
    except PortalError as error:
        logger.error("Portal check failed ({})", error)
        if error.suspend:
            raise SystemExit(1) from None
    except (LuxmedSniperError, ValueError, OSError, KeyError, TypeError, yaml.YAMLError):
        logger.error("Unable to run with the current configuration")
        raise SystemExit(1) from None


if __name__ == "__main__":
    setup_logging()
    logger.info("Lux Med Appointment Sniper")
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "-c", "--config",
        help="Configuration file path", default=["luxmed_sniper.yaml"],
        nargs="*"
    )
    parser.add_argument(
        "-d", "--delay",
        type=int, help="Delay in fetching updates [s]", default=1800
    )
    group = parser.add_argument_group('dump-ids')
    group.add_argument(
        "--dump-ids",
        action='store_true', dest='dump_ids', help="Dump current ids", default=False
    )
    group.add_argument(
        "--dump-ids-city",
        type=str, dest='dump_ids_city', help="Dump facilities and doctors only from this city (wildcard)", default=None
    )
    group.add_argument(
        "--dump-ids-doctors",
        action='store_true', dest='dump_ids_doctors', help="Dump facilities and doctors also (many requests)", default=False
    )
    args = parser.parse_args()

    if args.dump_ids is True:
        logger.info(f'Dumping IDs')
        try:
            dump_current_ids(args.config, args.dump_ids_city, args.dump_ids_doctors)
        except PortalError as error:
            logger.error("Portal dictionary lookup failed ({})", error)
            raise SystemExit(1) from None
        except (LuxmedSniperError, ValueError, OSError, KeyError, TypeError, yaml.YAMLError):
            logger.error("Unable to read or write portal dictionaries")
            raise SystemExit(1) from None
    else:
        logger.info(f'Start working every: {args.delay} s')
        work(args.config)
        schedule.every(args.delay).seconds.do(work, args.config)
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            ...
    logger.info('Exiting')
