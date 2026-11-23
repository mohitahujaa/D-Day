import unittest
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

from date_resolver import resolve_date_time, parse_date, parse_time


class TestDateResolver(unittest.TestCase):
    def setUp(self):
        # Using the exact reference from the project specifications:
        # Thursday, 2026-09-24 21:42 Asia/Kolkata
        self.tz_str = "Asia/Kolkata"
        self.tz = ZoneInfo(self.tz_str)
        self.ref = datetime(2026, 9, 24, 21, 42, tzinfo=self.tz)

    def test_today(self):
        res = resolve_date_time("today", "5 PM", reference_datetime=self.ref, timezone_str=self.tz_str)
        self.assertEqual(res["status"], "RESOLVED")
        self.assertEqual(res["date"], "2026-09-24")
        self.assertEqual(res["time"], "17:00")
        self.assertEqual(res["datetime_iso"], "2026-09-24T17:00:00+05:30")
        self.assertFalse(res["is_time_inferred"])

    def test_tomorrow(self):
        res = resolve_date_time("tomorrow", "10 AM", reference_datetime=self.ref, timezone_str=self.tz_str)
        self.assertEqual(res["status"], "RESOLVED")
        self.assertEqual(res["date"], "2026-09-25")
        self.assertEqual(res["time"], "10:00")
        self.assertEqual(res["datetime_iso"], "2026-09-25T10:00:00+05:30")
        self.assertFalse(res["is_time_inferred"])

    def test_friday(self):
        # Ref is Thursday 2026-09-24. "Friday" should resolve to 2026-09-25
        res = resolve_date_time("Friday", "6 PM", reference_datetime=self.ref, timezone_str=self.tz_str, item_type="DEADLINE")
        self.assertEqual(res["status"], "RESOLVED")
        self.assertEqual(res["date"], "2026-09-25")
        self.assertEqual(res["time"], "18:00")
        self.assertEqual(res["datetime_iso"], "2026-09-25T18:00:00+05:30")

        # "this Friday"
        res_this = resolve_date_time("this Friday", "5 PM", reference_datetime=self.ref, timezone_str=self.tz_str, item_type="DEADLINE")
        self.assertEqual(res_this["status"], "RESOLVED")
        self.assertEqual(res_this["date"], "2026-09-25")
        self.assertEqual(res_this["time"], "17:00")

    def test_next_monday(self):
        # Ref is Thursday 2026-09-24. "next Monday" can mean the coming Monday
        # (2026-09-28) or the one after (2026-10-05); the user must choose.
        res = resolve_date_time("next Monday", "10 AM", reference_datetime=self.ref, timezone_str=self.tz_str, item_type="EVENT")
        self.assertEqual(res["status"], "AMBIGUOUS")
        self.assertIsNone(res["date"])
        self.assertEqual([c["date"] for c in res["candidates"]], ["2026-09-28", "2026-10-05"])
        self.assertEqual(res["time"], "10:00")
        self.assertTrue(res["ambiguities"])

    def test_explicit_am_pm_times(self):
        test_cases = [
            ("6 PM", "18:00"),
            ("6:30 PM", "18:30"),
            ("6pm", "18:00"),
            ("10 AM", "10:00"),
            ("10:15 am", "10:15"),
            ("12 PM", "12:00"),
            ("12 AM", "00:00"),
            ("noon", "12:00"),
        ]
        for t_input, expected_time in test_cases:
            with self.subTest(t_input=t_input):
                res = resolve_date_time("Friday", t_input, reference_datetime=self.ref, timezone_str=self.tz_str)
                self.assertEqual(res["status"], "RESOLVED")
                self.assertEqual(res["time"], expected_time)

    def test_11_59_pm(self):
        res = resolve_date_time("Friday", "11:59 PM", reference_datetime=self.ref, timezone_str=self.tz_str, item_type="DEADLINE")
        self.assertEqual(res["status"], "RESOLVED")
        self.assertEqual(res["date"], "2026-09-25")
        self.assertEqual(res["time"], "23:59")
        self.assertEqual(res["datetime_iso"], "2026-09-25T23:59:00+05:30")
        self.assertFalse(res["is_time_inferred"])

    def test_missing_time(self):
        # Case A0: DEADLINE with missing time -> all-day by default (no invented time)
        res_allday = resolve_date_time("Friday", None, reference_datetime=self.ref, timezone_str=self.tz_str, item_type="DEADLINE")
        self.assertEqual(res_allday["status"], "RESOLVED")
        self.assertEqual(res_allday["date"], "2026-09-25")
        self.assertIsNone(res_allday["time"])
        self.assertTrue(res_allday["all_day"])
        self.assertIsNone(res_allday["datetime_iso"])

        # Case A: opt-in legacy policy -> 23:59 (inferred=True)
        res_deadline = resolve_date_time("Friday", None, reference_datetime=self.ref, timezone_str=self.tz_str, item_type="DEADLINE", untimed_deadline="end_of_day")
        self.assertEqual(res_deadline["status"], "RESOLVED")
        self.assertEqual(res_deadline["date"], "2026-09-25")
        self.assertEqual(res_deadline["time"], "23:59")
        self.assertTrue(res_deadline["is_time_inferred"])
        self.assertEqual(res_deadline["datetime_iso"], "2026-09-25T23:59:00+05:30")

        # Case B: EVENT with missing time -> all-day / date-only (time=None, datetime_iso=None)
        res_event = resolve_date_time("Friday", None, reference_datetime=self.ref, timezone_str=self.tz_str, item_type="EVENT")
        self.assertEqual(res_event["status"], "RESOLVED")
        self.assertEqual(res_event["date"], "2026-09-25")
        self.assertIsNone(res_event["time"])
        self.assertIsNone(res_event["datetime_iso"])
        self.assertFalse(res_event["is_time_inferred"])

        # Case C: Completely missing date and time (TASK) -> status="NO_DATE"
        res_task = resolve_date_time(None, None, reference_datetime=self.ref, timezone_str=self.tz_str, item_type="TASK")
        self.assertEqual(res_task["status"], "NO_DATE")
        self.assertIsNone(res_task["date"])
        self.assertIsNone(res_task["time"])
        self.assertIsNone(res_task["datetime_iso"])

    def test_invalid_unknown_date_expressions(self):
        invalid_expressions = [
            "invalid date text",
            "submission assignment 2",
            "hello world",
            "random meeting notes",
            "xyz123",
        ]
        for expr in invalid_expressions:
            with self.subTest(expr=expr):
                res = resolve_date_time(expr, "5 PM", reference_datetime=self.ref, timezone_str=self.tz_str)
                self.assertEqual(res["status"], "UNRESOLVED")
                self.assertIsNone(res["date"])
                self.assertIsNone(res["datetime_iso"])
                self.assertIsNotNone(res["error"])


if __name__ == "__main__":
    unittest.main()
