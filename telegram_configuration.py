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


def validate(settings) -> dict:
    if not isinstance(settings, dict) or any(
        not isinstance(settings.get(key), str) or not settings[key].strip() for key in SECRET_KEYS
    ):
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
    return profiles


def worker_settings() -> dict:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        return json.loads((Path(directory) / "telegram-config").read_text(encoding="utf-8"))
    return dict(os.environ)


def package_settings(settings: dict, public_key: str) -> bytes:
    from nacl.public import PublicKey, SealedBox

    validate(settings)
    key = PublicKey(base64.b64decode(public_key, validate=True))
    payload = json.dumps({name: settings[name] for name in SECRET_KEYS}, ensure_ascii=False).encode("utf-8")
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
