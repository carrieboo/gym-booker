import datetime
import json
import os
import time

import gspread
import requests
from google.oauth2.service_account import Credentials

BASE = "https://cms.oneplayground.com.au/api/timetable"

# Type any of these names in the sheet's location column.
# Matching ignores case and spaces, so "North Sydney" == "northsydney".
CENTERS = {
    "surryhills": 101,
    "bunker": 102,
    "marrickville": 103,
    "newtown": 104,
    "haymarket": 105,
    "merrylands": 106,
    "northsydney": 107,
    "zetland": 108,
}

# Used when a row leaves the location column blank.
CENTER_ID = int(os.environ["GYM_CENTER_ID"])

# Your home centre. Part of your identity, NOT the class location -
# it stays the same even when booking at a different gym.
PERSON_CENTER = int(os.environ["GYM_PERSON_CENTER"])

SHEET_ID = os.environ["GYM_SHEET_ID"]

PERSON_KEY = {
    "center": PERSON_CENTER,
    "externalId": os.environ["GYM_EXTERNAL_ID"],
    "id": int(os.environ["GYM_PERSON_ID"]),
}

# Booking opens exactly this long before the class starts.
WINDOW = datetime.timedelta(hours=72)

# How far ahead of a release we're willing to sit and wait.
WAIT_HORIZON = datetime.timedelta(minutes=45)

# Fire this many milliseconds early to absorb network latency.
LEAD_MS = 400

# Keep retrying this long past the release moment.
BURST_SECONDS = 20
RETRY_INTERVAL = 0.25

DRY_RUN = False   # no bookings are sent while this is True

session = requests.Session()


# --------------------------------------------------------------- gym API

def get_sessions(from_date, to_date, center_id):
    """Fetch every class between two dates, both INCLUSIVE (YYYY-MM-DD).

    The API treats to_date as exclusive, so we add a day before sending.
    """
    end = datetime.date.fromisoformat(to_date) + datetime.timedelta(days=1)
    response = session.post(
        f"{BASE}/get-sessions-by-center-and-date",
        json={
            "center_id": center_id,
            "from_date": from_date,
            "to_date": end.isoformat(),
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["sessions"]


def find_class(sessions, booking_name, start_datetime):
    """Return every ACTIVE session matching this name and start time.

    Cancelled classes stay in the listing with their spots restored, so
    filtering on booking_state is what stops us booking a ghost.
    """
    return [
        s for s in sessions
        if s["booking_state"] == "ACTIVE"
        and s["booking_name"] == booking_name
        and s["booking_start_datetime"] == start_datetime
    ]


def resolve(name, date, time_str, center_id):
    """Find the one matching class. Returns (session, error_text)."""
    sessions = get_sessions(date, date, center_id)
    matches = find_class(sessions, name, f"{date} {time_str}:00")

    if not matches:
        return None, "NOT FOUND - check name/time"
    if len(matches) > 1:
        return None, f"AMBIGUOUS - {len(matches)} matches"
    return matches[0], None


def book(pk, sk):
    """Attempt one booking. Returns (status_text, settled).

    settled=True means stop trying: we're in, or we never will be.
    """
    payload = {
        "person_key": PERSON_KEY,
        "pk": pk,
        "sk": sk,
        "send_confirmation_message": True,
    }

    if DRY_RUN:
        return "DRY RUN - would book", False

    try:
        response = session.post(
            f"{BASE}/create-participation-and-send-message",
            json=payload,
            timeout=15,
        )
    except requests.RequestException as exc:
        return f"ERROR - {exc}", False

    if response.status_code == 200:
        return f"BOOKED {datetime.datetime.now():%Y-%m-%d %H:%M}", True

    try:
        code = response.json()["response"]["code"]
    except Exception:
        return f"FAILED - HTTP {response.status_code}", False

    if code == "PARTICIPANT_ALREADY_BOOKED":
        return "ALREADY BOOKED", True
    if code == "LISTS_FULL":
        return "FULL - missed it", True
    return f"FAILED - {code}", False


# ------------------------------------------------------------------ timing

def sleep_until(target):
    """Block until target, coarsely at first then precisely near the end."""
    while True:
        remaining = (target - datetime.datetime.now()).total_seconds()
        if remaining <= 0:
            return
        if remaining > 30:
            time.sleep(remaining - 15)
        elif remaining > 2:
            time.sleep(remaining - 1)
        else:
            time.sleep(0.005)


# ------------------------------------------------------------------- sheet

def write(sheet, row_number, status):
    """Write a status and log it, so sheet and log never disagree."""
    print(f"  row {row_number}: {status}")
    sheet.update_cell(row_number, 4, status)


def parse_row(row):
    """Pull fields out of a sheet row.

    Returns (name, date, time_str, start, center_id, error), or None if
    the row is blank. error is set when the row is unusable in a way
    worth reporting back to the sheet.
    """
    name = str(row.get("class_name", "")).strip()
    date = str(row.get("date", "")).strip()
    time_str = str(row.get("time", "")).strip()
    location = str(row.get("location", "")).strip()

    if not (name and date and time_str):
        return None

    try:
        start = datetime.datetime.fromisoformat(f"{date} {time_str}")
    except ValueError:
        return (None, None, None, None, None,
                f"BAD DATE/TIME - {date} {time_str}")

    if location:
        key = location.lower().replace(" ", "")
        if key not in CENTERS:
            known = ", ".join(sorted(CENTERS))
            return (None, None, None, None, None,
                    f"UNKNOWN LOCATION '{location}' - try: {known}")
        center_id = CENTERS[key]
    else:
        center_id = CENTER_ID

    return name, date, time_str, start, center_id, None


# ---------------------------------------------------------------- booking

def attempt_now(sheet, row_number, name, date, time_str, center_id):
    """Window is already open - resolve and book immediately."""
    target, error = resolve(name, date, time_str, center_id)
    if error:
        write(sheet, row_number, error)
        return

    status, _ = book(target["pk"], target["sk"])
    write(sheet, row_number, status)


def attempt_at(sheet, row_number, name, date, time_str, center_id, opens_at):
    """Wait for the release moment, then fire repeatedly.

    The class is resolved BEFORE the wait, so the call at the release
    moment is a single request with nothing to look up first.
    """
    target, error = resolve(name, date, time_str, center_id)
    if error:
        write(sheet, row_number, error)
        return

    print(f"    waiting until {opens_at:%H:%M:%S} for {name}...")
    sleep_until(opens_at - datetime.timedelta(milliseconds=LEAD_MS))

    deadline = opens_at + datetime.timedelta(seconds=BURST_SECONDS)
    attempts = 0
    last = "no attempts made"

    while datetime.datetime.now() < deadline:
        attempts += 1
        status, settled = book(target["pk"], target["sk"])
        if settled:
            print(f"    settled on attempt {attempts}")
            write(sheet, row_number, status)
            return
        last = status
        time.sleep(RETRY_INTERVAL)

    write(sheet, row_number, f"FAILED after {attempts} attempts - {last}")


# -------------------------------------------------------------------- main

def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    rows = sheet.get_all_records()
    now = datetime.datetime.now()

    print(f"{len(rows)} row(s) in sheet, now {now:%a %d %b %H:%M}")

    pending = []   # (opens_at, row_number, name, date, time_str, center_id)

    for index, row in enumerate(rows):
        row_number = index + 2  # row 1 is headers

        existing = str(row.get("status", "")).strip()
        if existing.startswith(("BOOKED", "ALREADY BOOKED", "FULL", "SKIPPED")):
            continue

        parsed = parse_row(row)
        if parsed is None:
            continue

        name, date, time_str, start, center_id, error = parsed
        if error:
            write(sheet, row_number, error)
            continue

        if start < now:
            write(sheet, row_number, "SKIPPED - already started")
            continue

        opens_at = start - WINDOW

        if now >= opens_at:
            attempt_now(sheet, row_number, name, date, time_str, center_id)
        else:
            pending.append(
                (opens_at, row_number, name, date, time_str, center_id)
            )

    if not pending:
        return

    # Waiting blocks everything, so only ever wait for the soonest release.
    # A later run picks up the rest.
    pending.sort()
    opens_at, row_number, name, date, time_str, center_id = pending[0]

    for other in pending[1:]:
        write(sheet, other[1], f"waiting - opens {other[0]:%a %d %b %H:%M}")

    if opens_at - now <= WAIT_HORIZON:
        attempt_at(sheet, row_number, name, date, time_str, center_id, opens_at)
    else:
        write(sheet, row_number, f"waiting - opens {opens_at:%a %d %b %H:%M}")


if __name__ == "__main__":
    main()
