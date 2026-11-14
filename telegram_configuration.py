"""Validate private bot settings and package them for the deployment host."""

import base64
import json
import os
import re
import sys
from pathlib import Path


PROFILE_KEYS = ("patient1", "patient2", "patient3")
SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_ID",
    "LUXMED_TELEGRAM_PROFILES",
    *(f"LUXMED_{key.upper()}_{field}" for key in PROFILE_KEYS for field in ("LOGIN", "PASSWORD")),
)
OPTIONAL_SECRET_KEYS = ("OPENAI_API_KEY", "LUXMED_BOOKING_CALENDAR", "LUXMED_DIRECT_BOOKING", "LUXMED_DIRECT_BOOKING_PATIENTS")


def booking_patients(settings: dict) -> frozenset[str]:
    scope = settings.get("LUXMED_DIRECT_BOOKING_PATIENTS", "").strip()
    if not scope:
        return frozenset(PROFILE_KEYS) if settings.get("LUXMED_DIRECT_BOOKING") == "1" else frozenset()
    patients = frozenset(part.strip() for part in scope.split(","))
    if not patients.issubset(PROFILE_KEYS):
        raise ValueError("Invalid direct booking patient list")
    return patients


def calendar_settings(settings: dict) -> dict | None:
    value = settings.get("LUXMED_BOOKING_CALENDAR", "").strip()
    if not value:
        return None
    data = json.loads(value)
    if (
        not isinstance(data, dict)
        or any(not isinstance(data.get(key), str) or not data[key].strip()
               for key in ("client_id", "client_secret", "refresh_token"))
        or not isinstance(data.get("calendar_ids"), list) or not 1 <= len(data["calendar_ids"]) <= 50
        or any(not isinstance(key, str) or not key.strip() for key in data["calendar_ids"])
        or ("destination_calendar_id" in data and (
            not isinstance(data["destination_calendar_id"], str) or not data["destination_calendar_id"].strip()
        ))
    ):
        raise ValueError("Invalid booking calendar configuration")
    return data


def validate(settings) -> dict:
    if not isinstance(settings, dict) or any(
        not isinstance(settings.get(key), str) or not settings[key].strip() for key in SECRET_KEYS
    ) or any(key in settings and not isinstance(settings[key], str) for key in OPTIONAL_SECRET_KEYS):
        raise ValueError("Incomplete configuration")
    token = settings["TELEGRAM_BOT_TOKEN"]
    owner = settings["TELEGRAM_OWNER_ID"]
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
        raise ValueError("Invalid Telegram configuration")
    profiles = json.loads(settings["LUXMED_TELEGRAM_PROFILES"])
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_KEYS):
        raise ValueError("Invalid profile configuration")
    for profile in profiles.values():
        if not isinstance(profile, dict):
            raise ValueError("Invalid profile configuration")
        name = profile.get("name")
        aliases = profile.get("aliases", [])
        if (
            not isinstance(name, str) or not name.strip() or len(name) > 40
            or type(profile.get("child")) is not bool
            or not isinstance(aliases, list) or any(not isinstance(alias, str) or len(alias) > 40 for alias in aliases)
        ):
            raise ValueError("Invalid profile configuration")
    calendar_settings(settings)
    booking_patients(settings)
    return profiles


def worker_settings() -> dict:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        return json.loads((Path(directory) / "telegram-config").read_text(encoding="utf-8"))
    return dict(os.environ)


def trusted_devices(profiles) -> dict:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return {}
    devices = {}
    for patient in profiles:
        credential = Path(directory) / f"luxmed-device-{patient}"
        try:
            cookie = json.loads(credential.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if (
            not isinstance(cookie, dict)
            or set(cookie) != {"name", "value", "domain", "path", "secure", "expires"}
            or not isinstance(cookie["name"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", cookie["name"])
            or not re.search(r"device|trust|mfa", cookie["name"], re.IGNORECASE)
            or not isinstance(cookie["value"], str)
            or not re.fullmatch(r"[!-~]{1,8192}", cookie["value"])
            or ";" in cookie["value"]
            or cookie["domain"] not in ("portalpacjenta.luxmed.pl", ".portalpacjenta.luxmed.pl", ".luxmed.pl", "luxmed.pl")
            or cookie["path"] not in ("/", "/PatientPortal", "/PatientPortal/")
            or cookie["secure"] is not True
            or type(cookie["expires"]) is not int
            or cookie["expires"] <= 0
        ):
            raise ValueError("Invalid LUX MED device credential")
        devices[patient] = cookie
    return devices


def package_settings(settings: dict, public_key: str) -> bytes:
    from nacl.public import PublicKey, SealedBox

    validate(settings)
    key = PublicKey(base64.b64decode(public_key, validate=True))
    values = {name: settings[name] for name in SECRET_KEYS}
    values.update({
        name: settings[name] for name in OPTIONAL_SECRET_KEYS
        if isinstance(settings.get(name), str) and settings[name].strip()
    })
    payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
    return SealedBox(key).encrypt(payload)


def main() -> int:
    try:
        encrypted = package_settings(dict(os.environ), os.environ["CONFIG_RECEIVER_PUBLIC_KEY"])
        Path("telegram-config.sealed").write_bytes(encrypted)
    except (KeyError, ValueError, OSError):
        print("Private configuration is incomplete or the receiver key is invalid.", file=sys.stderr)
        return 2
    print("Encrypted configuration is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
