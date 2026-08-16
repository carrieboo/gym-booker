# gym-booker

Books One Playground classes the moment their 72-hour booking window opens.

You add classes to a Google Sheet from your phone. A GitHub Actions job runs
every 30 minutes, and when a class you want is about to become bookable it
waits for the exact release moment and fires. Results are written back to the
sheet so you can see what happened at a glance.

Built for classes that fill in about five minutes, where being a few seconds
late means missing out.

---

## How it works

Booking opens exactly 72 hours before a class starts, so a Saturday 08:00
class unlocks Wednesday 08:00. Every class has its own release moment.

A run does this:

1. Reads the sheet.
2. Skips rows already settled (`BOOKED`, `FULL`, `SKIPPED`, `ALREADY BOOKED`).
3. Books anything whose window is already open.
4. Of the rest, finds the one opening soonest. If that's within 45 minutes,
   it resolves the class, sleeps until the release moment, and fires —
   retrying every 250ms for 20 seconds in case of clock skew.
5. Writes every outcome back to column D.

### Why the sleeping matters

GitHub's scheduled runs are queued, not punctual — 5 to 15 minutes late is
routine. But the lateness only affects *when the script starts*. Once it's
running, Python's clock is accurate to milliseconds, so a run that starts at
07:42 can still fire at exactly 08:00:00.

The 45-minute horizon is the buffer that absorbs the delay. With runs every
30 minutes, a release is caught by at least one run with margin to spare.

### Why only one class per run

`sleep_until` blocks everything. If the script waited for two classes it
would sit through the first while the second's window came and went. So each
run waits for the soonest release only, and marks the rest as waiting —
a later run picks them up.

---

## The sheet

| Column | Header | Example | Notes |
|---|---|---|---|
| A | `class_name` | `Yoga: Flowstate` | Must match the app **exactly**, colon included |
| B | `date` | `2026-08-29` | Format columns B and C as plain text |
| C | `time` | `08:00` | 24-hour |
| D | `status` | | Written by the script — leave blank |
| E | `location` | `Haymarket` | Blank = your default centre |

Locations accepted (case and spaces ignored): Surry Hills, Bunker,
Marrickville, Newtown, Haymarket, Merrylands, North Sydney, Zetland.

Add a class **more than an hour before its window opens** so a run can pick
it up in time. Usually easy, since you're adding days ahead.

### Statuses

| Status | Meaning | Retried? |
|---|---|---|
| `waiting - opens ...` | Window not open yet | Yes, every run |
| `BOOKED <timestamp>` | Got it | No |
| `ALREADY BOOKED` | You were already in — treated as success | No |
| `FULL - missed it` | Sold out before we got there | No |
| `SKIPPED - already started` | Class is in the past | No |
| `NOT FOUND - check name/time` | Usually a typo in the class name | Yes |
| `AMBIGUOUS - N matches` | Two active classes match — needs a human | Yes |
| `UNKNOWN LOCATION '...'` | Typo in column E, valid options listed | Yes |
| `FAILED - ...` | Something else went wrong | Yes |

To retry a settled row, clear its status cell.

---

## Setup

### 1. Your person key

The API has no authentication — your IDs *are* your account. Get them by
logging into the app with DevTools open (Network tab) and finding the
`person-auth` response:

```json
"personKey": { "externalId": "NNNNN", "center": NNN, "id": NNNNN }
```

### 2. Google service account

The script needs write access to your sheet.

1. console.cloud.google.com → new project
2. Enable the **Google Sheets API**
3. Create a **service account**, then Keys → Add Key → JSON
4. Open your sheet → Share → paste the service account's `client_email` →
   **Editor**

Step 4 is the one people forget. The robot can't see the sheet until you
share it with it.

### 3. Secrets

Repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `GYM_CENTER_ID` | Default centre for rows with a blank location |
| `GYM_PERSON_CENTER` | Your home centre — part of your identity, not the class location |
| `GYM_EXTERNAL_ID` | From `personKey.externalId` |
| `GYM_PERSON_ID` | From `personKey.id` |
| `GYM_SHEET_ID` | From the sheet URL, between `/d/` and `/edit` |
| `GOOGLE_CREDENTIALS` | Entire contents of the service account JSON |

`GYM_CENTER_ID` and `GYM_PERSON_CENTER` are usually the same number but mean
different things — one is where to search, the other is who you are.

### 4. Test

Actions tab → Book gym classes → **Run workflow**. Check the log and the
sheet agree.

---

## Running locally

Create a `.env` (already in `.gitignore` — keep it that way):

```
export GYM_CENTER_ID=103
export GYM_PERSON_CENTER=103
export GYM_EXTERNAL_ID=NNNNN
export GYM_PERSON_ID=NNNNN
export GYM_SHEET_ID=...
export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
```

The credentials JSON must be on one line, in single quotes.

```bash
pip3 install -r requirements.txt
source .env
python3 booker.py
```

Set `DRY_RUN = True` in `booker.py` to see what it would do without booking.

---

## API notes

Two endpoints, both `POST` to `https://cms.oneplayground.com.au/api/timetable/`.

**`get-sessions-by-center-and-date`** — `{center_id, from_date, to_date}`.
`to_date` is **exclusive**; `get_sessions` adds a day to hide this.

**`create-participation-and-send-message`** — `{person_key, pk, sk,
send_confirmation_message}`. The `pk` and `sk` are copied verbatim from a
session record; nothing is derived or constructed.

### Gotchas found the hard way

**Cancelled classes stay in the listing** with their spots restored. Filter
on `booking_state == "ACTIVE"` or you'll book a class that doesn't exist.
There are real cases of an active and a cancelled class at the same time in
the same room.

**Match on `booking_name`, not `activity_name`.** They usually agree, but
where they differ the app shows `booking_name`.

**The 72-hour window is not enforced server-side.** The API will accept a
booking ten days out. The script enforces the window itself — deliberately.
Booking outside it is conspicuous and likely to get an account flagged.

**Error codes are machine-readable**, which is what makes the retry loop
work: `PARTICIPANT_ALREADY_BOOKED` and `LISTS_FULL` are final, anything else
is worth retrying.

---

## When it breaks

**`NOT FOUND` on a class you can see in the app** — the name doesn't match
character for character. Check the colon and any capitalisation.

**Everything suddenly failing** — the gym probably changed their API.
Recapture both requests in DevTools and compare against the payloads above.

**Nothing running at all** — GitHub pauses scheduled workflows on repos with
no activity for 60 days. Push any commit to wake it.

---

## Sharing this

Fork it rather than sharing a repo. Secrets live on the repo, not in the
files, so a fork gets the code and none of your credentials. Your friend
adds her own person key, her own sheet, her own service account.

Bear in mind that several people running the same script against the same
release moments starts to look like a pattern rather than a member using a
tool.
