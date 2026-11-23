import json
import urllib.parse
from unittest import mock

import pytest
import requests

import calendar_client as cc
from token_store import TokenStore


# ------------------------------------------------------------------ helpers
class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class MemStore:
    def __init__(self, tok=None):
        self.tok = tok

    def load(self):
        return self.tok

    def save(self, tok):
        self.tok = tok

    def clear(self):
        self.tok = None


@pytest.fixture
def secrets_file(tmp_path):
    p = tmp_path / "client_secret.json"
    p.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com",
                                           "client_secret": "csecret"}}))
    return str(p)


def make_auth(secrets_file, tok=None, now=1000.0):
    session = mock.Mock()
    auth = cc.GoogleAuth(lambda: secrets_file, MemStore(tok), session=session, clock=lambda: now)
    return auth, session


VALID_TOKEN = {"access_token": "at", "refresh_token": "rt", "expires_at": 99999, "scope": cc.SCOPES[0]}

TASK_TIMED_DEADLINE = {"title": "Submit DBMS assignment", "event_type": "assignment",
                       "due_date": "2026-11-27", "due_time": "23:59", "all_day": False,
                       "timezone": "Asia/Kolkata", "description": "Upload to Classroom"}
TASK_ALL_DAY = {"title": "Lab records", "event_type": "deadline", "due_date": "2026-12-01",
                "all_day": True, "timezone": "Asia/Kolkata"}
TASK_MEETING = {"title": "Project review", "event_type": "meeting", "due_date": "2026-11-23",
                "due_time": "10:00", "all_day": False, "timezone": "Asia/Kolkata"}


# ------------------------------------------------------------------ payloads
def test_payload_all_day_uses_exclusive_end_date():
    p = cc.build_event_payload(TASK_ALL_DAY)
    ev = p["event"]
    assert ev["start"] == {"date": "2026-12-01"} and ev["end"] == {"date": "2026-12-02"}
    assert ev["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 900}]}
    assert p["all_day"] is True and "(all day)" in p["when"]


def test_payload_timed_deadline_ends_at_due_time():
    ev = cc.build_event_payload(TASK_TIMED_DEADLINE)["event"]
    assert ev["start"] == {"dateTime": "2026-11-27T23:29:00+05:30", "timeZone": "Asia/Kolkata"}
    assert ev["end"] == {"dateTime": "2026-11-27T23:59:00+05:30", "timeZone": "Asia/Kolkata"}
    assert ev["description"].startswith("Deadline: Fri 27 Nov 2026, 23:59 (Asia/Kolkata)")
    assert "Upload to Classroom" in ev["description"]
    assert [o["minutes"] for o in ev["reminders"]["overrides"]] == [1440, 60]


def test_payload_deadline_starts_at_due_setting():
    ev = cc.build_event_payload(TASK_TIMED_DEADLINE, {"deadline_mode": "starts_at_due",
                                                      "deadline_duration_minutes": 15})["event"]
    assert ev["start"]["dateTime"] == "2026-11-27T23:59:00+05:30"
    assert ev["end"]["dateTime"] == "2026-11-28T00:14:00+05:30"


def test_payload_meeting_default_duration_and_explicit_end():
    ev = cc.build_event_payload(TASK_MEETING)["event"]
    assert ev["end"]["dateTime"] == "2026-11-23T11:00:00+05:30"
    ev = cc.build_event_payload({**TASK_MEETING, "end_time": "12:30"})["event"]
    assert ev["end"]["dateTime"] == "2026-11-23T12:30:00+05:30"


def test_payload_other_timezone():
    ev = cc.build_event_payload({**TASK_MEETING, "timezone": "America/New_York"})["event"]
    assert ev["start"] == {"dateTime": "2026-11-23T10:00:00-05:00", "timeZone": "America/New_York"}


def test_payload_custom_reminders_and_source():
    ev = cc.build_event_payload({**TASK_MEETING, "reminders": "10, 30, 30", "include_source": True,
                                 "source_text": "Review on Monday 10 AM"})["event"]
    assert [o["minutes"] for o in ev["reminders"]["overrides"]] == [30, 10]
    assert "Source excerpt:\nReview on Monday 10 AM" in ev["description"]
    ev2 = cc.build_event_payload({**TASK_MEETING, "include_source": False, "source_text": "secret"})["event"]
    assert "secret" not in ev2["description"]


def test_payload_missing_time_becomes_all_day():
    p = cc.build_event_payload({**TASK_MEETING, "due_time": None, "all_day": False})
    assert p["all_day"] is True


@pytest.mark.parametrize("patch,msg", [
    ({"title": "  "}, "title"),
    ({"due_date": None}, "date"),
    ({"due_date": "2026-02-30"}, "not valid"),
    ({"due_time": "25:00"}, "valid time"),
    ({"due_time": "7pm"}, "HH:MM"),
    ({"timezone": "Mars/Base"}, "timezone"),
    ({"event_type": "party"}, "type"),
    ({"reminders": [1, 2, 3, 4, 5, 6]}, "at most 5"),
    ({"reminders": [99999]}, "between"),
])
def test_payload_validation(patch, msg):
    with pytest.raises(cc.InvalidTask) as exc:
        cc.build_event_payload({**TASK_MEETING, **patch})
    assert msg in str(exc.value)


def test_payload_strips_control_chars():
    ev = cc.build_event_payload({**TASK_MEETING, "title": "Quiz\x00\x07 1"})["event"]
    assert ev["summary"] == "Quiz 1"


def test_event_id_is_valid_base32hex_and_deterministic():
    a = cc.event_id_for("key-123")
    assert a == cc.event_id_for("key-123") and a != cc.event_id_for("key-123", 1)
    assert set(a) <= set("0123456789abcdefghijklmnopqrstuv") and 5 <= len(a) <= 1024


# ------------------------------------------------------------------ OAuth
def test_not_configured(tmp_path):
    auth = cc.GoogleAuth(lambda: str(tmp_path / "missing.json"), MemStore())
    assert auth.is_configured() is False
    with pytest.raises(cc.NotConfigured):
        auth.authorization_url("http://127.0.0.1:8765/oauth/callback")


def test_authorization_url_uses_pkce_and_min_scope(secrets_file):
    auth, _ = make_auth(secrets_file)
    url = auth.authorization_url("http://127.0.0.1:8765/oauth/callback")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/tasks"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    assert q["access_type"] == ["offline"] and q["state"][0]
    assert "csecret" not in url


def test_callback_exchanges_code_and_stores_token(secrets_file):
    auth, session = make_auth(secrets_file)
    url = auth.authorization_url("http://127.0.0.1:8765/oauth/callback")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    session.post.return_value = FakeResp(200, {"access_token": "at", "refresh_token": "rt",
                                               "expires_in": 3600, "scope": cc.SCOPES[0]})
    auth.handle_callback(state, "code123")
    sent = session.post.call_args.kwargs["data"]
    assert sent["code_verifier"] and sent["grant_type"] == "authorization_code"
    assert auth.is_connected()
    # state cannot be replayed
    with pytest.raises(cc.AuthRequired):
        auth.handle_callback(state, "code123")


def test_callback_rejects_unknown_state_and_user_denial(secrets_file):
    auth, _ = make_auth(secrets_file)
    with pytest.raises(cc.AuthRequired):
        auth.handle_callback("forged", "code")
    with pytest.raises(cc.AuthRequired):
        auth.handle_callback(None, None, error="access_denied")


def test_callback_rejects_missing_scope(secrets_file):
    auth, session = make_auth(secrets_file)
    url = auth.authorization_url("http://127.0.0.1:8765/oauth/callback")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    session.post.return_value = FakeResp(200, {"access_token": "at", "refresh_token": "rt",
                                               "scope": "openid email"})
    with pytest.raises(cc.AuthRequired):
        auth.handle_callback(state, "code")
    assert not auth.is_connected()


def test_expired_access_token_is_refreshed(secrets_file):
    auth, session = make_auth(secrets_file, tok={**VALID_TOKEN, "expires_at": 1000.0})
    session.post.return_value = FakeResp(200, {"access_token": "new", "expires_in": 3600})
    assert auth.access_token() == "new"
    assert auth.store.tok["refresh_token"] == "rt"  # kept when Google omits it


def test_revoked_refresh_token_clears_store(secrets_file):
    auth, session = make_auth(secrets_file, tok={**VALID_TOKEN, "expires_at": 0})
    session.post.return_value = FakeResp(400, {"error": "invalid_grant"})
    with pytest.raises(cc.AuthRequired) as exc:
        auth.access_token()
    assert "reconnect" in str(exc.value).lower()
    assert auth.store.tok is None


def test_not_connected(secrets_file):
    auth, _ = make_auth(secrets_file)
    with pytest.raises(cc.AuthRequired):
        auth.access_token()


def test_token_endpoint_network_failure(secrets_file):
    auth, session = make_auth(secrets_file, tok={**VALID_TOKEN, "expires_at": 0})
    session.post.side_effect = requests.exceptions.ConnectionError()
    with pytest.raises(cc.NetworkError):
        auth.access_token()


def test_disconnect_revokes_and_clears(secrets_file):
    auth, session = make_auth(secrets_file, tok=dict(VALID_TOKEN))
    session.post.side_effect = requests.exceptions.ConnectionError()  # still clears locally
    auth.disconnect()
    assert auth.store.tok is None


# ------------------------------------------------------------------ event creation
@pytest.fixture
def client(secrets_file, tmp_path):
    auth, _ = make_auth(secrets_file, tok=dict(VALID_TOKEN))
    http = mock.Mock()
    sleeps = []
    c = cc.CalendarClient(auth, cc.EventLedger(str(tmp_path / "ledger.json")), session=http, sleep=sleeps.append)
    c.sleeps = sleeps
    return c, http


CREATED = {"id": "abc", "htmlLink": "https://www.google.com/calendar/event?eid=abc", "status": "confirmed"}


def test_create_event_success(client):
    c, http = client
    http.request.return_value = FakeResp(200, CREATED)
    res = c.create_event(TASK_ALL_DAY, None, "key-0001")
    assert res["status"] == "created" and res["html_link"] == CREATED["htmlLink"]
    method, url = http.request.call_args.args
    body = http.request.call_args.kwargs["json"]
    assert method == "POST" and url.endswith("/calendars/primary/events")
    assert body["id"] == cc.event_id_for("key-0001")
    assert http.request.call_args.kwargs["headers"]["Authorization"] == "Bearer at"


def test_same_key_twice_does_not_call_google_again(client):
    c, http = client
    http.request.return_value = FakeResp(200, CREATED)
    c.create_event(TASK_ALL_DAY, None, "key-0001")
    res = c.create_event(TASK_ALL_DAY, None, "key-0001")
    assert res["status"] == "already_created"
    assert http.request.call_count == 1


def test_identical_event_new_key_requires_confirmation(client):
    c, http = client
    http.request.return_value = FakeResp(200, CREATED)
    c.create_event(TASK_ALL_DAY, None, "key-0001")
    with pytest.raises(cc.DuplicateSuspected) as exc:
        c.create_event(TASK_ALL_DAY, None, "key-0002")
    assert exc.value.extra["html_link"] == CREATED["htmlLink"]
    res = c.create_event(TASK_ALL_DAY, None, "key-0002", allow_duplicate=True)
    assert res["status"] == "created"


def test_conflict_from_lost_response_returns_existing(client):
    c, http = client
    http.request.side_effect = [FakeResp(409, {"error": {"message": "duplicate"}}), FakeResp(200, CREATED)]
    res = c.create_event(TASK_ALL_DAY, None, "key-0003")
    assert res["status"] == "already_created" and res["event_id"] == "abc"


def test_conflict_with_deleted_event_uses_new_id(client):
    c, http = client
    http.request.side_effect = [
        FakeResp(409, {}), FakeResp(200, {**CREATED, "status": "cancelled"}),
        FakeResp(200, CREATED),
    ]
    res = c.create_event(TASK_ALL_DAY, None, "key-0004")
    assert res["status"] == "created"
    assert http.request.call_args.kwargs["json"]["id"] == cc.event_id_for("key-0004", 1)


def test_401_refreshes_once_then_succeeds(client):
    c, http = client
    c.auth.http.post.return_value = FakeResp(200, {"access_token": "fresh", "expires_in": 3600})
    http.request.side_effect = [FakeResp(401), FakeResp(200, CREATED)]
    c.create_event(TASK_ALL_DAY, None, "key-0005")
    assert http.request.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"


def test_401_twice_requires_reauth(client):
    c, http = client
    c.auth.http.post.return_value = FakeResp(200, {"access_token": "fresh", "expires_in": 3600})
    http.request.return_value = FakeResp(401)
    with pytest.raises(cc.AuthRequired):
        c.create_event(TASK_ALL_DAY, None, "key-0006")


def test_rate_limit_backoff_then_success(client):
    c, http = client
    rl = FakeResp(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
    http.request.side_effect = [rl, FakeResp(429, headers={"Retry-After": "2"}), FakeResp(200, CREATED)]
    assert c.create_event(TASK_ALL_DAY, None, "key-0007")["status"] == "created"
    assert c.sleeps == [1.0, 2.0]


def test_rate_limit_exhausted(client):
    c, http = client
    http.request.return_value = FakeResp(429)
    with pytest.raises(cc.RateLimited):
        c.create_event(TASK_ALL_DAY, None, "key-0008")


def test_network_errors_retry_then_fail(client):
    c, http = client
    http.request.side_effect = requests.exceptions.ConnectionError()
    with pytest.raises(cc.NetworkError):
        c.create_event(TASK_ALL_DAY, None, "key-0009")
    assert http.request.call_count == cc.CalendarClient.MAX_ATTEMPTS


def test_server_error_retry(client):
    c, http = client
    http.request.side_effect = [FakeResp(503), FakeResp(200, CREATED)]
    assert c.create_event(TASK_ALL_DAY, None, "key-0010")["status"] == "created"


def test_permission_denied(client):
    c, http = client
    http.request.return_value = FakeResp(403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}})
    with pytest.raises(cc.AuthRequired):
        c.create_event(TASK_ALL_DAY, None, "key-0011")


def test_google_rejects_payload(client):
    c, http = client
    http.request.return_value = FakeResp(400, {"error": {"message": "Invalid start time"}})
    with pytest.raises(cc.InvalidTask) as exc:
        c.create_event(TASK_ALL_DAY, None, "key-0012")
    assert "Invalid start time" in str(exc.value)


def test_invalid_task_never_calls_google(client):
    c, http = client
    with pytest.raises(cc.InvalidTask):
        c.create_event({**TASK_ALL_DAY, "due_date": None}, None, "key-0013")
    with pytest.raises(cc.InvalidTask):
        c.create_event(TASK_ALL_DAY, None, "bad key!")
    http.request.assert_not_called()


# ------------------------------------------------------------------ token storage
def test_token_store_roundtrip(tmp_path):
    store = TokenStore(str(tmp_path / "tok.bin"))
    assert store.load() is None
    store.save({"refresh_token": "secret-rt"})
    assert store.load() == {"refresh_token": "secret-rt"}
    if store.backend == "dpapi":
        assert b"secret-rt" not in (tmp_path / "tok.bin").read_bytes()
    store.clear()
    assert store.load() is None


def test_token_store_corrupt_file_is_signed_out(tmp_path):
    p = tmp_path / "tok.bin"
    p.write_bytes(b"\x00garbage")
    assert TokenStore(str(p)).load() is None


def test_list_upcoming_reads_only_dday_events(client):
    c, http = client
    http.request.return_value = FakeResp(200, {"items": [
        {"id": "a", "summary": "Submit DBMS assignment", "status": "confirmed",
         "start": {"dateTime": "2026-11-27T23:29:00+05:30"}, "end": {"dateTime": "2026-11-27T23:59:00+05:30"},
         "htmlLink": "https://www.google.com/calendar/event?eid=a",
         "extendedProperties": {"private": {"dday": "1", "ddayType": "assignment"}}},
        {"id": "b", "summary": "Lab records", "start": {"date": "2026-12-01"}, "end": {"date": "2026-12-02"}},
        {"id": "c", "summary": "deleted", "status": "cancelled", "start": {"date": "2026-12-01"}, "end": {"date": "2026-12-02"}},
    ]})
    items = c.list_upcoming()
    params = http.request.call_args.kwargs["params"]
    assert params["privateExtendedProperty"] == "dday=1" and params["orderBy"] == "startTime"
    assert [i["title"] for i in items] == ["Submit DBMS assignment", "Lab records"]
    assert items[0]["event_type"] == "assignment" and items[1]["all_day"] is True


def test_list_upcoming_requires_auth(secrets_file, tmp_path):
    auth, _ = make_auth(secrets_file)  # not connected
    c = cc.CalendarClient(auth, cc.EventLedger(str(tmp_path / "l.json")), session=mock.Mock())
    with pytest.raises(cc.AuthRequired):
        c.list_upcoming()



def test_payload_area_colour_and_tag():
    ev = cc.build_event_payload({**TASK_MEETING, "area": "job"})["event"]
    assert ev["colorId"] == "11" and ev["extendedProperties"]["private"]["ddayArea"] == "job"
    assert "colorId" not in cc.build_event_payload(TASK_MEETING)["event"]
    with pytest.raises(cc.InvalidTask):
        cc.build_event_payload({**TASK_MEETING, "area": "fun"})
