import base64
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from nacl.public import PrivateKey, SealedBox

from luxmed_sniper import WARSAW, Appointment, Luxmed, PortalError
from telegram_bot import Profile, SearchBot, Telegram, TelegramError
from telegram_configuration import SECRET_KEYS, package_settings
from telegram_state import SearchState, next_search_time


def local_timestamp(day, hour, minute=0):
    return int(dt.datetime(2030, 3, day, hour, minute, tzinfo=WARSAW).timestamp())


class BotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state.sqlite3"
        self.state = SearchState(self.path)
        self.addCleanup(lambda: self.state.db.close())
        self.telegram = MagicMock()
        self.telegram.owner = 100
        self.telegram.send.return_value = 42
        self.now = local_timestamp(30, 10)
        self.profiles = {
            "patient1": Profile("Adult", Luxmed(login="fixture1", password="dummy")),
            "patient2": Profile("Child", Luxmed(login="fixture2", password="dummy"), ("Dziecka",), True),
            "patient3": Profile("Other", Luxmed(login="fixture3", password="dummy"), child=True),
        }
        self.bot = SearchBot(self.telegram, self.state, self.profiles, clock=lambda: self.now)

    def update(self, number, text, *, sender=100, chat_id=100, chat_type="private", reply_to=None):
        message = {
            "from": {"id": sender},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        }
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        self.bot.accept({"update_id": number, "message": message})

    def test_owner_only_private_chat(self):
        self.update(1, "Find an internist for Adult", sender=200, chat_id=200)
        self.update(2, "Find an internist for Adult", chat_id=-100, chat_type="group")
        self.assertEqual(self.state.active(), [])
        self.assertIsNone(self.state.next_message())
        self.assertEqual(self.state.get_setting("offset"), "3")

    def test_pediatric_clarification_survives_restart_and_queues_after_hours(self):
        self.now = local_timestamp(30, 23, 15)
        self.update(1, "Znajdź pediatrę dla Dziecka we Wroclaw")
        self.assertIn("Który rodzaj wizyty?", self.state.next_message()["text"])
        self.assertEqual(self.state.active(), [])
        self.state.db.close()
        self.state = SearchState(self.path)
        self.bot = SearchBot(self.telegram, self.state, self.profiles, clock=lambda: self.now)
        self.update(2, "4")
        row, = self.state.active()
        self.assertEqual(row["patient"], "patient2")
        self.assertEqual(row["service"], "Konsultacja pediatryczna - gabinet dzieci zdrowych")
        self.assertEqual(row["next_due"], local_timestamp(31, 6))
        self.assertEqual(self.state.claim_batch(self.now), [])

    def test_same_update_does_not_duplicate_a_search_and_stop_reply_targets_one(self):
        self.update(1, "Find an internist for Adult")
        self.update(1, "Find an internist for Adult")
        self.bot.flush()
        self.update(2, "/find Child | health check")
        self.update(3, "Stop", reply_to=42)
        row, = self.state.active()
        self.assertEqual(row["patient"], "patient2")
        self.update(4, f"Zatrzymaj {row['id']}")
        self.assertEqual(self.state.active(), [])

    def test_scheduled_batch_uses_one_login_per_patient_per_hour(self):
        self.update(1, "Find an internist for Adult")
        self.update(2, "/find Adult | Dermatolog")
        self.update(3, "/find Child | health check")
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.side_effect = [12, 13, 14]
            portal.return_value.search.return_value = []
            self.bot.search_due()
            portal.assert_called_once_with(self.profiles["patient1"].credentials)
            portal.return_value.get_services.assert_called_once()
            self.assertEqual(portal.return_value.search.call_count, 2)
            self.update(4, "/find Adult | Alergolog")
            self.bot.search_due()
            self.assertEqual(portal.call_count, 2)
            portal.assert_called_with(self.profiles["patient2"].credentials)
            self.bot.search_due()
            self.assertEqual(portal.call_count, 2)
        self.assertTrue(all(row["next_due"] >= self.now + 3600 for row in self.state.active()))

    def test_verification_pauses_only_affected_account(self):
        self.update(1, "Find an internist for Adult")
        self.update(2, "/find Child | health check")
        with patch("telegram_bot.LuxMedPortal", side_effect=PortalError("login-unconfirmed", suspend=True)):
            self.bot.search_due()
        adult, child = self.state.active()
        self.assertTrue(adult["suspended"])
        self.assertFalse(child["suspended"])
        self.update(3, "/resume Adult")
        self.assertFalse(self.state.active()[0]["suspended"])
        child_batch = self.state.claim_batch(self.now)
        self.assertEqual([row["patient"] for row in child_batch], ["patient2"])
        self.assertEqual(self.state.claim_batch(self.now), [])

    def test_found_slots_complete_search_and_deliver_once(self):
        self.update(1, "/find Child | routine visit by phone")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = [
                Appointment(dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW), 2, "Fixture clinic", "Fixture doctor", 12),
            ]
            self.bot.search_due()
            portal.return_value.resolve_service.assert_called_once()
            self.assertTrue(portal.return_value.resolve_service.call_args.kwargs["telephone"])
            self.bot.search_due()
            portal.return_value.search.assert_called_once()
        self.assertIn("Available slots", self.state.next_message()["text"])
        self.assertEqual(self.state.active(), [])
        self.bot.flush()
        self.assertIsNone(self.state.next_message())

    def test_warsaw_close_and_dst(self):
        self.assertEqual(next_search_time(local_timestamp(30, 22)), local_timestamp(30, 22))
        self.assertEqual(next_search_time(local_timestamp(30, 22, 1)), local_timestamp(31, 6))
        self.assertEqual(next_search_time(local_timestamp(31, 5, 59)), local_timestamp(31, 6))


class TelegramTransportTests(unittest.TestCase):
    def test_protected_messages_always_target_configured_owner(self):
        api = Telegram("123:fixture", 42)
        response = MagicMock(status_code=200)
        response.json.return_value = {"ok": True, "result": {"message_id": 22}}
        with patch.object(api.session, "post", return_value=response) as request:
            self.assertEqual(api.send("fixture"), 22)
        self.assertEqual(request.call_args.kwargs["json"], {"chat_id": 42, "text": "fixture", "protect_content": True})
        self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_transport_does_not_expose_request_details(self):
        api = Telegram("123:fixture", 42)
        with patch.object(api.session, "post", side_effect=requests.Timeout("private-fixture")):
            with self.assertRaises(TelegramError) as caught:
                api.poll(10)
        self.assertNotIn("private-fixture", str(caught.exception))


class DeploymentConfigTests(unittest.TestCase):
    def test_package_contains_only_allowlisted_settings_and_is_encrypted(self):
        config = {name: "fixture" for name in SECRET_KEYS}
        config.update({
            "TELEGRAM_BOT_TOKEN": "123:fixture",
            "TELEGRAM_OWNER_ID": "42",
            "LUXMED_TELEGRAM_PROFILES": json.dumps({
                f"patient{n}": {"name": f"Fixture {n}", "child": n > 1} for n in range(1, 4)
            }),
            "UNRELATED_SETTING": "excluded",
        })
        key = PrivateKey.generate()
        public = base64.b64encode(bytes(key.public_key)).decode("ascii")
        ciphertext = package_settings(config, public)
        self.assertNotIn(b"fixture", ciphertext)
        decoded = json.loads(SealedBox(key).decrypt(ciphertext))
        self.assertEqual(decoded, {name: config[name] for name in SECRET_KEYS})
