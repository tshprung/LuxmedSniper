import argparse
import asyncio
import base64
import copy
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import time
import typing
import unicodedata
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
from luxmed_questionnaire import QuestionnaireReceipt, visible_text

WARSAW = ZoneInfo("Europe/Warsaw")
PORTAL_TIMEOUT = (5, 15)
BOOKED_STATUSES = (1, 2)  # Reserved and Confirmed in the Patient Portal.
COMPLETED_VISIT_STATUS = 4  # Realized in the Patient Portal.
CONSULTATION_TYPES = frozenset({1, 5, 7, 9, 12, 14})
# Visit and examination types in the portal's scheduled timeline.
SCHEDULED_VISIT_MODES = {
    1: "", 2: "", 4: "", 5: "Online consultation", 7: "Phone consultation", 9: "Video consultation",
    12: "", 13: "", 14: "Phone consultation", 101: "",
}
IN_PERSON_SERVICE_NAMES = (
    {"internista", "konsultacja internisty", "konsultacja internistyczna"},
    {"alergolog", "konsultacja alergologiczna"},
    {"laryngolog", "konsultacja laryngologiczna"},
    {"ortopeda", "konsultacja ortopedyczna"},
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
    is_additional: bool = False


@dataclass(frozen=True, repr=False)
class VisitSummary:
    starts_at: datetime.datetime
    service: str
    clinic: str
    clinician: str
    mode: str


@dataclass(frozen=True, repr=False)
class ScheduledVisit(VisitSummary):
    event_id: int | None = None
    event_type: int = 0
    cancellation_instance: int | None = None


@dataclass(frozen=True, repr=False)
class CompletedVisit(VisitSummary):
    event_id: int
    event_type: int
    assigned_from_reservation: bool | int | None


@dataclass(frozen=True)
class CancellationTerms:
    late_hours: float | None = None
    no_show: bool = False


@dataclass(frozen=True)
class PackageCoverage:
    status: str
    referral_required: bool | None = None
    usage_limit: bool | None = None


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
    SCHEDULED_VISITS_URL = f"{NEW_PORTAL_URL}/Timeline/GetStartEvents"
    HISTORICAL_VISITS_URL = f"{NEW_PORTAL_URL}/Timeline/GetHistoricalEvents"
    VISIT_DETAILS_URL = f"{NEW_PORTAL_URL}/Timeline/geteventdetails"
    VISITS_URL = f"{NEW_PORTAL_URL}/Visits"
    MEDICAL_PACKAGE_URL = f"{NEW_PORTAL_URL}/MedicalPackage"
    NON_SHOW_PENALTIES_URL = 'https://portalpacjenta.luxmed.pl/PatientPortal/NewPortal/Reservation/CheckUserNonShowPenalties'

    def __init__(self, credentials: Luxmed, *, trusted_device: dict | None = None):
        self.session = requests.Session()
        try:
            if trusted_device is not None:
                self.session.cookies.set(**trusted_device)
                self.session.headers["X-Requested-With"] = "XMLHttpRequest"
            data = self._portal_json(
                "POST",
                self.LUXMED_LOGIN_URL,
                json={"login": credentials.login, "password": credentials.password},
                authenticating=True, stage="login",
            )
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
                raise PortalError("login-unconfirmed", suspend=True, stage="login")
            self.token_expires_at = self._token_expiry(token)
            if trusted_device is None:
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
                     stage: str | None = None, allow_empty: bool = False, **kwargs: Any) -> Any:
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
        if allow_empty and status in (200, 204) and not response.content.strip():
            return None
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

    def search(self, locator: DoctorLocator, *, days: int = 14, date: datetime.date | None = None,
               nationwide_from: str | None = None, questionnaire: QuestionnaireReceipt | None = None) -> list[Appointment]:
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
        if questionnaire is not None:
            params.update(processId=questionnaire.process_id, pnmExecutionId=questionnaire.execution_id)
        if nationwide_from is not None:
            # LUX MED uses the selected city with this service's default nationwide scope.
            params.update({
                "searchPlace.name": nationwide_from, "nextSearch": "false", "searchByMedicalSpecialist": "false",
            })
        content = self._portal_json("GET", self.NEW_PORTAL_RESERVATION_URL, params=params, stage="availability")
        try:
            appointments = self._parse_visits_new_portal(content, locator)
            if nationwide_from is not None:
                appointments.extend(self._load_limited_days(locator, params, content, now))
                if any(not appointment.DoctorName and appointment.doctor_id for appointment in appointments):
                    self._fill_phone_doctors(appointments, params)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise PortalError("unexpected-response", suspend=True, stage="availability") from None
        return [
            appointment for appointment in appointments
            if now <= appointment.AppointmentDate <= end
            and (date is None or appointment.AppointmentDate.date() == date)
        ]

    def _load_limited_days(self, locator: DoctorLocator, params: dict, content: dict,
                           now: datetime.datetime) -> list[Appointment]:
        service = content["termsForService"]
        appointments = []
        daily_params = {key: value for key, value in params.items() if key not in ("searchDatePreset", "nextSearch")}
        first_day = max(params["searchDateFrom"], now.astimezone(WARSAW).date().isoformat())
        loaded_days = {day.get("day", "")[:10] for day in service["termsForDays"] if day["terms"]}
        for summary in service.get("termsInfoForDays", []):
            count = (summary.get("termsCounter") or {}).get("termsNumber")
            if summary.get("isLimitedDay") is not True or type(count) is not int or count <= 0:
                continue
            day = datetime.date.fromisoformat(summary["day"][:10]).isoformat()
            if day in loaded_days or not first_day <= day <= params["searchDateTo"]:
                continue
            daily = self._portal_json(
                "GET", f"{self.NEW_PORTAL_URL}/terms/oneDayTerms",
                params={**daily_params, "searchDateFrom": day, "searchDateTo": day, "expectedTermsNumber": count},
                stage="phone-availability",
            )
            appointments.extend(self._parse_visits_new_portal({
                "correlationId": daily["correlationId"],
                "termsForService": {"termsForDays": [daily["termsForDay"]], "additionalData": service.get("additionalData")},
            }, locator))
            loaded_days.add(day)
        return appointments

    def _fill_phone_doctors(self, appointments: list[Appointment], params: dict) -> None:
        doctors = self._portal_json(
            "GET", f"{self.DICTIONARY_URL}/Doctors",
            params={"serviceVariantId": params["serviceVariantId"], "visitLanguageId": params["languageId"]},
            stage="phone-doctors",
        )
        if not isinstance(doctors, list):
            raise ValueError("Expected a list of clinicians")
        by_id = {int(doctor["id"]): doctor for doctor in doctors}
        for appointment in appointments:
            if appointment.DoctorName or appointment.doctor_id not in by_id:
                continue
            doctor = by_id[appointment.doctor_id]
            title, first_name, last_name = (
                (doctor.get(key) or "").strip() for key in ("academicTitle", "firstName", "lastName")
            )
            appointment.doctor_search_name = " ".join(filter(None, (first_name, last_name)))
            if appointment.doctor_search_name:
                appointment.DoctorName = " ".join(filter(None, (title, appointment.doctor_search_name)))
            if appointment.booking_term:
                appointment.booking_term["doctor"] = {
                    "id": appointment.doctor_id,
                    **{key: doctor.get(key) for key in ("academicTitle", "firstName", "lastName")},
                }

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
                doctor = term.get("doctor") or {}
                clinic_id = int(term["clinicGroupId"])
                doctor_id = int(doctor["id"]) if doctor.get("id") is not None else None
                if locator.doctor_ids and doctor_id not in locator.doctor_ids:
                    continue
                if locator.clinic_ids and clinic_id not in locator.clinic_ids:
                    continue
                starts_at = datetime.datetime.fromisoformat(term["dateTimeFrom"])
                if starts_at.tzinfo is None:
                    starts_at = starts_at.replace(tzinfo=WARSAW)
                title, first_name, last_name = (
                    (doctor.get(key) or "").strip() for key in ("academicTitle", "firstName", "lastName")
                )
                name = " ".join(filter(None, (first_name, last_name)))
                appointments.append(
                    Appointment(
                        starts_at.astimezone(WARSAW),
                        term["clinicId"],
                        term["clinic"],
                        " ".join(filter(None, (title, name))) if name else "",
                        term["serviceId"],
                        doctor_search_name=name,
                        doctor_id=doctor_id,
                        booking_term=LuxMedPortal._booking_term(term, terms_for_service, content.get("correlationId"))
                        if doctor_id is not None else None,
                        requires_preparation=requires_preparation,
                        is_additional=term.get("isAdditional") is True,
                    ),
                )
        return appointments

    @staticmethod
    def _booking_term(term: dict, service: dict, correlation_id) -> dict | None:
        """Keep the fields the standard reservation flow needs in memory."""
        telephone = term.get("isTelemedicine")
        city_id = term.get("cityId")
        valid_city = city_id is None or (type(city_id) is int and city_id >= (0 if telephone else 1))
        if (
            not isinstance(correlation_id, str) or not correlation_id
            or type(telephone) is not bool
            or term.get("isVideoConsultation", False) is not False
            or (telephone and term.get("isPoz", False) is not False)
            or any(term.get(key) is not False for key in ("isAdditional", "isImpediment"))
            or not valid_city
            or any(type(term.get(key)) is not int or term[key] <= 0
                   for key in ("serviceId", "clinicId", "roomId", "scheduleId"))
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
            "cityId": city_id, "cityName": term.get("city"), "clinicGroupId": term.get("clinicGroupId"),
            "isTelemedicine": telephone,
            "doctor": {key: term["doctor"].get(key) for key in ("id", "academicTitle", "firstName", "lastName")},
            "start": start.astimezone(WARSAW) if start.tzinfo else start.replace(tzinfo=WARSAW),
            "end": end.astimezone(WARSAW) if end.tzinfo else end.replace(tzinfo=WARSAW),
        }

    def get_cities(self) -> list[dict]:
        return self._portal_json("GET", self.DICTIONARY_CITIES_URL)

    def booked_visit_ids(self, *, telephone: bool = False) -> set[int]:
        """Return IDs of reserved or confirmed visits of the requested type."""
        content = self._portal_json("GET", self.UPCOMING_VISITS_URL, stage="booking-verification")
        events = content.get("events") if isinstance(content, dict) else None
        if not isinstance(events, list):
            raise PortalError("unexpected-upcoming-visits", suspend=True, stage="booking-verification")

        event_type = 7 if telephone else 1  # Telemedicine or Visit in the portal enum.
        booked = set()
        for event in events:
            if not isinstance(event, dict) or type(event.get("eventType")) is not int:
                raise PortalError("unexpected-upcoming-visits", suspend=True, stage="booking-verification")
            if event["eventType"] != event_type:
                continue
            status = event.get("status")
            if type(status) is not int or not 0 <= status <= 6:
                raise PortalError("unexpected-upcoming-visits", suspend=True, stage="booking-verification")
            if status in BOOKED_STATUSES:
                visit_id = event.get("eventId")
                if type(visit_id) is not int or visit_id <= 0:
                    raise PortalError("unexpected-upcoming-visits", suspend=True, stage="booking-verification")
                booked.add(visit_id)
        return booked

    def scheduled_visits(self, now: datetime.datetime) -> list[ScheduledVisit]:
        """Return future reserved and confirmed appointments from the scheduled timeline."""
        content = self._portal_json("GET", self.SCHEDULED_VISITS_URL, stage="scheduled-visits")
        events = content.get("events") if isinstance(content, dict) else None
        if not isinstance(events, list):
            raise PortalError("unexpected-scheduled-visits", stage="scheduled-visits")
        cutoff = now.timestamp()
        visits = []
        seen = set()
        for event in events:
            if not isinstance(event, dict) or type(event.get("eventType")) is not int:
                raise PortalError("unexpected-scheduled-visits", stage="scheduled-visits")
            kind = event["eventType"]
            if kind not in SCHEDULED_VISIT_MODES:
                continue
            status = event.get("status")
            if type(status) is not int:
                raise PortalError("unexpected-scheduled-visits", stage="scheduled-visits")
            if status not in BOOKED_STATUSES:
                continue
            starts = timeline_visit_date(event.get("date"), stage="scheduled-visits")
            if starts.timestamp() < cutoff:
                continue
            event_id = event.get("eventId")
            if type(event_id) is int and event_id > 0:
                if (kind, event_id) in seen:
                    continue
                seen.add((kind, event_id))
            summary = timeline_visit_summary(event, kind)
            instance_id = event.get("serviceInstanceId")
            actions = event.get("actions")
            # The timeline exposes CancelTerm (2) and paymentState None (5).
            standard_cancellation = (
                kind in (1, 7)
                and bool(visit_label(event.get("title"), 160))
                and type(event_id) is int and event_id > 0
                and type(instance_id) is int and instance_id >= 0
                and isinstance(actions, list) and any(type(action) is int and action == 2 for action in actions)
                and type(event.get("paymentState")) is int and event["paymentState"] == 5
                and event.get("fromEreferral", False) is False and event.get("isOutsourced", False) is False
                and event.get("slotId") in (None, 0)
                and not any(event.get(key) is not None for key in ("clipId", "externalClinic"))
            )
            visits.append(ScheduledVisit(
                starts, *summary,
                event_id if type(event_id) is int and event_id > 0 else None, kind,
                instance_id if standard_cancellation else None,
            ))
        return sorted(visits, key=lambda visit: visit.starts_at.timestamp())

    def latest_completed_visit(self, now: datetime.datetime) -> CompletedVisit | None:
        cutoff = now.timestamp()
        # Today's completed visits can remain in the start feed until the history updates.
        start = self._portal_json("GET", self.SCHEDULED_VISITS_URL, stage="visit-history")
        events = start.get("events") if isinstance(start, dict) else None
        if not isinstance(events, list):
            raise PortalError("unexpected-visit-history", stage="visit-history")
        latest = latest_completed_in(events, cutoff)
        before = None
        while True:
            params = {} if before is None else {"beforeDate": before.strftime("%Y-%m-%d %H:%M:%S")}
            page = self._portal_json("GET", self.HISTORICAL_VISITS_URL, stage="visit-history", params=params)
            events = page.get("events") if isinstance(page, dict) else None
            if not isinstance(events, list) or type(page.get("isEndOfList")) is not bool:
                raise PortalError("unexpected-visit-history", stage="visit-history")
            latest = latest_completed_in(events, cutoff, latest)
            if page["isEndOfList"]:
                return latest
            if not events:
                raise PortalError("unexpected-visit-history", stage="visit-history")
            cursor = timeline_visit_date(events[-1].get("date"), stage="visit-history")
            if latest is not None and cursor.timestamp() <= latest.starts_at.timestamp():
                return latest
            if before is not None and cursor.timestamp() >= before.timestamp():
                raise PortalError("unexpected-visit-history", stage="visit-history")
            before = cursor

    def visit_recommendations(self, visit: CompletedVisit) -> str:
        if visit.assigned_from_reservation is None:
            raise PortalError("visit-reference-unavailable", stage="visit-details")
        details = self._portal_json("GET", self.VISIT_DETAILS_URL, stage="visit-details", params={
            "eventId": str(visit.event_id),
            "eventType": str(visit.event_type),
            "assignedFromReservationId": str(visit.assigned_from_reservation).lower(),
        })
        if (not isinstance(details, dict) or details.get("eventId") != visit.event_id
                or details.get("eventType") != visit.event_type or details.get("status") != COMPLETED_VISIT_STATUS):
            raise PortalError("unexpected-visit-details", stage="visit-details")
        recommendations = details.get("recommendations")
        if recommendations is not None and not isinstance(recommendations, str):
            raise PortalError("unexpected-visit-details", stage="visit-details")
        return visible_text(recommendations)

    def cancellation_terms(self, visit: ScheduledVisit) -> CancellationTerms:
        result = self._portal_json(
            "GET", f"{self.VISITS_URL}/isLateCancellation",
            params={"reservationId": str(visit.event_id)}, stage="cancellation-eligibility",
        )
        if (not isinstance(result, dict) or type(result.get("isLate")) is not bool
                or result.keys() - {"isLate", "parameters", "allowNonShowProcess"}):
            raise PortalError("cancellation-eligibility-unverified", stage="cancellation-eligibility")
        if not result["isLate"]:
            return CancellationTerms()
        parameters = result.get("parameters")
        hours = parameters.get("hours") if isinstance(parameters, dict) else None
        no_show = result.get("allowNonShowProcess")
        if type(hours) not in (int, float) or not 0 < hours < sys.float_info.max or type(no_show) is not bool:
            raise PortalError("cancellation-eligibility-unverified", stage="cancellation-eligibility")
        return CancellationTerms(hours, no_show)

    def cancel_standard_visit(self, visit: ScheduledVisit) -> None:
        if visit.cancellation_instance is None or visit.event_type not in (1, 7) or visit.event_id is None:
            raise PortalError("cancellation-refused", stage="cancellation")
        response = self._portal_json(
            "POST", f"{self.VISITS_URL}/cancelVisit", headers=self._booking_request_headers(),
            json={"reservationId": visit.event_id, "serviceInstanceId": visit.cancellation_instance},
            stage="cancellation",
        )
        if isinstance(response, dict) and response.get("isSuccess") is False:
            raise PortalError("cancellation-refused", stage="cancellation")
        if not isinstance(response, dict) or response.get("isSuccess") is not True:
            raise PortalError("cancellation-outcome-unverified", stage="cancellation")

    def has_booking_conflict(self, start: datetime.datetime, end: datetime.datetime) -> bool:
        """Check overlaps against the upcoming events used on the portal search page."""
        content = self._portal_json("GET", f"{self.NEW_PORTAL_URL}/terms/getevents", stage="booking-conflicts")
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
            stage="booking-penalty",
        )
        can_reserve = content.get("canReserve") if isinstance(content, dict) else None
        if type(can_reserve) is not bool:
            raise PortalError("unexpected-penalty-status", suspend=True, stage="booking-penalty")
        return not can_reserve

    def _booking_request_headers(self) -> dict[str, str]:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        result = self._portal_json(
            "GET", f"{self.NEW_PORTAL_URL}/security/getforgerytoken",
            headers=headers, stage="booking-request-token",
        )
        token = result.get("token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise PortalError("unexpected-response", stage="booking-request-token")
        return {**headers, "XSRF-TOKEN": token}

    def check_booking_eligibility(self, city_id: int, service_variant_id: int) -> None:
        """Check the normal portal prerequisites for a visit without a referral."""
        self.check_booking_requirements(service_variant_id)
        if (form := self.booking_questionnaire(city_id, service_variant_id)) is not None:
            raise BookingQuestionnaireRequired(form)
        self.check_booking_coverage(city_id, service_variant_id)

    def check_booking_requirements(self, service_variant_id: int) -> None:
        limits = self._portal_json("GET", f"{self.NEW_PORTAL_URL}/Reservation/GetLimits", stage="booking-limits")
        if not isinstance(limits, dict) or limits.get("canReserve") is not True:
            raise PortalError("booking-limit")
        referrals = self._portal_json(
            "GET", f"{self.NEW_PORTAL_URL}/Reservation/GetReferralsForReservation", stage="referrals",
        )
        entries = referrals.get("referrals") if isinstance(referrals, dict) else None
        if not isinstance(entries, list) or any(not isinstance(referral, dict) for referral in entries):
            raise PortalError("booking-referrals-unconfirmed", stage="referrals")
        if any(referral.get("serviceVariantId") == service_variant_id for referral in entries):
            raise PortalError("booking-referral-review", stage="referrals")

    def booking_questionnaire(self, city_id: int, service_variant_id: int) -> dict | None:
        params = {"locationId": city_id, "serviceVariantId": service_variant_id, "process": 4}
        triage = self._portal_json(
            "GET", f"{self.NEW_PORTAL_URL}/PNM/Index",
            params=params, headers=self._booking_request_headers(), stage="questionnaire",
        )
        # The portal can return JSON null after the patient completes the questions.
        if triage is None:
            triage = {}
        if not isinstance(triage, dict):
            raise PortalError("questionnaire-response-unrecognized", stage="questionnaire")
        questions = triage.get("questions")
        if questions is not None and (
            not isinstance(questions, list) or any(not isinstance(question, dict) for question in questions)
        ):
            raise PortalError("questionnaire-response-unrecognized", stage="questionnaire")
        return triage if questions else None

    def submit_booking_questionnaire(self, occurrence: dict) -> None:
        result = self._portal_json(
            "POST", f"{self.NEW_PORTAL_URL}/PNM/Occurrence", json=occurrence,
            headers=self._booking_request_headers(), allow_empty=True, stage="questionnaire-submission",
        )
        if result is not None and result != {} and result is not True:
            raise PortalError("questionnaire-submission-unconfirmed", stage="questionnaire-submission")

    def check_booking_coverage(self, city_id: int, service_variant_id: int) -> None:
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

    def check_booking_city(self, appointment: Appointment, city_id: int) -> None:
        term = appointment.booking_term
        error = PortalError("booking-location-unverified", stage="booking-location")
        if term is None:
            raise error
        if term["cityId"] is not None:
            if term["cityId"] != city_id:
                raise error
            return
        term.pop("verifiedCityId", None)
        # The city directory lists clinic groups. A slot also has a distinct clinic ID.
        group_id = term.get("clinicGroupId")
        if type(group_id) is not int or group_id <= 0:
            raise error
        directory = self.get_facilities_and_doctors(city_id, appointment.service_id)
        facilities = directory.get("facilities") if isinstance(directory, dict) else None
        if not isinstance(facilities, list) or not any(
            isinstance(facility, dict) and type(facility.get("id")) is int and facility["id"] == group_id
            for facility in facilities
        ):
            raise error
        term["verifiedCityId"] = city_id

    def reserve_standard_visit(self, appointment: Appointment, service_name: str, *,
                               questionnaire: QuestionnaireReceipt | None = None) -> int:
        """Hold and confirm one routine covered visit. Return its reservation ID."""
        term = appointment.booking_term
        if term is None or appointment.doctor_id is None:
            raise PortalError("booking-details-missing")
        telephone = term["isTelemedicine"]
        missing_city = not telephone and term["cityId"] is None
        if missing_city and (type(term.get("verifiedCityId")) is not int or term["verifiedCityId"] <= 0):
            raise PortalError("booking-location-unverified", stage="booking-location")
        payload = {
            "cityId": term["cityId"], "cityName": term["cityName"], "serviceVariantId": appointment.service_id,
            "serviceVariantName": service_name,
            "facilityId": appointment.clinic_id,
            "facilityName": "Konsultacja telefoniczna" if telephone else appointment.ClinicPublicName,
            "doctorId": appointment.doctor_id, "doctor": term["doctor"],
            "roomId": term["roomId"], "scheduleId": term["scheduleId"],
            "date": term["start"].astimezone(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "timeFrom": term["start"].strftime("%H:%M"),
            "timeTo": term["end"].strftime("%H:%M"), "correlationId": term["correlationId"],
            "isTelemedicine": telephone, "isPoz": False, "isRehabilitation": False, "isVideoConsultation": False,
            "isAdditional": False, "isImpediment": False, "isPreparationRequired": False,
            "parentReservationId": None, "referralId": None, "eReferralId": None, "referralTypeId": None,
            "executedTriageId": questionnaire.execution_id if questionnaire else None,
        }
        if missing_city:
            # The portal omits the city fields when its term does not provide them.
            del payload["cityId"]
            if payload["cityName"] is None:
                del payload["cityName"]
        base = f"{self.NEW_PORTAL_URL}/Reservation"
        headers = self._booking_request_headers()
        hold = self._portal_json("POST", f"{base}/LockTerm", json=payload, headers=headers, stage="booking-hold")
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
            and value.get("conflictedVisit") is None
            and value.get("changeTermAvailable", False) is False
            and (not telephone or value.get("isAlfavoxVideoConsultation", False) is False)
            and isinstance(valuation, dict)
            and type(valuation.get("valuationType")) is int and valuation["valuationType"] == 1  # InPackage.
            and type(valuation.get("price")) in (int, float) and valuation["price"] == 0
            and (alternative_price is None or (type(alternative_price) in (int, float) and alternative_price == 0))
            and valuation.get("isReferralRequired") is False
            and valuation.get("requireReferralForPP", False) is False
        )
        if not can_confirm:
            self.release_booking_hold(hold_id, headers)
            raise PortalError("booking-needs-portal")
        confirmation = {key: payload[key] for key in (
            "cityId", "serviceVariantId", "doctorId", "facilityId", "roomId", "scheduleId",
            "date", "timeFrom", "parentReservationId", "referralId", "eReferralId", "executedTriageId",
        ) if key in payload}
        confirmation.update(temporaryReservationId=hold_id, referralRequired=False, valuationId=None, valuation=valuation)
        try:
            result = self._portal_json("POST", f"{base}/Confirm", json=confirmation, headers=headers, stage="booking-confirm")
        except PortalError as error:
            raise PortalError("booking-outcome-unknown", suspend=error.suspend,
                              stage=error.stage, http_status=error.http_status) from None
        if isinstance(result, dict) and result.get("hasErrors") is True:
            self.release_booking_hold(hold_id, headers)
            raise PortalError("booking-rejected")
        value = result.get("value") if isinstance(result, dict) and result.get("hasErrors") is False else None
        reservation_id = value.get("reservationId") if isinstance(value, dict) else None
        if type(reservation_id) is not int or reservation_id <= 0:
            raise PortalError("booking-outcome-unknown", stage="booking-confirm")
        return reservation_id

    def release_booking_hold(self, hold_id: int, headers: dict[str, str]) -> None:
        try:
            self._portal_json(
                "POST", f"{self.NEW_PORTAL_URL}/Reservation/ReleaseTerm",
                params={"reservationId": hold_id}, json={}, headers=headers, stage="booking-release",
            )
        except PortalError as error:
            if error.suspend:
                raise

    def get_services(self) -> list[dict]:
        return self._portal_json("GET", self.DICTIONARY_SERVICES_URL, stage="service-catalog")

    def medical_package_ids(self) -> tuple[str, ...]:
        packages = self._portal_json("GET", f"{self.MEDICAL_PACKAGE_URL}/GetActiveBeneficiaries",
                                     stage="coverage-packages")
        if not isinstance(packages, list):
            raise PortalError("unexpected-response", stage="coverage-packages")
        package_ids = []
        for package in packages:
            package_id = package.get("packageId") if isinstance(package, dict) else None
            if type(package_id) not in (int, str) or not re.fullmatch(r"[\w-]{1,80}", str(package_id)):
                raise PortalError("unexpected-response", stage="coverage-packages")
            package_ids.append(str(package_id))
        return tuple(dict.fromkeys(package_ids))

    def medical_package_services(self) -> list[dict]:
        catalog = self._portal_json("GET", f"{self.MEDICAL_PACKAGE_URL}/GetServiceVariantGroups",
                                    stage="coverage-catalog")
        return list({
            variant["id"]: {"id": variant["id"], "name": visit_label(variant["name"], 180)}
            for variant in service_variants(catalog)
            if type(variant.get("id")) is int and variant["id"] > 0 and variant["name"].strip()
        }.values())

    def medical_package_coverage(self, package_id: str, service_variant_id: int) -> PackageCoverage:
        details = self._portal_json(
            "GET", f"{self.MEDICAL_PACKAGE_URL}/GetServiceVariantInfo",
            params={"serviceVariantId": service_variant_id, "beneficiaryId": package_id},
            stage="coverage-details",
        )
        if not isinstance(details, dict):
            raise PortalError("unexpected-response", stage="coverage-details")
        return parse_package_coverage(details)

    def resolve_in_person_service(self, service_name: str) -> int:
        return self.resolve_service(service_name, self.get_services())

    @staticmethod
    def resolve_service(service_name: str, catalog: Any, *, telephone: bool = False, regular_phone: bool = False) -> int:
        target = " ".join(service_name.casefold().split())
        names = {target}
        if not telephone:
            names = next((aliases for aliases in IN_PERSON_SERVICE_NAMES if target in aliases), names)
        matches = set()

        for node in service_variants(catalog):
            name = " ".join(node["name"].casefold().split())
            if name in names and node.get("isTelemedicine") is telephone:
                if regular_phone and (node.get("isVideoConsultation") is not False or node.get("isPoz") is not False):
                    continue
                service_id = node.get("id")
                if type(service_id) is not int or service_id <= 0:
                    raise PortalError("unexpected-catalog", suspend=True)
                matches.add(service_id)
        if not matches:
            raise PortalError("service-not-in-catalog", suspend=True)
        if len(matches) > 1:
            raise PortalError("ambiguous-service", suspend=True)
        return matches.pop()

    def get_facilities_and_doctors(self, city_id: int, service_variant_id: int) -> dict[str, Any]:
        params = {"cityId": city_id, "serviceVariantId": service_variant_id}
        return self._portal_json("GET", self.DICTIONARY_FACILITIES_AND_DOCTORS, params=params, stage="facility-directory")


def service_variants(catalog: Any) -> Iterable[dict]:
    if not isinstance(catalog, list):
        raise PortalError("unexpected-catalog", suspend=True)
    for node in catalog:
        if not isinstance(node, dict) or not isinstance(node.get("name"), str):
            raise PortalError("unexpected-catalog", suspend=True)
        if children := node.get("children"):
            yield from service_variants(children)
        else:
            yield node


def visit_label(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""

def timeline_visit_date(value: Any, *, stage: str) -> datetime.datetime:
    try:
        if not isinstance(value, str) or len(value) < 16:
            raise ValueError("Visit requires a date and time")
        starts = datetime.datetime.fromisoformat(value)
        return starts.astimezone(WARSAW) if starts.tzinfo else starts.replace(tzinfo=WARSAW)
    except (TypeError, ValueError, OverflowError):
        raise PortalError(f"unexpected-{stage}", stage=stage) from None


def timeline_visit_summary(event: dict, kind: int) -> tuple[str, str, str, str]:
    mode = SCHEDULED_VISIT_MODES[kind]
    clinic = event.get("externalClinic") or event.get("clinic")
    doctor = event.get("doctor")
    return (
        visit_label(event.get("title"), 160) or "LUX MED appointment",
        visit_label(clinic.get("name"), 100) if isinstance(clinic, dict) and not mode else "",
        visit_label(doctor.get("name"), 100) if isinstance(doctor, dict) else "",
        mode,
    )


def latest_completed_in(events: list, cutoff: float, latest: CompletedVisit | None = None) -> CompletedVisit | None:
    for event in events:
        if not isinstance(event, dict) or type(event.get("eventType")) is not int:
            raise PortalError("unexpected-visit-history", stage="visit-history")
        kind = event["eventType"]
        if kind not in CONSULTATION_TYPES:
            continue
        if type(event.get("status")) is not int:
            raise PortalError("unexpected-visit-history", stage="visit-history")
        if event["status"] != COMPLETED_VISIT_STATUS:
            continue
        starts = timeline_visit_date(event.get("date"), stage="visit-history")
        if starts.timestamp() > cutoff or (latest and starts.timestamp() <= latest.starts_at.timestamp()):
            continue
        event_id = event.get("eventId")
        if type(event_id) is not int or event_id <= 0:
            raise PortalError("unexpected-visit-history", stage="visit-history")
        assigned = event.get("assignedFromReservationId")
        latest = CompletedVisit(starts, *timeline_visit_summary(event, kind), event_id, kind,
                                assigned if type(assigned) in (bool, int) else None)
    return latest


def package_item_labels(item: Any) -> Iterable[str]:
    texts = item.get("itemTexts") if isinstance(item, dict) else None
    if not isinstance(texts, list):
        return
    for text in texts:
        plain = visible_text(text.get("htmlText")) if isinstance(text, dict) else ""
        if plain:
            decomposed = unicodedata.normalize("NFKD", plain.casefold().replace("ł", "l"))
            yield " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).split())


def parse_package_coverage(details: dict) -> PackageCoverage:
    headers = list(package_item_labels(details.get("header")))
    header = " ".join(headers)
    sections = details.get("executingConditions")
    labels = [
        label
        for section in sections if isinstance(section, dict) and isinstance(section.get("items"), list)
        for item in section["items"]
        for label in package_item_labels(item)
    ] if isinstance(sections, list) else []
    conditions = " ".join(labels)
    summary = f"{header} {conditions}"
    outside = re.search(
        r"\b(?:poza (?:zakresem )?(?:twoim |twojego )?(?:abonamen|pakie)\w*|"
        r"nie (?:jest objeta|jest dostepna|przysluguje|masz)\b.{0,70}\b(?:abonamen|pakie)\w*|"
        r"(?:not (?:included|covered)|outside)\b.{0,70}\b(?:package|subscription|plan))", header,
    )
    if re.search(r"\b(?:z doplata|(?<!nie )wymaga doplaty|(?<!nie )podlega wspolplatnosci|"
                 r"co-?payment (?:applies|is required))\b", summary):
        status = "copay"
    elif any(re.fullmatch(
        r"(?:(?:ta )?usluga (?:jest )?(?:platna|odplatna)|this service is (?:paid|payable|chargeable))[.!]?", label,
    ) for label in [*headers, *labels]):
        status = "paid"
    elif outside:
        status = "outside"
    elif re.search(r"\b(?:(?:ta )?usluga (?:jest )?(?:dostepna )?|masz (?:te )?usluge )"
                   r"w (?:ramach )?(?:twoim |twojego |swoim )?(?:abonamen|pakie)\w*|"
                   r"\b(?:included|covered) (?:in|by) (?:your|the) (?:package|subscription|plan)\b", header):
        status = "covered"
    else:
        status = "unknown"
    referral_required = None
    if re.search(r"\b(?:nie wymaga skierowania|nie potrzebujesz skierowania|bez skierowania|"
                 r"no referral required|referral is not required)\b", conditions):
        referral_required = False
    elif re.search(r"\b(?:wymaga skierowania|wymagane (?:jest )?skierowanie|potrzebujesz skierowania|referral is required)\b",
                   conditions):
        referral_required = True
    usage_limit = None
    if re.search(r"\b(?:bez limitu|nielimitowana|no usage limit)\b", conditions):
        usage_limit = False
    elif re.search(r"\b(?:limit (?:wykorzystania|wykonan|uslug|wizyt)|usluga limitowana|usage limit)\b", conditions):
        usage_limit = True
    return PackageCoverage(status, referral_required, usage_limit)


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


class BookingQuestionnaireRequired(PortalError):
    def __init__(self, form: dict):
        super().__init__("portal-questions", stage="questionnaire")
        self.form = form


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
