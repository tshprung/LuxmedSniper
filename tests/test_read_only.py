import base64
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

    def test_login_records_token_expiry_as_a_cache_hint(self):
        expiry = int(NOW.timestamp())
        for claims, expected in (({"exp": expiry}, expiry), ({"exp": True}, None), ({"exp": "invalid"}, None)):
            with self.subTest(claims=claims):
                encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                with patch.object(requests.Session, "request", return_value=response({"token": f"fixture.{encoded}.fixture"})):
                    portal = LuxMedPortal(Luxmed(login="fixture", password="fixture"))
                    with portal.session:
                        self.assertEqual(portal.token_expires_at, expected)

    def test_booking_verification_uses_only_reserved_and_confirmed_visit_ids(self):
        events = [
            {"eventType": 1, "status": status, "eventId": status + 10}
            for status in range(7)
        ] + [{"eventType": 16, "status": 1, "eventId": 100}]
        with patch.object(requests.Session, "request", side_effect=[
            response({"token": "fixture-token"}), response({"events": events}),
        ]) as request:
            portal = LuxMedPortal(Luxmed(login="fixture", password="fixture"))
            with portal.session:
                self.assertEqual(portal.booked_visit_ids(), {11, 12})
        self.assertEqual(request.call_args.args, ("GET", LuxMedPortal.UPCOMING_VISITS_URL))

    def test_booking_holds_and_confirms_the_exact_covered_slot(self):
        locator = DoctorLocator(id="5*30*-1*-1")
        term = dict(slot("2030-04-01T10:00:00+02:00"), cityId=5, city="Fixture city",
                    dateTimeTo="2030-04-01T10:15:00+02:00", roomId=19, scheduleId=20,
                    isTelemedicine=False, isAdditional=False, isImpediment=False)
        result = {"correlationId": "fixture", "termsForService": {"termsForDays": [{"terms": [term]}]}}
        appointment, = LuxMedPortal._parse_visits_new_portal(result, locator)
        valuation = {"valuationType": 1, "price": 0, "alternativePrice": None, "isReferralRequired": False}
        hold = {"hasErrors": False, "hasWarnings": False,
                "value": {"temporaryReservationId": 135, "askForReferral": False, "valuations": [valuation]}}
        confirmed = {"hasErrors": False, "value": {"reservationId": 246}}
        with patch.object(requests.Session, "request", side_effect=[
            response({"token": "fixture-token"}), response({"token": "fixture-request-token"}),
            response(hold), response(confirmed),
        ]) as request:
            portal = LuxMedPortal(Luxmed(login="fixture", password="fixture"))
            with portal.session:
                self.assertEqual(portal.reserve_standard_visit(appointment, "Fixture service"), 246)
        request_token, lock, confirm = request.call_args_list[1:]
        self.assertTrue(request_token.args[1].endswith("/security/getforgerytoken"))
        self.assertEqual(request_token.args[0], "GET")
        for call in (lock, confirm):
            self.assertEqual(call.kwargs["headers"], {
                "X-Requested-With": "XMLHttpRequest", "XSRF-TOKEN": "fixture-request-token",
            })
        self.assertTrue(lock.args[1].endswith("/Reservation/LockTerm"))
        self.assertEqual(lock.kwargs["json"]["doctorId"], 9)
        self.assertEqual(lock.kwargs["json"]["date"], "2030-04-01T08:00:00.000Z")
        self.assertEqual(lock.kwargs["json"]["timeFrom"], "10:00")
        self.assertIsNone(lock.kwargs["json"]["referralTypeId"])
        self.assertTrue(confirm.args[1].endswith("/Reservation/Confirm"))
        self.assertEqual(confirm.kwargs["json"]["temporaryReservationId"], 135)
        self.assertEqual(confirm.kwargs["json"]["date"], "2030-04-01T08:00:00.000Z")
        self.assertEqual(confirm.kwargs["json"]["timeFrom"], "10:00")
        self.assertEqual(confirm.kwargs["json"]["valuation"], valuation)

        result["termsForService"]["additionalData"] = {
            "isPreparationRequired": True, "preparationItems": ["private-fixture"],
        }
        preparation_slot, = LuxMedPortal._parse_visits_new_portal(result, locator)
        self.assertTrue(preparation_slot.requires_preparation)
        self.assertIsNone(preparation_slot.booking_term)
        self.assertNotIn("private-fixture", repr(preparation_slot))

    def test_booking_hold_releases_without_confirmation_if_portal_requires_review(self):
        locator = DoctorLocator(id="5*30*-1*-1")
        term = dict(slot("2030-04-01T10:00:00"), cityId=5, dateTimeTo="2030-04-01T10:15:00",
                    roomId=19, scheduleId=20, isTelemedicine=False, isAdditional=False, isImpediment=False)
        result = {"correlationId": "fixture", "termsForService": {"termsForDays": [{"terms": [term]}]}}
        appointment, = LuxMedPortal._parse_visits_new_portal(result, locator)
        for case in ("warning", "price", "alternative price", "conflict", "change", "portal referral"):
            with self.subTest(case=case):
                valuation = {
                    "valuationType": 1,
                    "price": 25 if case == "price" else 0,
                    "alternativePrice": 25 if case == "alternative price" else None,
                    "isReferralRequired": False,
                    "requireReferralForPP": case == "portal referral",
                }
                hold = {
                    "hasErrors": False, "hasWarnings": case == "warning",
                    "value": {
                        "temporaryReservationId": 135, "askForReferral": False, "valuations": [valuation],
                        "conflictedVisit": {"eventId": 246} if case == "conflict" else None,
                        "changeTermAvailable": case == "change",
                    },
                }
                with patch.object(requests.Session, "request", side_effect=[
                    response({"token": "fixture-token"}), response({"token": "fixture-request-token"}),
                    response(hold), response({}),
                ]) as request:
                    portal = LuxMedPortal(Luxmed(login="fixture", password="fixture"))
                    with portal.session, self.assertRaisesRegex(Exception, "booking-needs-portal"):
                        portal.reserve_standard_visit(appointment, "Fixture service")
                self.assertTrue(request.call_args.args[1].endswith("/Reservation/ReleaseTerm"))
                self.assertEqual(request.call_args.kwargs["params"], {"reservationId": 135})
                self.assertEqual(request.call_args.kwargs["headers"]["XSRF-TOKEN"], "fixture-request-token")

    def test_booking_eligibility_redirect_stops_before_other_requests(self):
        with patch.object(requests.Session, "request", side_effect=[
            response({"token": "fixture-token"}), response(status=302),
        ]) as request:
            portal = LuxMedPortal(Luxmed(login="fixture", password="fixture"))
            with portal.session, self.assertRaisesRegex(Exception, "session-unconfirmed"):
                portal.check_booking_eligibility(5, 30)
        self.assertEqual(request.call_count, 2)


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

    def test_booking_preflight_reports_the_failed_step_and_stops(self):
        for http_status, error in ((302, "session-unconfirmed"), (401, "access-denied")):
            with self.subTest(http_status=http_status):
                status, output, request = self.run_audit("patient1", {
                    "LUXMED_PATIENT1_LOGIN": "fixture",
                    "LUXMED_PATIENT1_PASSWORD": "fixture",
                    "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
                }, [
                    response({"token": "fixture-token"}),
                    response({"canReserve": True}),
                    response({"referrals": []}),
                    response({"token": "fixture-request-token"}),
                    response(raw="private-fixture", status=http_status),
                ], options=("--booking-preflight",))
                self.assertEqual((status, output), (1, f"{error} · questionnaire · HTTP {http_status}"))
                self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", *["GET"] * 4])
                self.assertTrue(request.call_args.args[1].endswith("/PNM/Index"))
                self.assertFalse(request.call_args.kwargs["allow_redirects"])
                self.assertEqual(request.call_args.kwargs["headers"], {
                    "X-Requested-With": "XMLHttpRequest", "XSRF-TOKEN": "fixture-request-token",
                })

    def test_booking_preflight_stops_when_portal_omits_request_token(self):
        status, output, request = self.run_audit("patient1", {
            "LUXMED_PATIENT1_LOGIN": "fixture",
            "LUXMED_PATIENT1_PASSWORD": "fixture",
            "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
        }, [
            response({"token": "fixture-token"}),
            response({"canReserve": True}),
            response({"referrals": []}),
            response({}),
        ], options=("--booking-preflight",))
        self.assertEqual((status, output), (1, "unexpected-response · booking-request-token"))
        self.assertTrue(request.call_args.args[1].endswith("/security/getforgerytoken"))

    def test_booking_preflight_distinguishes_questions_from_unrecognized_responses(self):
        for payload, expected in (
            ({"questions": [{"name": "private-fixture"}]}, "portal-questions"),
            ({"questions": {"name": "private-fixture"}}, "questionnaire-response-unrecognized"),
            ({"questions": ["private-fixture"]}, "questionnaire-response-unrecognized"),
            (["private-fixture"], "questionnaire-response-unrecognized"),
        ):
            with self.subTest(expected=expected):
                status, output, request = self.run_audit("patient1", {
                    "LUXMED_PATIENT1_LOGIN": "fixture",
                    "LUXMED_PATIENT1_PASSWORD": "fixture",
                    "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
                }, [
                    response({"token": "fixture-token"}),
                    response({"canReserve": True}),
                    response({"referrals": []}),
                    response({"token": "fixture-request-token"}),
                    response(payload),
                ], options=("--booking-preflight",))
                self.assertEqual((status, output), (1, f"{expected} · questionnaire"))
                self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", *["GET"] * 4])
                self.assertTrue(request.call_args.args[1].endswith("/PNM/Index"))

    def test_booking_preflight_checks_eligibility_penalty_and_verification_without_holding(self):
        status, output, request = self.run_audit("patient1", {
            "LUXMED_PATIENT1_LOGIN": "fixture",
            "LUXMED_PATIENT1_PASSWORD": "fixture",
            "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
        }, [
            response({"token": "fixture-token"}),
            response({"canReserve": True}),
            response({"referrals": []}),
            response({"token": "fixture-request-token"}),
            response({"questions": []}),
            response({
                "isNotAvailableInCity": False, "isNotAvailableInContract": False,
                "isNotAvailableInAreaOfCare": False, "blockedByAccountLevel": False,
            }),
            response({"canReserve": True}),
            response({"events": []}),
        ], options=("--booking-preflight",))
        self.assertEqual((status, output), (0, "booking-preflight-completed"))
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", *["GET"] * 7])
        self.assertEqual(request.call_args_list[-2].args[1], LuxMedPortal.NON_SHOW_PENALTIES_URL)
        self.assertEqual(request.call_args.args[1], LuxMedPortal.UPCOMING_VISITS_URL)

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

    def test_booking_audit_reports_only_completion_and_uses_read_only_checks(self):
        status, output, request = self.run_audit("patient1", {
            "LUXMED_PATIENT1_LOGIN": "fixture",
            "LUXMED_PATIENT1_PASSWORD": "fixture",
            "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
        }, [
            response({"token": "fixture-token"}),
            response({"events": [{"eventType": 1, "status": 1, "eventId": 4321, "title": "private-fixture"}]}),
            response({"canReserve": False, "blockadeTo": "private-fixture"}),
        ], options=("--booking-check",))
        self.assertEqual((status, output), (0, "booking-check-completed"))
        self.assertEqual([call.args for call in request.call_args_list], [
            ("POST", LuxMedPortal.LUXMED_LOGIN_URL),
            ("GET", LuxMedPortal.UPCOMING_VISITS_URL),
            ("GET", LuxMedPortal.NON_SHOW_PENALTIES_URL),
        ])
        self.assertEqual(request.call_args.kwargs["params"], {
            "cityId": 8, "serviceVariantId": 20, "referralId": "",
        })

    def test_booking_audit_stops_on_unknown_response(self):
        environment = {
            "LUXMED_PATIENT1_LOGIN": "fixture",
            "LUXMED_PATIENT1_PASSWORD": "fixture",
            "LUXMED_PATIENT1_LOCATOR": "8*20*-1*-1",
        }
        for payload, message, request_count in (
            ([response({"events": [{"eventType": 1, "status": 1, "eventId": "private-fixture"}]})],
             "unexpected-upcoming-visits", 2),
            ([response({"events": []}), response({"canReserve": "private-fixture"})],
             "unexpected-penalty-status", 3),
        ):
            with self.subTest(message=message):
                status, output, request = self.run_audit("patient1", environment, [
                    response({"token": "fixture-token"}), *payload,
                ], options=("--booking-check",))
                self.assertEqual((status, output), (1, message))
                self.assertEqual(request.call_count, request_count)

    def test_service_name_resolves_from_live_catalog_before_search(self):
        catalog = [{
            "name": "Consultations",
            "children": [
                {"id": 734, "name": "Internista", "isTelemedicine": True},
                {"id": 735, "name": "Konsultacja internistyczna", "isTelemedicine": False},
            ],
        }]
        status, output, request = self.run_audit("patient1", {
            "LUXMED_PATIENT1_LOGIN": "fixture",
            "LUXMED_PATIENT1_PASSWORD": "fixture",
            "LUXMED_PATIENT1_LOCATOR": "5*Internista*-1*-1",
        }, [
            response({"token": "fixture-token"}),
            response(catalog),
            response({"termsForService": {"termsForDays": []}}),
        ], options=("--status-only",))
        self.assertEqual((status, output), (0, "search-completed"))
        self.assertEqual(request.call_args_list[1].args, ("GET", LuxMedPortal.DICTIONARY_SERVICES_URL))
        self.assertEqual(request.call_args_list[2].args, ("GET", LuxMedPortal.NEW_PORTAL_RESERVATION_URL))
        self.assertEqual(request.call_args_list[2].kwargs["params"]["serviceVariantId"], "735")

    def test_pediatric_service_name_matches_routine_in_person_visit(self):
        catalog = [{
            "name": "Consultations",
            "children": [
                {"id": 701, "name": "Pediatra - gabinet dzieci chorych", "isTelemedicine": False},
                {"id": 702, "name": "Konsultacja pediatryczna - gabinet dzieci zdrowych", "isTelemedicine": False},
                {"id": 703, "name": "Pediatra - gabinet dzieci zdrowych", "isTelemedicine": True},
            ],
        }]
        status, output, request = self.run_audit("patient2", {
            "LUXMED_PATIENT2_LOGIN": "fixture",
            "LUXMED_PATIENT2_PASSWORD": "fixture",
            "LUXMED_PATIENT2_LOCATOR": "5*Pediatra - gabinet dzieci zdrowych*-1*-1",
        }, [
            response({"token": "fixture-token"}),
            response(catalog),
            response({"termsForService": {"termsForDays": []}}),
        ], options=("--status-only",))
        self.assertEqual((status, output), (0, "search-completed"))
        self.assertEqual(request.call_args.kwargs["params"]["serviceVariantId"], "702")

    def test_service_name_requires_one_in_person_match(self):
        service = {"id": 735, "name": "Internista", "isTelemedicine": False}
        for catalog, message in (
            ([{**service, "isTelemedicine": True}], "service-not-in-catalog"),
            ([service, {**service, "id": 736}], "ambiguous-service"),
            ({"unexpected": []}, "unexpected-catalog"),
        ):
            with self.subTest(message=message):
                status, output, request = self.run_audit("patient1", {
                    "LUXMED_PATIENT1_LOGIN": "fixture",
                    "LUXMED_PATIENT1_PASSWORD": "fixture",
                    "LUXMED_PATIENT1_LOCATOR": "5*Internista*-1*-1",
                }, [response({"token": "fixture-token"}), response(catalog)])
                self.assertEqual((status, output), (1, message))
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
