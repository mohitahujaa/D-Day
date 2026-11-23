"""
Deterministic reading of a message, used alongside the LLM:

* find_phrases          every date/time expression in the text (with spans)
* find_message_timestamp when the message was sent/posted (Gmail, Classroom, Outlook headers)
* find_revision          "postponed to ...", "extended from X to Y", "moved to ..."
* find_relation          "before the viva", "after the meeting"
* find_location          meeting links and rooms/halls/labs

These give the small model hints and let the pipeline correct its quotes,
so intent ("the NEW date", "relative to when it was sent") comes out right
without asking the user.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple

from dateutil import parser as dateutil_parser

from date_resolver import MONTHS, WEEKDAYS, NUMBER_WORDS

_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_NUM = r"\d+|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)"
_MERID = r"(?:a\.?m\.?|p\.?m\.?)"
_TZ = r"(?:\s*(?:ist|utc|gmt|est|edt|pst|pdt|cst|cdt|bst|cet|jst|sgt)\b)?"

_DATE_PATTERNS = [
    r"(?:the\s+)?day\s+after\s+tomorrow",
    r"today|tonight|tomorrow|tmrw|yesterday",
    r"parso|parson|aaj|kal",
    r"(?:agle\s+)?(?:somvar|somwar|mangalvar|mangalwar|budhvar|budhwar|guruvar|guruwar|shukravar|shukrawar|shanivar|shaniwar|ravivar|raviwar)",
    rf"(?:(?:this\s+coming|this|next|coming|upcoming|following|last)\s+)?(?:{_WD})\b\.?(?:\s*,?\s*(?:of\s+)?(?:next|this)\s+week)?",
    rf"(?:next|this)\s+week(?:'s)?\s+(?:{_WD})\b",
    rf"\d{{1,2}}{_ORD}?\s+(?:of\s+)?(?:{_MON})[a-z]*\.?(?:,?\s+\d{{4}})?",
    rf"(?:{_MON})[a-z]*\.?\s+\d{{1,2}}{_ORD}?\b(?:,?\s+\d{{4}})?",
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}-\d{1,2}-\d{2,4}|\d{1,2}\.\d{1,2}\.\d{2,4}",
    rf"(?:in|within)\s+(?:the\s+next\s+)?(?:{_NUM})\s+(?:days?|weeks?|months?)",
    rf"(?:{_NUM})\s+(?:days?|weeks?)\s+from\s+(?:now|today)",
    r"(?:the\s+)?end\s+of\s+(?:the\s+)?(?:this\s+|next\s+)?month",
    rf"(?:the\s+)?\d{{1,2}}{_ORD}\b",
]
_TIME_ONE = rf"(?:\d{{1,2}}(?::\d{{2}})?\s*{_MERID}|\d{{1,2}}:\d{{2}}|noon|midnight|eod|end\s+of\s+(?:the\s+)?day)"
_TIME_PATTERNS = [
    rf"(?:from\s+)?{_TIME_ONE}\s*(?:-|–|to|till|until)\s*{_TIME_ONE}{_TZ}",
    rf"\d{{1,2}}\s*(?:-|–|to)\s*\d{{1,2}}\s*{_MERID}{_TZ}",
    rf"{_TIME_ONE}{_TZ}",
    r"(?:(?:subah|savere|dopahar|shaam|sham|raat)\s+)?\d{1,2}(?::\d{2})?\s*baje(?:\s+(?:subah|savere|dopahar|shaam|sham|raat))?",
    # bare hour: "at 6", "before 1", "by 5" (not "by 2 students", "until 3 days")
    r"(?:(?<=\bat\s)|(?<=\bby\s)|(?<=\bbefore\s)|(?<=\buntil\s)|(?<=\btill\s))\d{1,2}\b(?![:/\d]|\.\d)"
    r"(?!\s*(?:%|x\b|st\b|nd\b|rd\b|th\b|students?|people|persons?|members?|days?|weeks?|months?|hours?|hrs?|"
    r"minutes?|mins?|pm\b|am\b|of\b|marks?|points?|pages?|words?|questions?|problems?))",
]
_DATE_RE = re.compile(r"\b(?:" + "|".join(_DATE_PATTERNS) + r")", re.IGNORECASE)
_TIME_RE = re.compile(r"(?:" + "|".join(_TIME_PATTERNS) + r")(?![\w])", re.IGNORECASE)


@dataclass
class Phrase:
    date: Optional[str]
    time: Optional[str]
    start: int
    end: int
    sentence: int

    @property
    def text(self) -> str:
        return " ".join(p for p in (self.date, self.time) if p)


_ABBREV_END = re.compile(r"(?:^|\s)(?:dr|mr|mrs|ms|prof|sr|jr|st|no|vs|etc|e\.g|i\.e|approx|dept|govt|sec|rd|ave)\.$", re.IGNORECASE)


def sentence_spans(text: str) -> List[Tuple[int, int]]:
    spans, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])|\n+|;\s+|\s+\d\)\s+", text):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    # Re-join splits after abbreviations ("Dr. Rao", "Prof. Sharma", "No. 5").
    merged = []
    for a, b in spans:
        if merged and _ABBREV_END.search(text[merged[-1][0]:merged[-1][1]]) and text[merged[-1][1]:a].strip() == "":
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged or [(0, len(text))]


def _sentence_of(pos: int, spans) -> int:
    for i, (a, b) in enumerate(spans):
        if a <= pos < b or (pos == b and i == len(spans) - 1):
            return i
    return len(spans) - 1


def _overlaps(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _raw_matches(regex, text, exclude):
    out = []
    for m in regex.finditer(text):
        span = (m.start(), m.end())
        s = m.group(0).strip(" ,.")
        if not s or any(_overlaps(span, e) for e in exclude):
            continue
        out.append((span[0], span[0] + len(m.group(0).rstrip(" ,.")), s))
    return out


def find_phrases(text: str, exclude: Optional[List[Tuple[int, int]]] = None) -> List[Phrase]:
    """All date/time expressions, with a time attached to the date it belongs to."""
    exclude = list(exclude or [])
    spans = sentence_spans(text)
    times = _raw_matches(_TIME_RE, text, exclude)
    time_spans = [(a, b) for a, b, _ in times]
    dates = []
    for a, b, s in _raw_matches(_DATE_RE, text, exclude):
        if any(_overlaps((a, b), t) for t in time_spans):
            continue  # e.g. "11" inside "11:59"
        # drop ordinal-only/number-only matches that are part of other words like "5th semester"
        if re.fullmatch(r"(?:the\s+)?\d{1,2}(?:st|nd|rd|th)", s, re.I) and re.match(r"\s+(?:semester|sem|year|floor|edition|unit|chapter|module|lab|class|standard|period|round|attempt|place|position|lecture|hour|slot|week)", text[b:], re.I):
            continue
        dates.append((a, b, s))

    # Merge adjacent date pieces: "Tuesday, 24 Nov" -> one phrase.
    merged = []
    for a, b, s in dates:
        if merged and re.fullmatch(r"[\s,]*(?:the\s+)?", text[merged[-1][1]:a], re.I) and a - merged[-1][1] <= 3:
            pa, _, _ = merged[-1]
            merged[-1] = (pa, b, text[pa:b])
        else:
            merged.append((a, b, s))

    # Attach each time to its nearest date in the same clause (ties: the preceding date).
    owner = {}
    for i, (ta, tb, ts) in enumerate(times):
        sent = _sentence_of(ta, spans)
        best, best_gap = None, 999
        for j, (a, b, s) in enumerate(merged):
            if _sentence_of(a, spans) != sent:
                continue
            gap = ta - b if ta >= b else a - tb
            between = text[b:ta] if ta >= b else text[tb:a]
            if gap > 25 or re.search(r"\b(and|but|also|while|whereas|from)\b|[.;]", between, re.I):
                continue
            score = gap + (0 if ta >= b else 0.5)
            if score < best_gap and j not in owner.values():
                best, best_gap = j, score
        if best is not None:
            owner[i] = best
    by_date = {j: i for i, j in owner.items()}

    phrases: List[Phrase] = []
    for j, (a, b, s) in enumerate(merged):
        tm, start, end = None, a, b
        if j in by_date:
            ta, tb, tm = times[by_date[j]]
            start, end = min(a, ta), max(b, tb)
        phrases.append(Phrase(date=s, time=tm, start=start, end=end, sentence=_sentence_of(a, spans)))
    for i, (ta, tb, ts) in enumerate(times):
        if i not in owner:
            phrases.append(Phrase(date=None, time=ts, start=ta, end=tb, sentence=_sentence_of(ta, spans)))
    phrases.sort(key=lambda p: p.start)
    return phrases


# ---------------------------------------------------------------------------
# Message timestamp ("sent 3 days ago")
# ---------------------------------------------------------------------------

_STAMP_PATTERNS = [
    # Gmail: "Nov 16, 2026, 9:12 AM (3 days ago)" / "Mon, Nov 16, 9:12 AM (3 days ago)"
    re.compile(rf"((?:(?:{_WD}),?\s+)?(?:{_MON})[a-z]*\.?\s+\d{{1,2}}(?:,\s*\d{{4}})?,?\s+\d{{1,2}}:\d{{2}}\s*{_MERID})\s*\(\s*(?:\d+|an?)\s+(?:minutes?|hours?|days?|weeks?)\s+ago\s*\)", re.I),
    # Headers: "Sent: Monday, November 16, 2026 9:12 AM", "Date: 16/11/2026", "Posted Nov 16", "Created 16 Nov"
    re.compile(r"^\s*(?:sent|date|received|posted(?:\s+on)?|created|published)\s*:?\s+([^\n]{4,60}?)\s*(?:\(edited[^)]*\))?\s*$", re.I | re.M),
]


def find_message_timestamp(text: str, now: datetime) -> Optional[Tuple[datetime, Tuple[int, int]]]:
    """Returns (sent_datetime, span) if the selection includes when the message was sent."""
    for pat in _STAMP_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1)
        if not re.search(rf"(?:{_MON})|\d{{1,2}}[/-]\d{{1,2}}", raw, re.I):
            continue
        try:
            default = datetime(now.year, now.month, now.day, 12, 0)
            dt = dateutil_parser.parse(raw, default=default, fuzzy=True, dayfirst=True)
        except (ValueError, OverflowError):
            continue
        dt = dt.replace(tzinfo=now.tzinfo) if dt.tzinfo is None else dt.astimezone(now.tzinfo)
        has_year = bool(re.search(r"\b(19|20)\d\d\b", raw))
        if not has_year and dt > now + timedelta(hours=1):
            dt = dt.replace(year=dt.year - 1)
        if now - timedelta(days=180) <= dt <= now + timedelta(hours=1):
            return dt, (m.start(), m.end())
    return None


# ---------------------------------------------------------------------------
# Revisions, relations, locations
# ---------------------------------------------------------------------------

_REVISION_RE = re.compile(
    r"\b(postponed|rescheduled|re-scheduled|moved|shifted|extended|preponed|changed|pushed(?:\s+back)?|deferred|revised|now)\b"
    r"(?:[^.;\n]*?)\b(to|till|until|on|for|at)\b",
    re.IGNORECASE,
)


def find_revision(sentence: str) -> Optional[int]:
    """Index in the sentence after which the NEW date appears, if the sentence revises a date."""
    m = _REVISION_RE.search(sentence)
    if not m:
        return None
    if m.group(1).lower() == "now" and m.group(2).lower() not in ("on", "at"):
        return None
    return m.end(2)


_RELATION_RE = re.compile(
    r"\b(before|by|after|during|at|prior\s+to|ahead\s+of)\s+(?:the\s+|your\s+|our\s+)([a-z][a-z0-9\- ]{2,40}?)(?=[,.;:!?)]|\s+(?:and|to|on|in|with)\b|$)",
    re.IGNORECASE,
)


def find_relation(text: Optional[str]) -> Optional[Tuple[str, str]]:
    """('before', 'viva') for 'Before the viva, submit...'."""
    if not text:
        return None
    m = _RELATION_RE.search(text)
    if not m:
        return None
    noun = m.group(2).strip().lower()
    if re.search(rf"\b(?:{_WD}|{_MON}|today|tomorrow|deadline|end|day|week|month|time)\b", noun):
        return None
    return re.sub(r"\s+", " ", m.group(1).lower()), noun


_URL_RE = re.compile(
    r"https?://(?:meet\.google\.com|[\w-]+\.zoom\.us|zoom\.us|teams\.microsoft\.com|teams\.live\.com|[\w.-]*webex\.com)/[^\s<>\"')]+",
    re.IGNORECASE,
)
_ROOM_RE = re.compile(
    r"\b((?i:seminar\s+hall|lecture\s+hall|exam\s+hall|conference\s+room|computer\s+lab|room|hall|lab|auditorium|block|lt|classroom|venue)"
    r"(?:\s*(?:no\.?|number|#))?\s*[-:]?\s*(?:[A-Z]{0,3}\d{1,4}[A-Z]?|[A-Z]\b|[IVX]{1,4}\b))",
)


def find_location(sentence: str) -> Optional[str]:
    m = _URL_RE.search(sentence)
    if m:
        return m.group(0).rstrip(".,;")
    m = _ROOM_RE.search(sentence)
    if m:
        loc = m.group(1).strip()
        return loc[0].upper() + loc[1:]
    m = re.search(r"\b(?:venue|location|place)\s*[:\-]\s*([^\n.;]{3,60})", sentence, re.I)
    if m:
        return m.group(1).strip()
    if re.search(r"\b(auditorium|main\s+ground|library|cafeteria)\b", sentence, re.I):
        return re.search(r"\b(auditorium|main\s+ground|library|cafeteria)\b", sentence, re.I).group(1).title()
    return None


_OBLIGATION_RE = re.compile(
    r"\b(submit|submission|due|deadline|last\s+date|closes?|exam|quiz|test|viva|interview|meeting|"
    r"presentation|demo|register|registration|apply|pay|fees?|attend|report|scheduled|held|conducted|"
    r"complete|upload|send|bring|webinar|seminar|workshop|lecture|class(?:es)?\s+(?:will|begin|start))\b",
    re.IGNORECASE,
)


def has_obligation_cue(sentence: str) -> bool:
    return bool(_OBLIGATION_RE.search(sentence))
