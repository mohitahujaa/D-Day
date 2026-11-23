"""
Selection -> reviewed-task pipeline.

    selected text
      -> signals: sent-date, date/time phrases, revisions, relations, locations
      -> extractor: local LLM proposes items (title, type, quoted date/time)
      -> anchoring: each item takes the date phrase from ITS OWN sentence
         (fixes small-model mix-ups and ignores dates in injected instructions)
      -> date_resolver: deterministic dates relative to when the message was sent
      -> intent passes: relations ("before the viva"), duplicates, missed items
      -> smart defaults: most likely date auto-picked (alternatives kept),
         reminders by type, confident items pre-selected

Nothing here talks to Google. The user confirms in the extension.
"""

import inspect
import re
import uuid
from datetime import datetime, date, time
from typing import Any, Callable, Dict, List, Optional, Tuple

import date_resolver
import extractor
import signals

EVENT_TYPES = ("deadline", "exam", "meeting", "assignment", "reminder", "other")

_KEYWORDS = [
    ("exam", r"\b(exam|examination|quiz|test|midterm|mid-sem|end-sem|viva|finals?)\b"),
    ("assignment", r"\b(assignment|homework|worksheet|lab record|report|submission|submit|project)\b"),
    ("meeting", r"\b(meeting|call|interview|review|presentation|demo|seminar|lecture|class|webinar|session|standup|workshop)\b"),
    ("reminder", r"\b(remind|remember|don't forget|do not forget|pay|fee|fees)\b"),
]

# Life areas, highest priority first. Hiring is never allowed to be misfiled.
AREAS = {"job": 1, "work": 2, "college": 3, "personal": 4}
AREA_LABELS = {"job": "Internship / Job", "work": "Work", "college": "College", "personal": "Personal"}
_JOB_RE = re.compile(
    r"\b(intern(?:ship)?s?|placements?|campus\s+(?:drive|hiring|recruitment)|hiring|recruit(?:ment|er|ing)?|"
    r"jobs?|careers?|vacanc(?:y|ies)|offer\s+letter|job\s+offer|stipend|ctc|shortlisted|hr\s+round|"
    r"technical\s+interview|interviews?|aptitude\s+test|assessment\s+(?:test|link|platform)|resume|cv|"
    r"applicants?|candidates?|full[-\s]time|part[-\s]time|position|role)\b",
    re.IGNORECASE,
)
_COLLEGE_RE = re.compile(
    r"\b(assignments?|homework|exams?|quiz|mid-?sem|end-?sem|semester|lab|labs|viva|class(?:es)?|lecture|"
    r"syllabus|attendance|course|college|university|hostel|prof(?:essor)?|faculty|hod|dean|students?|"
    r"re-?evaluation|marks|grades?|tutorial|practical|project\s+review|mini\s+project|synopsis|"
    r"library|club|fest|hackathon|seminar|guest\s+(?:talk|lecture))\b",
    re.IGNORECASE,
)
_WORK_RE = re.compile(
    r"\b(clients?|sprint|standup|stand-up|manager|office|deliverables?|eod|minutes\s+of|stakeholders?|"
    r"jira|deploy(?:ment)?|invoice|timesheet|meeting\s+notes|1:1|one-on-one|all-hands|okrs?|"
    r"team\s+meeting|work\s+report|shift)\b",
    re.IGNORECASE,
)
_PERSONAL_RE = re.compile(
    r"\b(dentist|doctor|clinic|hospital|check-?up|appointment|electricity|water\s+bill|gas\s+bill|"
    r"phone\s+bill|wifi\s+bill|broadband|bill|rent|emi|insurance|bank|birthday|anniversary|wedding|"
    r"family|mom|dad|mother|father|sister|brother|gym|salon|haircut|flight|train|movie|recharge|"
    r"passport|aadhaar|pan\s+card|vaccin\w*|medicine|pharmacy|grocer\w*|dinner|lunch\s+with)\b",
    re.IGNORECASE,
)


_COURSEWORK_RE = re.compile(
    r"\b(assignments?|homework|home\s+work|report|lab\s+(?:file|record|manual)|file|record|project|synopsis|"
    r"submission|submit|worksheet|essay|thesis|dissertation|ppt|presentation|notes|journal|portfolio|code)\b",
    re.IGNORECASE,
)
_STRONG_JOB_RE = re.compile(
    r"\b(internships?|placements?|hiring|recruit(?:ment|er|ing)|campus\s+(?:drive|hiring)|shortlisted|"
    r"stipend|ctc|offer\s+letter|job\s+offer|hr\s+round|selection\s+process)\b",
    re.IGNORECASE,
)


# Companies that commonly run campus hiring in India; a company name next to
# "test"/"register"/"interview" is a hiring signal on its own.
_RECRUITER_RE = re.compile(
    r"\b(infosys|tcs|wipro|accenture|cognizant|capgemini|hcl(?:tech)?|tech\s+mahindra|deloitte|kpmg|pwc|"
    r"ibm|oracle|amazon(?!\s+(?:pay|prime|order|delivery))|"
    r"google(?!\s+(?:meet|calendar|classroom|forms?|drive|docs|sheets|slides|pay|maps|chat|sites))|"
    r"microsoft(?!\s+(?:teams|word|excel|forms|office|365))|meta|flipkart|zomato|swiggy|paytm|razorpay|zoho|freshworks|"
    r"lti(?:mindtree)?|mindtree|mphasis|hexaware|persistent|zs|mu\s+sigma|goldman\s+sachs|jp\s*morgan|"
    r"morgan\s+stanley|barclays|sap|adobe|cisco|intel|nvidia|qualcomm|samsung|byju'?s|unacademy|ola|"
    r"uber|phonepe|cred|meesho|myntra|nykaa|reliance\s+jio|jio|l&t|larsen|bosch|siemens|indian\s+army)\b",
    re.IGNORECASE,
)


def infer_area(item: Dict[str, Any], sentence: Optional[str], text: str) -> str:
    """Internship/Job > Work > College > Personal. Hiring signals in the item's own
    sentence always win; message-wide ones apply only to items with no other signal."""
    own = " ".join(filter(None, [item.get("action"), item.get("details"), sentence]))
    # Links and email addresses aren't signals ("meet.google.com", "hr@infosys.com" is fine to ignore).
    own = re.sub(r"https?://\S+|\S+@\S+", " ", own)
    text = re.sub(r"https?://\S+|\S+@\S+", " ", text)
    if _JOB_RE.search(own) or _RECRUITER_RE.search(own):
        return "job"
    own_other = _COLLEGE_RE.search(own) or _WORK_RE.search(own) or _PERSONAL_RE.search(own)
    if not own_other and (_STRONG_JOB_RE.search(text) or len({m.lower() for m in _JOB_RE.findall(text)}) >= 2):
        return "job"
    if _WORK_RE.search(own):
        return "work"
    if _PERSONAL_RE.search(own) and not _COLLEGE_RE.search(own):
        return "personal"
    model = item.get("area")
    if model in AREAS:
        return model
    if _COLLEGE_RE.search(own):
        return "college"
    return "personal"


# Reminder minutes by type. All-day reminders count back from midnight
# (900 = 9:00 the day before; 1380 = 1:00 PM... two days = 2340 = 9:00 two days before).
REMINDERS_TIMED = {
    "exam": [1440, 120], "assignment": [1440, 120], "deadline": [1440, 120],
    "meeting": [30], "reminder": [15], "other": [60],
}
# Internship/Job is the highest priority: 2 days, 1 day and 2 hours before
# (all-day: 9:00 three days, two days and one day before).
REMINDERS_JOB = {"timed": [2880, 1440, 120], "all_day": [3780, 2340, 900]}
REMINDERS_ALL_DAY = {
    "exam": [2340, 900], "assignment": [2340, 900], "deadline": [2340, 900],
    "meeting": [900], "reminder": [900], "other": [900],
}

_INJECTION_RE = re.compile(
    r"\b(ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|rules|prompts?)|"
    r"disregard\s+(?:all|any|the|previous)|system\s*:|assistant\s*:|you\s+are\s+now|new\s+instructions?|"
    r"output\s+(?:an?\s+)?(?:item|json))",
    re.IGNORECASE,
)
_PAST_INFO_RE = re.compile(
    r"\b(was|were|has been delivered|was delivered|delivered|happened|took place|yesterday|last (?:week|month)|"
    r"insightful|thank(?:s| you) for (?:attending|shopping|your order))\b",
    re.IGNORECASE,
)
_SUBJECT_RE = re.compile(
    r"^(?:all\s+|eligible\s+|interested\s+|the\s+|registered\s+)*(?:students|candidates|participants|"
    r"everyone|you|we|members|applicants)\s+(?:must|should|need\s+to|have\s+to|are\s+required\s+to|"
    r"are\s+requested\s+to|will\s+need\s+to|can)\s+",
    re.IGNORECASE,
)
_MUST_RE = re.compile(
    r"\b(must|required|mandatory|need to|have to|last date|deadline|due|compulsory|make sure|ensure)\b",
    re.IGNORECASE,
)
_PROMO_RE = re.compile(
    r"(\b(?:flash\s+sale|sale|discount|coupon|promo(?:\s+code)?|use\s+code|unsubscribe|shop\s+now|buy\s+now|"
    r"limited[-\s]time|offer\s+ends|cashback|deal)\b|\d+\s*%\s*off\b)",
    re.IGNORECASE,
)
_STOP = {"the", "a", "an", "to", "and", "of", "for", "on", "in", "at", "by", "your", "my", "our", "with",
         "is", "are", "be", "will", "please", "all", "this", "that", "from", "before", "after", "due"}
_PAST_MSG = "This date/time is already in the past. Please check it."


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"[^\w:/'+-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> List[str]:
    return re.findall(r"[a-z]+|\d+", _norm(s or ""))


def _stems(s: str) -> set:
    return {t[:5] for t in _tokens(s) if t not in _STOP and len(t) > 2 and not t.isdigit()}


def quote_supported(quote: Optional[str], source: str) -> bool:
    """True if every word/number of the quoted phrase occurs in the source text."""
    if not quote:
        return True
    if _norm(quote) in _norm(source):
        return True
    src = set(_tokens(source))
    # "6pm" tokenises to "6","pm" while "6 p.m." gives "p","m": accept either.
    if {"a", "m"} <= src:
        src.add("am")
    if {"p", "m"} <= src:
        src.add("pm")
    toks = _tokens(quote)
    filler = {"at", "by", "on", "the", "of", "before", "until", "due"}
    return bool(toks) and all(t in src or t in filler for t in toks)


_MODIFIERS = r"(?:this\s+coming|next|this|coming|upcoming|following|last)"


def expand_date_quote(date_text: Optional[str], excerpt: Optional[str], text: str) -> Optional[str]:
    """Restore a modifier the model dropped ("next Monday" -> "Monday")."""
    if not date_text:
        return date_text
    if re.match(rf"^\s*{_MODIFIERS}\b", date_text, re.IGNORECASE) or re.search(r"\bweek\b", date_text, re.IGNORECASE):
        return date_text
    quoted = re.escape(date_text.strip().rstrip(".,"))
    for haystack in filter(None, (excerpt, text)):
        found = [((m.group(1) or "").strip(), (m.group(2) or "").strip()) for m in re.finditer(
            rf"(?:\b({_MODIFIERS})\s+)?{quoted}\b(\s+(?:of\s+)?(?:next|this)\s+week)?", haystack, re.IGNORECASE)]
        if found:
            if len(set(found)) == 1:
                pre, post = found[0]
                return " ".join(p for p in (pre, date_text.strip(), post) if p)
            return date_text
    return date_text


def infer_event_type(item: Dict[str, Any]) -> str:
    if item.get("category") in EVENT_TYPES and item["category"] != "other":
        return item["category"]
    haystack = " ".join(filter(None, [item.get("action"), item.get("source_text")]))
    for etype, pattern in _KEYWORDS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return etype
    return {"DEADLINE": "deadline", "EVENT": "other", "TASK": "reminder"}.get(item.get("type"), "other")


_GENERIC_WORDS = {
    "apply", "application", "applications", "for", "the", "a", "an", "position", "job", "role", "post",
    "opening", "vacancy", "submit", "submission", "assignment", "task", "deadline", "form", "register",
    "registration", "event", "meeting", "exam", "test", "attend", "complete", "pay", "fee", "fees",
    "payment", "last", "date", "due", "to", "of", "this", "internship", "interview", "round",
}


def _smart_case(s: str) -> str:
    """'FULL STACK DEVELOPER (ERP)' -> 'Full Stack Developer (ERP)'; leaves mixed case alone."""
    if s.upper() != s:
        return s
    return " ".join(w if len(w) <= 3 and w.isalpha() and w not in ("FOR", "AND", "THE", "OF", "AT", "IN", "ON", "TO")
                    else w.capitalize() if w.isalpha() else w for w in s.split()).replace(" For ", " for ")


def _heading(text: str):
    """(heading, organisation) of a listing/notice: first line if it looks like a
    title (job, subject), plus the next short mixed-case line (e.g. 'Indian Army')."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None, None
    first = lines[0]
    if len(first) > 100 or re.search(r"[.!?]$|\d{1,2}[:/.-]\d", first) or len(first.split()) < 2:
        return None, None
    org = None
    if len(lines) > 1:
        nxt = lines[1]
        if len(nxt) <= 40 and nxt.upper() != nxt and not re.search(r"\d", nxt) and not nxt.endswith((".", ":")):
            org = nxt
    return first, org


def _shorten(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def enrich_title(title: str, item: Dict[str, Any], text: str) -> str:
    """Make a generic title ("Apply for position") specific using the details or the heading."""
    words = _tokens(title)
    if words and not all(w in _GENERIC_WORDS for w in words):
        return title
    heading, org = _heading(text)
    for specific in (item.get("details"), heading):
        if not specific:
            continue
        specific = _smart_case(re.sub(r"\s+", " ", specific).strip(" .:-"))
        if not specific or _stems(specific) <= _stems(title):
            continue
        if org and org.lower() not in specific.lower():
            specific = f"{_shorten(specific, 55)} – {org}"
        else:
            specific = _shorten(specific, 70)
        verb = title.split()[0] if title else ""
        if verb.lower() in ("apply", "register", "submit", "pay", "attend", "complete"):
            joiner = " for " if verb.lower() in ("apply", "register") else " "
            return f"{verb.capitalize()}{joiner}{specific}"
        return f"{title}: {specific}" if title else specific
    return title


# Common course codes (Indian engineering/CS curricula). Two-letter codes only count
# when written in capitals in the message, to avoid words like "is", "co", "de".
KNOWN_SUBJECTS = {
    "dbms", "dwdm", "dsa", "daa", "toc", "coa", "oops", "nlp", "iot", "ccn", "bda", "hci", "mpmc", "ppl",
    "pps", "fsd", "dld", "dsp", "vlsi", "beee", "egd", "evs", "java", "python", "maths", "math", "physics",
    "chemistry", "dbs", "ics", "cyber", "cns", "spm", "stqa", "mad", "awt", "wt", "os", "cn", "ds", "ai",
    "ml", "dl", "se", "cd", "cg", "cc", "m1", "m2", "m3", "m4",
}
_NOT_SUBJECTS = {"AM", "PM", "IST", "UTC", "GMT", "OK", "PDF", "URL", "ID", "NO", "EOD", "ASAP", "FYI", "HR",
                 "CTC", "PS", "RSVP", "TBD", "TBA", "DM", "BTW", "LOL", "OTP", "UPI", "INR", "RS", "USA", "SMS"}
_SHORT_FORMS = {"ext": "external", "int": "internal", "prac": "practical", "pract": "practical",
                "assgn": "assignment", "assign": "assignment", "asgn": "assignment", "sub": "submission",
                "presi": "presentation", "ppt": "presentation"}
_TASK_VERB_RE = re.compile(r"^(submit|attend|complete|prepare|send|upload|pay|apply|register|finish|write|"
                           r"study|revise|bring|book|join|give|take|appear|fill|collect|make|do|read|review)\b", re.I)


_SAFE_SHORT_CODES = {"os", "cn", "ds", "ml", "dl", "cd", "cg", "wt", "m1", "m2", "m3", "m4"}
_CAPS_WORDS = {"HI", "HEY", "ALL", "DEAR", "NOTE", "NEW", "NOW", "FREE", "SALE", "OFF", "YES", "AND", "THE",
               "FOR", "YOU", "ARE", "NOT", "BUT", "ANY", "OUR", "WILL", "MUST", "ONLY", "LAST", "DATE", "TIME",
               "DAY", "JOB", "TYPE", "DUE", "ASAP", "INFO", "HELP", "TEAM", "SIR", "MAAM", "BRO", "PLS", "PLZ",
               "LINK", "MEET", "CALL", "TEST", "EXAM", "QUIZ", "LAB", "ROOM", "HALL"}


def _subject_codes(sentence: str) -> List[str]:
    """Course codes in the sentence: known codes (any case, 2-letter ones only in capitals)
    or other ALL-CAPS 2-4 letter codes that aren't words or labels ("NOTE:")."""
    found = []
    letters = [c for c in (sentence or "") if c.isalpha()]
    shouting = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.6
    for m in re.finditer(r"\b[A-Za-z][A-Za-z0-9+]{1,5}\b", sentence or ""):
        w = m.group(0)
        up, low = w.upper(), w.lower()
        if up in _NOT_SUBJECTS or sentence[m.end():m.end() + 1] == ":":
            continue
        known = low in KNOWN_SUBJECTS and (len(low) >= 3 or w.isupper() or low in _SAFE_SHORT_CODES)
        caps_code = (not shouting and w.isupper() and w.isalpha() and 2 <= len(w) <= 4
                     and up not in _CAPS_WORDS)
        if (known or caps_code) and up not in found:
            found.append(up)
    return found


def fix_internal_external(title: str, source: str) -> str:
    """The model sometimes flips 'ext' to 'internal'. Trust the text."""
    says_ext = re.search(r"\b(ext|external)\b", source or "", re.I)
    says_int = re.search(r"\b(int|internal)\b", source or "", re.I)
    if says_ext and not says_int:
        return re.sub(r"\binternal\b", "external", re.sub(r"\bInternal\b", "External", title))
    if says_int and not says_ext:
        return re.sub(r"\bexternal\b", "internal", re.sub(r"\bExternal\b", "Internal", title))
    return title


def add_subject(title: str, sentence: Optional[str], event_type: str, item_type: Optional[str]) -> str:
    """'Ext practical' + 'bhai ... dwdm ka' -> 'DWDM external practical';
    'Dbms assignment' -> 'Submit DBMS assignment'."""
    if not title:
        return title
    sentence = sentence or ""
    # Use only the clause this item comes from ("ext viva h os ka, aur cn ka assignment").
    clauses = [c for c in re.split(r",|;|\baur\b|\band\b|\balso\b|\bplus\b|\bthen\b|\bphir\b", sentence, flags=re.I) if c.strip()]
    if len(clauses) > 1:
        tstems = {(_SHORT_FORMS.get(w, w))[:5] for w in _tokens(title)}
        clauses.sort(key=lambda c: -len(tstems & {(_SHORT_FORMS.get(w, w))[:5] for w in _tokens(c)}))
        sentence = clauses[0]
    codes = _subject_codes(sentence)
    fixed = []
    for w in title.split():
        core = w.strip(".,:;")
        low = core.lower()
        if low in _SHORT_FORMS:
            w = w.replace(core, _SHORT_FORMS[low])
        elif core.upper() in codes or (low in KNOWN_SUBJECTS and len(low) >= 3):
            w = w.replace(core, core.upper())
        fixed.append(w)
    title = " ".join(fixed)
    have = {w.strip(".,:;").upper() for w in title.split()}
    already_has_code = bool(have & (set(codes) | {c.upper() for c in KNOWN_SUBJECTS}))
    missing = [] if already_has_code else [c for c in codes if c not in have]
    if missing:
        m = _TASK_VERB_RE.match(title)
        if m:
            title = f"{title[:m.end()]} {missing[0]}{title[m.end():]}"
        else:
            first = title.split()[0]
            title = f"{missing[0]} {title if first.isupper() else title[:1].lower() + title[1:]}"
    if event_type == "assignment" and item_type in (None, "DEADLINE", "TASK") and not _TASK_VERB_RE.match(title):
        first = title.split()[0]
        title = f"Submit {title if first.isupper() else title[:1].lower() + title[1:]}"
    return title[:1].upper() + title[1:]


def clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip(" .,:;-")
    t = re.sub(r"^(please|kindly|you (?:need|have) to|you must|remember to|don't forget to)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:by|on|before|at|until|till|due)\s*$", "", t, flags=re.I)
    words = t.split()
    if len(words) > 16:
        t = " ".join(words[:16])
    if len(t) > 100:
        t = t[:100].rsplit(" ", 1)[0] + "…"
    return (t[:1].upper() + t[1:]) if t else t


# ---------------------------------------------------------------------------
# Message analysis
# ---------------------------------------------------------------------------

class Analysis:
    def __init__(self, text: str, now: datetime):
        self.text = text
        self.now = now
        stamp = signals.find_message_timestamp(text, now)
        self.sent_at: Optional[datetime] = stamp[0] if stamp else None
        exclude = [stamp[1]] if stamp else []
        self.stamp_span = stamp[1] if stamp else None
        self.reference = self.sent_at or now
        self.spans = signals.sentence_spans(text)
        self.sentences = [text[a:b] for a, b in self.spans]
        self.untrusted = {i for i, s in enumerate(self.sentences) if _INJECTION_RE.search(s)}
        self.phrases = [p for p in signals.find_phrases(text, exclude) if p.sentence not in self.untrusted]
        self.revision_at = {}
        for i, s in enumerate(self.sentences):
            pos = signals.find_revision(s)
            if pos is not None:
                self.revision_at[i] = self.spans[i][0] + pos

    def phrases_in(self, sent: int) -> List[signals.Phrase]:
        ps = [p for p in self.phrases if p.sentence == sent]
        if sent in self.revision_at:
            after = [p for p in ps if p.start >= self.revision_at[sent]]
            if after:
                return after  # the NEW date wins; old one is ignored
        return ps

    def sentence_for(self, item: Dict[str, Any]) -> Optional[int]:
        """Sentence the item is about: best overlap with its title/details."""
        want = _stems(item.get("action", "")) | (_stems(item.get("details") or "") if not item.get("action") else set())
        best, best_score = None, 0
        for i, s in enumerate(self.sentences):
            score = len(want & _stems(s))
            if score > best_score:
                best, best_score = i, score
        if best is None and item.get("date_text"):
            for i, s in enumerate(self.sentences):
                if item["date_text"] and _norm(item["date_text"]) in _norm(s):
                    return i
        return best

    def phrase_matching(self, quote: str, sent: Optional[int]) -> Optional[signals.Phrase]:
        q = _norm(quote)
        pool = (self.phrases_in(sent) if sent is not None else []) + self.phrases
        for p in pool:
            pd = _norm(p.date or "")
            pt = _norm(p.text)
            if q and (q in pt or (pd and pd in q)):
                return p
        return None


# ---------------------------------------------------------------------------
# Building tasks
# ---------------------------------------------------------------------------

def _anchor(item: Dict[str, Any], an: Analysis, notes: List[str], ambiguities: List[str]):
    """Choose the date/time phrase for an item. Returns (date_text, time_text, sentence, relation)."""
    date_text, time_text = item.get("date_text"), item.get("time_text")
    sent = an.sentence_for(item)
    relation = signals.find_relation(date_text) if date_text else None

    if date_text and not relation and not quote_supported(date_text, an.text):
        ambiguities.append(f"The model suggested the date '{date_text}', but it is not in the selected text.")
        date_text = None
    if time_text and not quote_supported(time_text, an.text):
        time_text = None

    own = an.phrases_in(sent) if sent is not None else []
    own_dated = [p for p in own if p.date]
    if not relation and not own_dated and sent is not None:
        # "Before the viva, submit the report": linked to the viva, even if the
        # model copied the viva's date onto it.
        relation = signals.find_relation(an.sentences[sent])

    if relation:
        if own_dated and not signals.find_relation(an.sentences[sent] if sent is not None else ""):
            relation = None  # the sentence has a real date; prefer it
        else:
            return None, time_text, sent, relation

    if sent is not None and sent in an.untrusted:
        ambiguities.append("This part of the text looks like instructions to software, so its dates were not used.")
        return None, None, sent, None

    chosen = an.phrase_matching(date_text, sent) if date_text else None
    if chosen is not None and own_dated and chosen not in own_dated:
        # The model took a date from a different sentence; use the one next to this item.
        notes.append("Used the date written next to this item.")
        chosen = own_dated[0]
    if chosen is None and date_text and own_dated and not quote_supported(date_text, an.sentences[sent]):
        chosen = own_dated[0]
    if chosen is None and not date_text and len(own_dated) == 1:
        chosen = own_dated[0]
    if chosen is None and date_text and not own_dated:
        # Quote exists but not as a detected phrase in a trusted sentence.
        loose = an.phrase_matching(date_text, None)
        if loose is None and any(date_text.lower() in an.sentences[i].lower() for i in an.untrusted):
            ambiguities.append(f"'{date_text}' comes from text that looks like injected instructions; not used.")
            return None, None, sent, None
        chosen = loose

    if chosen is not None:
        item["_date_sentence"] = chosen.sentence
        item["_borrowed"] = sent is None or chosen.sentence != sent
        date_text = chosen.date or date_text
        if chosen.time:
            if not time_text or not quote_supported(time_text, an.sentences[chosen.sentence]):
                time_text = chosen.time
        elif time_text and sent is not None and not quote_supported(time_text, an.sentences[sent]):
            time_text = None
    elif date_text:
        date_text = expand_date_quote(date_text, an.sentences[sent] if sent is not None else None, an.text)
    return date_text, time_text, sent, None


def _confidence(item, res, flags: int) -> float:
    c = 0.92
    c -= {"explicit": 0.0, "potential": 0.25, "unclear": 0.4}.get(item.get("applicability"), 0.4)
    status = res["status"]
    if status == date_resolver.STATUS_AMBIGUOUS:
        c -= 0.12
    elif status == date_resolver.STATUS_UNRESOLVED:
        c -= 0.3
    elif status == date_resolver.STATUS_NO_DATE:
        c -= 0.15
    if res.get("date_source") == "inferred":
        c -= 0.08
    c -= 0.08 * flags
    return round(max(0.05, min(0.99, c)), 2)


def _is_past(due_date: Optional[str], due_time: Optional[str], tz, now: datetime) -> bool:
    if not due_date:
        return False
    d = date.fromisoformat(due_date)
    if not due_time:
        return d < now.date()
    return datetime.combine(d, time.fromisoformat(due_time), tzinfo=tz) < now


def build_task(item: Dict[str, Any], an: Analysis, timezone: str, untimed_deadline: str = "all_day",
               auto_resolve: bool = True) -> Dict[str, Any]:
    notes: List[str] = []
    ambiguities: List[str] = []
    tz, timezone = date_resolver.get_timezone(timezone)

    date_text, time_text, sent, relation = _anchor(item, an, notes, ambiguities)
    excerpt = an.sentences[sent].strip()[:600] if sent is not None else None

    res = date_resolver.resolve_date_time(
        date_text, time_text, reference_datetime=an.reference, timezone_str=timezone,
        item_type=item.get("type"), untimed_deadline=untimed_deadline,
    )
    if res["status"] == date_resolver.STATUS_UNRESOLVED and time_text and res.get("date_source") is not None:
        # The date is fine but the "time" isn't a clock time ("3rd period"):
        # keep the date as an all-day item rather than losing it.
        res = date_resolver.resolve_date_time(
            date_text, None, reference_datetime=an.reference, timezone_str=timezone,
            item_type=item.get("type"), untimed_deadline=untimed_deadline,
        )
        notes.append(f"'{time_text}' isn't a clock time, so this is saved as all-day.")
    notes.extend(res["notes"])
    ambiguities.extend(a for a in res["ambiguities"] if a != _PAST_MSG and a != res.get("date_question"))

    due_date = res["date"] if res["status"] == date_resolver.STATUS_RESOLVED else None
    due_time = res["time"] if res["status"] in (date_resolver.STATUS_RESOLVED, date_resolver.STATUS_AMBIGUOUS) else None
    end_time = res.get("end_time")
    date_status = res["status"]
    chosen = None
    if res["status"] == date_resolver.STATUS_AMBIGUOUS and auto_resolve and res["suggested_candidate"] is not None:
        chosen = res["suggested_candidate"]
        cand = res["candidates"][chosen]
        due_date = cand["date"]
        date_status = "ASSUMED"
        label = cand["label"]
        notes.insert(0, f"Assumed {label[:1].lower() + label[1:]}.")
    elif res["status"] == date_resolver.STATUS_AMBIGUOUS:
        ambiguities.insert(0, res.get("date_question") or "Please choose the date.")

    if res["status"] == date_resolver.STATUS_NO_DATE and not relation and not ambiguities:
        if item.get("type") != "TASK":
            ambiguities.append("No date was found for this item. Please choose one.")
    elif res["status"] == date_resolver.STATUS_UNRESOLVED and not res["ambiguities"]:
        ambiguities.append(f"Could not work out a date from '{date_text}'. Please choose one.")

    all_day = due_time is None
    if an.sent_at and res["date_source"] == "relative":
        notes.append(f"'{date_text}' was read relative to when the message was sent "
                     f"({an.sent_at.strftime('%a %d %b, %H:%M')}).")

    location = item.get("location")
    if location and not quote_supported(location, an.text):
        location = None
    detected = signals.find_location(an.sentences[sent]) if sent is not None else None
    if detected and (not location or detected.startswith("http")):
        location = detected

    if sent is not None and _PROMO_RE.search(an.sentences[sent]):
        item["applicability"] = "potential"
        notes.append("Looks like an advertisement, so it is not pre-selected.")
    elif item.get("applicability") == "potential":
        notes.append("Looks optional or informational, so it is not pre-selected.")
    elif item.get("applicability") == "unclear":
        notes.append("Not sure this applies to you, so it is not pre-selected.")

    title = clean_title(enrich_title(clean_title(item.get("action") or ""), item, an.text))
    event_type = infer_event_type({**item, "source_text": excerpt})
    if event_type == "assignment" and not _COURSEWORK_RE.search(f"{title} {excerpt or ''}"):
        # The model called it an assignment, but nothing in the text is coursework
        # ("dentist appointment"): decide from the words instead.
        event_type = infer_event_type({**item, "category": None, "source_text": excerpt})
        if event_type == "assignment":
            event_type = "deadline" if item.get("type") == "DEADLINE" else "other"
    title = clean_title(add_subject(title, excerpt or an.text, event_type, item.get("type")))
    title = fix_internal_external(title, excerpt or an.text)
    m = re.match(r"submit\s+(.+)", title, re.I)
    if m and not re.search(r"\bsubmi", excerpt or an.text, re.I) and not _COURSEWORK_RE.search(m.group(1)):
        title = m.group(1)[:1].upper() + m.group(1)[1:]  # "Submit dentist appointment" -> "Dentist appointment"
    area = infer_area(item, excerpt, an.text)
    if event_type == "assignment" and re.match(r"(apply|register|pay|book|enrol|enroll)\b", title, re.I):
        event_type = "deadline"  # applications/registrations aren't coursework
    task = {
        "id": uuid.uuid4().hex,
        "title": title,
        "description": item.get("details") or "",
        "event_type": event_type,
        "area": area,
        "area_label": AREA_LABELS[area],
        "priority": AREAS[area],
        "item_type": item.get("type"),
        "applicability": item.get("applicability"),
        "due_date": due_date,
        "due_time": due_time,
        "end_time": None if all_day else end_time,
        "all_day": all_day,
        "timezone": timezone,
        "location": location,
        "date_text": date_text or item.get("date_text"),
        "time_text": time_text or item.get("time_text"),
        "date_status": date_status,
        "date_source": res["date_source"],
        "time_source": res["time_source"],
        "is_time_inferred": res["is_time_inferred"],
        "candidates": res["candidates"],
        "suggested_candidate": res["suggested_candidate"],
        "chosen_candidate": chosen,
        "candidate_time": res["time"] if res["status"] == date_resolver.STATUS_AMBIGUOUS else None,
        "date_question": res.get("date_question"),
        "source_text": excerpt,
        "ambiguities": ambiguities,
        "notes": notes,
        "_relation": relation,
        "_date_sentence": item.get("_date_sentence"),
        "_borrowed": item.get("_borrowed", False),
        "_sentence": sent,
        "_res": res,
    }
    return task


# ---------------------------------------------------------------------------
# Whole-message passes
# ---------------------------------------------------------------------------

def _link_relations(tasks: List[Dict[str, Any]], an: Analysis) -> None:
    """'Before the viva, submit the report' -> due at the viva's start."""
    for t in tasks:
        rel = t.pop("_relation", None)
        if not rel:
            continue
        kind, noun = rel
        noun_stems = _stems(noun)
        target = next((o for o in tasks if o is not t and o.get("due_date")
                       and noun_stems & (_stems(o["title"]) | _stems(o.get("source_text") or ""))), None)
        if target is None:
            t["ambiguities"].append(f"Due {kind} the {noun}, but its date wasn't found. Please choose a date.")
            continue
        t["due_date"] = target["due_date"]
        t["date_status"] = "LINKED"
        t["date_source"] = "inferred"
        if kind in ("before", "by", "prior to", "ahead of", "at", "during") and target.get("due_time"):
            t["due_time"] = target["due_time"]
            t["all_day"] = False
            t["time_source"] = "inferred"
        when = target["due_date"] + (f" {target['due_time']}" if target.get("due_time") else "")
        t["notes"].insert(0, f"Due {kind} the {noun} ({when}).")
        t["ambiguities"] = [a for a in t["ambiguities"] if "No date" not in a and "no date" not in a]


def _merge_duplicates(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in tasks:
        dup = None
        for o in out:
            same_day = t["due_date"] and t["due_date"] == o["due_date"]
            same_time = not t["due_time"] or not o["due_time"] or t["due_time"] == o["due_time"]
            overlap = _stems(t["title"]) & _stems(o["title"])
            exact_time = t["due_time"] == o["due_time"]
            # Two items pinned to the SAME date phrase, at least one of which had to
            # borrow it from another sentence, are parts of one obligation (e.g. the
            # coding section of a test whose link "expires on 20 Nov").
            same_phrase = (t.get("_date_sentence") is not None
                           and t.get("_date_sentence") == o.get("_date_sentence")
                           and (t.get("_borrowed") or o.get("_borrowed")))
            if ((same_day and same_time and overlap) or _norm(t["title"]) == _norm(o["title"])
                    or (same_day and exact_time and same_phrase)):
                dup = o
                break
        if dup is None:
            out.append(t)
            continue
        if not (_stems(t["title"]) & _stems(dup["title"])) and _norm(t["title"]) != _norm(dup["title"]):
            part = f"Includes: {t['title']}"
            dup["description"] = f"{dup['description']}\n{part}".strip() if dup.get("description") else part
        for k in ("due_time", "end_time", "location", "description"):
            if not dup.get(k) and t.get(k):
                dup[k] = t[k]
        if dup.get("due_time"):
            dup["all_day"] = False
        if t.get("applicability") == "explicit":
            dup["applicability"] = "explicit"
    return out


def _fallback_items(tasks: List[Dict[str, Any]], an: Analysis) -> List[Dict[str, Any]]:
    """Recover obligations the model missed: a date phrase + obligation cue in a sentence no task covers."""
    used_sentences = {t.get("_sentence") for t in tasks}
    used_dates = {(t.get("due_date"), t.get("due_time")) for t in tasks}
    extra = []
    for p in an.phrases:
        if not p.date or p.sentence in used_sentences or p.sentence in an.untrusted:
            continue
        sentence = an.sentences[p.sentence]
        if not signals.has_obligation_cue(sentence) or _PAST_INFO_RE.search(sentence):
            continue
        if p.sentence in an.revision_at and p.start < an.revision_at[p.sentence]:
            continue
        # Title: the clause around the date phrase, without the date words.
        rel_start = p.start - an.spans[p.sentence][0]
        # Blank out links (same length, so offsets stay valid) before splitting at ':'.
        clean_sentence = re.sub(r"https?://\S+", lambda m: " " * len(m.group(0)), sentence)
        clauses = list(re.finditer(r"[^,;:]+", clean_sentence))
        clause = next((c.group(0) for c in clauses if c.start() <= rel_start < c.end()), sentence)
        title = clause.replace(p.text, " ")
        for piece in filter(None, (p.date, p.time)):
            title = title.replace(piece, " ")
        title = re.sub(r"^\s*(?:\d+\)|also|and|please|note)\b[\s,]*", "", title, flags=re.I)
        title = _SUBJECT_RE.sub("", title.strip())
        title = re.sub(r"https?://\S+", " ", title)
        title = re.sub(r"\b(?:via|on|over|through|using)\s+(?:google\s+meet|zoom|ms\s+teams|teams|webex|meet)\b", " ", title, flags=re.I)
        # drop prepositions left dangling once the date/time words are removed
        prev = None
        while prev != title:
            prev = title
            title = re.sub(r"\b(?:on|at|by|via|from|before|until|till|in|for)\s+(?=(?:on|at|by|via|from|before|until|till|in|for)\b|$|[,.;])", " ", title.strip(), flags=re.I)
        title = re.sub(r"\s+", " ", title)
        title = re.sub(r"^(?:your|our|the|my)\s+", "", title.strip(" .,;:-"), flags=re.I)
        title = re.sub(r"\s+(?:is|are|was|will\s+be|has\s+been|have\s+been|scheduled|held|conducted|planned)(?:\s+(?:on|at|for))?\s*$",
                       "", title, flags=re.I)
        title = re.sub(r"\b(?:on|by|at|before|is|are|from|due)\s*$", "", title.strip(), flags=re.I)
        title = clean_title(title)
        if len(_tokens(title)) < 1:
            continue
        item = {"type": "DEADLINE" if re.search(r"\b(submit|due|deadline|last date|close|pay|register)", sentence, re.I) else "EVENT",
                "category": None, "action": title, "details": None, "date_text": p.date, "time_text": p.time,
                "location": None,
                "applicability": "explicit" if (_MUST_RE.search(sentence) or re.search(r"\byou(?:r)?\b", sentence, re.I)) else "unclear"}
        t = build_task(item, an, an_timezone(an))
        if (t["due_date"], t["due_time"]) in used_dates:
            continue
        t["notes"].insert(0, "Found by pattern matching (the model missed it); check the title.")
        used_sentences.add(p.sentence)
        extra.append(t)
    return extra


def _relation_items(tasks: List[Dict[str, Any]], an: Analysis) -> List[Dict[str, Any]]:
    """'Before the viva, submit the final report.' as its own task when the model folded it away."""
    used = {t.get("_sentence") for t in tasks}
    extra = []
    for i, sentence in enumerate(an.sentences):
        if i in used or i in an.untrusted or not signals.has_obligation_cue(sentence):
            continue
        if any(p.date for p in an.phrases_in(i)):
            continue  # dated sentences are handled by the normal fallback
        rel = signals.find_relation(sentence)
        if not rel:
            continue
        m = re.search(r"\b(?:before|by|after|prior\s+to|ahead\s+of)\s+(?:the|your|our)\s+[^,]+,\s*(.+)$", sentence.strip(), re.I)
        action = (m.group(1) if m else sentence).strip(" .,;:")
        action = _SUBJECT_RE.sub("", action)
        title = clean_title(re.sub(r"^(?:please|kindly|also)\s+", "", action, flags=re.I))
        if len(_tokens(title)) < 2 or any(_stems(title) <= _stems(t["title"]) for t in tasks):
            continue
        item = {"type": "DEADLINE", "category": None, "area": None, "action": title, "details": None,
                "date_text": None, "time_text": None, "location": None, "applicability": "explicit"}
        t = build_task(item, an, an_timezone(an))
        if t.get("_relation"):
            t["notes"].insert(0, "Found by reading the text (the model missed it).")
            extra.append(t)
    return extra


def an_timezone(an: Analysis) -> str:
    return getattr(an, "timezone", date_resolver.DEFAULT_TIMEZONE)


def _finalize(t: Dict[str, Any], an: Analysis, tz) -> Dict[str, Any]:
    t.pop("_relation", None)
    t.pop("_sentence", None)
    res = t.pop("_res", {})
    t["is_past"] = _is_past(t["due_date"], t["due_time"], tz, an.now)
    if t["is_past"]:
        t["ambiguities"].append(_PAST_MSG)
    t["all_day"] = not t["due_time"]
    if t["all_day"]:
        t["end_time"] = None
    etype = t["event_type"]
    t["reminders"] = (REMINDERS_ALL_DAY if t["all_day"] else REMINDERS_TIMED).get(etype, [60])
    if t.get("area") == "job":
        t["reminders"] = REMINDERS_JOB["all_day" if t["all_day"] else "timed"]
    t["ambiguities"] = list(dict.fromkeys(t["ambiguities"]))
    t["notes"] = list(dict.fromkeys(t["notes"]))
    t["confidence"] = _confidence(t, res or {"status": t["date_status"]}, len(t["ambiguities"]))
    if any("not in the selected text" in a for a in t["ambiguities"]):
        t["confidence"] = round(max(0.05, t["confidence"] - 0.25), 2)  # the model hallucinated
    t["confidence_label"] = "high" if t["confidence"] >= 0.75 else "medium" if t["confidence"] >= 0.45 else "low"
    t["needs_review"] = bool(not t["due_date"] or t["ambiguities"])
    trusted = not any("injected" in a or "instructions" in a for a in t["ambiguities"])
    is_ad = any("advertisement" in n for n in t["notes"])
    wanted = t["applicability"] == "explicit" or (t.get("area") == "job" and t["applicability"] == "potential")
    t["selected"] = bool(wanted and t["due_date"] and not t["is_past"] and trusted and not is_ad)
    if t["selected"] and t["applicability"] != "explicit":
        t["notes"] = [n for n in t["notes"] if "not pre-selected" not in n]
        t["notes"].append("Internship / Job is your top priority, so it is pre-selected.")
    return t


def _call_extract(extract_fn, text: str, context: Dict[str, Any]) -> Dict[str, Any]:
    fn = extract_fn or extractor.extract_items
    try:
        accepts = "context" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        accepts = False
    return fn(text, context=context) if accepts else fn(text)


def extract_tasks(
    text: str,
    reference_datetime: Optional[datetime] = None,
    timezone: str = date_resolver.DEFAULT_TIMEZONE,
    untimed_deadline: str = "all_day",
    extract_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    reader: Optional[str] = None,
    auto_resolve: bool = True,
) -> Dict[str, Any]:
    """
    Returns {"tasks": [...], "warnings": [...], "latency_seconds", "reference_time",
             "message_sent_at", "timezone"}.
    Raises extractor.ExtractionError on LLM failures and ValueError on empty input.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("No text was selected.")
    tz, timezone = date_resolver.get_timezone(timezone)
    now = reference_datetime or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    now = now.astimezone(tz)

    an = Analysis(text, now)
    an.timezone = timezone
    stamp_text = text[an.stamp_span[0]:an.stamp_span[1]] if an.stamp_span else ""

    def hint_fn(chunk: str) -> List[str]:
        sub = Analysis(chunk, now)
        return [p.text for p in sub.phrases if not (p.date and p.date in stamp_text)]

    context = {"reader": reader, "hint_fn": hint_fn,
               "sent": an.sent_at.strftime("%a %d %b %Y, %H:%M") if an.sent_at else None}
    raw = _call_extract(extract_fn, text, context)

    tasks = [build_task(it, an, timezone, untimed_deadline, auto_resolve) for it in raw["items"]]
    tasks.extend(_fallback_items(tasks, an))
    tasks.extend(_relation_items(tasks, an))
    _link_relations(tasks, an)
    tasks = _merge_duplicates(tasks)
    tasks = [_finalize(t, an, tz) for t in tasks]
    # Items whose only date comes from a sentence about the past ("Yesterday's lecture
    # was great") are history, not obligations.
    tasks = [t for t in tasks if not (t["is_past"] and t.get("_date_sentence") is not None
                                      and _PAST_INFO_RE.search(an.sentences[t["_date_sentence"]]))]
    for t in tasks:
        t.pop("_date_sentence", None)
        t.pop("_borrowed", None)
    if tasks and not any(t["selected"] for t in tasks):
        # The user selected this text to add something; offer the recovered items.
        for t in tasks:
            recovered = any("model missed it" in n for n in t["notes"])
            if recovered and t["due_date"] and not t["is_past"] and t["applicability"] == "unclear":
                t["selected"] = True
                t["notes"] = [n for n in t["notes"] if "not pre-selected" not in n]
    tasks.sort(key=lambda t: (not t["selected"], t.get("priority", 9), t["due_date"] or "9999", t["due_time"] or ""))
    return {
        "tasks": tasks,
        "warnings": raw.get("warnings", []),
        "latency_seconds": raw.get("latency_seconds"),
        "reference_time": an.reference.isoformat(),
        "message_sent_at": an.sent_at.isoformat() if an.sent_at else None,
        "timezone": timezone,
    }
