"""
Deterministic date/time resolution for D-Day.

The LLM only *quotes* date/time phrases from the source text ("Friday",
"next Monday", "11:59 PM"). This module turns those phrases into concrete
dates in the user's timezone, without guessing:

* A reference timestamp makes every result reproducible in tests.
* Explicit dates ("28 Sept 2026") are distinguished from relative ones
  ("tomorrow") and from inferred ones ("the 28th": month assumed).
* Genuinely ambiguous phrases ("next Friday", "05/06", "Sept 3" when that
  already passed) return status AMBIGUOUS with candidate dates instead of
  picking one silently.
* Past dates are flagged (``is_past``), never silently moved forward.
* Date-only deadlines stay date-only (all-day) unless the caller opts into
  the legacy "end of day 23:59" behaviour.

Public API (kept compatible with the original prototype):
    resolve_date_time(date_text, time_text, reference_datetime=None,
                      timezone_str="Asia/Kolkata", item_type=None,
                      untimed_deadline="all_day") -> dict
    parse_date(date_text, ref_date) -> (date | None, error | None)
    parse_time(time_text, default_for_type=None) -> (time | None, inferred, error | None)
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta, timezone as dt_timezone, tzinfo
from typing import Optional, Tuple, Dict, Any, List
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser
from dateutil.relativedelta import relativedelta

DEFAULT_TIMEZONE = "Asia/Kolkata"

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30,
}

# Common timezone abbreviations seen in notices/emails. Ambiguous ones
# (e.g. "CST" = China or US Central) map to the most common usage and the
# result carries a note saying the time was converted.
TZ_ABBREVIATIONS = {
    "ist": "Asia/Kolkata",
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago", "ct": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver", "mt": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "bst": "Europe/London",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin",
    "jst": "Asia/Tokyo", "sgt": "Asia/Singapore", "hkt": "Asia/Hong_Kong",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney",
    "gst": "Asia/Dubai", "pkt": "Asia/Karachi", "npt": "Asia/Kathmandu",
}

_TZ_ALT = "|".join(sorted(TZ_ABBREVIATIONS, key=len, reverse=True))
_TZ_OFFSET_RE = r"(?:utc|gmt)\s*[+-]\s*\d{1,2}(?::?\d{2})?"
_TIME_TOKEN = (
    r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2}|noon|midnight|midday|eod)"
)

STATUS_RESOLVED = "RESOLVED"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_UNRESOLVED = "UNRESOLVED"
STATUS_NO_DATE = "NO_DATE"

# Year-less dates up to this many days in the past are most likely "this
# year, already passed" (e.g. a late notice); older ones probably mean next
# year. Either way the user is asked to choose.
RECENT_PAST_DAYS = 60


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

# Hinglish (Hindi in Latin script) date/time words -> English, so casual
# messages like "kal 11 baje" resolve like "tomorrow 11".
_HINGLISH_DATE = [
    (r"\b(?:parso|parson|parsoon)\b", "day after tomorrow"),
    (r"\b(?:aaj|aj)\b", "today"),
    (r"\b(?:kal|kl)\b", "tomorrow"),
    (r"\b(?:somvar|somwar)\b", "monday"), (r"\b(?:mangalvar|mangalwar)\b", "tuesday"),
    (r"\b(?:budhvar|budhwar)\b", "wednesday"), (r"\b(?:guruvar|guruwar|brihaspativar)\b", "thursday"),
    (r"\b(?:shukravar|shukrawar)\b", "friday"), (r"\b(?:shanivar|shaniwar)\b", "saturday"),
    (r"\b(?:ravivar|raviwar|itvaar|itwar)\b", "sunday"),
    (r"\bagle\s+(?:hafte|week)\b", "next week"), (r"\bis\s+(?:hafte|week)\b", "this week"),
    (r"\s+(?:tak|ko|se\s+pehle|se\s+pahle|wala|waala)\s*$", ""),
]
_HINGLISH_TIME = [
    (r"\b(?:subah|savere|sawere)\s*(\d{1,2}(?::\d{2})?)\s*(?:baje)?\b", r"\1 am"),
    (r"\b(\d{1,2}(?::\d{2})?)\s*baje\s*(?:subah|savere)\b", r"\1 am"),
    (r"\b(?:dopahar|dopehar|shaam|sham|raat|rat)\s*(?:ko\s*)?(\d{1,2}(?::\d{2})?)\s*(?:baje)?\b", r"\1 pm"),
    (r"\b(\d{1,2}(?::\d{2})?)\s*baje\s*(?:dopahar|dopehar|shaam|sham|raat|rat)\b", r"\1 pm"),
    (r"\b(\d{1,2}(?::\d{2})?)\s*baje\b", r"\1"),
    (r"\bsubah\b", "morning"), (r"\b(?:dopahar|dopehar)\b", "afternoon"),
    (r"\b(?:shaam|sham)\b", "evening"), (r"\b(?:raat|rat)\b", "night"),
    (r"\s+(?:tak|ko|se\s+pehle)\s*$", ""),
]


def hinglish_to_english(text: Optional[str], kind: str = "date") -> Optional[str]:
    if not text:
        return text
    out = text
    for pat, rep in (_HINGLISH_DATE if kind == "date" else _HINGLISH_TIME):
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    return out.strip()


def normalize_text(text: Optional[str]) -> Optional[str]:
    """Cleans raw model extraction strings, normalizing empty/null representations."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    val = text.strip()
    if val.lower() in ("null", "none", "n/a", "na", "unknown", "nil", "undefined", "", "-", "tbd", "tba"):
        return None
    return val


def clean_tokens(text: str) -> str:
    """Lowers text, strips punctuation and common leading prepositions."""
    cleaned = text.strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[.,;!?]+$", "", cleaned)
    # Repeatedly strip leading filler: "due by", "on or before", "the" ...
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = re.sub(
            r"^(by|before|on or before|on|until|till|at|due|due\s+by|due\s+on|"
            r"scheduled\s+for|deadline(?:\s+is)?|no later than|latest by|the|of|for|from)\s+",
            "",
            cleaned,
        )
    return cleaned.strip()


def extract_embedded_time(date_text: str, time_text: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    If the date text contains an embedded time expression (e.g. 'Friday at 6 PM',
    'Sep 28 11:59 PM IST') and time_text is not provided, split them cleanly.
    """
    if time_text:
        return date_text, time_text

    tz_part = rf"(?:\s*(?:{_TZ_OFFSET_RE}|\b(?:{_TZ_ALT})\b))?"
    pattern = (
        rf"(?:\b(?:at|by|before|@)\s*)?\b({_TIME_TOKEN}"
        rf"(?:\s*(?:-|–|to)\s*{_TIME_TOKEN})?{tz_part})(?![\w:])"
    )
    m = re.search(pattern, date_text, re.IGNORECASE)
    if m:
        extracted_time = m.group(1).strip()
        cleaned_date = (date_text[:m.start()] + " " + date_text[m.end():]).strip()
        cleaned_date = re.sub(r"\b(at|by|before|@)\s*$", "", cleaned_date, flags=re.IGNORECASE)
        cleaned_date = re.sub(r"[\s,]+$", "", cleaned_date).strip()
        return cleaned_date, extracted_time

    return date_text, time_text


def _fmt(d: date) -> str:
    return f"{WEEKDAY_NAMES[d.weekday()]}, {d.strftime('%d %b %Y')}"


def _word_in(words: Dict[str, int], text: str) -> Optional[int]:
    for w, v in sorted(words.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{w}\b", text):
            return v
    return None


# ---------------------------------------------------------------------------
# Time interpretation
# ---------------------------------------------------------------------------

@dataclass
class TimeInterpretation:
    time: Optional[time] = None
    end_time: Optional[time] = None
    inferred: bool = False
    tz: Optional[tzinfo] = None
    tz_label: Optional[str] = None
    error: Optional[str] = None
    ambiguities: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _split_timezone(cleaned: str) -> Tuple[str, Optional[tzinfo], Optional[str]]:
    m = re.search(rf"\b({_TZ_OFFSET_RE})\b", cleaned)
    if m:
        off = re.sub(r"\s+", "", m.group(1))
        mm = re.match(r"(?:utc|gmt)([+-])(\d{1,2})(?::?(\d{2}))?", off)
        sign = 1 if mm.group(1) == "+" else -1
        delta = timedelta(hours=int(mm.group(2)), minutes=int(mm.group(3) or 0))
        rest = (cleaned[:m.start()] + cleaned[m.end():]).strip()
        return rest, dt_timezone(sign * delta), m.group(1).upper()
    m = re.search(rf"(?:\(|\b)({_TZ_ALT})(?:\)|\b)", cleaned)
    if m:
        rest = (cleaned[:m.start()] + cleaned[m.end():]).strip(" ()")
        return rest, ZoneInfo(TZ_ABBREVIATIONS[m.group(1)]), m.group(1).upper()
    return cleaned, None, None


def _parse_single_time(token: str, default_meridiem: Optional[str] = None):
    """Returns (time, had_meridiem, error)."""
    token = token.strip()
    token = re.sub(r"a\.?m\.?", "am", token)
    token = re.sub(r"p\.?m\.?", "pm", token)

    if token in ("noon", "midday"):
        return time(12, 0), True, None
    if token in ("midnight", "end of day", "eod", "cob", "close of business", "end of the day"):
        return time(23, 59), True, None

    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", token)
    if not m:
        m24 = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", token)
        if m24:
            h, mi, s = int(m24.group(1)), int(m24.group(2)), int(m24.group(3))
            if h <= 23 and mi <= 59 and s <= 59:
                return time(h, mi, s), True, None
            return None, False, f"Invalid time range: '{token}'"
        return None, False, f"Could not parse time expression: '{token}'"

    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    meridiem = m.group(3) or default_meridiem
    if minute > 59:
        return None, False, f"Invalid time range: '{token}'"

    if meridiem:
        if hour < 1 or hour > 12:
            return None, False, f"Invalid 12-hour hour: '{token}'"
        if hour == 12:
            hour = 0 if meridiem == "am" else 12
        elif meridiem == "pm":
            hour += 12
        return time(hour, minute), True, None

    if 0 <= hour <= 23:
        return time(hour, minute), False, None
    return None, False, f"Invalid time range: '{token}'"


def interpret_time(time_text: Optional[str]) -> TimeInterpretation:
    norm = normalize_text(hinglish_to_english(time_text, "time"))
    if not norm:
        return TimeInterpretation()

    cleaned = clean_tokens(norm)
    cleaned, tz, tz_label = _split_timezone(cleaned)
    cleaned = clean_tokens(cleaned) if cleaned else cleaned
    result = TimeInterpretation(tz=tz, tz_label=tz_label)

    approximate = {"morning": time(9, 0), "afternoon": time(14, 0),
                   "evening": time(18, 0), "night": time(20, 0), "tonight": time(20, 0)}
    if cleaned in approximate:
        result.time = approximate[cleaned]
        result.inferred = True
        result.notes.append(
            f"Only an approximate time was given ('{norm}'); assumed {result.time.strftime('%H:%M')}."
        )
        return result

    # Ranges: "10 am - 12 pm", "2-4 pm", "from 2 to 4 pm", "10:00–11:30"
    rng = re.fullmatch(r"(.+?)\s*(?:-|–|—|to|until|till)\s*(.+)", cleaned)
    if rng and re.search(r"\d|noon|midnight", rng.group(1)) and re.search(r"\d|noon|midnight", rng.group(2)):
        end_t, end_mer, end_err = _parse_single_time(rng.group(2))
        end_meridiem = None
        mm = re.search(r"(am|pm|a\.m\.|p\.m\.)\s*$", rng.group(2))
        if mm:
            end_meridiem = "am" if mm.group(1).startswith("a") else "pm"
        start_t, start_mer, start_err = _parse_single_time(rng.group(1), default_meridiem=end_meridiem)
        if start_err or end_err:
            result.error = start_err or end_err
            return result
        # "11-1 pm" -> 11 am to 1 pm
        if end_meridiem and start_t > end_t and not re.search(r"am|pm", rng.group(1)):
            start_t = time(start_t.hour - 12, start_t.minute) if start_t.hour >= 12 else start_t
        if not (start_mer or end_mer):
            start_t = _infer_meridiem(result, start_t, norm)
            if end_t.hour < start_t.hour and end_t.hour + 12 <= 23:
                end_t = time(end_t.hour + 12, end_t.minute)
        result.time, result.end_time = start_t, end_t
        return result

    # Find the first time-looking token inside a longer phrase ("by 5 pm sharp").
    m = re.search(r"\b(\d{1,2}(?::\d{2}){0,2}\s*(?:a\.?m\.?|p\.?m\.?)?|noon|midday|midnight|eod|end of (?:the )?day|cob|close of business)(?![\w])", cleaned)
    if not m:
        result.error = f"Could not parse time expression: '{time_text}'"
        return result
    t, had_mer, err = _parse_single_time(m.group(1))
    if err:
        result.error = err if "'" in err else f"{err}: '{time_text}'"
        if "Invalid" in err:
            result.error = err.replace(f"'{m.group(1).strip()}'", f"'{time_text}'")
        return result
    if not had_mer:
        t = _infer_meridiem(result, t, norm)
    result.time = t
    return result


def _infer_meridiem(result: TimeInterpretation, t: time, original: str) -> time:
    """
    A bare "at 6" or "5:30" has no AM/PM. Deadlines, classes and meetings
    happen in the day, so 1-7 means PM and 8-11 means AM. 24-hour values
    (13-23, or a leading zero like "09:30") are taken as written.
    """
    if not (1 <= t.hour <= 11) or re.search(r"\b0\d", original):
        return t
    guessed = time(t.hour + 12, t.minute) if t.hour <= 7 else t
    result.notes.append(
        f"'{original}' has no AM/PM; assumed {guessed.strftime('%I:%M %p').lstrip('0')}."
    )
    return guessed


def parse_time(
    time_text: Optional[str],
    default_for_type: Optional[str] = None,
) -> Tuple[Optional[time], bool, Optional[str]]:
    """
    Backward-compatible wrapper. Returns (parsed_time, is_inferred, error_message).

    Note: unlike the original prototype, a missing time is NOT defaulted to
    23:59 here; that policy lives in resolve_date_time(untimed_deadline=...).
    """
    interp = interpret_time(time_text)
    if interp.error:
        return None, False, interp.error
    return interp.time, interp.inferred, None


# ---------------------------------------------------------------------------
# Date interpretation
# ---------------------------------------------------------------------------

@dataclass
class DateInterpretation:
    date: Optional[date] = None
    source: Optional[str] = None  # "explicit" | "relative" | "inferred"
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    suggested: Optional[int] = None
    ambiguities: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None
    kind: Optional[str] = None  # e.g. "same_weekday"

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def _ambiguous(cands: List[Tuple[date, str]], message: str, suggested: Optional[int], source: str) -> DateInterpretation:
    return DateInterpretation(
        date=None,
        source=source,
        candidates=[{"date": d.isoformat(), "label": f"{lbl} ({_fmt(d)})"} for d, lbl in cands],
        suggested=suggested,
        ambiguities=[message],
    )


def _weekday_expression(cleaned: str, ref_date: date) -> Optional[DateInterpretation]:
    wd_alt = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
    w = ref_date.weekday()
    week_start = ref_date - timedelta(days=w)

    # "friday next week", "friday of next week", "next week friday", "this week's friday"
    m = re.fullmatch(rf"({wd_alt})(?:,)?\s+(?:of\s+)?(next|this|the following|following)\s+week", cleaned) or \
        re.fullmatch(rf"(next|this)\s+week(?:'s)?\s+({wd_alt})", cleaned)
    if m:
        groups = m.groups()
        wd_name = groups[0] if groups[0] in WEEKDAYS else groups[1]
        which = groups[1] if groups[0] in WEEKDAYS else groups[0]
        target = WEEKDAYS[wd_name]
        offset_weeks = 0 if which == "this" else 1
        d = week_start + timedelta(weeks=offset_weeks, days=target)
        return DateInterpretation(date=d, source="relative")

    m = re.fullmatch(
        rf"(next|this|this coming|coming|upcoming|following|last|past|previous)?\s*({wd_alt})",
        cleaned,
    )
    if not m:
        return None

    modifier = (m.group(1) or "").strip()
    target = WEEKDAYS[m.group(2)]
    days_ahead = (target - w) % 7
    name = WEEKDAY_NAMES[target]

    if modifier in ("last", "past", "previous"):
        back = (w - target) % 7 or 7
        return DateInterpretation(date=ref_date - timedelta(days=back), source="relative")

    if modifier in ("next", "following"):
        if days_ahead == 0:
            # "next Friday" said on a Friday: one week out.
            return DateInterpretation(date=ref_date + timedelta(days=7), source="relative")
        coming = ref_date + timedelta(days=days_ahead)
        week_after = coming + timedelta(days=7)
        # If the coming occurrence already falls in next calendar week, most
        # speakers mean it; otherwise "next" usually skips this week. Still a
        # genuine ambiguity, so the user must choose.
        suggested = 0 if target < w else 1
        return _ambiguous(
            [(coming, f"The coming {name}"), (week_after, f"{name} of the following week")],
            f"'next {name}' could mean {_fmt(coming)} or {_fmt(week_after)}.",
            suggested,
            "relative",
        )

    if days_ahead == 0:
        if modifier in ("coming", "upcoming", "this coming"):
            return DateInterpretation(date=ref_date + timedelta(days=7), source="relative")
        if modifier == "this":
            return DateInterpretation(date=ref_date, source="relative")
        interp = _ambiguous(
            [(ref_date, "Today"), (ref_date + timedelta(days=7), f"Next {name}")],
            f"Today is {name}; '{name}' could mean today or a week from today.",
            0,
            "relative",
        )
        interp.kind = "same_weekday"
        return interp

    return DateInterpretation(date=ref_date + timedelta(days=days_ahead), source="relative")


def _numeric_date(cleaned: str, ref_date: date, month_first: bool = False) -> Optional[DateInterpretation]:
    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", cleaned)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return DateInterpretation(date=date(y, mo, d), source="explicit")
        except ValueError:
            return DateInterpretation(error=f"Invalid date: '{m.group(0)}'")

    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2}|\d{4}))?\b", cleaned)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    year_txt = m.group(3)
    year = None
    if year_txt:
        year = int(year_txt) + (2000 if len(year_txt) == 2 else 0)

    readings = []  # (date, label)
    for day, month, label in ((a, b, "day/month"), (b, a, "month/day")):
        try:
            readings.append((date(year or ref_date.year, month, day), label))
        except ValueError:
            pass
    # Deduplicate (e.g. 05/05)
    uniq = []
    for d, lbl in readings:
        if d not in [u[0] for u in uniq]:
            uniq.append((d, lbl))
    if not uniq:
        return DateInterpretation(error=f"Invalid date: '{m.group(0)}'")

    if len(uniq) == 1:
        interp = DateInterpretation(date=uniq[0][0], source="explicit")
        return _apply_missing_year(interp, year is not None, ref_date)

    # Both readings valid: DD/MM is the default in India, but ask.
    cands = []
    for d, lbl in uniq:
        if year is None and d < ref_date - timedelta(days=RECENT_PAST_DAYS):
            d = d.replace(year=d.year + 1)
        cands.append((d, f"Read as {lbl}"))
    preferred = "month/day" if month_first else "day/month"
    suggested = next((i for i, (_, lbl) in enumerate(cands) if preferred in lbl), 0)
    return _ambiguous(
        cands,
        f"'{m.group(0)}' could be read as day/month or month/day.",
        suggested,
        "explicit",
    )


def _apply_missing_year(interp: DateInterpretation, has_year: bool, ref_date: date) -> DateInterpretation:
    """A year-less date that already passed is flagged, never silently moved."""
    if has_year or interp.date is None or interp.date >= ref_date:
        return interp
    past = interp.date
    try:
        future = past.replace(year=past.year + 1)
    except ValueError:  # Feb 29
        future = past + relativedelta(years=1)
    suggested = 0 if (ref_date - past).days <= RECENT_PAST_DAYS else 1
    return _ambiguous(
        [(past, "This year (already passed)"), (future, "Next year")],
        f"No year was given and {_fmt(past)} has already passed. Did you mean next year?",
        suggested,
        interp.source or "explicit",
    )


def interpret_date(date_text: Optional[str], ref_date: date, month_first: bool = False) -> DateInterpretation:
    raw_norm = normalize_text(date_text)
    norm = normalize_text(hinglish_to_english(date_text, "date"))
    if not norm:
        return DateInterpretation()
    if raw_norm and re.search(r"\b(?:kal|kl)\b", raw_norm, re.IGNORECASE):
        hinglish_note = "Read 'kal' as tomorrow (it can also mean yesterday)."
    else:
        hinglish_note = None
    result = _interpret_date_core(norm, date_text, ref_date, month_first)
    if hinglish_note and not result.error:
        result.notes.append(hinglish_note)
    return result


def _interpret_date_core(norm: str, date_text: Optional[str], ref_date: date, month_first: bool) -> DateInterpretation:

    cleaned = clean_tokens(norm)
    cleaned = re.sub(r"\s*\(.*?\)\s*", " ", cleaned).strip()  # drop "(Friday)" style asides kept below
    weekday_hint = _word_in(WEEKDAYS, clean_tokens(norm))

    # --- Simple relative terms -------------------------------------------------
    simple = {
        "today": 0, "tonight": 0, "this evening": 0, "this morning": 0, "this afternoon": 0,
        "tomorrow": 1, "tmrw": 1, "tmr": 1, "tomorrow morning": 1, "tomorrow evening": 1,
        "tomorrow night": 1, "tomorrow afternoon": 1,
        "day after tomorrow": 2, "the day after tomorrow": 2, "overmorrow": 2,
        "yesterday": -1,
    }
    if cleaned in simple:
        return DateInterpretation(date=ref_date + timedelta(days=simple[cleaned]), source="relative")

    # --- Offsets: "in 3 days", "in three weeks", "2 days from now", "a week from today"
    num_alt = r"\d+|" + "|".join(NUMBER_WORDS)
    m = re.fullmatch(
        rf"(?:in|within)\s+(?:the\s+next\s+)?({num_alt})\s+(day|days|week|weeks|month|months)"
        rf"|({num_alt})\s+(day|days|week|weeks|month|months)\s+(?:from\s+(?:now|today)|later)",
        cleaned,
    )
    if m:
        qty_txt = m.group(1) or m.group(3)
        unit = m.group(2) or m.group(4)
        qty = int(qty_txt) if qty_txt.isdigit() else NUMBER_WORDS[qty_txt]
        if unit.startswith("day"):
            d = ref_date + timedelta(days=qty)
        elif unit.startswith("week"):
            d = ref_date + timedelta(weeks=qty)
        else:
            d = ref_date + relativedelta(months=qty)
        interp = DateInterpretation(date=d, source="relative")
        if cleaned.startswith("within"):
            interp.notes.append(f"'{norm}' is a window; using its last day as the deadline.")
        return interp

    # --- End of month -------------------------------------------------------
    m = re.fullmatch(r"(?:the\s+)?end\s+of\s+(?:the\s+)?(this|next)?\s*month", cleaned)
    if m:
        base = ref_date + relativedelta(months=1 if m.group(1) == "next" else 0)
        last = calendar.monthrange(base.year, base.month)[1]
        return DateInterpretation(date=base.replace(day=last), source="relative")
    m = re.fullmatch(r"(?:the\s+)?end\s+of\s+([a-z]+)(?:\s+(\d{4}))?", cleaned)
    if m and m.group(1) in MONTHS:
        mo = MONTHS[m.group(1)]
        yr = int(m.group(2)) if m.group(2) else ref_date.year
        interp = DateInterpretation(date=date(yr, mo, calendar.monthrange(yr, mo)[1]), source="explicit")
        return _apply_missing_year(interp, bool(m.group(2)), ref_date)

    # --- Vague periods: refuse to guess a specific day -------------------------
    if re.fullmatch(
        r"(?:this|next|the)?\s*(?:week|weekend|month|year)|(?:end|middle) of (?:the )?(?:this |next )?week|"
        r"soon|asap|later|eow|next few days|early next week|later this week|mid[- ]?[a-z]+",
        cleaned,
    ):
        return DateInterpretation(
            error=f"'{norm}' does not name a specific day.",
            ambiguities=[f"'{norm}' does not name a specific day; please pick a date."],
        )

    # --- Weekdays -------------------------------------------------------------
    wk = _weekday_expression(cleaned, ref_date)
    if wk is not None:
        return wk

    # --- Numeric dates ----------------------------------------------------------
    num = _numeric_date(cleaned, ref_date, month_first)
    if num is not None:
        return _check_weekday(num, weekday_hint)

    # --- Month-name dates ---------------------------------------------------------
    month = _word_in(MONTHS, cleaned)
    no_year = re.sub(r"\b(19|20)\d\d\b", " ", cleaned)
    day_match = re.search(r"\b(\d{1,2})(st|nd|rd|th)?\b", no_year)
    has_year = bool(re.search(r"\b(19|20)\d\d\b", cleaned))

    if month is not None:
        if not day_match:
            return DateInterpretation(
                error=f"'{norm}' names a month but no day.",
                ambiguities=[f"'{norm}' names a month but no specific day; please pick a date."],
            )
        try:
            dt = dateutil_parser.parse(
                cleaned,
                default=datetime(ref_date.year, month, 1),
                fuzzy=True,
                dayfirst=True,
            )
        except (ValueError, OverflowError):
            return DateInterpretation(error=f"Could not parse date expression: '{date_text}'")
        interp = DateInterpretation(date=dt.date(), source="explicit")
        interp = _apply_missing_year(interp, has_year, ref_date)
        return _check_weekday(interp, weekday_hint)

    # --- Day-of-month only: "the 28th", "Monday the 28th" ---------------------------
    m = re.fullmatch(rf"(?:(?:{'|'.join(WEEKDAYS)}),?\s+)?(?:the\s+)?(\d{{1,2}})(st|nd|rd|th)", cleaned)
    if m:
        day = int(m.group(1))
        cands = []
        probe = ref_date.replace(day=1)
        for _ in range(3):
            if day <= calendar.monthrange(probe.year, probe.month)[1]:
                cand = probe.replace(day=day)
                if cand >= ref_date and (weekday_hint is None or cand.weekday() == weekday_hint):
                    cands.append(cand)
                    break
            probe = probe + relativedelta(months=1)
        if not cands:
            return DateInterpretation(error=f"Could not determine the month for '{norm}'.")
        d = cands[0]
        return DateInterpretation(
            date=d,
            source="inferred",
            notes=[f"No month was given for '{norm}'; assumed {_fmt(d)}."],
        )

    return DateInterpretation(error=f"Could not parse date expression: '{date_text}'")


def _check_weekday(interp: DateInterpretation, weekday_hint: Optional[int]) -> DateInterpretation:
    if weekday_hint is None or interp.date is None:
        return interp
    if interp.date.weekday() != weekday_hint:
        interp.ambiguities.append(
            f"The text says {WEEKDAY_NAMES[weekday_hint]}, but {interp.date.isoformat()} is a "
            f"{WEEKDAY_NAMES[interp.date.weekday()]}. Please check the date."
        )
    return interp


def parse_date(
    date_text: Optional[str],
    ref_date: date,
) -> Tuple[Optional[date], Optional[str]]:
    """
    Backward-compatible wrapper. Returns (parsed_date, error_message).
    Ambiguous expressions return (None, <explanation>).
    """
    interp = interpret_date(date_text, ref_date)
    if interp.error:
        return None, interp.error
    if interp.ambiguous:
        return None, interp.ambiguities[0]
    return interp.date, None


# ---------------------------------------------------------------------------
# Combined resolution
# ---------------------------------------------------------------------------

def get_timezone(timezone_str: Optional[str]) -> Tuple[ZoneInfo, str]:
    try:
        return ZoneInfo(timezone_str or DEFAULT_TIMEZONE), timezone_str or DEFAULT_TIMEZONE
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE


def resolve_date_time(
    date_text: Optional[str],
    time_text: Optional[str],
    reference_datetime: Optional[datetime] = None,
    timezone_str: str = DEFAULT_TIMEZONE,
    item_type: Optional[str] = None,
    untimed_deadline: str = "all_day",
) -> Dict[str, Any]:
    """
    Deterministically resolve natural-language date and time expressions into
    a timezone-aware concrete date/datetime.

    Args:
        date_text: Raw date expression quoted from the text ("Friday", "28/09").
        time_text: Raw time expression quoted from the text ("6 PM", "10-11 am").
        reference_datetime: When the text was read/received. Defaults to now.
        timezone_str: The user's IANA timezone. Invalid values fall back to Asia/Kolkata.
        item_type: "DEADLINE", "EVENT" or "TASK"; only used for untimed deadlines.
        untimed_deadline: "all_day" (default) keeps a date-only deadline as an
            all-day item; "end_of_day" uses 23:59 and marks the time inferred.

    Returns a dict with:
        status: RESOLVED | AMBIGUOUS | UNRESOLVED | NO_DATE
        date, time, end_time, datetime_iso, end_datetime_iso, timezone, all_day,
        is_time_inferred, date_source, time_source, is_past, candidates,
        suggested_candidate, ambiguities, notes, error
    """
    tz, timezone_str = get_timezone(timezone_str)

    if reference_datetime is None:
        ref_dt = datetime.now(tz)
    elif reference_datetime.tzinfo is None:
        ref_dt = reference_datetime.replace(tzinfo=tz)
    else:
        ref_dt = reference_datetime.astimezone(tz)
    ref_date = ref_dt.date()

    out: Dict[str, Any] = {
        "status": STATUS_NO_DATE,
        "datetime_iso": None,
        "end_datetime_iso": None,
        "date": None,
        "time": None,
        "end_time": None,
        "timezone": timezone_str,
        "all_day": False,
        "is_time_inferred": False,
        "date_source": None,
        "time_source": None,
        "is_past": False,
        "candidates": [],
        "suggested_candidate": None,
        "date_question": None,
        "ambiguities": [],
        "notes": [],
        "error": None,
    }

    norm_date = normalize_text(date_text)
    norm_time = normalize_text(time_text)
    if norm_date:
        norm_date, norm_time = extract_embedded_time(norm_date, norm_time)
        norm_date = normalize_text(norm_date)

    # ---- time first (it is independent of the date) ----
    t_interp = interpret_time(norm_time)
    if t_interp.error:
        out["status"] = STATUS_UNRESOLVED
        out["error"] = t_interp.error
        out["ambiguities"].append(f"Could not understand the time '{norm_time}'.")
    out["ambiguities"].extend(t_interp.ambiguities)
    out["notes"].extend(t_interp.notes)

    if not norm_date:
        if out["status"] != STATUS_UNRESOLVED:
            out["status"] = STATUS_NO_DATE
        if t_interp.time:
            out["time"] = t_interp.time.strftime("%H:%M")
            out["time_source"] = "inferred" if t_interp.inferred else "explicit"
            out["ambiguities"].append("A time was given but no date.")
        return out

    d_interp = interpret_date(norm_date, ref_date, month_first=timezone_str.startswith("America/"))
    out["ambiguities"].extend(d_interp.ambiguities)
    out["notes"].extend(d_interp.notes)
    out["date_source"] = d_interp.source

    if d_interp.error:
        out["status"] = STATUS_UNRESOLVED
        out["error"] = d_interp.error
        out["date_source"] = None
        return out

    if out["status"] == STATUS_UNRESOLVED:
        # Date ok but the time could not be read.
        out["date_source"] = d_interp.source
        if d_interp.date:
            out["date"] = d_interp.date.isoformat()
        out["candidates"] = d_interp.candidates
        return out

    # ---- time defaults ----
    time_val, end_val = t_interp.time, t_interp.end_time
    inferred = t_interp.inferred
    if time_val is not None:
        out["time_source"] = "inferred" if inferred else "explicit"
    elif (item_type or "").upper() == "DEADLINE" and untimed_deadline == "end_of_day":
        time_val, inferred = time(23, 59), True
        out["time_source"] = "inferred"
        out["notes"].append("No time given; using 23:59 (end of day) per settings.")

    if d_interp.ambiguous:
        out["status"] = STATUS_AMBIGUOUS
        out["candidates"] = d_interp.candidates
        out["date_question"] = d_interp.ambiguities[0] if d_interp.ambiguities else None
        out["suggested_candidate"] = d_interp.suggested
        if d_interp.kind == "same_weekday" and time_val is not None:
            # "Thursday 4 pm" said on a Thursday morning: today, if 4 pm is still ahead.
            out["suggested_candidate"] = 0 if time_val > ref_dt.time().replace(tzinfo=None) else 1
        if time_val:
            out["time"] = time_val.strftime("%H:%M")
            out["end_time"] = end_val.strftime("%H:%M") if end_val else None
            out["is_time_inferred"] = inferred
        else:
            out["all_day"] = True
        # Past-ness of each candidate is visible from its label; nothing chosen yet.
        return out

    parsed_date = d_interp.date
    out["status"] = STATUS_RESOLVED
    out["date"] = parsed_date.isoformat()

    if time_val is None:
        out["all_day"] = True
        out["is_past"] = parsed_date < ref_date
    else:
        source_tz = t_interp.tz or tz
        start = datetime.combine(parsed_date, time_val, tzinfo=source_tz)
        end = None
        if end_val is not None:
            end = datetime.combine(parsed_date, end_val, tzinfo=source_tz)
            if end <= start:
                end += timedelta(days=1)
        if t_interp.tz is not None:
            local_start = start.astimezone(tz)
            if local_start.utcoffset() != start.utcoffset():
                out["notes"].append(
                    f"Converted from {start.strftime('%H:%M')} {t_interp.tz_label} on "
                    f"{start.date().isoformat()} to {timezone_str}."
                )
                if local_start.date() != parsed_date:
                    out["ambiguities"].append(
                        f"Converting from {t_interp.tz_label} moves this to {_fmt(local_start.date())} in {timezone_str}."
                    )
        start = start.astimezone(tz)
        end = end.astimezone(tz) if end else None
        out["date"] = start.date().isoformat()
        out["time"] = start.strftime("%H:%M")
        out["datetime_iso"] = start.isoformat()
        if end:
            out["end_time"] = end.strftime("%H:%M")
            out["end_datetime_iso"] = end.isoformat()
        out["is_time_inferred"] = inferred
        out["is_past"] = start < ref_dt

    if out["is_past"]:
        out["ambiguities"].append("This date/time is already in the past. Please check it.")
    return out


if __name__ == "__main__":
    ref = datetime(2026, 9, 24, 21, 42, tzinfo=ZoneInfo("Asia/Kolkata"))
    examples = [
        ("Friday", "6 PM", "DEADLINE"),
        ("next Monday", "10 AM", "EVENT"),
        ("this Friday", "5 PM", "DEADLINE"),
        (None, None, "TASK"),
        ("tomorrow", "noon", "EVENT"),
        ("Monday, September 28", "5 PM", "DEADLINE"),
        ("September 30", None, "DEADLINE"),
        ("in three days", None, "DEADLINE"),
        ("05/10", "11:59 pm", "DEADLINE"),
        ("Friday", "9 AM EST", "EVENT"),
        ("invalid date string", "5 PM", "DEADLINE"),
    ]
    print("Reference:", ref.isoformat())
    for d, t, itype in examples:
        res = resolve_date_time(d, t, reference_datetime=ref, item_type=itype)
        print(f"{d!r} + {t!r} ({itype}) -> {res['status']} date={res['date']} time={res['time']} "
              f"all_day={res['all_day']} cands={[c['date'] for c in res['candidates']]} "
              f"amb={res['ambiguities']} err={res['error']}")
