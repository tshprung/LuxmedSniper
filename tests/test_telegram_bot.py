import base64
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from nacl.public import PrivateKey, SealedBox

from luxmed_sniper import WARSAW, Appointment, LuxMedPortal, Luxmed, PortalError
from telegram_bot import Profile, SearchBot, Telegram, TelegramError
from telegram_calendar import CalendarAvailability, CalendarUnavailable
from telegram_configuration import SECRET_KEYS, booking_patients, package_settings, trusted_devices
from telegram_free_text import FreeText, redacted_request
from telegram_requests import INTERNIST_PHONE_SERVICE
from telegram_state import SearchState, next_search_time


def local_timestamp(day, hour, minute=0):
    return int(dt.datetime(2030, 3, day, hour, minute, tzinfo=WARSAW).timestamp())


class CalendarTests(unittest.TestCase):
    def test_free_busy_uses_only_intervals_and_checks_errors_for_each_calendar(self):
        calendar = CalendarAvailability({
            "client_id": "fixture", "client_secret": "fixture", "refresh_token": "fixture",
            "calendar_ids": ["first", "second"],
        })
        start = dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW)
        end = start + dt.timedelta(minutes=15)
        with patch.object(calendar, "post", side_effect=[
            {"access_token": "fixture", "expires_in": 3600},
            {"calendars": {"first": {"busy": []}, "second": {"busy": [{"start": start.isoformat(), "end": end.isoformat()}]}}},
            {"calendars": {"first": {"busy": []}, "second": {"errors": [{"reason": "notFound"}]}}},
        ]) as post:
            self.assertTrue(calendar.busy(start, end))
            with self.assertRaises(CalendarUnavailable):
                calendar.busy(start, end)
            self.assertEqual(post.call_args.kwargs["json"], {
                "timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": "first"}, {"id": "second"}],
            })


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
        self.bot = SearchBot(self.telegram, self.state, self.profiles, clock=lambda: self.now, booking_enabled=True)

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

    def test_portal_session_is_replaced_at_token_expiry(self):
        with patch("telegram_bot.LuxMedPortal") as portal:
            first, replacement = MagicMock(), MagicMock()
            first.token_expires_at = self.now + 60
            replacement.token_expires_at = None
            portal.side_effect = [first, replacement]
            self.assertIs(self.bot.portal_for("patient1"), first)
            self.now += 59
            self.assertIs(self.bot.portal_for("patient1"), first)
            self.now += 1
            self.assertIs(self.bot.portal_for("patient1"), replacement)
            first.session.close.assert_called_once()
            self.assertEqual(portal.call_count, 2)

    def test_device_registration_is_used_only_for_its_patient_and_before_expiry(self):
        device = {"expires": self.now + 60}
        self.bot.device_credentials = {"patient1": device}
        with patch("telegram_bot.LuxMedPortal") as portal:
            self.bot.portal_for("patient1")
            portal.assert_called_with(self.profiles["patient1"].credentials, trusted_device=device)
            self.bot.portal_for("patient2")
            portal.assert_called_with(self.profiles["patient2"].credentials)
            self.bot.close_portals()
            self.now += 60
            self.bot.portal_for("patient1")
            portal.assert_called_with(self.profiles["patient1"].credentials)

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
        self.update(1, "/find Child | routine visit")
        self.bot.flush()
        date = dt.datetime(2030, 3, 31, tzinfo=WARSAW)
        slots = [Appointment(date.replace(hour=hour), 2, "Fixture clinic", f"Doctor {hour}", 12, doctor_id=hour)
                 for hour in range(9, 14)]
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
            self.assertIn("Checking the exact slot", self.state.next_message()["text"])
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

    def test_unnamed_clinicians_in_saved_results_are_omitted_from_list_and_details(self):
        self.update(1, "/find Adult | Fixture service")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            portal.return_value.search.return_value = [
                Appointment(dt.datetime(2030, 3, 31, 10, tzinfo=WARSAW), 2, "Fixture clinic",
                            "None None None", 12, requires_preparation=True),
            ]
            self.bot.search_due()
        listing = self.state.next_message()["text"]
        self.assertIn("1. 31.03 10:00 · Fixture clinic\n", listing)
        self.assertNotIn("None", listing)
        self.bot.flush()
        self.update(2, "Details 1")
        details = self.state.next_message()["text"]
        self.assertIn("Option 1. 31.03 10:00 · Fixture clinic\n", details)
        self.assertIn("preparation instructions", details)
        self.assertNotIn("None", details)

    def test_booking_asks_for_approval_when_calendar_is_unavailable_or_short_notice(self):
        self.update(1, "/find Child | routine visit")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            client.resolve_service.return_value = 12
            start = dt.datetime.fromtimestamp(self.now + 5 * 3600, WARSAW)
            slot = Appointment(start, 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9,
                               booking_term={"cityId": 5, "end": start + dt.timedelta(minutes=15)})
            client.search.return_value = [slot]
            client.is_booking_blocked_by_penalty.return_value = False
            client.has_booking_conflict.return_value = False
            client.reserve_standard_visit.return_value = 543
            client.booked_visit_ids.return_value = {543}
            self.bot.search_due()
            self.bot.flush()
            self.update(2, "Book 1")
            self.bot.flush()
            self.bot.booking_due()
            prompt = self.state.next_message()["text"]
            self.assertIn("within six hours", prompt)
            self.assertIn("couldn’t check your Google Calendar", prompt)
            self.assertIn("Approve 1", prompt)
            client.reserve_standard_visit.assert_not_called()
            self.bot.flush()
            self.update(3, "Approve 1")
            self.bot.flush()
            self.bot.booking_due()
            client.reserve_standard_visit.assert_called_once()
            self.assertIn("Booked and verified in LUX MED", self.state.next_message()["text"])
            self.bot.flush()
            self.update(4, "Book 1")
            self.bot.booking_due()
            client.reserve_standard_visit.assert_called_once()
            self.assertIn("Already booked", self.state.next_message()["text"])

    def test_clear_booking_submits_once_and_verification_failure_is_not_retried(self):
        self.bot.calendar = MagicMock()
        self.bot.calendar.busy.return_value = False
        self.update(1, "/find Child | routine visit")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            start = dt.datetime.fromtimestamp(self.now + 8 * 3600, WARSAW)
            slot = Appointment(start, 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9,
                               booking_term={"cityId": 5, "end": start + dt.timedelta(minutes=15)})
            client.resolve_service.return_value = 12
            client.search.return_value = [slot]
            client.is_booking_blocked_by_penalty.return_value = False
            client.has_booking_conflict.return_value = False
            client.reserve_standard_visit.return_value = 543
            client.booked_visit_ids.return_value = set()
            self.bot.search_due()
            self.bot.flush()
            self.update(2, "Book 1")
            self.bot.flush()
            self.bot.booking_due()
            client.reserve_standard_visit.assert_called_once()
            self.assertIn("booking outcome is unclear", self.state.next_message()["text"])
            self.bot.flush()
            self.update(3, "Book 1")
            self.bot.booking_due()
            self.assertIn("prior booking for this patient is unverified", self.state.next_message()["text"])
            client.reserve_standard_visit.assert_called_once()

    def test_regular_phone_booking_from_search_to_verified_appointment(self):
        self.bot.allowed_booking_patients = frozenset({"patient1"})
        catalog = [{"name": INTERNIST_PHONE_SERVICE, "id": 30,
                    "isTelemedicine": True, "isVideoConsultation": False, "isPoz": False}]
        slot = {
            "dateTimeFrom": "2030-03-31T10:00:00+02:00", "dateTimeTo": "2030-03-31T10:15:00+02:00",
            "clinicGroupId": 4, "clinicId": 12, "clinic": "Fixture clinic", "cityId": None,
            "serviceId": 30, "roomId": 19, "scheduleId": 20,
            "doctor": {"id": 9, "firstName": "Fixture", "lastName": "Doctor", "academicTitle": None},
            "isTelemedicine": True, "isAdditional": False, "isImpediment": False,
        }
        availability = {
            "isNotAvailableInCity": False, "isNotAvailableInContract": False,
            "isNotAvailableInAreaOfCare": False, "blockedByAccountLevel": False,
        }
        valuation = {"valuationType": 1, "price": 0, "isReferralRequired": False}
        hold = {"hasErrors": False, "hasWarnings": False, "value": {
            "temporaryReservationId": 135, "askForReferral": False, "valuations": [valuation],
        }}
        terms = {"termsForService": {"termsForDays": [{"terms": [slot]}]}}
        portal = LuxMedPortal
        base = portal.NEW_PORTAL_URL
        replies = {
            ("POST", portal.LUXMED_LOGIN_URL): {"token": "fixture-token"},
            ("GET", portal.DICTIONARY_SERVICES_URL): catalog,
            ("GET", portal.NEW_PORTAL_RESERVATION_URL): {**terms, "correlationId": "fixture-correlation"},
            ("GET", portal.NON_SHOW_PENALTIES_URL): {"canReserve": True},
            ("GET", f"{base}/Reservation/GetLimits"): {"canReserve": True},
            ("GET", f"{base}/Reservation/GetReferralsForReservation"): {"referrals": []},
            ("GET", f"{base}/security/getforgerytoken"): {"token": "fixture-request-token"},
            ("GET", f"{base}/PNM/Index"): None,
            ("GET", f"{base}/Reservation/serviceVariantAvailabilityDetails"): availability,
            ("GET", f"{base}/terms/getevents"): {"events": []},
            ("POST", f"{base}/Reservation/LockTerm"): hold,
            ("POST", f"{base}/Reservation/Confirm"): {"hasErrors": False, "value": {"reservationId": 246}},
            ("GET", portal.UPCOMING_VISITS_URL): {"events": [{"eventType": 7, "status": 1, "eventId": 246}]},
        }

        def request(method, url, **kwargs):
            result = requests.Response()
            result.status_code = 200
            result._content = json.dumps(replies[method, url]).encode()
            return result

        with (
            patch("luxmed_sniper.datetime.datetime", wraps=dt.datetime) as clock,
            patch.object(requests.Session, "request", side_effect=request) as http,
        ):
            clock.now.return_value = dt.datetime.fromtimestamp(self.now, dt.UTC)
            self.update(1, "Find an internist phone consultation for Adult in Wrocław")
            self.bot.flush()
            self.bot.search_due()
            self.assertIn("Book <option number>", self.state.next_message()["text"])
            self.bot.flush()
            self.update(2, "Book 1")
            self.bot.flush()
            self.bot.booking_due()
            self.assertIn("Approve 1", self.state.next_message()["text"])
            reserve = (f"{base}/Reservation/LockTerm", f"{base}/Reservation/Confirm")
            self.assertFalse(any(call.args[1] in reserve for call in http.call_args_list))
            self.bot.flush()
            self.update(3, "Approve 1")
            self.bot.flush()
            self.bot.booking_due()
            self.assertIn("Booked and verified in LUX MED", self.state.next_message()["text"])

        lock, confirm = [call for call in http.call_args_list if call.args[1] in reserve]
        self.assertIs(lock.kwargs["json"]["isTelemedicine"], True)
        self.assertIs(lock.kwargs["json"]["isPoz"], False)
        self.assertIs(lock.kwargs["json"]["isVideoConsultation"], False)
        self.assertIsNone(lock.kwargs["json"]["cityId"])
        self.assertEqual(lock.kwargs["json"]["timeFrom"], "10:00")
        self.assertEqual(confirm.kwargs["json"]["temporaryReservationId"], 135)
        self.assertEqual(http.call_args.args, ("GET", portal.UPCOMING_VISITS_URL))

    def test_nfz_internist_phone_slot_remains_in_the_portal(self):
        self.bot.allowed_booking_patients = frozenset({"patient1"})
        self.update(1, f"/find Adult | {INTERNIST_PHONE_SERVICE} NFZ (POZ)")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            client.resolve_service.return_value = 12
            client.search.return_value = [
                Appointment(dt.datetime.fromtimestamp(self.now + 8 * 3600, WARSAW),
                            2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9),
            ]
            self.bot.search_due()
            self.assertIn("Details <option number>", self.state.next_message()["text"])
            self.bot.flush()
            self.update(2, "Book 1")
            self.assertIn("needs to be booked in the Patient Portal", self.state.next_message()["text"])
            self.bot.booking_due()
            client.reserve_standard_visit.assert_not_called()

    def test_disabled_booking_returns_portal_link_without_accessing_luxmed(self):
        self.bot.booking_enabled = False
        self.update(1, "/find Child | routine visit")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            client.resolve_service.return_value = 12
            client.search.return_value = [
                Appointment(dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW),
                            2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9),
            ]
            self.bot.search_due()
            self.bot.flush()
            self.update(2, "Book 1")
            self.bot.booking_due()
            self.assertIn("Direct booking is unavailable", self.state.next_message()["text"])
            client.check_booking_eligibility.assert_not_called()
            client.reserve_standard_visit.assert_not_called()

    def test_booking_pilot_keeps_other_profiles_searchable_but_excludes_their_bookings(self):
        self.bot.allowed_booking_patients = frozenset({"patient1"})
        self.update(1, "/find Child | routine visit")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            client.resolve_service.return_value = 12
            client.search.return_value = [
                Appointment(dt.datetime.fromtimestamp(self.now + 7 * 3600, WARSAW),
                            2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9),
            ]
            self.bot.search_due()
            self.assertIn("Details <option number>", self.state.next_message()["text"])
            self.bot.flush()
            self.update(2, "Book 1")
            self.assertIn("Direct booking is unavailable", self.state.next_message()["text"])
            self.bot.flush()
            self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM booking_requests").fetchone()[0], 0)

            with self.state.db:
                request = self.state.request_booking(1, 1, "patient2", "older request")
                self.state.booking_status(request["id"], "approval")
            self.update(3, f"Approve {request['id']}")
            self.assertEqual(self.state.db.execute("SELECT status FROM booking_requests").fetchone()[0], "approval")
            with self.state.db:
                self.state.booking_status(request["id"], "queued")
            self.bot.booking_due()
            self.assertEqual(self.state.db.execute("SELECT status FROM booking_requests").fetchone()[0], "queued")
            client.check_booking_eligibility.assert_not_called()
            client.reserve_standard_visit.assert_not_called()

    def test_questionnaire_denial_pauses_booking_and_preserves_searches(self):
        self.update(1, "/find Adult | Internista")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            client.resolve_service.return_value = 12
            client.search.return_value = [
                Appointment(dt.datetime(2030, 3, 31, hour, tzinfo=WARSAW),
                            2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9)
                for hour in (11, 12)
            ]
            client.is_booking_blocked_by_penalty.return_value = False
            client.check_booking_eligibility.side_effect = PortalError(
                "access-denied", suspend=True, stage="questionnaire", http_status=401,
            )
            self.bot.search_due()
            self.bot.flush()
            self.update(2, "Book 1")
            self.update(3, "Book 2")
            self.bot.flush()
            self.bot.booking_due()
            self.bot.booking_due()
            self.assertIn("Direct booking is paused", self.state.next_message()["text"])
            self.assertTrue(self.state.booking_paused("patient1"))
            self.assertFalse(self.state.account_paused("patient1"))
            self.assertFalse(self.state.booking_paused("patient2"))
            client.check_booking_eligibility.assert_called_once()
            client.reserve_standard_visit.assert_not_called()
            self.bot.flush()
            self.update(4, "Book 2")
            self.assertIn("Direct booking is paused", self.state.next_message()["text"])
            self.bot.flush()
            self.update(5, "/find Adult | Internista")
            self.bot.search_due()
            self.assertEqual(client.search.call_count, 2)
            self.update(6, "/resume Adult")
            self.assertFalse(self.state.booking_paused("patient1"))

    def test_sick_child_search_shows_questionnaire_and_preparation_with_portal_handoff(self):
        self.update(1, "/find Child | sick kids")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            client.resolve_service.return_value = 12
            client.search.return_value = [
                Appointment(dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW), 2, "Fixture clinic",
                            "Fixture doctor", 12, doctor_id=9, requires_preparation=True),
            ]
            self.bot.search_due()
            message = self.state.next_message()["text"]
            self.assertIn("requires a medical questionnaire", message)
            self.assertIn("Zobacz szczegóły", message)
            self.assertIn("Reply Details <option number>", message)
            self.assertNotIn("Reply Book", message)
            self.bot.flush()
            self.update(2, "Details 1")
            self.assertIn("requires a medical questionnaire", self.state.next_message()["text"])
            self.bot.flush()
            self.update(3, "Book 1")
            self.bot.booking_due()
            message = self.state.next_message()["text"]
            self.assertIn("Book this visit in the Patient Portal", message)
            self.assertIn("Zobacz szczegóły", message)
            client.check_booking_eligibility.assert_not_called()
            client.reserve_standard_visit.assert_not_called()

    def test_reply_to_previous_list_does_not_select_from_new_list_with_same_search_number(self):
        date = dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW)
        with patch("telegram_bot.LuxMedPortal") as portal:
            portal.return_value.resolve_service.return_value = 12
            for update_id, patient, doctor in ((1, "Child", "First doctor"), (2, "Other", "Second doctor")):
                self.update(update_id, f"/find {patient} | routine visit")
                self.telegram.send.return_value = 40 + update_id * 2
                self.bot.flush()
                portal.return_value.search.return_value = [
                    Appointment(date, 2, "Fixture clinic", doctor, 12, doctor_id=update_id),
                ]
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
        self.assertIn("LUX MED appointment assistant", self.state.next_message()["text"])
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
    def test_direct_booking_patient_scope_overrides_the_legacy_switch(self):
        self.assertEqual(booking_patients({}), frozenset())
        self.assertEqual(booking_patients({"LUXMED_DIRECT_BOOKING": "1"}), {"patient1", "patient2", "patient3"})
        self.assertEqual(booking_patients({
            "LUXMED_DIRECT_BOOKING": "1", "LUXMED_DIRECT_BOOKING_PATIENTS": "patient1",
        }), {"patient1"})
        with self.assertRaisesRegex(ValueError, "Invalid direct booking patient list"):
            booking_patients({"LUXMED_DIRECT_BOOKING_PATIENTS": "patient1,unknown"})

    def test_device_credentials_load_per_profile_and_require_secure_portal_scope(self):
        cookie = {"name": "FixtureDevice", "value": "fixture", "domain": "portalpacjenta.luxmed.pl",
                  "path": "/PatientPortal", "secure": True, "expires": 2000000000}
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CREDENTIALS_DIRECTORY": directory}):
            credential = Path(directory) / "luxmed-device-patient1"
            credential.write_text(json.dumps(cookie), encoding="utf-8")
            self.assertEqual(trusted_devices(("patient1", "patient2")), {"patient1": cookie})
            credential.write_text(json.dumps({**cookie, "domain": "example.org"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid LUX MED device credential"):
                trusted_devices(("patient1", "patient2"))

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
