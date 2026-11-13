"""Check availability without reading Google Calendar event details."""

import datetime as dt
import time

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

    def busy(self, start: dt.datetime, end: dt.datetime) -> bool:
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
        ids = self.config["calendar_ids"]
        result = self.post(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": key} for key in ids]},
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        calendars = result.get("calendars")
        if not isinstance(calendars, dict) or any(
            not isinstance(calendars.get(key), dict) or calendars[key].get("errors")
            or not isinstance(calendars[key].get("busy"), list) for key in ids
        ):
            raise CalendarUnavailable
        return any(calendars[key]["busy"] for key in ids)
