# LuxmedSniper
LUX MED appointments sniper
=======================================
Simple tool to notify about available slot in LUX MED medical care service using pushover notifications.

## Milestone 1 read-only search

Run the offline tests with Python 3.11 or newer.

    python -m pip install -r requirements.txt
    python -m unittest discover -s tests

The manual smoke check uses one separately authorized account and one city and
service variant per run. Configure the selected profile through a private
environment or secrets runner. For `patient1` it reads `LUXMED_PATIENT1_LOGIN`,
`LUXMED_PATIENT1_PASSWORD` and `LUXMED_PATIENT1_LOCATOR`. The other profiles
use the corresponding `PATIENT2` and `PATIENT3` prefixes. Use a confirmed
locator in the existing `cityId*serviceVariantId*facilitiesIds*doctorsIds`
format. You can use the exact Polish Patient Portal service name in the second
field instead of a variant ID. The check resolves one in-person service from
the authenticated catalog and stops if it finds zero or multiple matches.
Keep credentials and patient-specific searches out of the repository,
shell history and shared logs.

    python audit_search.py patient1

Start during 06:00–22:00 Europe/Warsaw, including the 22:00 minute. The
command performs one login and, if a session token is returned, resolves the
service name when needed and makes one availability request for the next 14
days. It outputs a status and, on
success, a count. Each run uses a fresh session and has no Telegram,
appointment or calendar write operations. A successful count of zero means
the expected slot response was returned with no matching slots.

`login-unconfirmed` means the legacy endpoint did not establish a recognized
session. It can indicate a verification challenge or a changed login flow.
If the official Patient Portal requests verification, the account holder
completes it there. A successful browser login alone cannot validate the
script. Stop the smoke test on access denial, a rate limit or unexpected
portal response and verify the supported flow before continuing.
GitHub Actions runs the tests with simulated responses and requires no patient
credentials. The smoke check has no scheduler.

### Run one live check from GitHub

1. In the repository's **Settings → Environments**, create `luxmed-audit`.
   Restrict the environment to the default branch and add the selected
   profile's `LOGIN`, `PASSWORD` and `LOCATOR` secrets using the full names
   above. The locator contains the search criteria and belongs in a secret.
2. Once the workflow is on the default branch, open **Actions → Read-only
   LUX MED check**. Run it on the default branch and select the profile you
   configured. Use it once per account per hour during the Warsaw search
   window. Run the first check with one authorized account before adding the
   others.
3. Read the status in the final step. The job supplies only the selected
   profile's secrets to the check. The workflow log omits slot counts and
   appointment details. Complete any verification in the official Patient
   Portal. Stop if the check reports a restriction or an unconfirmed session.

## Private Telegram availability worker

The worker accepts messages only from its configured owner's private chat.
Example requests include `Find an internist for <patient>`,
`Znajdź pediatrę dla <pacjent>`, and
`/find patient | exact portal service | city`. Wrocław is the default city.
A generic pediatric request asks which visit type to use. Use `/active` to
list searches with their last result and next check. Use `Stop 123` to stop
one, or reply `Stop` to a search's message. Use
`/change 123 | service | city` or `/zmień 123 | usługa | miasto` to update
an active search. You can leave the service blank to change only the city.
An update keeps the search ID, patient and hourly account limit.
Appointment-time, doctor and clinic preferences are outside this first
Telegram worker.

Searches use a rolling 14-day window and repeat at most hourly during
06:00–22:00 Europe/Warsaw until slots are found or the search is stopped.
After-hours requests queue until the next opening. An access restriction or
unconfirmed login pauses that account. Check the official Patient Portal
before using `/resume <patient>`. Availability is returned in the private
chat. The first completed check reports whether it found a match. Later
checks with no matches update `/active` quietly. The worker does not write
appointments or calendar events.

### Deploy to the Linux VM

1. Stage a reviewed release in `/opt/luxmed-telegram/current` with Python
   3.11 or newer and a virtual environment containing `requirements.txt`
   and `deploy/requirements.txt`. Install `deploy/luxmed-telegram.service`.
2. Use the existing `luxmed-audit` GitHub environment on the default branch.
   Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID` and
   `LUXMED_TELEGRAM_PROFILES` as environment secrets alongside the three
   account logins and passwords. The profiles secret maps `patient1` through
   `patient3` to objects with `name`, `child` and optional `aliases`.
3. In the release directory on the VM, run
   `sudo .venv/bin/python -m deploy.install_credentials public-key`.
   Set the returned public key as the environment variable
   `LUXMED_TELEGRAM_RECEIVER_KEY` in GitHub. Run **Package Telegram
   configuration** on the default branch and retrieve that run's encrypted
   artifact. Deliver the sealed file to the VM over its trusted SSH connection.
4. In the release directory on the VM, run
   `sudo .venv/bin/python -m deploy.install_credentials install < telegram-config.sealed`.
   The installer validates the decrypted configuration and stores it as a
   host-bound systemd credential. Enable and start `luxmed-telegram.service`
   after the credential is installed. On rotation, prepare a new transfer key
   and restart the service after installing the new credential.

The VM stores active searches, Telegram offsets and pending notifications in
a private SQLite database. An interruption between Telegram accepting a
notification and the worker recording delivery can result in a duplicate
notification.

How to use LuxmedSniper?
--------------------
First of all create virtualenv and install Python requirements from requirements.txt

1) For each luxmed users create configuration file (yaml format) and save it for example as luxmed_username.yml:
```
luxmed:
  email: EMAIL
  password: PASSWORD
luxmedsniper:
  doctor_locators:
    - id: 1*7409*-1*-1 # (cityId, serviceVariantId, facilitiesIds, doctorsIds) -1 means any.
      # You can get those ids by calling script with "--dump-ids" argument: python3 luxmed_sniper.py --dump-ids
      name: Your unique search name
      enabled: False  # temporary disable from searching
    - id: 1*7681*-1*-1
      name: Your unique search name 2
      enabled: True
  lookup_time_days: 14 # How many days from now should script look at.
pushover:
  user_key: # Your pushover.net user key
  api_token:  # pushover.net App API Token
  message_template: "New visit! {AppointmentDate} at {ClinicPublicName} - {DoctorName}"
  title: "New Lux Med visit available!" # Pushover message topic
misc:
  notifydb: ./notifications-{email}.db # State file used to remember which notifications has been sent already
```

2) Run it
```
nohup python3 luxmed_sniper.py -c /path/to/luxmed_john.yml &
```
or you can split the configuration into separate users/doctors/providers config files
```
nohup python3 luxmed_sniper.py -c user_config.yml luxmed_john.yml &
```
3) Wait for new appointment notifications in your pushover app on mobile :)!

# Warning

Please be advised that running too many queries against LuxMed API may result in locking your LuxMed account.
Breaching the 'fair use policy' for the first time locks the account temporarily for 1 day.
Breaching it again locks it indefinitely and manual intervention with "Patient Portal Support" is required to unlock it.
