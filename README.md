# D-Day

Select a deadline anywhere in your browser, right-click **Add to D-Day**, review what a
**local** AI model extracted, and save it to Google Calendar. Nothing is created until you
press **Save**.

```
 Browser (MV3 extension)                    Local service (python server.py, 127.0.0.1:8765)
 ┌──────────────────────────┐   selected    ┌───────────────────────────────────────────────┐
 │ context menu "Add to     │ ───text────▶  │ extractor.py   Ollama qwen3:1.7b (local)      │
 │ D-Day" → review window   │               │                quotes items, never resolves   │
 │ edit / pick date / Save  │ ◀──tasks────  │ pipeline.py    checks quotes against the text │
 │                          │               │ date_resolver  deterministic dates, timezone  │
 │                          │ ──confirmed─▶ │ calendar_client OAuth + Calendar API ──────────┼──▶ Google Calendar
 └──────────────────────────┘    event      └───────────────────────────────────────────────┘
```

* **Local AI:** text is analysed by Ollama on your machine. No cloud LLM, no paid API.
* **Deterministic dates:** the model only *quotes* phrases like "next Monday". `date_resolver.py`
  turns them into dates using your timezone (default `Asia/Kolkata`). Ambiguous phrases
  (e.g. "next Friday", "05/10", a past date with no year) make you choose; nothing is guessed.
* **You confirm everything:** you can edit every field before saving. A date-only deadline
  becomes an all-day event by default, not a made-up midnight time.

## How D-Day understands a message

The 1.7B model is good at spotting *what* the obligations are, but small models confuse dates.
So D-Day combines it with deterministic reading of the text (`signals.py`) and decides on your behalf
when the intent is clear:

| Situation | What D-Day does |
|---|---|
| Gmail/Outlook/Classroom header ("Nov 16, 9:12 AM (3 days ago)", "Posted Nov 16") | "Tomorrow" means the day after the message was **sent**, and the sent date is never made into an item |
| "Postponed / moved / rescheduled / extended **to** …" | Uses only the **new** date |
| "Before the viva, submit the report" | Due at the viva's date and time |
| The model takes a date from the wrong sentence | Each item uses the date written in **its own** sentence |
| Text that tries to give instructions ("ignore previous instructions…") | Dates in that sentence are never used |
| "next Monday", "05/12", "Thursday" said on a Thursday | Picks the most likely date and shows **Change to …** for the other |
| "at 6" with no AM/PM | 1–7 means PM and 8–11 means AM, shown as an assumption |
| Model missed an item that has a date and a cue like *submit / exam / register* | Added by pattern matching, **unticked** |
| Same deadline extracted twice | Merged |
| Rooms, halls and Meet/Zoom/Teams links | Filled into the event's location |

Items are **pre-ticked** only when they are clearly yours, dated, and in the future. Optional notices
(internships, closures), past events and anything uncertain are shown but unticked. In the common
case you review the summary and press **Save N to Google Calendar** (or Ctrl+Enter). Reminders are
suggested by type: exams and deadlines get 1 day and 2 hours before; meetings get 30 minutes before.

Measured on `eval/cases.json` (20 realistic messages, real model): see [Evaluation](#evaluation).

## Categories

Every task gets one of four categories (the model decides, with keyword checks as a safety net):

| Priority | Category | Covers | Calendar colour |
|---|---|---|---|
| 1 | Internship / Job | applications, placement drives, hiring tests, interviews, offers | Tomato (red) |
| 2 | Work | tasks and meetings for a current job, client or team | Blueberry |
| 3 | College | classes, assignments, exams, notices, fees, viva | Basil (green) |
| 4 | Personal | bills, appointments, family, errands | Banana (yellow) |

Anything mentioning hiring (internship, placement, interview, stipend, shortlisted…) is always filed
under **Internship / Job**. Job items are listed first, pre-ticked even when the message calls them
optional, and get extra reminders (2 days, 1 day and 2 hours before). Change a category with the chip
on each card.

## Dashboard and phone (Telegram)

**Dashboard:** open <http://127.0.0.1:8765/app> in Chrome, or use **Open dashboard** in the extension
popup. It's a local page served by the D-Day service, so it works without internet (only Telegram and Google
need it). To give it its own window and taskbar icon: Chrome ⋮ → *Cast, save and share* → *Install page as app*.

| Tab | What it shows |
|---|---|
| Inbox | Messages from your phone and the paste box, each split into tasks. Tick, edit, change category, then **Save N to Google Calendar** |
| Upcoming | Your D-Day events from Google Calendar, grouped by category, with countdowns |
| History | Everything saved with D-Day |
| Settings | Phone setup, Google connection, name, timezone, start with Windows |

**Share straight from WhatsApp (Android):** the `share_app/` folder is a tiny installable web app that appears
in Android's Share menu as **D-Day**. Long-press a message → Share → D-Day → Send, and it lands in your "D-Day"
Google Tasks list from anywhere (queued on the phone if you're offline). Setup, once:
1. Do the Google Tasks setup below.
2. The app is published automatically to GitHub Pages at `https://mohitahujaa.github.io/D-Day/` by the
   `Publish share app` workflow (`.github/workflows/pages.yml`), whenever `share_app/` changes on `main`.
3. Google Cloud Console → Credentials → Create credentials → OAuth client ID → **Web application**, with
   **Authorized JavaScript origins** = `https://mohitahujaa.github.io`. Paste its Client ID into the dashboard
   (Settings → Share from phone).
4. Scan the QR code shown there with your phone, open it in Chrome, then ⋮ → **Add to Home screen → Install**.

WhatsApp doesn't let other apps receive text messages, so it has no Share → D-Day option. Turn on
**Copy mode** in the phone app instead: copy the message in WhatsApp, then open D-Day and it sends the copied
text by itself (after a 3-second countdown you can cancel; never the same text twice). Android doesn't let apps
read the clipboard in the background, so D-Day reads it only when you open it. With Copy mode off, the app only
sends what you share, paste or type.

**Phone setup with Google Tasks (once, about 3 minutes):**
1. Google Cloud Console → your D-Day project → APIs & Services → Library → **Google Tasks API** → **Enable**.
2. Dashboard → Settings → Phone → **Reconnect Google**, and tick **Google Tasks** on the permission screen.
   (Calendar keeps working even if you leave Tasks unticked.)
3. On your phone, open the **Google Tasks** app. D-Day creates a list called **D-Day**. Add a task there and paste
   the message as its title or details, or share text into Tasks if your phone offers that.

When the PC is on, D-Day checks the list every minute, reads new entries with the local model, puts them in the
dashboard inbox, and ticks them off on your phone so you can see they were picked up. Message text goes through
Google (which already holds your calendar); only events you confirm are created.

**Telegram (optional):** a private bot can feed the same inbox (Settings → Phone (Telegram)). Telegram may refuse
requests from some connections or new bots with "Too Many Requests"; if that happens, use Google Tasks.

Notes:
* The PC must be on (and D-Day running) to process messages. Turn on **Start D-Day when Windows starts** (Settings).
* Inbox messages are kept locally in `%LOCALAPPDATA%\D-Day\inbox.db`; saved or dismissed ones are deleted after 30 days.

## Requirements

* Windows 10/11 (macOS/Linux also work), 8 GB+ RAM (16 GB comfortable)
* Python 3.11+
* [Ollama](https://ollama.com) with the model: `ollama pull qwen3:1.7b` (~1.4 GB)
* Chrome, Edge, Brave or another Chromium browser (version 110+)
* A Google account and a (free) Google Cloud project for the Calendar API

## Quick start

1. **Install dependencies**
   ```bat
   python -m pip install -r requirements.txt
   ollama pull qwen3:1.7b
   ```
2. **Set up Google OAuth** (once): see [Google Cloud setup](#google-cloud-setup) and save the
   downloaded file as `credentials\client_secret.json`.
3. **Install the on/off helper** (once, after loading the extension in step 4):
   `python native_host/install.py`. It registers a small helper so the extension can start and stop
   D-Day itself. After that, use the **On/Off switch** in the D-Day popup, or just use
   **Add to D-Day**: it switches D-Day on automatically. `start-dday.bat` still works too.
4. **Load the extension**: open `chrome://extensions` (or `edge://extensions`), turn on
   **Developer mode**, click **Load unpacked**, and choose the `extension` folder.
5. **Connect Google**: click the D-Day toolbar icon, then **Connect Google Calendar**, then sign in
   and allow access. You'll see "Google Calendar connected".
6. **Use it**: select text such as
   *"Submit the DBMS assignment by Friday at 11:59 PM. The presentation is next Monday at 10 AM."*,
   right-click it and choose **Add to D-Day** (or press **Alt+Shift+D**). Review the cards, pick a
   date where asked, and press **Save to Google Calendar**. Each saved card shows a link to the event.

The first extraction after Ollama starts takes longer while the model loads. On a typical
laptop CPU, expect about 15–45 s per selection; with a GPU it's a few seconds.

## Google Cloud setup

1. Go to <https://console.cloud.google.com/> and create a project (e.g. "D-Day").
2. **APIs & Services → Library**: enable **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   * User type **External** (or Internal for a Workspace org). Fill in the app name and your email.
   * **Data access / Scopes**: add `https://www.googleapis.com/auth/calendar.events` and, for the phone
     inbox, `https://www.googleapis.com/auth/tasks`.
     These are the only scopes D-Day requests. It lets D-Day create events, but not change calendar
     settings, sharing or any other Google data.
   * **Audience / Test users**: add your own Google address while the app is in *Testing*.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   * Application type **Desktop app**.
   * **Redirect URIs:** none need to be registered. Desktop clients accept loopback redirects, and
     D-Day uses `http://127.0.0.1:8765/oauth/callback`.
     If you create a *Web application* client instead, add exactly that URI.
5. Download the JSON and save it as **one** of:
   * `credentials\client_secret.json` in this folder (git-ignored), or
   * `%LOCALAPPDATA%\D-Day\client_secret.json`, or
   * any path set in the `DDAY_GOOGLE_CLIENT_SECRETS` environment variable.
6. Restart the service and connect from the extension popup.

> **Testing-mode note:** while the consent screen is in *Testing*, Google expires refresh
> tokens after 7 days. D-Day detects this and asks you to reconnect. Publishing the app (no
> verification is needed for personal use with your own account) removes the limit; Google
> then shows an "unverified app" warning that you can click through.

### How sign-in and token storage work

* OAuth 2.0 authorization-code flow with **PKCE** and a random `state`, handled by the local
  service. The client secret is read from your JSON file; nothing is hardcoded.
* The refresh token is stored in `%LOCALAPPDATA%\D-Day\google_token.bin`, **encrypted with Windows
  DPAPI** (only your Windows account can decrypt it). On macOS/Linux it uses the OS keyring if the
  `keyring` package is installed, otherwise a `chmod 600` file.
* Expired access tokens are refreshed automatically. If access is revoked (for example at
  <https://myaccount.google.com/permissions>), the stored token is deleted and the review window
  shows **Connect Google Calendar** while keeping your task.
* **Disconnect Google** in the popup revokes the token at Google and deletes it locally.

## Review window

Each extracted item becomes a card showing:

* **Title, type** (deadline, assignment, exam, meeting, reminder, other), and an **All-day** toggle
* **Date, time and optional end time.** All times are in your configured timezone, shown in the header.
* **"Which date did you mean?"** buttons for ambiguous phrases. Save stays disabled until a date is set.
* **Please check:** past dates, a time with no AM/PM, dates the model "quoted" that aren't in your
  text, items that may not apply to you, and so on
* **Reminders** in minutes (all-day: minutes before midnight, so `900` = 9:00 the day before)
* **Description and source excerpt.** The excerpt is only sent to Google if you tick the box.
* **"Will be saved as"**: the exact date, time and timezone, computed by the same code that creates the event
* **Save to Google Calendar** / **Skip**, plus **Cancel** for the whole window, and
  **+ Add a task manually** if nothing was found

Your selection and edits are kept in the extension's local storage until every card is saved
or skipped, so a failure (service stopped, Ollama down, Google offline, sign-in expired) never
loses the task. The toolbar popup lists **Unsaved selections** so you can reopen them.

### Settings (toolbar popup)

| Setting | Default | Meaning |
|---|---|---|
| Your name | (empty) | Lets the model tell tasks addressed to you from ones for others |
| Pick the most likely date automatically | on | Off = you choose a date for every ambiguous phrase |
| Timezone | `Asia/Kolkata` | Used to resolve "tomorrow", "Friday", etc. and for the events |
| Deadlines without a time | All-day | Or a timed event at 23:59 |
| Timed deadlines | Block ending at due time | Or a block starting at the due time |
| Deadline block / event length | 30 / 60 min | Duration when no end time is given |
| Reminders | suggested by type (fallback: timed `60, 1440`; all-day `900`) | Popup reminders, up to 5; editable per item |
| Include source excerpt | off | Adds the excerpt to the event description |
| Local service port | 8765 | Must match `DDAY_PORT` |

## Privacy

| Data | Where it goes |
|---|---|
| Selected text | Extension local storage → local service → Ollama on `localhost`. **Never sent to Google or any AI service.** |
| Page URL, title, other page content | Not read or stored. Only the selection is captured, and only when you invoke D-Day. |
| On **Save** | Title, date/time, timezone, description, reminders (and the excerpt if you ticked it) → Google Calendar API |
| OAuth tokens | DPAPI-encrypted file on your PC; never logged |
| `created_events.json` | Event IDs/links and hashes, kept locally for duplicate detection |

The service log records only request paths, status codes and text *lengths*.

## Security model

* The service listens on **127.0.0.1 only** and rejects other `Host` headers (DNS rebinding).
* `/api/*` accepts only browser-extension origins that send the `X-DDay-Client` header, so a
  website you visit can't make your D-Day create events. To pin your extension's ID, set
  `DDAY_EXTENSION_IDS=<id>` (shown on `chrome://extensions`).
* `/api/events` additionally requires `"confirmed": true` (sent only by the Save button) and
  re-validates every field server-side.
* Webpage text is treated as data: it's delimited in the prompt, the model's output is
  schema-validated, and any date the model reports that isn't in your text is discarded.
* The UI renders all text with `textContent` (no `innerHTML`), uses a strict extension CSP, and
  only links to `https://*.google.com`.
* Duplicate protection: each card has an idempotency key that maps to a deterministic Google event
  ID, so double-clicks and retries after a lost response can't create duplicates. Saving an
  identical event again from a new selection asks first (**Save another copy**).

## Configuration (environment variables)

| Variable | Default |
|---|---|
| `DDAY_PORT` | `8765` |
| `DDAY_TIMEZONE` | `Asia/Kolkata` (server fallback; the extension sends your setting) |
| `DDAY_MODEL` | `qwen3:1.7b` |
| `DDAY_OLLAMA_URL` | `http://localhost:11434` |
| `DDAY_OLLAMA_TIMEOUT` | `120` seconds per request |
| `DDAY_DATA_DIR` | `%LOCALAPPDATA%\D-Day` |
| `DDAY_GOOGLE_CLIENT_SECRETS` | (search order above) |
| `DDAY_EXTENSION_IDS` | any extension origin |

## Troubleshooting

| Symptom | Fix |
|---|---|
| "D-Day is switched off" | Turn it on with the switch in the popup. If the switch says the helper isn't installed, run `python native_host/install.py` (again after moving the folder or reloading the extension under a new ID). |
| Switch turns on but stays off | See `%LOCALAPPDATA%\D-Day\service.log`. |
| "Cannot reach Ollama" | Open the Ollama app or run `ollama serve`. |
| "Model 'qwen3:1.7b' is not installed" | `ollama pull qwen3:1.7b` |
| Extraction times out | Select less text; close other heavy apps; the first run after a restart is slowest. |
| "Google OAuth client is not configured" | Put `client_secret.json` in place (see Google Cloud setup) and restart the service. |
| "Access blocked: app has not completed verification" | Add your account under OAuth consent screen → Test users. |
| "Google access was revoked or has expired" | Click **Connect Google Calendar** again (7-day limit in Testing mode). |
| `redirect_uri_mismatch` | Use a **Desktop app** client, or add `http://127.0.0.1:8765/oauth/callback` to a Web client. |
| No "Add to D-Day" menu | The menu only appears when text is selected. Reload the extension after updating it. |
| Nothing captured on a PDF or restricted page | Paste the text into **Selected text** in the review window and press **Analyse again**. |
| Wrong day for "Friday" etc. | Check the timezone in the popup settings. |

## Development

```bat
python -m pytest -q            # LLM, Google and network calls are mocked
python date_resolver.py        # resolver demo
python extractor.py            # live model demo (needs Ollama)
```

| File | Purpose |
|---|---|
| `extractor.py` | Ollama call, robust JSON parsing, lenient schema repair, chunking for long text |
| `date_resolver.py` | Deterministic date/time resolution with a reference timestamp |
| `signals.py` | Deterministic reading: date phrases, sent-date, postponements, relations, locations |
| `pipeline.py` | Combines model + signals + resolver; anchoring, intent passes, smart defaults |
| `eval/` | Real-model quality benchmark |
| `calendar_client.py` | OAuth (PKCE), token refresh/revocation, event payloads, retries, idempotency |
| `token_store.py` | DPAPI / keyring / file token storage |
| `server.py` | Local Flask API with origin/host guards |
| `native_host/` | Native messaging helper behind the popup's On/Off switch, and its installer |
| `app/`, `app_api.py` | Local dashboard (installable web page) and its API |
| `inbox.py` | Local inbox (SQLite), background extraction worker, save-on-confirm |
| `gtasks_source.py` | Phone inbox via a "D-Day" Google Tasks list |
| `share_app/`, `.github/workflows/pages.yml` | Android share-sheet app, published to GitHub Pages, that adds shared messages to that list |
| `telegram_bot.py` | Optional private Telegram bot (long polling, pairing code, Save button) |
| `settings_store.py`, `autostart.py` | Preferences; start with Windows |
| `extension/` | MV3 extension: context menu, review window, popup |

### Evaluation

`eval/cases.json` holds 20 realistic messages covering college notices, Gmail with headers, Classroom
posts, chat-style text, postponements, extensions, numeric dates, noise and prompt injection, each
with the expected items. `python eval/run_eval.py --label mine` runs them through the **real**
model and reports item recall, the share of items that need no edit, false positives, and the number
of user corrections. Results are saved to `eval/results/`. To compare a bigger model:
`ollama pull qwen3:4b` then `python eval/run_eval.py --model qwen3:4b --label qwen4b`.

### Task schema (`/api/extract` → `tasks[]`)

`title`, `description`, `due_date` (ISO date or null), `due_time` (`HH:MM` or null),
`end_time`, `event_type`, `all_day`, `timezone`, `location`, `source_text`, `confidence` (0–1) and
`confidence_label`, `ambiguities[]` (needs attention), `notes[]` (assumptions made), `selected`
(pre-ticked), `needs_review`, `reminders`, plus resolver details: `date_status`
(`RESOLVED|ASSUMED|LINKED|AMBIGUOUS|UNRESOLVED|NO_DATE`), `date_source` (`explicit|relative|inferred`),
`time_source`, `is_past`, `candidates[]`, `date_question`, `date_text`/`time_text` (the quotes).

### Known limitations

* Recurring events ("every Monday") are created as a single event; add the recurrence in Google Calendar.
* Dates are resolved relative to when you made the selection, not when an email was sent.
  Check relative dates in old messages.
* Extraction quality is limited by the 1.7B model. That's why every item is reviewable and
  dates are checked deterministically.
