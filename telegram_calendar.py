"""Check availability and sync verified bookings to Google Calendar."""

import datetime as dt
import time
from urllib.parse import quote

import requests


class CalendarUnavailable(Exception):
    pass


class CalendarAvailability:
    def __init__(self, config: dict):
        self.config = config
        self.access_token = ""
        self.expires = 0

    @staticmethod
    def post(url: str, **kwargs) -> dict:
        try:
            with requests.post(url, timeout=(5, 15), allow_redirects=False, **kwargs) as response:
                if response.status_code != 200:
                    raise CalendarUnavailable
                data = response.json()
        except (requests.RequestException, ValueError):
            raise CalendarUnavailable from None
        if not isinstance(data, dict):
            raise CalendarUnavailable
        return data

    def _headers(self) -> dict[str, str]:
        if time.monotonic() >= self.expires:
            token = self.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token", "refresh_token": self.config["refresh_token"],
                    "client_id": self.config["client_id"], "client_secret": self.config["client_secret"],
                },
            )
            access, duration = token.get("access_token"), token.get("expires_in")
            if not isinstance(access, str) or not access or type(duration) is not int or duration <= 60:
                raise CalendarUnavailable
            self.access_token, self.expires = access, time.monotonic() + duration - 60
        return {"Authorization": f"Bearer {self.access_token}"}

    def busy(self, start: dt.datetime, end: dt.datetime) -> bool:
        ids = self.config["calendar_ids"]
        result = self.post(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": key} for key in ids]},
            headers=self._headers(),
        )
        calendars = result.get("calendars")
        if ids == ["primary"] and isinstance(calendars, dict) and len(calendars) == 1:
            # Google can return the actual calendar ID for the primary alias.
            calendars = {"primary": next(iter(calendars.values()))}
        if not isinstance(calendars, dict) or any(
            not isinstance(calendars.get(key), dict) or calendars[key].get("errors")
            or not isinstance(calendars[key].get("busy"), list) for key in ids
        ):
            raise CalendarUnavailable
        return any(calendars[key]["busy"] for key in ids)

    def add_booking(self, calendar_id: str, event_id: str, start: dt.datetime, end: dt.datetime) -> None:
        url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"
        headers = self._headers()
        try:
            with requests.post(
                url, headers=headers, timeout=(5, 15), allow_redirects=False,
                params={"sendUpdates": "none", "fields": "id"},
                json={
                    "id": event_id, "summary": "Appointment", "visibility": "private",
                    "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
                },
            ) as response:
                if response.status_code in (200, 201) and response.json().get("id") == event_id:
                    return
                if response.status_code != 409:
                    raise CalendarUnavailable
            # A previous attempt may have succeeded before the worker got the response.
            with requests.get(
                f"{url}/{event_id}", headers=headers, timeout=(5, 15), allow_redirects=False,
                params={"fields": "id,status"},
            ) as response:
                event = response.json() if response.status_code == 200 else {}
                if event.get("id") != event_id or event.get("status") == "cancelled":
                    raise CalendarUnavailable
        except (requests.RequestException, ValueError, AttributeError):
            raise CalendarUnavailable from None
