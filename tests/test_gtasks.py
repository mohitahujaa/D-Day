from unittest import mock

import pytest

import calendar_client as cc
import inbox as inbox_mod
from gtasks_source import GoogleTasksSource, TasksError


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = b"x"

    def json(self):
        return self._body


class FakeGoogle:
    """Minimal Google Tasks API: one 'D-Day' list (or none) with some tasks."""
    def __init__(self, has_list=True, tasks=None, disabled=False):
        self.lists = [{"id": "L1", "title": "D-Day"}] if has_list else [{"id": "L0", "title": "My Tasks"}]
        self.tasks = tasks or []
        self.disabled = disabled
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        path = url.split("/tasks/v1", 1)[1]
        self.calls.append((method, path, json))
        if self.disabled:
            return Resp(403, {"error": {"message": "Google Tasks API has not been used in project 1 before or it is disabled.",
                                        "errors": [{"reason": "accessNotConfigured"}]}})
        if method == "GET" and path == "/users/@me/lists":
            return Resp(200, {"items": self.lists})
        if method == "POST" and path == "/users/@me/lists":
            self.lists.append({"id": "L2", "title": json["title"]})
            return Resp(200, {"id": "L2", "title": json["title"]})
        if method == "GET" and path.endswith("/tasks"):
            return Resp(200, {"items": [t for t in self.tasks if t.get("status") != "completed"]})
        if method == "PATCH":
            tid = path.rsplit("/", 1)[1]
            for t in self.tasks:
                if t["id"] == tid:
                    t.update(json)
            return Resp(200, {})
        return Resp(404, {"error": {"message": "not found"}})


@pytest.fixture
def setup(tmp_path):
    box = inbox_mod.Inbox(str(tmp_path / "inbox.db"))
    worker = inbox_mod.InboxWorker(box, lambda text, ref: {"tasks": [], "warnings": []})
    worker.enqueue = mock.Mock()
    auth = mock.Mock()
    auth.is_connected.return_value = True
    auth.has_scope.side_effect = lambda s: s == cc.TASKS_SCOPE
    auth.access_token.return_value = "at"

    def make(google):
        return GoogleTasksSource(auth, box, worker, str(tmp_path / "gtasks.json"), session=google, sleep=lambda s: None)
    return box, worker, auth, make


def test_imports_new_tasks_and_ticks_them_off(setup):
    box, worker, auth, make = setup
    google = FakeGoogle(tasks=[
        {"id": "t1", "title": "Submit DBMS assignment by Friday 11:59 PM", "updated": "2026-11-24T05:00:00.000Z"},
        {"id": "t2", "title": "Infosys test", "notes": "28 Nov at 10 AM, register by tomorrow 5 PM", "status": "needsAction"},
        {"id": "t3", "title": "   ", "notes": ""},                       # empty: ignored
    ])
    src = make(google)
    assert src.poll_once() == 2
    items = box.list()
    texts = sorted(i["text"] for i in items)
    assert texts == ["Infosys test\n28 Nov at 10 AM, register by tomorrow 5 PM", "Submit DBMS assignment by Friday 11:59 PM"]
    first = next(i for i in items if i["meta"]["task_id"] == "t1")
    assert first["source"] == "gtasks" and first["reference_time"].startswith("2026-11-24T05:00")
    assert worker.enqueue.call_count == 2
    assert [t.get("status") for t in google.tasks[:2]] == ["completed", "completed"]
    # nothing new next time
    assert src.poll_once() == 0


def test_never_imports_twice_even_if_tick_off_fails(setup):
    box, worker, auth, make = setup
    google = FakeGoogle(tasks=[{"id": "t1", "title": "Quiz on Monday"}])
    real = google.request

    def no_patch(method, url, **kw):
        if method == "PATCH":
            return Resp(500, {"error": {"message": "boom"}})
        return real(method, url, **kw)
    google.request = no_patch
    src = make(google)
    assert src.poll_once() == 1
    assert src.poll_once() == 0          # still visible on Google, but remembered locally
    assert len(box.list()) == 1


def test_creates_list_when_missing(setup):
    box, worker, auth, make = setup
    google = FakeGoogle(has_list=False)
    src = make(google)
    src.poll_once()
    assert any(m == "POST" and p == "/users/@me/lists" for m, p, _ in google.calls)
    assert src.status()["list_ready"] is True


def test_api_disabled_is_explained(setup):
    box, worker, auth, make = setup
    src = make(FakeGoogle(disabled=True))
    with pytest.raises(TasksError) as exc:
        src.poll_once()
    assert exc.value.code == "api_disabled" and "Enable" in str(exc.value)


def test_missing_permission_asks_to_reconnect(setup):
    box, worker, auth, make = setup
    auth.has_scope.side_effect = lambda s: False
    src = make(FakeGoogle())
    with pytest.raises(TasksError) as exc:
        src.poll_once()
    assert exc.value.code == "permission_missing"
    assert src.status()["permission"] is False


def test_pause_and_resume(setup):
    box, worker, auth, make = setup
    src = make(FakeGoogle())
    assert src.set_enabled(False)["enabled"] is False
    assert src.set_enabled(True)["enabled"] is True


def test_calendar_still_works_if_tasks_permission_declined(tmp_path):
    import json
    import urllib.parse
    secrets = tmp_path / "cs.json"
    secrets.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}}))
    store = mock.Mock()
    store.load.return_value = None
    http = mock.Mock()
    auth = cc.GoogleAuth(lambda: str(secrets), store, session=http, clock=lambda: 1000.0)
    url = auth.authorization_url("http://127.0.0.1:8765/oauth/callback")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert cc.TASKS_SCOPE in q["scope"][0] and cc.CALENDAR_SCOPE in q["scope"][0]
    http.post.return_value = mock.Mock(status_code=200, json=lambda: {
        "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": cc.CALENDAR_SCOPE})
    auth.handle_callback(q["state"][0], "code")   # only Calendar granted: accepted
    assert store.save.called


def test_share_app_title_plus_details_not_duplicated(setup):
    box, worker, auth, make = setup
    long_msg = "Hi all, DBMS assignment due Friday 11:59 PM\nSubmit on Classroom. Late submissions get zero."
    google = FakeGoogle(tasks=[{"id": "s1", "title": "Hi all, DBMS assignment due Friday 11:59 PM", "notes": long_msg},
                               {"id": "s2", "title": "Hi all, a very long first line that the app cut short…",
                                "notes": "Hi all, a very long first line that the app cut short and more text"}])
    make(google).poll_once()
    texts = sorted(i["text"] for i in box.list())
    assert texts == sorted([long_msg, "Hi all, a very long first line that the app cut short and more text"])
