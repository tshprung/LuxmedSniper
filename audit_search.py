"""Run one private, read-only availability check for one configured account."""

import argparse
import datetime
import os
import re

from luxmed_sniper import WARSAW, DoctorLocator, Luxmed, LuxMedPortal, PortalError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("patient1", "patient2", "patient3"))
    parser.add_argument("--status-only", action="store_true", help="Omit the slot count from output")
    args = parser.parse_args()

    now = datetime.datetime.now(WARSAW)
    if not datetime.time(6) <= now.time() < datetime.time(22, 1):
        print("outside-search-hours")
        return 2

    prefix = f"LUXMED_{args.profile.upper()}"
    login = os.environ.get(f"{prefix}_LOGIN", "")
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    locator_id = os.environ.get(f"{prefix}_LOCATOR", "")
    if not login.strip() or not password:
        print("missing-credentials")
        return 2
    locator_pattern = r"[0-9]+\*(?:[0-9]+|[^\W\d_][^*\r\n]{0,119})\*(?:-1|[0-9]+(?:,[0-9]+)*)\*(?:-1|[0-9]+(?:,[0-9]+)*)"
    if not re.fullmatch(locator_pattern, locator_id):
        print("invalid-search-configuration")
        return 2

    try:
        locator = DoctorLocator(id=locator_id)
    except ValueError:
        print("invalid-search-configuration")
        return 2
    try:
        portal = LuxMedPortal(Luxmed(login=login, password=password))
        with portal.session:
            if not re.fullmatch(r"[0-9]+", locator.service_id):
                city, service_name, clinics, doctors = locator_id.split("*")
                service_id = portal.resolve_in_person_service(service_name)
                locator = DoctorLocator(id=f"{city}*{service_id}*{clinics}*{doctors}")
            slots = portal.search(locator)
    except PortalError as error:
        print(error)
        return 1

    if args.status_only:
        print("search-completed")
    else:
        print(f"search-completed, matching-slots={len(slots)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
