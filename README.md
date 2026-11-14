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

The worker accepts English messages from its configured owner's private chat.
Example requests include `Find an internist for <patient>`,
`Find a pediatrician for <patient>`, and
`/find patient | exact portal service | city`. Wrocław is the default city.
A generic pediatric request asks which visit type to use. Use `/active` to
list searches with their last result and next check. Use `Stop 123` to stop
one, or reply `Stop` to a search's message. Use
`/change 123 | service | city | filters` to update
an active search. You can leave the service blank to change only the city.
An update keeps the search ID and patient and checks the new criteria at
the next eligible time.
If the requested service conflicts with the named patient's age group, the
worker asks for a corrected request. When every search has ended, the next
search is numbered #1. A reply to an older message stays tied to that
message's original search.

For example, send “Find a pediatrician for <patient> next week, after 16:00,
near Dominikańska” and then choose the visit type. The worker shows the
interpreted filters in the confirmation and in `/active`. Date filters
include today, tomorrow, a weekday, this week, next week or a date in
YYYY-MM-DD format. A week runs Monday to Sunday. Times use Warsaw time.
Use `around 16:00` for a one-hour tolerance in either direction, `after
16:00`, `before 16:00`, or `between 09:00 and 12:00`. Use `with Dr
<full name>` to filter for a doctor and `clinic <name>` to filter for a
clinic. `Near <name>` prefers clinics whose displayed name or address
matches that text. The worker can tell you about matches elsewhere while
continuing to look for the preferred clinic. It does not calculate distances.
Use `Change 123 to after 17:00` or `Change 123 to clear filters` to update
an active search in plain English. A search for a fixed date window ends
once those dates have passed.

Searches use a rolling 14-day window. A new search gets its first check
as soon as the worker can run it during 06:00–22:00 Europe/Warsaw, even
when another search for the same patient is already running. Each search
then repeats hourly until slots are found or the search is stopped. The
worker can reuse a recent signed-in session for that patient's searches.
After-hours requests queue until the next opening. An access restriction or
unconfirmed login pauses that account. Check the official Patient Portal
before using `/resume <patient>`. Availability is returned in the private
chat. The first completed check reports whether it found a match. Later
checks with no matches update `/active` quietly.

Results are numbered in groups of three. Reply `More` for more options from
the same check, or `Details 2` for the details of option 2 and the Patient
Portal link. The bot flags selections within six hours of the appointment.
Confirm availability and book in the portal. `Continue`
or `Find something else` starts a follow-up with the same filters, skipping
slots already shown and respecting the search's next hourly check.
Reply to a result message to select that list. Standalone commands use the
latest list.

The standard sick-child pediatric visit has a LUX MED questionnaire. The
bot can list times, and `Details 2` links to the Patient Portal to complete
the questionnaire and reserve the visit there. When LUX MED flags a visit
as requiring preparation, the bot points you to the portal instructions.

### Direct Telegram booking pilot

Direct booking defaults to off. The live audit of the worker's session
received HTTP 401 at the questionnaire check after the booking limit,
referral, and anti-forgery token requests succeeded. Enable the pilot only
after the worker has a session that passes the portal's booking checks.
The configuration package accepts the environment variable
`LUXMED_DIRECT_BOOKING=1` for that rollout.

A read-only internist check in a temporary GCP session verified by email
received HTTP 200 and a questionnaire. After the patient answered it in
Chrome, a fresh email-verified GCP session received JSON `null`, and the
eligibility, no-show restriction, and reservation verification reads passed.
The bot treats JSON `null` as an empty questionnaire. It stops when LUX MED
returns questions or when it cannot recognize the response.

With the pilot enabled, reply `Book 2` to a fresh list to request that exact
slot. The initial scope is covered in-person internist and routine
pediatric visits. The bot rechecks the slot and asks for
approval when it starts in less than six hours, it detects a scheduling
conflict, or a conflict check is unavailable. Use `Approve 1` with the
request number shown, or `Stop booking 1` to drop a pending request.
The bot hands off to the Patient Portal for charges, referrals, changes
to an existing visit, or a booking restriction. If confirmation is
unclear, check the Patient Portal and reply `Reviewed 1` to allow new
booking requests for that patient. The worker never retries an uncertain
reservation.

If the questionnaire endpoint returns HTTP 401, the worker pauses direct
booking for that patient and ends their pending booking requests.
Availability searches keep their own access checks. Use `/resume <patient>`
after booking access is restored, then request a slot from a fresh search.

For calendar conflict checks, add the optional environment secret
`LUXMED_BOOKING_CALENDAR` as a JSON object with `client_id`, `client_secret`,
`refresh_token`, and a list of `calendar_ids`. Use a Google Calendar
free/busy scope and run the encrypted configuration handoff again. The
worker requests busy time ranges only. Without this configuration it
asks you to check your calendar before approving.

### Read-only booking feasibility

Run `python audit_search.py patient1 --booking-check` with the same private
profile settings and Warsaw search hours as the manual availability check.
The command reads the dashboard's upcoming visits and the no-show restriction
for the selected city and service without a referral. Its output is a status
only. The upcoming visits response can verify a reservation by its ID when
the dashboard shows the visit as reserved or confirmed. An absent ID leaves
the result unknown. A clear no-show restriction check does not establish
eligibility, payment terms, or calendar availability.

Use `--booking-preflight` to check booking eligibility, no-show restrictions,
and reservation verification in a fresh session. When the portal rejects
a booking prerequisite request, the audit reports the check and HTTP status.
The Telegram worker also discards a cached session when its token's
expiration is reached, if the portal supplied an expiry timestamp.

The [LUX MED booking guide](https://www.luxmed.pl/dla-pacjenta/portal-pacjenta/rezerwacja-i-zarzadzanie-uslugami)
describes the reservation summary and timeline. The portal's reservation
page places a temporary hold before displaying slot-specific warnings,
referral prompts and pricing. The audit performs only login and read
requests and leaves the appointment inventory untouched.

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

### Optional free-text requests

Set `OPENAI_API_KEY` as a secret in the same GitHub environment and repeat
the encrypted configuration handoff. Use an OpenAI API project key. The
worker uses the Responses API for short English requests that the
built-in parser could not interpret, such as “Could you check whether there’s
an internist available for [patient] in Wrocław?”

The worker recognizes the patient, service, and city locally. Only a small
set of scheduling words and placeholders leave the VM. It sends no chat
history, patient labels, service labels, results, or portal credentials.
Messages with unrecognized details stay local. The model classifies intent.
The worker validates the search criteria and controls all
account access and scheduling.

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
