import json
from unittest import mock

import pytest

import calendar_client as cc
import extractor
import server
from config import Config

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
HEADERS = {"Origin": EXT, "X-DDay-Client": "extension"}


class FakeCalendar:
    def __init__(self):
        self.calls = []
        self.raise_exc = None

    def create_event(self, task, settings, idempotency_key, allow_duplicate=False):
        self.calls.append((task, idempotency_key, allow_duplicate))
        if self.raise_exc:
            raise self.raise_exc
        return {"status": "created", "event_id": "e1", "html_link": "https://www.google.com/calendar/event?eid=e1",
                "when": "Fri 27 Nov 2026 (all day)"}


@pytest.fixture
def setup(tmp_path):
    cfg = Config(data_dir=str(tmp_path), port=8765)
    fake_cal = FakeCalendar()
    auth = mock.Mock()
    auth.is_configured.return_value = True
    auth.is_connected.return_value = False
    items = {"items": [{"type": "DEADLINE", "category": "assignment", "action": "Submit DBMS assignment",
                        "details": None, "date_text": "Friday", "time_text": "11:59 PM",
                        "source_text": None, "applicability": "explicit"}],
             "warnings": [], "latency_seconds": 0.1}
    extract_fn = mock.Mock(return_value=items)
    ollama = mock.Mock()
    ollama.get.return_value.json.return_value = {"models": [{"name": "qwen3:1.7b"}]}
    app = server.create_app(cfg, extract_fn=extract_fn, calendar=fake_cal, auth=auth, ollama_session=ollama)
    app.testing = True
    client = app.test_client()
    return client, fake_cal, extract_fn, auth


def post(client, path, body, headers=HEADERS, **kw):
    return client.post(path, data=json.dumps(body), content_type="application/json", headers=headers,
                       base_url="http://127.0.0.1:8765", **kw)


def get(client, path, headers=HEADERS):
    return client.get(path, headers=headers, base_url="http://127.0.0.1:8765")


# ------------------------------------------------------------ security
def test_rejects_web_origin(setup):
    client, *_ = setup
    r = post(client, "/api/extract", {"text": "x"}, headers={"Origin": "https://evil.example", "X-DDay-Client": "extension"})
    assert r.status_code == 403


def test_rejects_missing_client_header(setup):
    client, *_ = setup
    r = post(client, "/api/extract", {"text": "x"}, headers={"Origin": EXT})
    assert r.status_code == 403


def test_rejects_foreign_host_header(setup):
    client, *_ = setup
    r = client.get("/api/health", headers=HEADERS, base_url="http://attacker.example:8765")
    assert r.status_code == 403


def test_preflight_only_for_extensions(setup):
    client, *_ = setup
    ok = client.options("/api/events", headers={"Origin": EXT, "Access-Control-Request-Method": "POST"},
                        base_url="http://127.0.0.1:8765")
    assert ok.status_code == 204 and ok.headers["Access-Control-Allow-Origin"] == EXT
    bad = client.options("/api/events", headers={"Origin": "https://evil.example"}, base_url="http://127.0.0.1:8765")
    assert bad.status_code == 403
    assert "Access-Control-Allow-Origin" not in bad.headers


def test_pinned_extension_id(tmp_path):
    cfg = Config(data_dir=str(tmp_path), port=8765, allowed_extension_ids=["onlythisone"])
    app = server.create_app(cfg, extract_fn=mock.Mock(), calendar=FakeCalendar(), auth=mock.Mock(),
                            ollama_session=mock.Mock())
    r = get(app.test_client(), "/api/health")
    assert r.status_code == 403


# ------------------------------------------------------------ health
def test_health(setup):
    client, *_ = setup
    data = get(client, "/api/health").get_json()
    assert data["ollama"] == {"reachable": True, "model": extractor.MODEL, "model_installed": True}
    assert data["google"] == {"configured": True, "connected": False}


def test_health_ollama_down(setup, tmp_path):
    ollama = mock.Mock()
    ollama.get.side_effect = ConnectionError()
    app = server.create_app(Config(data_dir=str(tmp_path)), extract_fn=mock.Mock(), calendar=FakeCalendar(),
                            auth=mock.Mock(), ollama_session=ollama)
    data = get(app.test_client(), "/api/health").get_json()
    assert data["ollama"]["reachable"] is False


# ------------------------------------------------------------ extract
def test_extract_ok(setup):
    client, _, extract_fn, _ = setup
    r = post(client, "/api/extract", {"text": "Submit the DBMS assignment by Friday at 11:59 PM.",
                                      "timezone": "Asia/Kolkata", "reference_time": "2026-11-22T10:00:00+05:30"})
    assert r.status_code == 200
    t = r.get_json()["tasks"][0]
    assert t["due_date"] == "2026-11-27" and t["due_time"] == "23:59"


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": 42}])
def test_extract_empty(setup, body):
    client, *_ = setup
    r = post(client, "/api/extract", body)
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "empty_selection"


def test_extract_too_long(setup):
    client, *_ = setup
    r = post(client, "/api/extract", {"text": "a" * 20001})
    assert r.status_code == 413


def test_extract_bad_reference(setup):
    client, *_ = setup
    assert post(client, "/api/extract", {"text": "hi", "reference_time": "yesterday"}).status_code == 400


@pytest.mark.parametrize("exc,status,code", [
    (extractor.OllamaUnavailable("down"), 503, "ollama_unavailable"),
    (extractor.ModelNotFound("missing"), 503, "model_not_found"),
    (extractor.OllamaTimeout("slow"), 504, "ollama_timeout"),
    (extractor.ModelOutputError("bad json"), 502, "invalid_model_output"),
])
def test_extract_llm_failures(setup, exc, status, code):
    client, _, extract_fn, _ = setup
    extract_fn.side_effect = exc
    r = post(client, "/api/extract", {"text": "Quiz on Friday"})
    assert r.status_code == status and r.get_json()["error"]["code"] == code


# ------------------------------------------------------------ events
TASK = {"title": "Lab records", "event_type": "deadline", "due_date": "2026-12-01", "all_day": True,
        "timezone": "Asia/Kolkata"}


def test_preview(setup):
    client, fake_cal, *_ = setup
    r = post(client, "/api/events/preview", {"task": TASK})
    assert r.status_code == 200 and r.get_json()["event"]["start"] == {"date": "2026-12-01"}
    assert fake_cal.calls == []


def test_preview_invalid(setup):
    client, *_ = setup
    r = post(client, "/api/events/preview", {"task": {**TASK, "due_date": None}})
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "invalid_task"


def test_create_requires_confirmation(setup):
    client, fake_cal, *_ = setup
    r = post(client, "/api/events", {"task": TASK, "idempotency_key": "k" * 16})
    assert r.status_code == 400 and fake_cal.calls == []


def test_create_ok(setup):
    client, fake_cal, *_ = setup
    r = post(client, "/api/events", {"task": TASK, "idempotency_key": "k" * 16, "confirmed": True})
    assert r.status_code == 201 and r.get_json()["html_link"].startswith("https://www.google.com/")
    assert fake_cal.calls[0][1] == "k" * 16


@pytest.mark.parametrize("exc,status,code,has_auth_url", [
    (cc.AuthRequired("reconnect"), 401, "auth_required", True),
    (cc.NotConfigured("setup"), 503, "google_not_configured", True),
    (cc.RateLimited("slow down"), 429, "rate_limited", False),
    (cc.NetworkError("offline"), 503, "network_error", False),
    (cc.DuplicateSuspected("dup", html_link="https://www.google.com/calendar/x"), 409, "duplicate_suspected", False),
])
def test_create_errors(setup, exc, status, code, has_auth_url):
    client, fake_cal, *_ = setup
    fake_cal.raise_exc = exc
    r = post(client, "/api/events", {"task": TASK, "idempotency_key": "k" * 16, "confirmed": True})
    err = r.get_json()["error"]
    assert r.status_code == status and err["code"] == code
    assert ("auth_url" in err) is has_auth_url
    if code == "duplicate_suspected":
        assert err["html_link"]


def test_create_with_real_client_end_to_end(tmp_path):
    """Server + real CalendarClient with mocked Google HTTP."""
    secrets = tmp_path / "client_secret.json"
    secrets.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))

    class Store:
        tok = {"access_token": "at", "refresh_token": "rt", "expires_at": 9e12}
        def load(self): return self.tok
        def save(self, t): self.tok = t
        def clear(self): self.tok = None

    auth = cc.GoogleAuth(lambda: str(secrets), Store())
    http = mock.Mock()
    http.request.return_value = mock.Mock(status_code=200, json=lambda: {"id": "x1", "htmlLink": "https://www.google.com/calendar/event?eid=x1"})
    cal = cc.CalendarClient(auth, cc.EventLedger(str(tmp_path / "ledger.json")), session=http, sleep=lambda s: None)
    app = server.create_app(Config(data_dir=str(tmp_path)), extract_fn=mock.Mock(), calendar=cal, auth=auth,
                            ollama_session=mock.Mock())
    client = app.test_client()
    body = {"task": TASK, "idempotency_key": "abcdef123456", "confirmed": True}
    r1 = post(client, "/api/events", body)
    r2 = post(client, "/api/events", body)  # double submit
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.get_json()["status"] == "already_created"
    assert http.request.call_count == 1


# ------------------------------------------------------------ pages
def test_auth_start_not_configured(tmp_path):
    auth = cc.GoogleAuth(lambda: "", mock.Mock())
    app = server.create_app(Config(data_dir=str(tmp_path)), extract_fn=mock.Mock(), calendar=FakeCalendar(),
                            auth=auth, ollama_session=mock.Mock())
    r = app.test_client().get("/auth/start", base_url="http://127.0.0.1:8765")
    assert r.status_code == 503 and b"not set up" in r.data


def test_oauth_callback_error_page_escapes_html(tmp_path):
    auth = mock.Mock()
    auth.handle_callback.side_effect = cc.AuthRequired("<script>alert(1)</script>")
    app = server.create_app(Config(data_dir=str(tmp_path)), extract_fn=mock.Mock(), calendar=FakeCalendar(),
                            auth=auth, ollama_session=mock.Mock())
    r = app.test_client().get("/oauth/callback?state=x&code=y", base_url="http://127.0.0.1:8765")
    assert r.status_code == 400 and b"<script>" not in r.data
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]


def test_list_events_endpoint(setup):
    client, fake_cal, *_ = setup
    fake_cal.list_upcoming = lambda calendar_id="primary": [{"title": "Quiz", "start": "2026-11-23T09:00:00+05:30"}]
    r = get(client, "/api/events")
    assert r.status_code == 200 and r.get_json()["events"][0]["title"] == "Quiz"


def test_list_events_needs_google(setup):
    client, fake_cal, *_ = setup
    def boom(calendar_id="primary"):
        raise cc.AuthRequired("not connected")
    fake_cal.list_upcoming = boom
    r = get(client, "/api/events")
    assert r.status_code == 401 and "auth_url" in r.get_json()["error"]
