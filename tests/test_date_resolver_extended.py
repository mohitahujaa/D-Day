from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from date_resolver import resolve_date_time, parse_date, parse_time, extract_embedded_time

IST = ZoneInfo("Asia/Kolkata")
# Thursday 2026-09-24 21:42 IST
REF = datetime(2026, 9, 24, 21, 42, tzinfo=IST)


def r(d, t=None, ref=REF, tz="Asia/Kolkata", item_type="DEADLINE", **kw):
    return resolve_date_time(d, t, reference_datetime=ref, timezone_str=tz, item_type=item_type, **kw)


# ---------------------------------------------------------------- explicit dates
@pytest.mark.parametrize("text,expected", [
    ("2026-10-05", "2026-10-05"),
    ("5 October 2026", "2026-10-05"),
    ("October 5th, 2026", "2026-10-05"),
    ("Oct 5", "2026-10-05"),
    ("28/09/2026", "2026-09-28"),
    ("28-09", "2026-09-28"),       # 28 can only be a day
    ("09/28", "2026-09-28"),       # 28 can only be a day
    ("on or before 30th September", "2026-09-30"),
])
def test_explicit_dates(text, expected):
    res = r(text)
    assert res["status"] == "RESOLVED", res
    assert res["date"] == expected
    assert res["date_source"] == "explicit"
    assert res["all_day"] is True


def test_explicit_datetime_has_offset():
    res = r("5 Oct 2026", "11:59 PM")
    assert res["datetime_iso"] == "2026-10-05T23:59:00+05:30"
    assert res["time_source"] == "explicit"
    assert res["all_day"] is False


def test_embedded_time_in_date_text():
    res = r("Sep 28 11:59 PM", None)
    assert res["date"] == "2026-09-28"
    assert res["time"] == "23:59"
    assert extract_embedded_time("Friday at 6 PM", None) == ("Friday", "6 PM")


# ---------------------------------------------------------------- relative dates
@pytest.mark.parametrize("text,expected", [
    ("today", "2026-09-24"),
    ("tomorrow", "2026-09-25"),
    ("day after tomorrow", "2026-09-26"),
    ("in three days", "2026-09-27"),
    ("in 3 days", "2026-09-27"),
    ("in two weeks", "2026-10-08"),
    ("in 1 month", "2026-10-24"),
    ("3 days from now", "2026-09-27"),
    ("Saturday", "2026-09-26"),
    ("this Friday", "2026-09-25"),
    ("coming Wednesday", "2026-09-30"),
    ("Friday next week", "2026-10-02"),
    ("next week Monday", "2026-09-28"),
    ("end of the month", "2026-09-30"),
    ("end of next month", "2026-10-31"),
])
def test_relative_dates(text, expected):
    res = r(text)
    assert res["status"] == "RESOLVED", res
    assert res["date"] == expected
    assert res["date_source"] == "relative"


def test_month_boundary():
    ref = datetime(2026, 1, 30, 10, 0, tzinfo=IST)
    assert r("in 3 days", ref=ref)["date"] == "2026-02-02"
    assert r("tomorrow", ref=datetime(2026, 2, 28, 9, 0, tzinfo=IST))["date"] == "2026-03-01"
    assert r("in 1 month", ref=ref)["date"] == "2026-02-28"  # clamps, never invents Feb 30


def test_year_boundary():
    ref = datetime(2026, 12, 30, 12, 0, tzinfo=IST)  # Wednesday
    assert r("tomorrow", ref=ref)["date"] == "2026-12-31"
    assert r("Friday", ref=ref)["date"] == "2027-01-01"
    assert r("in 5 days", ref=ref)["date"] == "2027-01-04"
    # Year-less early-January date seen in late December -> clearly next year,
    # but still offered as a choice rather than silently moved.
    res = r("Jan 5", ref=ref)
    assert res["status"] == "AMBIGUOUS"
    assert [c["date"] for c in res["candidates"]] == ["2026-01-05", "2027-01-05"]
    assert res["suggested_candidate"] == 1


# ---------------------------------------------------------------- timezones
def test_reference_in_other_timezone_is_converted():
    # 2026-09-24 20:00 UTC == 2026-09-25 01:30 IST -> "tomorrow" is the 26th in IST
    ref_utc = datetime(2026, 9, 24, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert r("tomorrow", ref=ref_utc)["date"] == "2026-09-26"


def test_user_timezone_used_for_output():
    res = r("tomorrow", "9 AM", tz="America/New_York",
            ref=datetime(2026, 9, 24, 12, 0, tzinfo=ZoneInfo("America/New_York")))
    assert res["datetime_iso"] == "2026-09-25T09:00:00-04:00"
    assert res["timezone"] == "America/New_York"


def test_time_with_source_timezone_converted_to_user_tz():
    res = r("Friday", "9 AM EST", item_type="EVENT")  # EDT in September (UTC-4)
    assert res["date"] == "2026-09-25"
    assert res["time"] == "18:30"
    assert res["datetime_iso"] == "2026-09-25T18:30:00+05:30"
    assert any("Converted" in n for n in res["notes"])


def test_timezone_conversion_crossing_date_is_flagged():
    res = r("Friday", "11 PM EST", item_type="EVENT")
    assert res["date"] == "2026-09-26"
    assert res["time"] == "08:30"
    assert any("moves this" in a for a in res["ambiguities"])


def test_utc_offset_time():
    res = r("Friday", "10:00 UTC+2")
    assert res["time"] == "13:30"


def test_invalid_timezone_falls_back_to_default():
    res = r("tomorrow", "5 pm", tz="Not/AZone")
    assert res["timezone"] == "Asia/Kolkata"
    assert res["datetime_iso"].endswith("+05:30")


# ---------------------------------------------------------------- ambiguity
def test_next_friday_is_ambiguous():
    # Monday reference: "next Friday" = this week's Friday or the one after.
    ref = datetime(2026, 9, 21, 9, 0, tzinfo=IST)
    res = r("next Friday", "5 PM", ref=ref)
    assert res["status"] == "AMBIGUOUS"
    assert [c["date"] for c in res["candidates"]] == ["2026-09-25", "2026-10-02"]
    assert res["datetime_iso"] is None


def test_bare_weekday_on_same_day_is_ambiguous():
    res = r("Thursday", "11 PM")  # reference is a Thursday
    assert res["status"] == "AMBIGUOUS"
    assert [c["date"] for c in res["candidates"]] == ["2026-09-24", "2026-10-01"]


def test_day_month_order_ambiguity():
    res = r("05/10/2026")
    assert res["status"] == "AMBIGUOUS"
    assert [c["date"] for c in res["candidates"]] == ["2026-10-05", "2026-05-10"]
    assert res["suggested_candidate"] == 0  # DD/MM preferred


@pytest.mark.parametrize("text", ["next week", "this weekend", "soon", "September", "end of week"])
def test_vague_dates_need_user_input(text):
    res = r(text)
    assert res["status"] == "UNRESOLVED"
    assert res["date"] is None


def test_missing_meridiem_inferred_from_daytime():
    res = r("tomorrow", "at 5")
    assert res["status"] == "RESOLVED"
    assert res["time"] == "17:00"            # 1-7 without AM/PM -> afternoon
    assert any("AM/PM" in n for n in res["notes"])
    assert not res["ambiguities"]             # an assumption, not a blocker
    assert r("tomorrow", "at 9")["time"] == "09:00"
    assert r("tomorrow", "5:30")["time"] == "17:30"
    assert r("tomorrow", "09:30")["time"] == "09:30"   # leading zero = 24h as written
    assert not r("tomorrow", "17:00")["notes"]


def test_range_without_meridiem():
    res = r("tomorrow", "2-4", item_type="EVENT")
    assert (res["time"], res["end_time"]) == ("14:00", "16:00")


def test_same_weekday_with_time_prefers_today_if_still_ahead():
    # Reference is Thursday 21:42
    late = r("Thursday", "11 PM")
    assert late["status"] == "AMBIGUOUS" and late["suggested_candidate"] == 0
    early = r("Thursday", "9 AM")
    assert early["suggested_candidate"] == 1


def test_numeric_order_preference_follows_timezone():
    assert r("05/10/2026")["suggested_candidate"] == 0  # India: DD/MM
    us = r("05/10/2026", tz="America/New_York",
           ref=datetime(2026, 9, 24, 9, 0, tzinfo=ZoneInfo("America/New_York")))
    assert us["candidates"][us["suggested_candidate"]]["date"] == "2026-05-10"


def test_approximate_time_flagged_as_inferred():
    res = r("tomorrow", "morning", item_type="EVENT")
    assert res["is_time_inferred"] is True
    assert res["time_source"] == "inferred"
    assert res["notes"]


def test_weekday_mismatch_flagged():
    res = r("Tuesday, 28 September 2026")  # actually a Monday
    assert res["date"] == "2026-09-28"
    assert any("Monday" in a for a in res["ambiguities"])


def test_day_only_is_inferred():
    res = r("the 28th")
    assert res["date"] == "2026-09-28"
    assert res["date_source"] == "inferred"
    assert res["notes"]
    assert r("the 10th")["date"] == "2026-10-10"


# ---------------------------------------------------------------- past dates
def test_explicit_past_date_flagged_not_moved():
    res = r("2026-09-20", "5 PM")
    assert res["status"] == "RESOLVED"
    assert res["date"] == "2026-09-20"
    assert res["is_past"] is True
    assert any("past" in a for a in res["ambiguities"])


def test_yearless_past_date_offers_choice():
    res = r("September 20")
    assert res["status"] == "AMBIGUOUS"
    assert [c["date"] for c in res["candidates"]] == ["2026-09-20", "2027-09-20"]
    assert res["suggested_candidate"] == 0  # recent -> probably this year, already passed


def test_today_earlier_time_is_past():
    res = r("today", "9 AM")
    assert res["is_past"] is True
    assert r("today", "11 PM")["is_past"] is False


def test_yesterday_is_past():
    res = r("yesterday")
    assert res["is_past"] is True and res["all_day"] is True


# ---------------------------------------------------------------- times
def test_time_range():
    res = r("tomorrow", "10 AM - 12 PM", item_type="EVENT")
    assert res["time"] == "10:00" and res["end_time"] == "12:00"
    assert res["end_datetime_iso"] == "2026-09-25T12:00:00+05:30"
    res2 = r("tomorrow", "2-4 pm", item_type="EVENT")
    assert (res2["time"], res2["end_time"]) == ("14:00", "16:00")


def test_invalid_time():
    res = r("tomorrow", "25:99")
    assert res["status"] == "UNRESOLVED"
    assert res["error"]


def test_time_without_date():
    res = r(None, "5 PM")
    assert res["status"] == "NO_DATE"
    assert res["time"] == "17:00"


def test_compat_wrappers():
    from datetime import date, time
    assert parse_date("tomorrow", date(2026, 9, 24)) == (date(2026, 9, 25), None)
    d, err = parse_date("next Friday", date(2026, 9, 21))
    assert d is None and err
    assert parse_time("6:30 pm") == (time(18, 30), False, None)
    assert parse_time(None, default_for_type="DEADLINE") == (None, False, None)


def test_naive_reference_assumed_in_user_tz():
    res = resolve_date_time("tomorrow", "9 am", reference_datetime=datetime(2026, 9, 24, 23, 0))
    assert res["datetime_iso"] == "2026-09-25T09:00:00+05:30"


def test_null_like_strings():
    for v in ("null", "None", "N/A", "", "  "):
        assert r(v)["status"] == "NO_DATE"


# ---------------------------------------------------------------- Hinglish
@pytest.mark.parametrize("d,t,date_,time_", [
    ("kal", "11 baje", "2026-09-25", "11:00"),
    ("parso", "shaam 6 baje", "2026-09-26", "18:00"),
    ("aaj", "raat 9 baje", "2026-09-24", "21:00"),
    ("kal", "subah 8", "2026-09-25", "08:00"),
    ("shukravar", None, "2026-09-25", None),
    ("friday tak", None, "2026-09-25", None),
])
def test_hinglish_dates_and_times(d, t, date_, time_):
    res = r(d, t, ref=datetime(2026, 9, 24, 9, 0, tzinfo=IST))
    assert (res["status"], res["date"], res["time"]) == ("RESOLVED", date_, time_)


def test_kal_note():
    res = r("kal", None)
    assert any("kal" in n for n in res["notes"])
