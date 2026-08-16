import time
import datetime
import json
import os

import gspread
import requests
from google.oauth2.service_account import Credentials

BASE = "https://cms.oneplayground.com.au/api/timetable"
CENTER_ID = int(os.environ["GYM_CENTER_ID"])
SHEET_ID = os.environ["GYM_SHEET_ID"]

PERSON_KEY = {
    "center": CENTER_ID,
    "externalId": os.environ["GYM_EXTERNAL_ID"],
    "id": int(os.environ["GYM_PERSON_ID"]),
}

# Booking opens exactly this long before the class starts.
WINDOW = datetime.timedelta(hours=72)

DRY_RUN = False

session = requests.Session()

def get_sessions(from_date, to_date):
    """Fetch every class between two dates, both INCLUSIVE (YYYY-MM-DD).

    The API treats to_date as exclusive, so we add a day before sending.
    """
    end = datetime.date.fromisoformat(to_date) + datetime.timedelta(days=1)
    response = session.post(
        f"{BASE}/get-sessions-by-center-and-date",
        json={
            "center_id": CENTER_ID,
            "from_date": from_date,
            "to_date": end.isoformat(),
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["sessions"]


def find_class(sessions, booking_name, start_datetime):
    """Return every ACTIVE session matching this name and start time."""
    return [
        s for s in sessions
        if s["booking_state"] == "ACTIVE"
        and s["booking_name"] == booking_name
        and s["booking_start_datetime"] == start_datetime
    ]


def book(pk, sk):
    """Attempt one booking. Returns (status_text, ok)."""
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

    # The server returns a machine-readable code we can branch on.
    try:
        code = response.json()["response"]["code"]
    except Exception:
        return f"FAILED - HTTP {response.status_code}", False

    if code == "PARTICIPANT_ALREADY_BOOKED":
        return "ALREADY BOOKED", True
    if code == "LISTS_FULL":
        return "FULL - missed it", True
    return f"FAILED - {code}", False


# How far ahead of a release we're willing to sit and wait.
WAIT_HORIZON = datetime.timedelta(minutes=45)

# Fire this many milliseconds early to absorb network latency.
LEAD_MS = 400

# Keep retrying this long past the release moment.
BURST_SECONDS = 20
RETRY_INTERVAL = 0.25


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


def resolve(name, date, time_str):
    """Find the one matching class. Returns (session, error_text)."""
    sessions = get_sessions(date, date)
    matches = find_class(sessions, name, f"{date} {time_str}:00")

    if not matches:
        return None, "NOT FOUND - check name/time"
    if len(matches) > 1:
        return None, f"AMBIGUOUS - {len(matches)} matches"
    return matches[0], None


def process_row(row, now):
    """Decide what to do with one row. Returns status text, or None to wait."""
    name = str(row.get("class_name", "")).strip()
    date = str(row.get("date", "")).strip()
    time_str = str(row.get("time", "")).strip()

    if not (name and date and time_str):
        return None

    try:
        class_start = datetime.datetime.fromisoformat(f"{date} {time_str}")
    except ValueError:
        return f"BAD DATE/TIME - {date} {time_str}"

    if class_start < now:
        return "SKIPPED - already started"

    opens_at = class_start - WINDOW

    # Too far out to be worth waiting for. Try again on a later run.
    if opens_at - now > WAIT_HORIZON:
        return None

    target, error = resolve(name, date, time_str)
    if error:
        return error

    # Window already open - book immediately.
    if now >= opens_at:
        if target["remaining_spots"] == 0:
            return "FULL - missed it"
        status, _ = book(target["pk"], target["sk"])
        return status

    # Opening soon. Wait for it, then fire repeatedly.
    print(f"    waiting until {opens_at:%H:%M:%S} for {name}...")
    sleep_until(opens_at - datetime.timedelta(milliseconds=LEAD_MS))

    deadline = opens_at + datetime.timedelta(seconds=BURST_SECONDS)
    attempts = 0
    last = "no attempts made"

    while datetime.datetime.now() < deadline:
        attempts += 1
        status, settled = book(target["pk"], target["sk"])
        if settled:
            print(f"    attempt {attempts}: {status}")
            return status
        last = status
        time.sleep(RETRY_INTERVAL)

    return f"FAILED after {attempts} attempts - {last}"

def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    rows = sheet.get_all_records()
    now = datetime.datetime.now()

    print(f"{len(rows)} row(s) in sheet, now {now:%a %d %b %H:%M}\n")

    for index, row in enumerate(rows):
        row_number = index + 2  # row 1 is headers

        existing = str(row.get("status", "")).strip()
        if existing.startswith(("BOOKED", "ALREADY BOOKED", "FULL", "SKIPPED")):
            print(f"  row {row_number}: settled ({existing}) - skipping")
            continue

        status = process_row(row, now)
        if status is None:
            class_start = f"{row.get('date')} {row.get('time')}"
            try:
                opens = (datetime.datetime.fromisoformat(class_start)
                         - WINDOW)
                note = f"waiting - opens {opens:%a %d %b %H:%M}"
            except ValueError:
                continue
            print(f"  row {row_number}: {note}")
            sheet.update_cell(row_number, 4, note)
            continue

if __name__ == "__main__":
    main()
