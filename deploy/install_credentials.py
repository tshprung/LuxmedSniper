"""Receive encrypted GitHub settings and install a host-bound systemd credential."""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from nacl.public import PrivateKey, SealedBox

from telegram_configuration import validate


TRANSFER_DIR = Path("/run/luxmed-telegram-transfer")
KEY_PATH = TRANSFER_DIR / "recipient.key"
CONFIG_DIR = Path("/etc/luxmed-telegram")
CREDENTIAL_PATH = CONFIG_DIR / "telegram-config.cred"


def receiving_key() -> None:
    TRANSFER_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        with KEY_PATH.open("xb") as handle:
            key = PrivateKey.generate()
            handle.write(bytes(key))
    except FileExistsError:
        key = PrivateKey(KEY_PATH.read_bytes())
    print(base64.b64encode(bytes(key.public_key)).decode("ascii"))


def install() -> None:
    key = PrivateKey(KEY_PATH.read_bytes())
    ciphertext = sys.stdin.buffer.read(1_048_576)
    plaintext = SealedBox(key).decrypt(ciphertext)
    validate(json.loads(plaintext))
    encrypted = subprocess.run(
        ["systemd-creds", "encrypt", "--name=telegram-config", "--with-key=host", "-", "-"],
        input=plaintext, capture_output=True, check=True, timeout=15,
    ).stdout
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CONFIG_DIR) as temporary:
        pending = Path(temporary) / CREDENTIAL_PATH.name
        pending.write_bytes(encrypted)
        pending.chmod(0o600)
        pending.replace(CREDENTIAL_PATH)
    KEY_PATH.unlink()
    print("Private configuration installed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("public-key", "install"))
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        print("The host credential installer requires root.", file=sys.stderr)
        return 2
    try:
        if args.action == "public-key":
            receiving_key()
        else:
            install()
    except Exception:
        print("Private configuration could not be prepared or installed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
