import base64
import datetime as dt
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from nacl.public import PrivateKey, SealedBox

from luxmed_prices import ListedPrice
from luxmed_questionnaire import Questionnaire, QuestionnaireError
from luxmed_sniper import (
    WARSAW, Appointment, BookingQuestionnaireRequired, CancellationTerms, LuxMedPortal,
    Luxmed, PackageCoverage, PortalError, ScheduledVisit,
)
from telegram_bot import Profile, SearchBot, Telegram, TelegramError
from telegram_calendar import CalendarAvailability, CalendarUnavailable
from telegram_configuration import SECRET_KEYS, booking_patients, package_settings, trusted_devices
from telegram_free_text import FreeText, redacted_request
from telegram_preferences import matching_slots, parse_preferences
from telegram_requests import Criteria, INTERNIST_NFZ_SERVICE, INTERNIST_PHONE_SERVICE, pediatric_choice
from telegram_state import SearchState, next_search_time


def local_timestamp(day, hour, minute=0):
    return int(dt.datetime(2030, 3, day, hour, minute, tzinfo=WARSAW).timestamp())


class CalendarTests(unittest.TestCase):
    def test_primary_calendar_free_busy_accepts_the_resolved_calendar_id(self):
        calendar = CalendarAvailability({"calendar_ids": ["primary"]})
        start = dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW)
        with (
            patch.object(calendar, "_headers", return_value={}),
            patch.object(calendar, "post", return_value={"calendars": {"actual-id": {"busy": []}}}) as post,
        ):
            self.assertFalse(calendar.busy(start, start + dt.timedelta(minutes=15)))
        self.assertEqual(post.call_args.kwargs["json"]["items"], [{"id": "primary"}])

    def test_calendar_insert_recognizes_an_existing_event_after_retry(self):
        calendar = CalendarAvailability({})
        start = dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW)
        end = start + dt.timedelta(minutes=15)
        key = "fixture123"
        with (
            patch.object(calendar, "_headers", return_value={}),
            patch("telegram_calendar.requests.post") as insert,
            patch("telegram_calendar.requests.get") as get,
        ):
            insert.return_value.__enter__.return_value.status_code = 409
            existing = get.return_value.__enter__.return_value
            existing.status_code = 200
            existing.json.return_value = {"id": key, "status": "confirmed"}
            calendar.add_booking("primary", key, start, end, patient_name="Fixture patient")
            existing.json.return_value = {"id": key, "status": "cancelled"}
            with self.assertRaises(CalendarUnavailable):
                calendar.add_booking("primary", key, start, end, patient_name="Fixture patient")
        self.assertEqual(insert.call_args.kwargs["json"], {
            "id": key, "summary": "Fixture patient · Appointment", "visibility": "private",
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
        })
        self.assertTrue(get.call_args.args[0].endswith(f"/calendars/primary/events/{key}"))

    def test_remove_calendar_event_checks_absence_and_calendar_access(self):
        calendar = CalendarAvailability({})
        with (
            patch.object(calendar, "_headers", return_value={}),
            patch("telegram_calendar.requests.delete") as delete,
            patch("telegram_calendar.requests.get") as get,
        ):
            removed = delete.return_value.__enter__.return_value
            listing = get.return_value.__enter__.return_value
            removed.status_code = 204
            calendar.remove_booking("primary", "fixture123")
            removed.status_code = 410
            removed.json.return_value = {"error": {"errors": [{"reason": "deleted"}]}}
            calendar.remove_booking("primary", "fixture123")
            get.assert_not_called()
            removed.status_code = 404
            listing.status_code = 200
            listing.json.return_value = {"kind": "calendar#events", "accessRole": "owner"}
            calendar.remove_booking("primary", "fixture123")
            listing.status_code = 404
            with self.assertRaises(CalendarUnavailable):
                calendar.remove_booking("primary", "fixture123")
            self.assertEqual(delete.call_args.args[0],
                             "https://www.googleapis.com/calendar/v3/calendars/primary/events/fixture123")
            self.assertEqual(delete.call_args.kwargs["params"], {"sendUpdates": "none"})
            self.assertEqual(get.call_args.args[0],
                             "https://www.googleapis.com/calendar/v3/calendars/primary/events")

    def test_detailed_calendar_event_uses_clinic_only_for_in_person_visits(self):
        calendar = CalendarAvailability({})
        start = dt.datetime(2030, 3, 31, 11, tzinfo=WARSAW)
        end = start + dt.timedelta(minutes=15)
        with (
            patch.object(calendar, "_headers", return_value={}),
            patch("telegram_calendar.requests.post") as insert,
        ):
            response = insert.return_value.__enter__.return_value
            response.status_code = 200
            response.json.return_value = {"id": "fixture123"}
            for telephone in (False, True):
                with self.subTest(telephone=telephone):
                    calendar.add_booking(
                        "primary", "fixture123", start, end, patient_name="Fixture patient", service="Fixture service",
                        clinic="Fixture clinic", clinician="Dr. A & B", telephone=telephone,
                    )
                    payload = insert.call_args.kwargs["json"]
                    self.assertEqual(payload, {
                        "id": "fixture123", "summary": "Fixture patient · Fixture service — LUX MED",
                        "visibility": "private",
                        "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
                        "description": "Telephone consultation<br>Dr. A &amp; B" if telephone else "Dr. A &amp; B",
                        "reminders": {"useDefault": False, "overrides": [
                            {"method": "popup", "minutes": 10}, {"method": "popup", "minutes": 30},
                        ]},
                        **({} if telephone else {"location": "Fixture clinic"}),
                    })

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

    def update(self, number, text, *, sender=100, chat_id=100, chat_type="private", reply_to=None, reply_text=None):
        message = {
            "from": {"id": sender},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        }
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to, "text": reply_text}
        self.bot.accept({"update_id": number, "message": message})

    def tap(self, number, action, message_id=42, *, sender=100, chat_id=100, chat_type="private"):
        self.bot.accept({"update_id": number, "callback_query": {
            "id": f"fixture-{number}", "from": {"id": sender}, "data": action, "message": {
                "message_id": message_id, "chat": {"id": chat_id, "type": chat_type},
            },
        }})

    def test_owner_only_private_chat(self):
        self.update(1, "Find an internist for Adult", sender=200, chat_id=200)
        self.update(2, "Find an internist for Adult", chat_id=-100, chat_type="group")
        self.assertEqual(self.state.active(), [])
        self.assertIsNone(self.state.next_message())
        self.assertEqual(self.state.get_setting("offset"), "3")

    def test_appointments_read_the_requested_patient_timeline_without_ai_or_booking(self):
        self.bot.free_text = MagicMock()
        clients = {patient: MagicMock() for patient in self.profiles}
        starts = dt.datetime(2030, 4, 1, 13, tzinfo=WARSAW)
        clients["patient1"].scheduled_visits.return_value = []
        clients["patient2"].scheduled_visits.return_value = [
            ScheduledVisit(starts, "Fixture service", "Fixture clinic", "Fixture clinician", ""),
        ]
        clients["patient3"].scheduled_visits.return_value = []
        with patch.object(self.bot, "portal_for", side_effect=clients.__getitem__) as portal_for:
            self.update(1, "Show my next appointments", sender=200, chat_id=200)
            self.update(2, "Show upcoming visits for Child", chat_id=-100, chat_type="group")
            portal_for.assert_not_called()
            self.update(3, "Show me the next LUX MED booking for me")
            self.assertIn("no upcoming appointments for Adult", self.state.next_message()["text"])
            self.bot.flush()
            self.update(4, "Could you show me Child’s next appointments?")
            text = self.state.next_message()["text"]
            self.assertIn("for Child (Warsaw time)", text)
            self.assertIn("01 Apr 2030, 13:00 · Fixture service", text)
            self.assertIn("1. 01 Apr 2030", text)
            self.assertIn("Fixture clinic · Fixture clinician", text)
            self.bot.flush()
            self.update(5, "/appointments Other")
            self.bot.flush()
            self.update(6, "Show upcoming visits for Child or Other")
            self.assertIn("Whose appointments", self.state.next_message()["text"])
        self.assertEqual([call.args[0] for call in portal_for.call_args_list], ["patient1", "patient2", "patient3"])
        self.bot.free_text.criteria.assert_not_called()
        for client in clients.values():
            client.reserve_standard_visit.assert_not_called()
        self.assertEqual(self.state.active(), [])

    def test_coverage_checks_both_requested_services_and_no_booking_actions(self):
        self.bot.free_text = MagicMock()
        portal = MagicMock()
        portal.medical_package_ids.return_value = ("fixture-package",)
        portal.medical_package_services.return_value = [
            {"id": 111, "name": "Konsultacja kardiologiczna"},
            {"id": 112, "name": "Konsultacja kardiologiczna - telefoniczna"},
            {"id": 221, "name": "Założenie Holtera EKG"},
            {"id": 222, "name": "Założenie Holtera EKG - z 12 odprowadzeniami"},
            {"id": 223, "name": "Założenie Holtera ciśnieniowego"},
        ]
        portal.medical_package_coverage.side_effect = [PackageCoverage("paid"), PackageCoverage("paid")]
        with patch.object(self.bot, "portal_for", return_value=portal) as portal_for:
            self.update(1, "Is cardiolog covered for me?", sender=200)
            self.update(2, "Is cardiolog covered for me?", chat_type="group")
            portal_for.assert_not_called()
            self.update(3, "Are a cardiologist visit and a heart Holter covered for me?")
        portal_for.assert_called_once_with("patient1")
        self.assertEqual([(call[0], call.args) for call in portal.method_calls], [
            ("medical_package_ids", ()), ("medical_package_services", ()),
            ("medical_package_coverage", ("fixture-package", 111)),
            ("medical_package_coverage", ("fixture-package", 221)),
        ])
        text = self.state.next_message()["text"]
        self.assertIn("LUX MED subscription coverage for Adult", text)
        self.assertIn("Konsultacja kardiologiczna\nPayable", text)
        self.assertIn("Założenie Holtera EKG\nPayable", text)
        self.assertEqual(self.state.active(), [])
        self.bot.free_text.chat_action.assert_not_called()
        self.bot.free_text.criteria.assert_not_called()

    def test_ambiguous_holter_offers_read_only_choices_for_the_named_patient(self):
        portal = MagicMock()
        portal.medical_package_ids.return_value = ("plan1", "plan2")
        portal.medical_package_services.return_value = [
            {"id": 221, "name": "Założenie Holtera EKG"},
            {"id": 223, "name": "Założenie Holtera ciśnieniowego"},
        ]
        portal.medical_package_coverage.side_effect = [PackageCoverage("covered", True), PackageCoverage("unknown")]
        with patch.object(self.bot, "portal_for", return_value=portal) as portal_for:
            self.update(1, "Does my package cover a Holter for Child?")
            menu = self.state.next_message()
            self.assertIn("coverage for Child", menu["text"])
            self.assertIn("Założenie Holtera EKG", menu["text"])
            actions = {action for row in json.loads(menu["buttons"]) for _, action in row}
            self.assertEqual(actions, {"coverage patient2 221", "coverage patient2 223"})
            portal.medical_package_coverage.assert_not_called()
            self.bot.flush()
            self.tap(2, "coverage patient2 221")
        self.assertEqual([call.args[0] for call in portal_for.call_args_list], ["patient2", "patient2"])
        text = self.state.next_message()["text"]
        self.assertIn("Założenie Holtera EKG", text)
        self.assertIn("Package 1 · Included in the subscription · Referral required", text)
        self.assertIn("Package 2 · LUX MED coverage could not be confirmed", text)

    def test_payable_service_can_include_the_lowest_public_wroclaw_price(self):
        self.bot.public_prices = MagicMock()
        self.bot.public_prices.lookup.return_value = ListedPrice(Decimal("329"), ("Clinic A", "Clinic B"))
        portal = MagicMock()
        portal.medical_package_ids.return_value = ("package",)
        portal.medical_package_services.return_value = [{"id": 111, "name": "Konsultacja kardiologiczna"}]
        portal.medical_package_coverage.return_value = PackageCoverage("paid")
        with patch.object(self.bot, "portal_for", return_value=portal):
            self.update(1, "Is a cardiologist covered for me?")
        result = self.state.next_message()["text"]
        self.assertIn("Payable\nLowest LUX MED public price in Wrocław · 329 zł · 2 clinics at this price", result)
        self.assertNotIn("price unavailable", result)
        self.bot.public_prices.lookup.assert_called_once_with("Konsultacja kardiologiczna")

    def test_coverage_does_not_guess_on_a_catalog_miss_or_retry_a_rate_limit(self):
        portal = MagicMock()
        portal.medical_package_ids.return_value = ("plan",)
        portal.medical_package_services.return_value = [{"id": 221, "name": "Założenie Holtera EKG"}]
        with patch.object(self.bot, "portal_for", return_value=portal) as portal_for:
            self.update(1, "Is Holter EKG covered for someone else?")
            portal_for.assert_not_called()
            self.bot.flush()
            self.update(2, "/coverage Child | missing fixture service")
            self.assertIn("No match in the package catalog", self.state.next_message()["text"])
            portal.medical_package_coverage.assert_not_called()
            self.bot.flush()
            portal.medical_package_coverage.side_effect = PortalError(
                "rate-limited", suspend=True, stage="coverage-details", http_status=429,
            )
            self.update(3, "/coverage Child | Holter EKG")
            self.assertTrue(self.state.account_paused("patient2"))
            self.assertFalse(self.state.account_paused("patient1"))
            self.bot.flush()
            self.update(4, "/coverage Child | Holter EKG")
            portal.medical_package_coverage.assert_called_once_with("plan", 221)
            self.assertIn("LUX MED access for Child is paused", self.state.next_message()["text"])

    def test_numbered_cancel_selection_uses_the_latest_patient_and_rejects_an_old_button(self):
        self.telegram.send.side_effect = iter(range(40, 100))
        clients = {patient: MagicMock() for patient in self.profiles}
        starts = dt.datetime(2030, 4, 1, 13, tzinfo=WARSAW)
        clients["patient1"].scheduled_visits.return_value = [
            ScheduledVisit(starts, "First fixture visit", "", "", ""),
            ScheduledVisit(starts + dt.timedelta(hours=1), "Second fixture visit", "Test clinic", "", ""),
        ]
        clients["patient2"].scheduled_visits.return_value = [
            ScheduledVisit(starts, "Child fixture visit", "", "", ""),
        ]
        with patch.object(self.bot, "portal_for", side_effect=clients.__getitem__) as portal_for:
            self.update(1, "/appointments Adult")
            original_buttons = json.loads(self.state.next_message()["buttons"])
            self.assertEqual(original_buttons[1][0][0], "Cancel 2")
            self.bot.flush()
            self.update(2, "Cancel 2")
            self.assertIn("Appointment 2 for Adult", self.state.next_message()["text"])
            self.assertIn("Second fixture visit", self.state.next_message()["text"])
            self.assertIn("Are you sure", self.state.next_message()["text"])
            proceed = json.loads(self.state.next_message()["buttons"])[0][0][1]
            self.bot.flush()
            self.tap(3, proceed, 41)
            self.assertIn("Odwołaj", self.state.next_message()["text"])
            self.bot.flush()
            self.update(4, "/appointments Child")
            child_button = json.loads(self.state.next_message()["buttons"])[0][0][1]
            self.bot.flush()
            self.tap(5, child_button, 43)
            self.assertIn("Child fixture visit", self.state.next_message()["text"])
            self.bot.flush()
            self.tap(6, original_buttons[0][0][1], 40)
            self.assertIn("get the current visit numbers", self.state.next_message()["text"])
            self.bot.flush()
            self.update(7, "Cancel 1")
            self.assertIn("Child fixture visit", self.state.next_message()["text"])
            self.assertEqual(portal_for.call_count, 2)
            for patient in ("patient1", "patient2"):
                self.assertEqual([call[0] for call in clients[patient].method_calls], ["scheduled_visits"])

    def test_direct_cancellation_requires_confirming_the_exact_displayed_visit(self):
        self.telegram.send.side_effect = iter(range(40, 100))
        self.now = local_timestamp(30, 23)
        self.bot.calendar = MagicMock()
        self.bot.calendar_destination = "primary"
        self.bot.calendar.remove_booking.side_effect = [CalendarUnavailable, None]
        starts = dt.datetime(2030, 4, 1, 13, tzinfo=WARSAW)
        with self.state.db:
            for patient, telephone, event_id in (
                ("patient1", False, "selected"), ("patient1", True, "othermode"), ("patient2", False, "otherpatient"),
            ):
                criteria = Criteria(patient, "Fixture service", 5, "Wrocław", telephone)
                search_id, _, _, _ = self.state.add(criteria, self.now)
                booking = self.state.request_booking(search_id, 1, patient, event_id)
                self.state.booking_status(booking["id"], "booked", reservation_id=81)
                self.state.queue_calendar_event(booking["id"], event_id, "primary", int(starts.timestamp()),
                                                int(starts.timestamp()) + 900, self.now)
                self.state.finish_calendar_attempt(booking["id"], self.now, True)
        visit = ScheduledVisit(starts, "Fixture service", "Fixture clinic", "Fixture clinician", "",
                               event_id=81, event_type=1, cancellation_instance=82)
        client = MagicMock()
        client.scheduled_visits.side_effect = [[visit], [visit], []]
        client.cancellation_terms.return_value = CancellationTerms(6, True)
        with patch.object(self.bot, "portal_for", return_value=client):
            self.update(1, "/appointments Adult")
            self.bot.flush()
            self.update(2, "Cancel 1")
            message = self.state.next_message()
            self.assertIn("Appointment 1 for Adult", message["text"])
            self.assertIn("01 Apr 2030, 13:00 · Fixture service", message["text"])
            self.assertIn("Fixture clinic · Fixture clinician", message["text"])
            self.assertIn("Are you sure", message["text"])
            self.assertIn("within 6 hours", message["text"])
            self.assertIn("may affect future booking", message["text"])
            confirm, keep = json.loads(message["buttons"])[0]
            self.bot.flush()
            self.bot.cancellation_due()
            self.bot.sync_calendar_due()
            self.bot.calendar.remove_booking.assert_not_called()
            client.cancel_standard_visit.assert_not_called()
            self.tap(3, keep[1], 41)
            self.assertIn("Appointment kept", self.state.next_message()["text"])
            self.bot.flush()
            self.update(4, "Cancel 1")
            self.bot.flush()
            self.tap(5, confirm[1], 41)
            self.update(6, "Confirm cancellation", reply_to=43)
            client.cancel_standard_visit.assert_not_called()
            self.bot.flush()
            self.bot.cancellation_due()
            self.assertIn("Cancelled and verified in LUX MED", self.state.next_message()["text"])
            self.bot.flush()
            self.bot.sync_calendar_due()
            self.assertIn("retry removing", self.state.next_message()["text"])
            self.bot.sync_calendar_due()
            self.bot.calendar.remove_booking.assert_called_once_with("primary", "selected")
            self.bot.flush()
            self.state.db.close()
            self.state = self.bot.state = SearchState(self.path)
            self.now += 900
            self.bot.sync_calendar_due()
            self.assertIn("Cleared the matching entry from Google Calendar", self.state.next_message()["text"])
            self.bot.sync_calendar_due()
            self.assertEqual(self.bot.calendar.remove_booking.call_count, 2)
            self.bot.calendar.remove_booking.assert_called_with("primary", "selected")
            self.assertIsNone(self.state.due_calendar_removal("primary", self.now))
            self.bot.cancellation_due()
            client.cancel_standard_visit.assert_called_once_with(visit)
            self.assertEqual(client.cancellation_terms.call_count, 3)

    def test_confirmed_cancellation_stops_if_provider_conditions_change(self):
        starts = dt.datetime(2030, 4, 1, 13, tzinfo=WARSAW)
        visit = ScheduledVisit(starts, "Fixture service", "Fixture clinic", "", "",
                               event_id=91, event_type=1, cancellation_instance=92)
        client = MagicMock()
        client.scheduled_visits.return_value = [visit]
        client.cancellation_terms.side_effect = [CancellationTerms(), CancellationTerms(6, True)]
        with patch.object(self.bot, "portal_for", return_value=client):
            self.update(1, "/appointments Adult")
            self.bot.flush()
            self.update(2, "Cancel 1")
            self.bot.flush()
            self.update(3, "Confirm cancellation", reply_to=42)
            self.bot.flush()
            self.bot.cancellation_due()
            message = self.state.next_message()
            self.assertIn("cancellation conditions changed", message["text"])
            self.assertIn("Upcoming appointments", message["buttons"])
            client.cancel_standard_visit.assert_not_called()

    def test_interrupted_cancellation_is_never_resubmitted(self):
        with self.state.db:
            self.state.queue_cancellation("patient1", 981, 1, self.now + 7200, "fixture")
            request = self.state.claim_cancellation()
            self.state.cancellation_status(request["id"], "submitting")
        with patch.object(self.bot, "portal_for") as portal_for:
            self.bot.recover_cancellations()
            self.assertIn("I couldn’t verify the cancellation", self.state.next_message()["text"])
            self.bot.cancellation_due()
            portal_for.assert_not_called()
        with self.state.db:
            self.assertFalse(self.state.queue_cancellation("patient1", 981, 1, self.now + 7200, "fixture"))

    def test_appointments_respect_an_account_pause_even_before_first_search(self):
        with patch.object(self.bot, "portal_for", side_effect=PortalError("access-denied", suspend=True, stage="login")) as portal:
            self.update(1, "Show next appointments for Child")
            self.bot.flush()
            self.update(2, "Show next appointments for Child")
            self.assertIn("access for Child is paused", self.state.next_message()["text"])
        portal.assert_called_once_with("patient2")
        self.assertTrue(self.state.account_paused("patient2"))

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

    def test_number_or_button_selects_a_slot_before_explicit_booking(self):
        self.telegram.send.side_effect = iter(range(40, 100))
        self.update(1, "Find allergist for me")
        self.bot.flush()
        client = MagicMock()
        client.resolve_service.return_value = 12
        client.search.return_value = [
            Appointment(dt.datetime.fromtimestamp(self.now + hour * 3600, WARSAW), 2, "Fixture clinic",
                        "Fixture doctor", 12, doctor_id=9)
            for hour in (5, 6, 7, 8)
        ]
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        self.assertIn(["Option 3", "select 3"], json.loads(self.state.next_message()["buttons"])[0])
        self.bot.flush()
        results_message = 41
        self.update(2, "3")
        selected = self.state.next_message()
        self.assertIn("Option 3.", selected["text"])
        self.assertIn(["Book option 3", "book 3"], json.loads(selected["buttons"])[0])
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM booking_requests").fetchone()[0], 0)
        self.bot.flush()
        self.update(3, "give me options")
        self.assertIn(["Option 3", "select 3"], json.loads(self.state.next_message()["buttons"])[0])
        self.bot.flush()
        self.tap(4, "select 3", results_message)
        self.bot.flush()
        selected_message = 44
        self.tap(5, "book 3", selected_message, sender=200, chat_id=200)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM booking_requests").fetchone()[0], 0)
        self.tap(6, "book 3", selected_message)
        request, = self.state.db.execute("SELECT option_number, status FROM booking_requests")
        self.assertEqual(tuple(request), (3, "queued"))
        self.telegram.acknowledge.assert_any_call("fixture-6")
        client.reserve_standard_visit.assert_not_called()

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
            invalid = self.state.next_message()
            self.assertIn("Choose an option from this list", invalid["text"])
            self.assertIn(["See options", "options"], json.loads(invalid["buttons"])[0])
            self.bot.flush()
            self.update(3, "More", reply_to=43)
            self.assertIn("4. 31.03 12:00", self.state.next_message()["text"])
            self.assertIn("5. 31.03 13:00", self.state.next_message()["text"])
            self.bot.flush()
            self.update(4, "Book 4")
            confirmation = self.state.next_message()["text"]
            self.assertIn("Option 4. 31.03 12:00", confirmation)
            self.assertIn("Checking the exact slot", confirmation)
            self.assertNotIn("Booking request", confirmation)
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
        self.assertIn("1. 31.03 10:00 · Fixture clinic · Patient Portal\n", listing)
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
            self.assertIn("Approval code: 1", prompt)
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

    def test_late_approval_resumes_the_original_search_once_at_next_opening(self):
        self.now = local_timestamp(30, 21)
        self.update(1, "/find Child | routine visit | Wrocław | tomorrow, clinic Test Center")
        client = MagicMock()
        client.resolve_service.return_value = 12
        client.is_booking_blocked_by_penalty.return_value = client.has_booking_conflict.return_value = False
        starts = dt.datetime.fromtimestamp(local_timestamp(31, 6), WARSAW)
        selected = Appointment(starts, 2, "Test Center", "First fixture", 12, doctor_id=9,
                               booking_term={"cityId": 5, "end": starts + dt.timedelta(minutes=15)})
        other = Appointment(starts + dt.timedelta(hours=3), 2, "Test Center", "Second fixture", 12, doctor_id=10)
        client.search.return_value = [selected, other]
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
            self.bot.flush()
            original = self.state.criteria(self.state.results())
            self.update(2, "Book 1")
            self.bot.flush()
            self.bot.booking_due()
            self.assertIn("Approve by 31.03 05:00 Warsaw time", self.state.next_message()["text"])
            self.bot.flush()
            self.now = local_timestamp(31, 5)
            self.update(3, "Approve 1")
            self.assertIn("Approval 1 has expired", self.state.next_message()["text"])
            self.bot.booking_due()
            self.update(4, "Approve 1")
            followup, = self.state.active()
            self.assertEqual(self.state.criteria(followup), original)
            self.assertEqual(followup["next_due"], local_timestamp(31, 6))
            self.assertEqual([slot["doctor_id"] for slot in json.loads(followup["excluded_slots"])], [9])
            self.assertTrue(self.state.booking_approval_expired(1))
            client.reserve_standard_visit.assert_not_called()

    def test_unanswered_approval_does_not_restart_when_the_search_has_a_booking(self):
        self.update(1, "/find Child | routine visit")
        start = self.now + 2 * 3600
        with self.state.db:
            self.state.finish(1, "found")
            self.state.save_results(1, [
                {"start": start, "doctor": "Fixture A", "clinic": "Test clinic"},
                {"start": start + 1800, "doctor": "Fixture B", "clinic": "Test clinic"},
            ])
            first = self.state.request_booking(1, 1, "patient2", json.dumps([start, 2, 9, 12]))
            second = self.state.request_booking(1, 2, "patient2", json.dumps([start + 1800, 2, 10, 12]))
            self.state.booking_status(first["id"], "approval", reason="Fixture approval")
            self.state.booking_status(second["id"], "booked")
        self.now = start - 3600
        self.bot.booking_due()
        self.assertTrue(self.state.booking_approval_expired(first["id"]))
        self.assertEqual(self.state.active(), [])
        self.assertEqual(self.state.results(1)["status"], "found")

    def test_additional_allergy_slot_is_identified_and_regular_slot_is_listed_first(self):
        self.update(1, "Find allergist for me")
        self.bot.flush()
        client = MagicMock()
        client.resolve_service.return_value = 12
        client.search.return_value = [
            Appointment(dt.datetime.fromtimestamp(self.now + 3600, WARSAW),
                        2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9, is_additional=True),
            Appointment(dt.datetime.fromtimestamp(self.now + 7200, WARSAW),
                        2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9),
        ]
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        results = self.state.next_message()["text"]
        self.assertRegex(results, r"2\. [^\n]+ · Patient Portal")
        self.assertIn("Book <option number>", results)
        self.bot.flush()
        self.update(2, "2")
        message = self.state.next_message()
        self.assertIn("additional slot", message["text"])
        self.assertNotIn("Book option", message["buttons"])
        self.bot.flush()
        self.update(3, "Book 2")
        self.assertIn("additional slot", self.state.next_message()["text"])
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM booking_requests").fetchone()[0], 0)
        self.bot.flush()
        self.update(4, "1")
        self.assertIn("Book option 1", self.state.next_message()["buttons"])

    def test_unavailable_slot_offers_more_from_the_same_result_list(self):
        self.telegram.send.side_effect = iter(range(40, 100))
        self.update(1, "/find Adult | Internista")
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            client.resolve_service.return_value = 12
            client.is_booking_blocked_by_penalty.return_value = False
            slots = [Appointment(dt.datetime.fromtimestamp(self.now + hour * 3600, WARSAW),
                                 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9)
                     for hour in (5, 6, 7, 8)]
            client.search.side_effect = [slots, []]
            self.bot.search_due()
            self.bot.flush()
            self.update(2, "Book 1")
            self.bot.booking_due()
            self.bot.flush()
            message = self.telegram.send.call_args
            self.assertIn("That exact slot is no longer available", message.args[0])
            self.assertIn(["More", "more"], message.kwargs["buttons"][0])
            self.tap(3, "more", 43)
            self.assertIn("4. ", self.state.next_message()["text"])
            client.reserve_standard_visit.assert_not_called()

    def test_clear_booking_submits_once_and_verification_failure_is_not_retried(self):
        self.bot.calendar = MagicMock()
        self.bot.calendar_destination = "primary"
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
            self.assertIsNone(self.state.due_calendar_event("primary", self.now))
            self.bot.flush()
            self.update(3, "Book 1")
            self.bot.booking_due()
            self.assertIn("prior booking for this patient is unverified", self.state.next_message()["text"])
            client.reserve_standard_visit.assert_called_once()

    def test_verified_booking_retries_calendar_delivery_with_same_event_id(self):
        self.bot.calendar = MagicMock()
        self.bot.calendar.busy.return_value = False
        self.bot.calendar.add_booking.side_effect = [CalendarUnavailable, CalendarUnavailable, None]
        self.bot.calendar_destination = "primary"
        self.bot.calendar_detail_patients = frozenset({"patient1"})
        self.update(1, "/find Adult | Internista")
        self.bot.flush()
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            start = dt.datetime.fromtimestamp(self.now + 9 * 3600, WARSAW)
            slot = Appointment(start, 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9,
                               booking_term={"cityId": 5, "end": start + dt.timedelta(minutes=15)})
            client.resolve_service.return_value = 12
            client.search.return_value = [
                Appointment(start - dt.timedelta(minutes=15), 2, "Other clinic", "Other doctor", 12, doctor_id=8),
                slot,
            ]
            client.is_booking_blocked_by_penalty.return_value = False
            client.has_booking_conflict.return_value = False
            client.reserve_standard_visit.return_value = 543
            client.booked_visit_ids.return_value = {543}
            self.bot.search_due()
            self.bot.flush()
            self.update(2, "Book 2")
            self.bot.flush()
            self.bot.booking_due()
            self.assertIn("Booked and verified in LUX MED", self.state.next_message()["text"])
            self.bot.flush()
            self.bot.sync_calendar_due()
            delayed = self.state.next_message()["text"]
            self.assertIn("Adult · 30.03 19:00 (Warsaw time)", delayed)
            self.assertIn("I couldn’t confirm the Google Calendar event yet. I’ll retry automatically.", delayed)
            self.bot.flush()
            self.bot.sync_calendar_due()
            self.bot.calendar.add_booking.assert_called_once()
            self.now += 15 * 60
            self.bot.sync_calendar_due()
            self.assertIsNone(self.state.next_message())
            self.now += 15 * 60
            self.bot.sync_calendar_due()
            self.assertIn("Adult · 30.03 19:00 (Warsaw time)\nAdded the appointment to Google Calendar.",
                          self.state.next_message()["text"])
            self.bot.sync_calendar_due()
            first, second, retry = self.bot.calendar.add_booking.call_args_list
            self.assertEqual([first, second], [retry, retry])
            self.assertEqual(retry.args[0], "primary")
            self.assertEqual(retry.args[2], start)
            self.assertEqual(retry.kwargs, {
                "patient_name": "Adult",
                "service": "Internista", "clinic": "Fixture clinic", "clinician": "Fixture doctor",
                "telephone": False,
            })
            client.reserve_standard_visit.assert_called_once()

    def test_regular_phone_booking_from_search_to_verified_appointment(self):
        self.bot.allowed_booking_patients = frozenset({"patient1"})
        catalog = [{"name": INTERNIST_PHONE_SERVICE, "id": 30,
                    "isTelemedicine": True, "isVideoConsultation": False, "isPoz": False}]
        slot = {
            "dateTimeFrom": "2030-03-31T10:00:00+02:00", "dateTimeTo": "2030-03-31T10:15:00+02:00",
            "clinicGroupId": 4, "clinicId": 12, "clinic": None,
            "serviceId": 30, "roomId": 19, "scheduleId": 20,
            "doctor": {"id": 9},
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
        day = "2030-03-31"
        terms = {"termsForService": {"termsForDays": [], "termsInfoForDays": [{
            "day": day, "isLimitedDay": True, "termsCounter": {"termsNumber": 1},
        }]}}
        portal = LuxMedPortal
        base = portal.NEW_PORTAL_URL
        replies = {
            ("POST", portal.LUXMED_LOGIN_URL): {"token": "fixture-token"},
            ("GET", portal.DICTIONARY_SERVICES_URL): catalog,
            ("GET", f"{portal.DICTIONARY_URL}/Doctors"): [
                {"id": 9, "firstName": "Fixture", "lastName": "Doctor", "academicTitle": None},
            ],
            ("GET", portal.NEW_PORTAL_RESERVATION_URL): {**terms, "correlationId": "index-correlation"},
            ("GET", f"{base}/terms/oneDayTerms"): {
                "termsForDay": {"day": day, "terms": [slot]}, "correlationId": "daily-correlation",
            },
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
            self.assertIn("Nationwide (phone)", self.state.next_message()["text"])
            self.assertIn("Fixture Doctor", self.state.next_message()["text"])
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
        self.assertEqual(lock.kwargs["json"]["doctor"]["lastName"], "Doctor")
        self.assertEqual(lock.kwargs["json"]["correlationId"], "daily-correlation")
        self.assertEqual(lock.kwargs["json"]["timeFrom"], "10:00")
        self.assertEqual(confirm.kwargs["json"]["temporaryReservationId"], 135)
        self.assertEqual(http.call_args.args, ("GET", portal.UPCOMING_VISITS_URL))
        indexes = [call.kwargs["params"] for call in http.call_args_list
                   if call.args == ("GET", portal.NEW_PORTAL_RESERVATION_URL)]
        daily_calls = [call.kwargs["params"] for call in http.call_args_list
                       if call.args == ("GET", f"{base}/terms/oneDayTerms")]
        self.assertEqual(len(indexes), 3)
        self.assertEqual(len(daily_calls), 3)
        for index, daily in zip(indexes, daily_calls, strict=True):
            self.assertEqual(daily["processId"], index["processId"])
            self.assertEqual(daily["delocalized"], "false")
            self.assertEqual(daily["searchDateFrom"], daily["searchDateTo"])
            self.assertEqual(daily["searchDateFrom"], day)
        self.assertEqual(daily_calls[-1]["doctorsIds"], [9])

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

    def test_allergy_replies_submit_one_form_and_require_approval_for_the_same_slot(self):
        self.telegram.send.side_effect = iter(range(40, 100))
        client = MagicMock()
        self.bot.portals["patient1"] = (self.now + 900, client)
        client.resolve_service.return_value = client.resolve_in_person_service.return_value = 12
        client.is_booking_blocked_by_penalty.return_value = client.has_booking_conflict.return_value = False
        client.reserve_standard_visit.return_value = 246
        client.booked_visit_ids.return_value = {246}
        start = dt.datetime.fromtimestamp(self.now + 8 * 3600, WARSAW)
        slot = Appointment(start, 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9,
                           booking_term={"cityId": 5, "end": start + dt.timedelta(minutes=15)})
        client.search.return_value = [slot]
        client.booking_questionnaire.return_value = {
            "id": "fixture-form", "inputService": {"servaId": 12},
            "questions": [
                {"id": "start", "questionType": 1, "name": "<p>First question</p>",
                 "additionalInfo": "<p>Fixture information</p>",
                 "answers": [{"id": "first-choice", "order": 1, "name": "Option A", "nextQuestionUuid": "route"}]},
                {"id": "route", "questionType": 5,
                 "answers": [{"id": "configured-link", "order": 1, "nextQuestionUuid": "finish"}]},
                {"id": "finish", "questionType": 4, "name": "Second question",
                 "answers": [{"id": "last-choice", "order": 1, "name": "Continue",
                              "triageResult": {"discriminator": "ServiceVariantResultDto", "servaId": 12}}]},
            ],
        }
        self.update(1, "/find me | Alergolog")
        self.bot.search_due()
        self.bot.flush()
        self.update(2, "Book 1")
        self.bot.booking_due()
        self.bot.flush()
        first = self.bot.questionnaire_booking.message_id
        self.assertIn("Fixture information", self.telegram.send.call_args.args[0])
        self.assertIn(("1. Option A", "answer 1"), self.telegram.send.call_args.kwargs["buttons"][0])
        self.update(3, "Approve 1")
        self.bot.booking_due()
        client.reserve_standard_visit.assert_not_called()
        self.tap(4, "answer 1", first)
        self.bot.flush()
        second = self.bot.questionnaire_booking.message_id
        self.assertNotEqual(first, second)
        self.tap(5, "stop questionnaire", first)
        self.assertEqual(self.bot.questionnaire_booking.message_id, second)
        self.assertIn("button has expired", self.state.next_message()["text"])
        self.bot.flush()
        self.tap(6, "options", 41)
        self.assertIn("Available slots", self.state.next_message()["text"])
        self.assertEqual(self.bot.questionnaire_booking.message_id, second)
        self.update(7, "1", reply_to=first)
        self.update(8, "1", reply_to=second, sender=200, chat_id=200)
        self.bot.booking_due()
        client.submit_booking_questionnaire.assert_not_called()
        self.bot.flush()
        self.update(9, "1", reply_to=second)
        self.bot.booking_due()
        client.submit_booking_questionnaire.assert_called_once()
        payload = client.submit_booking_questionnaire.call_args.args[0]
        self.assertEqual(payload["selectedAnswers"], [
            {"id": "first-choice"}, {"id": "configured-link"}, {"id": "last-choice"},
        ])
        receipt = client.search.call_args.kwargs["questionnaire"]
        self.assertEqual((payload["executionId"], payload["processId"]), (receipt.execution_id, receipt.process_id))
        client.reserve_standard_visit.assert_not_called()
        self.assertIn("Reply Approve 1 to book", self.state.next_message()["text"])
        self.bot.flush()
        self.update(10, "Approve 1")
        self.bot.booking_due()
        client.reserve_standard_visit.assert_called_once_with(
            slot, "Konsultacja alergologiczna", questionnaire=receipt,
        )
        self.assertEqual(client.submit_booking_questionnaire.call_count, 1)
        self.assertIsNone(self.bot.questionnaire_booking)

    def test_in_person_internist_questionnaire_rechecks_and_requires_approval(self):
        client = MagicMock()
        self.bot.portals["patient1"] = (self.now + 900, client)
        client.resolve_service.return_value = client.resolve_in_person_service.return_value = 12
        client.is_booking_blocked_by_penalty.return_value = client.has_booking_conflict.return_value = False
        client.reserve_standard_visit.return_value = 246
        client.booked_visit_ids.return_value = {246}
        start = dt.datetime.fromtimestamp(self.now + 8 * 3600, WARSAW)
        slot = Appointment(start, 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9,
                           booking_term={"cityId": 5, "end": start + dt.timedelta(minutes=15)})
        client.search.return_value = [slot]
        client.check_booking_eligibility.side_effect = BookingQuestionnaireRequired({
            "id": "fixture-form", "inputService": {"servaId": 12},
            "questions": [
                {"id": "start", "questionType": 1, "name": "Fixture question",
                 "answers": [{"id": "first", "order": 1, "name": "Option A", "nextQuestionUuid": "finish"}]},
                {"id": "finish", "questionType": 4, "name": "Second fixture question",
                 "answers": [{"id": "chosen", "order": 1, "name": "Option B",
                              "triageResult": {"discriminator": "ServiceVariantResultDto", "servaId": 12}}]},
            ],
        })
        self.update(1, "Find an internist for Adult")
        self.bot.search_due()
        self.bot.flush()
        self.update(2, "Book 1")
        self.bot.booking_due()
        self.bot.flush()
        self.assertIn("Adult · Internista", self.telegram.send.call_args.args[0])
        client.submit_booking_questionnaire.assert_not_called()
        self.update(3, "1")
        self.update(4, "Option 1")
        self.assertEqual(self.bot.questionnaire_booking.form.answer_ids, ["first"])
        self.bot.flush()
        self.update(5, "Option 1")
        self.bot.booking_due()
        receipt = client.search.call_args.kwargs["questionnaire"]
        self.assertEqual(client.submit_booking_questionnaire.call_args.args[0]["selectedAnswers"],
                         [{"id": "first"}, {"id": "chosen"}])
        client.reserve_standard_visit.assert_not_called()
        self.assertIn("Please confirm this internist appointment", self.state.next_message()["text"])
        self.update(6, "Approve 1")
        self.bot.booking_due()
        client.reserve_standard_visit.assert_called_once_with(slot, "Internista", questionnaire=receipt)
        client.submit_booking_questionnaire.assert_called_once()
        client.check_booking_eligibility.assert_called_once()

    def test_questionnaire_cannot_submit_an_alternate_service_outcome(self):
        form = Questionnaire({
            "id": "fixture-form", "inputService": {"servaId": 12},
            "questions": [{"id": "start", "questionType": 1, "name": "Fixture question",
                           "answers": [{"id": "alternate", "order": 1, "name": "Option B",
                                        "triageResult": {"discriminator": "ServiceVariantResultDto", "servaId": 13}}]}],
        }, 12, 5, self.now)
        form.choose(1)
        self.assertFalse(form.permits_original_service())
        with self.assertRaises(QuestionnaireError):
            form.submission(self.now)

    def test_allergy_approval_expires_if_the_worker_loses_its_session(self):
        client = MagicMock()
        self.bot.portals["patient1"] = (self.now + 900, client)
        client.resolve_service.return_value = client.resolve_in_person_service.return_value = 12
        client.is_booking_blocked_by_penalty.return_value = client.has_booking_conflict.return_value = False
        client.booking_questionnaire.return_value = None
        start = dt.datetime.fromtimestamp(self.now + 8 * 3600, WARSAW)
        client.search.return_value = [Appointment(
            start, 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9,
            booking_term={"cityId": 5, "end": start + dt.timedelta(minutes=15)},
        )]
        self.update(1, "/find me | Alergolog")
        self.bot.search_due()
        self.bot.flush()
        self.update(2, "Book 1")
        self.bot.booking_due()
        self.bot.flush()
        self.bot = SearchBot(self.telegram, self.state, self.profiles, clock=lambda: self.now, booking_enabled=True)
        self.bot.portals["patient1"] = (self.now + 900, client)
        self.update(3, "Approve 1")
        self.bot.flush()
        self.bot.booking_due()
        self.assertIn("session has ended", self.state.next_message()["text"])
        client.reserve_standard_visit.assert_not_called()
        client.booking_questionnaire.assert_called_once()

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

    def test_booking_hold_denial_leaves_searches_active_and_names_the_failed_step(self):
        self.update(1, "Find an internist for Adult")
        self.bot.flush()
        self.bot.calendar = MagicMock()
        self.bot.calendar.busy.return_value = False
        with patch("telegram_bot.LuxMedPortal") as portal:
            client = portal.return_value
            start = dt.datetime.fromtimestamp(self.now + 8 * 3600, WARSAW)
            client.resolve_service.return_value = 12
            client.search.return_value = [
                Appointment(start, 2, "Fixture clinic", "Fixture doctor", 12, doctor_id=9,
                            booking_term={"cityId": 5, "end": start + dt.timedelta(minutes=15)}),
            ]
            client.is_booking_blocked_by_penalty.return_value = False
            client.has_booking_conflict.return_value = False
            client.reserve_standard_visit.side_effect = PortalError(
                "access-denied", suspend=True, stage="booking-hold", http_status=403,
            )
            self.bot.search_due()
            self.bot.flush()
            self.update(2, "Book 1")
            self.bot.flush()
            with patch("sys.stderr") as stderr:
                self.bot.booking_due()
            message = self.state.next_message()["text"]
            self.assertIn("slot hold (HTTP 403)", message)
            self.assertIn("/resume", message)
            self.assertNotIn("https://", message)
            self.assertTrue(stderr.write.called)
            self.assertTrue(self.state.booking_paused("patient1"))
            self.assertFalse(self.state.account_paused("patient1"))
            self.bot.flush()
            self.update(3, "Find an internist for Adult")
            self.bot.search_due()
            self.assertEqual(client.search.call_count, 3)
            client.reserve_standard_visit.assert_called_once()

    def test_sick_child_phone_and_nfz_results_explain_the_questionnaire(self):
        for choice in ("sick visit by phone", "sick kids NFZ"):
            with self.subTest(choice=choice):
                notice, = self.bot.portal_notices(pediatric_choice(choice), [])
                self.assertIn("requires a medical questionnaire", notice)
        self.assertEqual(self.bot.portal_notices(pediatric_choice("routine visit"), []), [])

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

    def test_active_buttons_edit_filters_and_stop_the_selected_search(self):
        self.telegram.send.side_effect = iter(range(40, 100))
        self.update(1, "Find an internist for Adult")
        self.update(2, "Find an allergist for Adult after 17:00")
        self.bot.flush()
        self.update(3, "/active")
        buttons = json.loads(self.state.next_message()["buttons"])
        self.assertEqual(buttons, [
            [["Change #1", "change search 1"], ["Stop #1", "stop search 1"]],
            [["Change #2", "change search 2"], ["Stop #2", "stop search 2"]],
        ])
        self.bot.flush()
        self.tap(4, "change search 2", 42)
        prompt = self.state.next_message()["text"]
        menu = json.loads(self.state.next_message()["buttons"])
        self.assertIn("Konsultacja alergologiczna", prompt)
        self.assertIn(["Tomorrow", "filter search 2 Tomorrow"], menu[0])
        self.bot.flush()
        self.tap(5, "filter search 2 Tomorrow", 43)
        first, second = self.state.active()
        self.assertIsNone(self.state.criteria(first).preferences.first_date)
        prefs = self.state.criteria(second).preferences
        self.assertEqual((prefs.first_date, prefs.start_minute), ("2030-03-31", 17 * 60))
        self.update(6, "near Dominikańska", reply_to=43, reply_text=prompt)
        self.assertEqual(self.state.criteria(self.state.active()[1]).preferences.clinic, "Dominikańska")
        self.update(7, "| Kraków | any time", reply_to=43, reply_text=prompt)
        criteria = self.state.criteria(self.state.active()[1])
        self.assertEqual((criteria.city, criteria.preferences.start_minute), ("Kraków", None))
        self.tap(8, "stop search 1", 42)
        remaining, = self.state.active()
        self.assertEqual((remaining["id"], remaining["patient"]), (2, "patient1"))

    def test_old_active_buttons_and_edit_reply_cannot_target_reused_search_number(self):
        self.telegram.send.side_effect = iter(range(40, 100))
        self.update(1, "Find an internist for Adult")
        self.update(2, "/active")
        self.bot.flush()
        self.tap(3, "change search 1", 41)
        prompt = self.state.next_message()["text"]
        self.bot.flush()
        self.tap(4, "stop search 1", 41)
        self.update(5, "Find an allergist for Adult")
        self.bot.flush()
        self.tap(6, "stop search 1", 41)
        self.tap(7, "filter search 1 Tomorrow", 42)
        self.update(8, "after 16:00", reply_to=42, reply_text=prompt)
        row, = self.state.active()
        self.assertEqual((row["id"], row["display_number"]), (2, 1))
        self.assertIsNone(self.state.criteria(row).preferences.start_minute)
        self.assertIsNone(self.state.criteria(row).preferences.first_date)
        self.assertIn("That search has ended", self.state.next_message()["text"])

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

    def test_child_internist_suggests_pediatrics_and_pediatr_opens_the_same_menu(self):
        self.update(1, "Find an internist for Other tomorrow after 16:00")
        message = self.state.next_message()
        self.assertIn("an internist is an adult service", message["text"])
        self.assertIn("Which type of pediatric visit", message["text"])
        self.assertEqual(self.state.active(), [])
        first_choice = json.loads(message["buttons"])[0][0][1]
        self.bot.flush()
        self.tap(2, first_choice)
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertEqual((criteria.patient, criteria.service), ("patient3", pediatric_choice("sick visit")))
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.start_minute), ("2030-03-31", 960))
        self.bot.flush()
        self.update(3, "Find pediatr for Other")
        self.assertIn("Which type of pediatric visit", self.state.next_message()["text"])
        self.assertEqual(len(self.state.active()), 1)

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

    def test_ai_chat_routes_cancellation_review_to_the_named_patient(self):
        self.bot.free_text = FreeText("fixture-key")
        reply = MagicMock(status_code=200)
        reply.json.return_value = {
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": '{"intent":"cancel"}'},
            ]}],
        }
        starts = dt.datetime(2030, 4, 1, 13, tzinfo=WARSAW)
        clients = {patient: MagicMock() for patient in self.profiles}
        for patient in ("patient1", "patient2"):
            clients[patient].scheduled_visits.return_value = [ScheduledVisit(starts, "Fixture visit", "", "", "")]
        with (patch.object(self.bot, "portal_for", side_effect=clients.__getitem__),
              patch.object(self.bot.free_text.session, "post", return_value=reply) as api):
            self.update(1, "/appointments Adult")
            self.bot.flush()
            self.update(2, "Could you cancel appointment number 1 for Child please?")
            self.assertEqual(api.call_args.kwargs["json"]["input"],
                             "could you cancel appointment number NUMBER for PERSON please")
            self.assertIn("Upcoming LUX MED appointments for Child", self.state.next_message()["text"])
            self.bot.flush()
            self.update(3, "Could you cancel appointment number 1 please?")
            self.assertIn("Appointment 1 for Child", self.state.next_message()["text"])
            self.assertIn("Are you sure", self.state.next_message()["text"])
            self.bot.cancellation_due()
            for client in clients.values():
                client.cancel_standard_visit.assert_not_called()

    def test_added_specialties_resolve_to_their_in_person_catalog_names(self):
        choices = (
            ("endocrinologist", "Endokrynolog"), ("gastrologa", "Gastroenterolog (Gastrolog)"),
            ("rheumatologist", "Reumatolog"), ("nephrologist", "Nefrolog"),
            ("pulmonologist", "Pulmonolog"), ("diabetologist", "Diabetolog"),
            ("hematologist", "Hematolog"),
        )
        for update_id, (alias, _) in enumerate(choices, 1):
            self.update(update_id, f"Find {alias} for Adult")
        self.assertEqual([(row["service"], row["telephone"]) for row in self.state.active()],
                         [(service, 0) for _, service in choices])

    def test_free_text_path_keeps_names_and_service_local(self):
        self.bot.free_text = FreeText("fixture-key")
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "intent": "search", "mode": "unspecified", "when": "",
            })}]}],
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

    def test_internist_phone_phrasing_resolves_locally_to_the_owner(self):
        for number, text in enumerate((
            "Find call with internist for Adult",
            "Find internist phone call for Adult",
            "Find internist phone call for me",
            "Could you find a phone call with an internist for me tomorrow, after 16:00?",
        ), 1):
            self.update(number, text)
        self.assertEqual(len(self.state.active()), 4)
        for row in self.state.active():
            self.assertEqual((row["patient"], row["service"], row["telephone"]),
                             ("patient1", INTERNIST_PHONE_SERVICE, 1))
        criteria = self.state.criteria(self.state.active()[-1])
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.start_minute), ("2030-03-31", 960))

    def test_model_interprets_a_search_once_and_preserves_patient_privacy(self):
        self.bot.free_text = FreeText("fixture-key")
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "intent": "search", "mode": "phone", "when": "tomorrow, between 12:00 and 17:00",
            })}],
        }]}
        with patch.object(self.bot.free_text.session, "post", return_value=response) as api:
            self.update(1, "I'd like an internist to call me tomorrow afternoon.")
            self.assertEqual(api.call_args.kwargs["json"]["input"],
                             "i d like an SERVICE to call me tomorrow afternoon")
            self.update(2, "Find call with internist for Adult tomorrow after 18:00")
            self.assertNotIn("adult", api.call_args.kwargs["json"]["input"].casefold())
            self.update(3, "/find me | Internista")
            self.assertEqual(api.call_count, 2)
        conversational, explicit, command = [self.state.criteria(row) for row in self.state.active()]
        self.assertEqual((conversational.patient, conversational.service, conversational.telephone),
                         ("patient1", INTERNIST_PHONE_SERVICE, True))
        self.assertEqual((conversational.preferences.first_date, conversational.preferences.start_minute,
                          conversational.preferences.end_minute), ("2030-03-31", 720, 1020))
        self.assertEqual(explicit.preferences.start_minute, 1080)
        self.assertEqual(command.service, "Internista")

    def test_in_person_default_resists_an_unrequested_phone_interpretation(self):
        self.bot.free_text = FreeText("fixture-key")
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "intent": "search", "mode": "phone", "when": "",
            })}],
        }]}
        with patch.object(self.bot.free_text.session, "post", return_value=response):
            self.update(1, "Find an allergist for me")
            self.update(2, "Find an allergist for me by phone")
            self.update(3, "Find an allergist for me in the office")
        rows = [self.state.criteria(row) for row in self.state.active()]
        self.assertEqual([(row.service, row.telephone) for row in rows], [
            ("Konsultacja alergologiczna", False),
            ("Konsultacja alergologiczna - telefoniczna", True),
            ("Konsultacja alergologiczna", False),
        ])

    def test_unclear_mode_gets_buttons_and_preserves_original_filters(self):
        self.update(1, "Find an allergist for me by phone or in person tomorrow after 16:00")
        message = self.state.next_message()
        self.assertIn("Would you like an in-person visit or a phone call?", message["text"])
        buttons = json.loads(message["buttons"])
        self.assertIn("In person", buttons[0][0][0])
        self.assertIn("Phone", buttons[1][0][0])
        self.assertEqual(self.state.active(), [])
        self.tap(2, buttons[1][0][1])
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertTrue(criteria.telephone)
        self.assertEqual(criteria.service, "Konsultacja alergologiczna - telefoniczna")
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.start_minute),
                         ("2030-03-31", 960))

    def test_internista_nfz_resolves_the_in_person_catalog_label_and_keeps_tomorrow(self):
        self.update(1, "find internista nfz for Adult tomorrow")
        self.bot.flush()
        client = MagicMock()
        client.resolve_service = LuxMedPortal.resolve_service
        client.get_services.return_value = [
            {"name": "Konsultacja internistyczna - NFZ (POZ)", "id": 15, "isTelemedicine": False},
            {"name": "Konsultacja internistyczna - telefoniczna", "id": 16, "isTelemedicine": True},
        ]
        client.search.return_value = []
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertEqual(criteria.service, "Konsultacja internistyczna - NFZ (POZ)")
        self.assertFalse(criteria.telephone)
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.last_date), ("2030-03-31",) * 2)
        self.assertEqual(client.search.call_args.args[0].service_id, "15")

    def test_clinician_first_nfz_search_keeps_two_separate_dates(self):
        self.now = int(dt.datetime(2026, 11, 16, 10, tzinfo=WARSAW).timestamp())
        for number, dates in enumerate(("next friday or monday", "friday or monay"), 1):
            self.update(number, f"Find appointment for me with Marta Babiarczyk internista na NFZ on {dates}")
        rows = self.state.active()
        self.assertEqual(len(rows), 2)
        for row in rows:
            criteria = self.state.criteria(row)
            self.assertEqual(criteria.patient, "patient1")
            self.assertEqual(criteria.service, INTERNIST_NFZ_SERVICE)
            self.assertEqual(criteria.preferences.doctor, "Marta Babiarczyk")
            self.assertEqual(criteria.preferences.date_choices, ("2026-11-20", "2026-11-23"))
        slots = [
            Appointment(dt.datetime(2026, 11, day, 13, tzinfo=WARSAW), 1, "Clinic", doctor, 15)
            for day, doctor in [(20, "Marta Babiarczyk"), (21, "Marta Babiarczyk"),
                                (23, "Other Clinician"), (23, "Marta Babiarczyk")]
        ]
        matching, _ = matching_slots(slots, criteria.preferences)
        self.assertEqual(matching, [slots[0], slots[3]])
        self.assertIn("2026-11-20 or 2026-11-23", self.state.next_message()["text"])
        revised = parse_preferences("next monday", dt.date(2026, 11, 16), criteria.preferences)
        self.assertEqual(revised.date_choices, ())
        self.assertEqual(revised.first_date, "2026-11-23")

    def test_ai_interprets_flexible_time_without_receiving_the_clinician_name(self):
        self.now = int(dt.datetime(2026, 11, 16, 10, tzinfo=WARSAW).timestamp())
        self.bot.free_text = FreeText("fixture-key")
        question = "Find appointment for me with Marta Babiarczyk internista na NFZ on Friday or Monday morning"
        with patch.object(self.bot.free_text, "interpret") as interpret:
            interpret.return_value = {"intent": "search", "mode": "unspecified", "when": "friday, before 12:00"}
            self.update(1, question)
            self.assertEqual(self.state.active(), [])
            interpret.return_value["when"] = "friday or monday"
            self.update(2, question)
            self.assertEqual(self.state.active(), [])
            interpret.return_value["when"] = "friday or monday, before 12:00"
            self.update(3, question)
        for call in interpret.call_args_list:
            self.assertEqual(call.args[0], "find SERVICE for me on friday or monday morning")
        row, = self.state.active()
        preferences = self.state.criteria(row).preferences
        self.assertEqual(preferences.date_choices, ("2026-11-20", "2026-11-23"))
        self.assertEqual(preferences.doctor, "Marta Babiarczyk")
        self.assertEqual((preferences.time_rule, preferences.start_minute), ("before", 720))

    def test_remote_catalog_variant_requires_selection_after_an_in_person_miss(self):
        self.update(1, "Find allergist for me")
        self.bot.flush()
        client = MagicMock()
        client.resolve_service = LuxMedPortal.resolve_service
        client.get_services.return_value = [
            {"name": "Konsultacja alergologiczna - telefoniczna", "id": 11, "isTelemedicine": True},
        ]
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        client.search.assert_not_called()
        message = self.state.next_message()
        self.assertIn("another visit type", message["text"])
        buttons = json.loads(message["buttons"])
        self.assertIn("Phone", buttons[0][0][0])
        self.tap(2, buttons[0][0][1])
        row, = self.state.active()
        self.assertTrue(row["telephone"])

    def test_missing_patient_offers_profiles_for_the_original_service(self):
        self.update(1, "Find an allergist")
        message = self.state.next_message()
        self.assertIn("Who is the appointment for?", message["text"])
        buttons = json.loads(message["buttons"])
        self.assertEqual([button[0] for button in buttons[0]], ["Adult", "Child", "Other"])
        self.tap(2, buttons[0][0][1])
        row, = self.state.active()
        self.assertEqual((row["patient"], row["service"], row["telephone"]),
                         ("patient1", "Konsultacja alergologiczna", 0))

    def test_plain_reply_can_answer_a_patient_question_for_the_current_search(self):
        request = "Find an allergist for someone tomorrow after 16:00"
        self.update(1, request)
        self.assertIn("Who is the appointment for?", self.state.next_message()["text"])
        self.bot.flush()
        self.update(2, "/start")
        self.update(3, "For Child, please.")
        self.assertEqual(self.state.active(), [])
        self.update(4, request)
        self.update(5, "For Child, please.")
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertEqual((criteria.patient, criteria.service), ("patient2", "Konsultacja alergologiczna"))
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.start_minute), ("2030-03-31", 960))

    def test_unspecified_adult_specialty_offers_choices_with_original_filters(self):
        self.update(1, "Find a doctor for me tomorrow after 16:00")
        message = self.state.next_message()
        self.assertIn("Which specialty should I search for?", message["text"])
        buttons = json.loads(message["buttons"])
        self.tap(2, buttons[0][0][1])
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertEqual((criteria.service, criteria.telephone), ("Internista", False))
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.start_minute), ("2030-03-31", 960))

    def test_live_catalog_matches_unlisted_names_and_preserves_remote_mode(self):
        self.update(1, "Find kontrola online for me tomorrow")
        self.bot.flush()
        client = MagicMock()
        client.resolve_service = LuxMedPortal.resolve_service
        client.get_services.return_value = [
            {"name": "Consultations", "children": [
                {"name": "Kontrola online – wideo", "id": 10, "isTelemedicine": True, "isVideoConsultation": True},
            ]},
        ]
        client.search.return_value = []
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        row, = self.state.active()
        self.assertEqual((row["patient"], row["service"], row["telephone"]),
                         ("patient1", "Kontrola online – wideo", 1))
        self.assertEqual(client.search.call_args.args[0].service_id, "10")
        self.assertIn("Kontrola online – wideo", self.state.next_message()["text"])

    def test_ambiguous_catalog_service_lists_choices_without_searching(self):
        self.update(1, "Find an ultrasound for me")
        self.bot.flush()
        client = MagicMock()
        client.resolve_service = LuxMedPortal.resolve_service
        client.get_services.return_value = [
            {"name": "USG stawu skokowego", "id": 11, "isTelemedicine": False},
            {"name": "USG tarczycy", "id": 12, "isTelemedicine": False},
        ]
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        client.search.assert_not_called()
        message = self.state.next_message()["text"]
        self.assertIn("Several LUX MED services match", message)
        self.assertIn("USG stawu skokowego", message)
        self.assertIn("USG tarczycy", message)

    def test_specialty_aliases_match_current_portal_labels(self):
        client = MagicMock()
        client.resolve_service = LuxMedPortal.resolve_service
        client.get_services.return_value = [
            {"name": "Konsultacja laryngologiczna", "id": 10, "isTelemedicine": False},
            {"name": "Konsultacja alergologiczna - telefoniczna", "id": 11, "isTelemedicine": True},
            {"name": "Konsultacja ortopedyczna", "id": 12, "isTelemedicine": False},
            {"name": "Konsultacja ortopedyczna - uraz", "id": 13, "isTelemedicine": False},
            {"name": "Konsultacja alergologiczna", "id": 14, "isTelemedicine": False},
        ]
        client.search.return_value = []
        for number, service in enumerate(("ENT", "allergist phone call", "ortopeda", "an alergolog", "an allergolog"), 1):
            self.update(number, f"Find {service} for me")
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        self.assertEqual([check.args[0].service_id for check in client.search.call_args_list], ["10", "11", "12", "14", "14"])

    def test_close_service_spelling_asks_then_retains_the_original_search_filters(self):
        self.update(1, "Find an allergsit for me tomorrow, after 16:00")
        self.bot.flush()
        client = MagicMock()
        client.resolve_service = LuxMedPortal.resolve_service
        client.get_services.return_value = [
            {"name": "Konsultacja alergologiczna", "id": 10, "isTelemedicine": False},
            {"name": "Konsultacja alergologiczna - telefoniczna", "id": 11, "isTelemedicine": True},
        ]
        with patch.object(self.bot, "portal_for", return_value=client):
            self.bot.search_due()
        client.search.assert_not_called()
        prompt = self.state.next_message()["text"]
        self.assertIn("Did you mean Konsultacja alergologiczna? Reply Service 1", prompt)
        self.update(2, "Service 1")
        row, = self.state.active()
        criteria = self.state.criteria(row)
        self.assertEqual((criteria.patient, criteria.service, criteria.telephone),
                         ("patient1", "Konsultacja alergologiczna", False))
        self.assertEqual((criteria.preferences.first_date, criteria.preferences.start_minute), ("2030-03-31", 960))
        self.assertEqual(self.state.get_setting("pending_service"), "")

    def test_free_text_does_not_send_unrecognized_details_or_broaden_constraints(self):
        for text in (
            "Please book an internist for Adult",
            "Please find an internist for Adult or Child",
            "Could you find a pediatric visit for Child with details unknown-private-note?",
            "Czy możesz sprawdzić dostępność pediatry dla Dziecka we Wrocławiu?",
        ):
            with self.subTest(text=text):
                self.assertIsNone(redacted_request(text, self.bot.aliases, self.bot.cities))

    def test_welcome_and_clarifications_use_conversational_replies(self):
        self.bot.free_text = MagicMock()
        expected = "LUX MED Personal Assistant\nHow may I help you today?"
        self.update(1, "/start")
        self.assertEqual(self.state.next_message()["text"], expected)
        self.assertIsNone(self.state.next_message()["buttons"])
        self.bot.flush()
        self.update(2, "Hello!")
        self.assertEqual(self.state.next_message()["text"], expected)
        self.bot.flush()
        self.update(3, "Thanks!")
        self.assertEqual(self.state.next_message()["text"], "You’re welcome.")
        self.bot.free_text.chat_action.assert_not_called()
        self.bot.free_text.criteria.assert_not_called()
        self.bot.free_text = None
        self.bot.flush()
        self.update(4, "Some words I can't interpret")
        self.assertIn("Could you say it another way", self.state.next_message()["text"])
        self.assertTrue(self.state.next_message()["buttons"])
        self.assertEqual(self.state.active(), [])

    def test_english_help_and_portal_service_names(self):
        self.update(1, "/help")
        self.assertIn("/find patient", self.state.next_message()["text"])
        self.assertNotIn("/szukaj", self.state.next_message()["text"])
        self.update(2, "/find Adult | Dermatolog | Wrocław")
        row, = self.state.active()
        self.assertEqual((row["service"], row["city"]), ("Dermatolog", "Wrocław"))


class TelegramTransportTests(unittest.TestCase):
    def test_copyable_messages_always_target_configured_owner(self):
        api = Telegram("123:fixture", 42)
        response = MagicMock(status_code=200)
        response.json.return_value = {"ok": True, "result": {"message_id": 22}}
        with patch.object(api.session, "post", return_value=response) as request:
            self.assertEqual(api.send("fixture"), 22)
        self.assertEqual(request.call_args.kwargs["json"], {"chat_id": 42, "text": "fixture"})
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
