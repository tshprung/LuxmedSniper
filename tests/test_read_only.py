import datetime
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

import audit_search
from luxmed_sniper import DoctorLocator, Luxmed, LuxMedPortal


UTC = datetime.UTC
NOW = datetime.datetime(2030, 3, 30, 23, 15, tzinfo=UTC)


def response(payload=None, *, status=200, raw=None):
    result = requests.Response()
    result.status_code = status
    result._content = (raw if raw is not None else json.dumps(payload)).encode()
    return result


def slot(starts_at, *, doctor=9, clinic_group=6):
    return {
        "dateTimeFrom": starts_at,
        "clinicGroupId": clinic_group,
        "clinicId": 12,
        "clinic": "Fixture clinic",
        "serviceId": 30,
        "doctor": {"id": doctor, "firstName": "Fixture", "lastName": "Doctor", "academicTitle": ""},
    }


class SearchTests(unittest.TestCase):
    def test_search_uses_warsaw_dates_and_filters_exact_instants(self):
        starts = [
            slot("2030-03-30T23:14:59Z"),
            slot("2030-03-30T23:15:00Z"),
            slot("2030-03-31T10:00:00"),
            slot("2030-03-31T10:00:00", doctor=8),
            slot("2030-03-31T10:00:00", clinic_group=5),
            slot("2030-04-13T23:15:00Z"),
            slot("2030-04-13T23:15:01Z"),
        ]
        payload = {"termsForService": {"termsForDays": [{"terms": starts}]}}
        locator = DoctorLocator(id="8*20*6*9")
        with (
            patch("luxmed_sniper.datetime.datetime", wraps=datetime.datetime) as clock,
            patch.object(requests.Session, "request", side_effect=[response({"token": "fixture"}), response(payload)]) as request,
        ):
            clock.now.return_value = NOW
            portal = LuxMedPortal(Luxmed(login="fixture", password="fixture"))
            with portal.session:
                matches = portal.search(locator)
        self.assertEqual(
            [item.AppointmentDate.isoformat() for item in matches],
            ["2030-03-31T00:15:00+01:00", "2030-03-31T10:00:00+02:00", "2030-04-14T01:15:00+02:00"],
        )
        self.assertEqual(
            [call.args for call in request.call_args_list],
            [("POST", LuxMedPortal.LUXMED_LOGIN_URL), ("GET", LuxMedPortal.NEW_PORTAL_RESERVATION_URL)],
        )
        params = request.call_args.kwargs["params"]
        self.assertEqual((params["searchDateFrom"], params["searchDateTo"]), ("2030-03-31", "2030-04-14"))
        self.assertEqual(params["searchDatePreset"], 14)
        self.assertEqual((params["facilitiesIds"], params["doctorsIds"]), ([6], [9]))
        for call in request.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["timeout"], (5, 15))

    def test_sessions_have_separate_cookies_and_authorization(self):
        with patch.object(requests.Session, "request", side_effect=[
            response({"token": "first-fixture"}), response({"token": "second-fixture"}),
        ]):
            first = LuxMedPortal(Luxmed(login="first", password="fixture"))
            second = LuxMedPortal(Luxmed(login="second", password="fixture"))
            with first.session, second.session:
                first.session.cookies.set("fixture-cookie", "first")
                self.assertNotIn("fixture-cookie", second.session.cookies)
                self.assertEqual(first.session.headers["authorization-token"], "Bearer first-fixture")
                self.assertEqual(second.session.headers["authorization-token"], "Bearer second-fixture")


class AuditCliTests(unittest.TestCase):
    def run_audit(self, profile, environment, replies, *, hour=10, options=()):
        out = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("sys.argv", ["audit_search.py", profile, *options]),
            patch("audit_search.datetime.datetime", wraps=datetime.datetime) as clock,
            patch.object(requests.Session, "request", side_effect=replies) as request,
            redirect_stdout(out),
        ):
            clock.now.return_value = NOW.replace(hour=hour, minute=0)
            status = audit_search.main()
        return status, out.getvalue().strip(), request

    def test_all_three_profiles_use_only_their_own_configuration(self):
        profiles = ("patient1", "patient2", "patient3")
        environment = {
            f"LUXMED_{profile.upper()}_{field}": value
            for index, profile in enumerate(profiles, start=1)
            for field, value in (
                ("LOGIN", f"login-{index}"),
                ("PASSWORD", f"password-{index}"),
                ("LOCATOR", f"8*{index}*-1*-1"),
            )
        }
        for index, profile in enumerate(profiles, start=1):
            with self.subTest(profile=profile):
                status, output, request = self.run_audit(profile, environment, [
                    response({"token": "fixture-token"}),
                    response({"termsForService": {"termsForDays": []}}),
                ])
                self.assertEqual((status, output), (0, "search-completed, matching-slots=0"))
                self.assertEqual(request.call_args_list[0].kwargs["json"], {
                    "login": f"login-{index}", "password": f"password-{index}",
                })
                self.assertEqual(request.call_args_list[1].kwargs["params"]["serviceVariantId"], str(index))

    def test_after_hours_and_missing_credentials_never_contact_portal(self):
        for hour, message in ((5, "outside-search-hours"), (23, "outside-search-hours"), (22, "missing-credentials")):
            with self.subTest(hour=hour):
                status, output, request = self.run_audit("patient3", {}, [], hour=hour)
                self.assertEqual((status, output), (2, message))
                request.assert_not_called()

    def test_status_only_omits_search_results(self):
        status, output, request = self.run_audit("patient1", {
            "LUXMED_PATIENT1_LOGIN": "fixture",
            "LUXMED_PATIENT1_PASSWORD": "fixture",
            "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
        }, [
            response({"token": "fixture-token"}),
            response({"termsForService": {"termsForDays": [{"terms": [slot("2030-03-31T14:00:00")]}]}}),
        ], options=("--status-only",))
        self.assertEqual((status, output), (0, "search-completed"))
        self.assertEqual(request.call_count, 2)

    def test_unconfirmed_and_restricted_logins_stop_after_one_request(self):
        for reply, message in (
            (response({"challenge": "private-fixture"}), "login-unconfirmed"),
            (response(raw="private-fixture"), "login-unconfirmed"),
            (response({"token": "private-fixture"}, status=302), "login-unconfirmed"),
            (response(raw="private-fixture", status=403), "access-denied"),
            (response(raw="private-fixture", status=429), "rate-limited"),
            (requests.Timeout("private-fixture"), "network-error"),
        ):
            with self.subTest(message=message):
                status, output, request = self.run_audit("patient1", {
                    "LUXMED_PATIENT1_LOGIN": "fixture",
                    "LUXMED_PATIENT1_PASSWORD": "fixture",
                    "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
                }, [reply])
                self.assertEqual((status, output), (1, message))
                self.assertEqual(request.call_count, 1)

    def test_search_denial_or_unknown_shape_is_not_an_empty_result(self):
        for reply, message in (
            (response(raw="private-fixture", status=401), "access-denied"),
            (response(raw="private-fixture", status=429), "rate-limited"),
            (response({"termsForService": {"termsForDays": {}}}), "unexpected-response"),
        ):
            with self.subTest(message=message):
                status, output, request = self.run_audit("patient2", {
                    "LUXMED_PATIENT2_LOGIN": "fixture",
                    "LUXMED_PATIENT2_PASSWORD": "fixture",
                    "LUXMED_PATIENT2_LOCATOR": "8*20*-1*-1",
                }, [response({"token": "fixture-token"}), reply])
                self.assertEqual((status, output), (1, message))
                self.assertEqual(request.call_count, 2)
