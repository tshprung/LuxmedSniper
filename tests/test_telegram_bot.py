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
from telegram_free_text import FreeText, redacted_request
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
        self.update(1, "Find a pediatrician for Child in Wroclaw")
        self.assertIn("Which type of pediatric visit should I search for Child?", self.state.next_message()["text"])
        self.assertEqual(self.state.active(), [])
        with self.state.db:
            pending = json.loads(self.state.get_setting("pending"))
            pending["polish"] = True
            self.state.set_setting("pending", json.dumps(pending))
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
        self.update(4, f"Stop {row['id']}")
        self.assertEqual(self.state.active(), [])

    def test_each_new_search_checks_immediately_and_reuses_the_patient_session(self):
        self.update(1, "Find an internist for Adult")
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.side_effect = [12, 13, 14]
            portal.return_value.search.return_value = []
            self.bot.search_due()
            portal.assert_called_once_with(self.profiles["patient1"].credentials)
            self.assertEqual(portal.return_value.search.call_count, 1)
            self.now += 300
            self.update(2, "/find Adult | Dermatolog")
            self.bot.search_due()
            portal.assert_called_once_with(self.profiles["patient1"].credentials)
            self.assertEqual(portal.return_value.search.call_count, 2)
            self.now += 300
            with self.state.db:
                self.state.db.execute(
                    "INSERT INTO accounts (patient, next_attempt) VALUES (?, ?)", ("patient2", self.now + 3000),
                )
            self.update(3, "/find Child | health check")
            self.bot.search_due()
            self.assertEqual(portal.call_count, 2)
            portal.assert_called_with(self.profiles["patient2"].credentials)
            self.bot.search_due()
            self.assertEqual(portal.call_count, 2)
            self.assertEqual(portal.return_value.search.call_count, 3)
        self.assertEqual([row["next_due"] for row in self.state.active()], [
            self.now + 3000, self.now + 3300, self.now + 3600,
        ])

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

    def test_first_empty_result_is_reported_and_later_checks_are_visible_in_active(self):
        self.update(1, "Find an internist for Adult")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = []
            self.bot.search_due()
            message = self.state.next_message()["text"]
            self.assertIn("#1 · Adult · Internista · Wrocław", message)
            self.assertIn("No matching slots in the next 14 days", message)
            self.assertIn("Next check: 30.03 11:00", message)
            self.bot.flush()
            self.now += 3600
            self.bot.search_due()
            self.assertIsNone(self.state.next_message())
        self.update(2, "/active")
        message = self.state.next_message()["text"]
        self.assertIn("Last check 30.03 11:00: no matching slots", message)
        self.assertIn("Next check 30.03 12:00 Warsaw time", message)

    def test_numbered_options_support_booking_steps_more_and_continue(self):
        self.update(1, "/find Child | sick kids")
        self.bot.flush()
        date = dt.datetime(2030, 3, 31, tzinfo=WARSAW)
        slots = [Appointment(date.replace(hour=hour), 2, "Fixture clinic", f"Doctor {hour}", 12) for hour in range(9, 14)]
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = slots
            self.bot.search_due()
            first = self.state.next_message()["text"]
            self.assertIn("1. 31.03 09:00", first)
            self.assertIn("3. 31.03 11:00", first)
            self.assertNotIn("4. 31.03", first)
            self.telegram.send.return_value = 43
            self.bot.flush()
            self.update(2, "Book 4", reply_to=43)
            self.assertIn("Choose an option number shown", self.state.next_message()["text"])
            self.bot.flush()
            self.update(3, "More", reply_to=43)
            self.assertIn("4. 31.03 12:00", self.state.next_message()["text"])
            self.assertIn("5. 31.03 13:00", self.state.next_message()["text"])
            self.bot.flush()
            self.update(4, "Book 4")
            self.assertIn("Option 4. 31.03 12:00", self.state.next_message()["text"])
            self.assertIn("No appointment has been booked", self.state.next_message()["text"])
            self.assertNotIn("within six hours", self.state.next_message()["text"])
            self.bot.flush()
            self.update(5, "find something else", reply_to=43)
            self.update(6, "Continue", reply_to=43)
            continued, = self.state.active()
            self.assertEqual(continued["patient"], "patient2")
            self.assertEqual(continued["next_due"], self.now + 3600)
            self.bot.search_due()
            portal.return_value.search.assert_called_once()
            self.now += 3600
            self.bot.flush()
            portal.return_value.search.return_value = [*slots, Appointment(date.replace(hour=14), 2, "New clinic", "New doctor", 12)]
            self.bot.search_due()
        final = self.state.next_message()["text"]
        self.assertIn("1. 31.03 14:00", final)
        self.assertNotIn("09:00", final)

    def test_booking_handoff_warns_for_appointments_less_than_six_hours_away(self):
        self.update(1, "/find Child | sick kids")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = [
                Appointment(dt.datetime.fromtimestamp(self.now + hours * 3600, WARSAW),
                            2, "Fixture clinic", "Fixture doctor", 12)
                for hours in (5, 6)
            ]
            self.bot.search_due()
        self.bot.flush()
        self.update(2, "Book 1")
        self.assertIn("within six hours", self.state.next_message()["text"])
        self.bot.flush()
        self.update(3, "Book 2")
        self.assertNotIn("within six hours", self.state.next_message()["text"])

    def test_reply_to_previous_list_does_not_select_from_new_list_with_same_search_number(self):
        date = dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW)
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            for update_id, patient, doctor in ((1, "Child", "First doctor"), (2, "Other", "Second doctor")):
                self.update(update_id, f"/find {patient} | routine visit")
                self.telegram.send.return_value = 40 + update_id * 2
                self.bot.flush()
                portal.return_value.search.return_value = [Appointment(date, 2, "Fixture clinic", doctor, 12)]
                self.bot.search_due()
                self.telegram.send.return_value += 1
                self.bot.flush()
        self.update(3, "Book 1", reply_to=43)
        self.assertIn("First doctor", self.state.next_message()["text"])
        self.bot.flush()
        self.update(4, "Book 1")
        self.assertIn("Second doctor", self.state.next_message()["text"])

    def test_change_keeps_id_and_patient_and_checks_the_new_criteria(self):
        self.update(1, "Find an internist for Adult")
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = []
            self.bot.search_due()
        self.update(2, "/change 1 | Dermatolog | Kraków")
        row, = self.state.active()
        self.assertEqual((row["id"], row["patient"], row["service"], row["city"]), (1, "patient1", "Dermatolog", "Kraków"))
        self.assertEqual(row["next_due"], self.now)
        self.assertIsNone(row["last_checked"])
        self.assertFalse(row["first_result_sent"])

    def test_migration_releases_unchecked_searches_and_preserves_recurring_times(self):
        self.update(1, "Find an internist for Adult")
        self.update(2, "/find Adult | Dermatolog")
        with self.state.db:
            self.state.db.execute("DELETE FROM settings WHERE key = 'search_schedule_per_search'")
            self.state.db.execute("UPDATE searches SET next_due = ?", (self.now + 3600,))
            self.state.record_result(1, self.now, "no-slots")
        self.state.db.close()
        self.state = SearchState(self.path, clock=lambda: self.now + 300)
        first, second = self.state.active()
        self.assertEqual(first["next_due"], self.now + 3600)
        self.assertEqual(second["next_due"], self.now + 300)
        self.assertEqual([row["id"] for row in self.state.claim_batch(self.now + 300)], [2])

    def test_paused_account_cannot_run_a_new_search(self):
        self.update(1, "/find Child | health check")
        with patch("telegram_bot.LuxMedPortal", side_effect=PortalError("rate-limited", suspend=True)):
            self.bot.search_due()
        self.update(2, "/find Child | routine visit")
        self.update(3, "/find Other | routine visit")
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.search.return_value = []
            self.bot.search_due()
            self.bot.search_due()
            portal.assert_called_once_with(self.profiles["patient3"].credentials)

    def test_pediatric_selection_keeps_date_time_and_clinic_preferences(self):
        self.update(1, "Find a pediatrician for Child next week, after 16:00, near Dominikańska.")
        self.update(2, "4")
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertEqual(criteria.service, "Konsultacja pediatryczna - gabinet dzieci zdrowych")
        self.assertEqual(criteria.preferences.first_date, "2030-04-01")
        self.assertEqual(criteria.preferences.last_date, "2030-04-07")
        self.assertEqual(criteria.preferences.start_minute, 960)
        self.assertTrue(criteria.preferences.preferred_clinic)
        self.update(3, "Change 1 to after 17:00")
        edited, = self.state.active()
        self.assertEqual(edited["id"], row["id"])
        self.assertEqual(self.state.criteria(edited).preferences.start_minute, 1020)
        self.assertEqual(self.state.criteria(edited).preferences.clinic, "Dominikańska")

    def test_sick_kids_nfz_after_patient_and_any_time(self):
        self.update(1, "Find a pediatrician for child sick kids nfz next week any time")
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertEqual(criteria.patient, "patient2")
        self.assertEqual(criteria.service, "Konsultacja pediatryczna - gabinet dzieci chorych NFZ (POZ)")
        self.assertFalse(criteria.telephone)
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.last_date), ("2030-04-01", "2030-04-07"))
        self.assertIsNone(criteria.preferences.time_rule)
        self.assertIn("gabinet dzieci chorych NFZ (POZ)", self.state.next_message()["text"])
        self.assertEqual(self.state.get_setting("pending"), "")
        self.update(2, "Change 1 to after 16:00")
        self.update(3, "Change 1 to any time")
        edited = self.state.criteria(self.state.active()[0])
        self.assertIsNone(edited.preferences.time_rule)
        self.assertEqual(edited.preferences.first_date, "2030-04-01")

    def test_unknown_pediatric_visit_type_does_not_start_a_generic_search(self):
        self.update(1, "Find a pediatrician for Child sick kids NFZ by video next week")
        self.assertEqual(self.state.active(), [])
        self.assertIn("couldn't match that pediatric visit type", self.state.next_message()["text"])

    def test_pediatric_search_for_adult_requires_a_new_request(self):
        self.update(1, "Find a pediatrician for Adult next week, after 16:00, near Dominikańska.")
        self.assertIn("Adult is configured as an adult, and pediatrics is for children.", self.state.next_message()["text"])
        self.assertIn("Please send a new request", self.state.next_message()["text"])
        self.assertEqual(self.state.active(), [])
        self.assertEqual(self.state.get_setting("pending"), "")
        self.bot.flush()
        self.update(2, "4")
        with patch("telegram_bot.LuxMedPortal") as portal:
            self.bot.search_due()
            portal.assert_not_called()
        self.assertEqual(self.state.active(), [])

    def test_service_mismatch_applies_to_exact_portal_labels_and_edits(self):
        self.update(1, "/find Adult | Konsultacja pediatryczna - gabinet dzieci zdrowych")
        self.assertEqual(self.state.active(), [])
        self.assertIn("pediatrics is for children", self.state.next_message()["text"])
        self.bot.flush()
        self.update(2, "Find an internist for Child")
        self.assertEqual(self.state.active(), [])
        self.assertIn("Child is configured as a child", self.state.next_message()["text"])
        self.bot.flush()
        self.update(3, "Find an internist for Adult")
        self.update(4, "/change 1 | pediatrician")
        row, = self.state.active()
        self.assertEqual((row["patient"], row["service"]), ("patient1", "Internista"))

    def test_numbers_reset_when_queue_is_empty_and_old_replies_are_safe(self):
        self.update(1, "Find an internist for Adult")
        self.bot.flush()
        with self.state.db:
            self.state.suspend("patient1")
        self.update(2, "/find Child | health check")
        self.assertEqual([row["display_number"] for row in self.state.active()], [1, 2])
        self.update(3, "Stop 1")
        self.update(4, "/find Other | routine visit")
        self.assertEqual([row["display_number"] for row in self.state.active()], [2, 3])
        self.update(5, "Stop 2")
        self.update(6, "Stop 3")
        self.update(7, "/find Other | routine visit")
        row, = self.state.active()
        self.assertEqual((row["id"], row["display_number"]), (4, 1))
        self.update(8, "Stop 1", reply_to=42)
        self.update(9, "Change 1 to after 17:00", reply_to=42)
        self.assertEqual(self.state.active()[0]["id"], 4)
        self.assertIsNone(self.state.criteria(self.state.active()[0]).preferences.time_rule)
        self.update(10, "Change 1 to after 16:00")
        self.assertEqual(self.state.criteria(self.state.active()[0]).preferences.start_minute, 960)
        self.update(11, "Stop 1")
        self.assertEqual(self.state.active(), [])

    def test_preferences_filter_returned_slots_and_expire_without_portal_call(self):
        self.update(1, "Find an internist for Adult tomorrow after 16:00 with Dr Anna Smith, near Dominikańska")
        self.bot.flush()
        date = dt.datetime(2030, 3, 31, tzinfo=WARSAW)
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = [
                Appointment(date.replace(hour=16), 1, "Plac Dominikański", "Anna Smith", 12),
                Appointment(date.replace(hour=17), 1, "Plac Dominikański", "John Smith", 12),
                Appointment(date.replace(hour=17), 2, "Swobodna", "Anna Smith", 12),
                Appointment(date.replace(day=30, hour=17), 1, "Plac Dominikański", "Anna Smith", 12),
            ]
            self.bot.search_due()
            self.assertIn("Other clinics have matches", self.state.next_message()["text"])
            self.assertEqual(len(self.state.active()), 1)
            self.bot.flush()
            self.now = int(dt.datetime(2030, 4, 1, 10, tzinfo=WARSAW).timestamp())
            self.bot.search_due()
            portal.assert_called_once()
        self.assertEqual(self.state.active(), [])
        self.assertIn("date window has ended", self.state.next_message()["text"])

    def test_preferred_time_returns_closest_matching_slot_on_same_day(self):
        self.update(1, "/find Adult | internist | Wrocław | around 4pm")
        self.bot.flush()
        date = dt.datetime(2030, 3, 31, tzinfo=WARSAW)
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = [
                Appointment(date.replace(hour=14, minute=59), 1, "Outside", "Anna Smith", 12),
                Appointment(date.replace(hour=15), 1, "Earlier", "Anna Smith", 12),
                Appointment(date.replace(hour=16, minute=10), 1, "Closer", "Anna Smith", 12),
            ]
            self.bot.search_due()
        result = self.state.next_message()["text"]
        self.assertNotIn("Outside", result)
        self.assertLess(result.index("Closer"), result.index("Earlier"))

    def test_invalid_time_filter_does_not_start_search(self):
        self.update(1, "Find an internist for Adult after 25:00")
        self.assertEqual(self.state.active(), [])
        self.assertIn("valid appointment time", self.state.next_message()["text"])

    def test_warsaw_close_and_dst(self):
        self.assertEqual(next_search_time(local_timestamp(30, 22)), local_timestamp(30, 22))
        self.assertEqual(next_search_time(local_timestamp(30, 22, 1)), local_timestamp(31, 6))
        self.assertEqual(next_search_time(local_timestamp(31, 5, 59)), local_timestamp(31, 6))

    def test_free_text_path_keeps_names_and_service_local(self):
        self.bot.free_text = FreeText("fixture-key")
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"intent":"search"}'}]}],
        }
        with patch.object(self.bot.free_text.session, "post", return_value=response) as api:
            self.update(1, "Could you check whether a pediatrician is available for Child in Wrocław?")
            payload = api.call_args.kwargs["json"]
            self.assertEqual(payload["input"], "could you check whether a SERVICE is available for PERSON in CITY")
            self.assertFalse(payload["store"])
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertEqual(self.state.active(), [])
            self.assertIn("Which type of pediatric visit should I search for Child?", self.state.next_message()["text"])
            self.update(2, "4")
            self.update(3, "/active")
            api.assert_called_once()
        row, = self.state.active()
        self.assertEqual(row["patient"], "patient2")
        self.assertIn("gabinet dzieci zdrowych", row["service"])

    def test_free_text_does_not_send_unrecognized_details_or_broaden_constraints(self):
        for text in (
            "Please find an internist for Adult tomorrow",
            "Please find an internist for Adult at 9",
            "Please book an internist for Adult",
            "Please find an internist for Adult or Child",
            "Could you find a pediatric visit for Child with details unknown-private-note?",
            "Czy możesz sprawdzić dostępność pediatry dla Dziecka we Wrocławiu?",
        ):
            with self.subTest(text=text):
                self.assertIsNone(redacted_request(text, self.bot.aliases, self.bot.cities))

    def test_english_help_and_portal_service_names(self):
        self.update(1, "/start")
        self.assertIn("Read-only appointment search", self.state.next_message()["text"])
        self.assertNotIn("/szukaj", self.state.next_message()["text"])
        self.update(2, "/find Adult | Dermatolog | Wrocław")
        row, = self.state.active()
        self.assertEqual((row["service"], row["city"]), ("Dermatolog", "Wrocław"))


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
            "OPENAI_API_KEY": "sk-fixture",
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
        self.assertEqual(decoded, {name: config[name] for name in (*SECRET_KEYS, "OPENAI_API_KEY")})
