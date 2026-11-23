import json
from unittest import mock

import pytest

import calendar_client as cc
import extractor
import inbox as inbox_mod
import server
from app_api import Services
from config import Config
from settings_store import SettingsStore
from telegram_bot import TelegramBot, TelegramError

TASK = {"id": "t1", "title": "Submit DBMS assignment", "event_type": "assignment", "area": "college",
        "due_date": "2026-11-27", "due_time": "23:59", "all_day": False, "timezone": "Asia/Kolkata",
        "selected": True, "reminders": [1440, 120], "area_label": "College", "ambiguities": [], "notes": []}


class MemStore:
    def __init__(self, data=None):
        self.data = data

    def load(self):
        return dict(self.data) if self.data else None

    def save(self, d):
        self.data = dict(d)

    def clear(self):
        self.data = None


class FakeCalendar:
    def __init__(self):
        self.calls = []
        self.exc = None

    def create_event(self, task, settings, key, allow_duplicate=False):
        self.calls.append((task["title"], key))
        if self.exc:
            raise self.exc
        return {"status": "created", "event_id": "e1", "html_link": "https://www.google.com/calendar/event?eid=e1",
                "when": "Fri 27 Nov"}


@pytest.fixture
def box(tmp_path):
    return inbox_mod.Inbox(str(tmp_path / "inbox.db"))


def ready_item(box, tasks=None, source="paste", meta=None):
    item_id = box.add(source, "Submit the DBMS assignment by Friday 11:59 PM", meta=meta)
    box.update(item_id, status="ready", tasks=tasks if tasks is not None else [dict(TASK)])
    return item_id


# ------------------------------------------------------------------ inbox store
def test_inbox_add_list_counts(box):
    a = box.add("paste", "hello")
    b = box.add("telegram", "world", reference_time="2026-11-20T10:00:00+00:00")
    assert [i["id"] for i in box.list()] == [b, a]
    assert box.counts() == {"queued": 2}
    assert box.get(b)["reference_time"].startswith("2026-11-20")
    with pytest.raises(ValueError):
        box.add("paste", "   ")


def test_edit_task_validates(box):
    item_id = ready_item(box)
    t = box.edit_task(item_id, "t1", {"title": "Submit DBMS assignment 2", "area": "job", "evil": "x"})
    assert t["title"] == "Submit DBMS assignment 2" and t["area"] == "job" and "evil" not in t
    t = box.edit_task(item_id, "t1", {"all_day": True})
    assert t["due_time"] is None
    with pytest.raises(ValueError):
        box.edit_task(item_id, "t1", {"due_date": "27/11/2026"})
    with pytest.raises(ValueError):
        box.edit_task(item_id, "t1", {"area": "fun"})
    with pytest.raises(KeyError):
        box.edit_task(item_id, "nope", {"title": "x"})


# ------------------------------------------------------------------ worker
def test_worker_processes_and_notifies(box):
    seen = []
    worker = inbox_mod.InboxWorker(box, lambda text, ref: {"tasks": [dict(TASK)], "warnings": []})
    worker.listeners.append(lambda item: seen.append(item["status"]))
    item_id = box.add("paste", "text")
    assert worker.process(item_id) is True
    item = box.get(item_id)
    assert item["status"] == "ready" and item["tasks"][0]["ui"] == "pending"
    assert seen == ["ready"]


def test_worker_retries_when_ollama_down(box):
    def boom(text, ref):
        raise extractor.OllamaUnavailable("down")
    worker = inbox_mod.InboxWorker(box, boom)
    item_id = box.add("paste", "text")
    assert worker.process(item_id) is False
    assert box.get(item_id)["status"] == "queued"


def test_worker_marks_failures(box):
    def bad(text, ref):
        raise extractor.ModelOutputError("bad json")
    worker = inbox_mod.InboxWorker(box, bad)
    item_id = box.add("paste", "text")
    worker.process(item_id)
    assert box.get(item_id)["status"] == "failed"


# ------------------------------------------------------------------ saving
def test_save_item_saves_selected_only(box):
    other = dict(TASK, id="t2", title="Optional thing", selected=False)
    item_id = ready_item(box, [dict(TASK), other])
    cal = FakeCalendar()
    res = inbox_mod.save_item(box, cal, item_id, {"timezone": "Asia/Kolkata"})
    assert [c[0] for c in cal.calls] == ["Submit DBMS assignment"]
    assert cal.calls[0][1].startswith(f"inbox{item_id}-")
    assert res["status"] == "saved"
    tasks = {t["id"]: t for t in box.get(item_id)["tasks"]}
    assert tasks["t1"]["ui"] == "saved" and tasks["t2"].get("ui") != "saved"
    # pressing Save again does nothing new
    inbox_mod.save_item(box, cal, item_id, {})
    assert len(cal.calls) == 1


def test_save_item_auth_error_keeps_tasks(box):
    item_id = ready_item(box)
    cal = FakeCalendar()
    cal.exc = cc.AuthRequired("reconnect")
    res = inbox_mod.save_item(box, cal, item_id, {})
    assert res["results"][0]["code"] == "auth_required"
    assert box.get(item_id)["status"] == "ready"
    assert box.get(item_id)["tasks"][0]["error"]["code"] == "auth_required"


# ------------------------------------------------------------------ telegram
TOKEN = "123456789:AAFAKE-test-token-not-real-0000000000"


class FakeHTTP:
    """Records Telegram API calls and returns canned results."""
    def __init__(self):
        self.calls = []
        self.next_id = 100

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, json or {}))
        resp = mock.Mock(status_code=200)
        if method == "getMe":
            resp.json.return_value = {"ok": True, "result": {"username": "dday_test_bot"}}
        elif method == "sendMessage":
            self.next_id += 1
            resp.json.return_value = {"ok": True, "result": {"message_id": self.next_id}}
        else:
            resp.json.return_value = {"ok": True, "result": True}
        return resp

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def bot(box):
    http = FakeHTTP()
    worker = inbox_mod.InboxWorker(box, lambda text, ref: {"tasks": [dict(TASK)], "warnings": []})
    cal = FakeCalendar()
    b = TelegramBot(MemStore(), box, worker, lambda item_id: inbox_mod.save_item(box, cal, item_id, {}), session=http)
    b.start = lambda: None  # no background thread in tests
    b.http_fake, b.worker_ref, b.cal = http, worker, cal
    return b


def msg(text, chat_id=42, mid=1, **extra):
    return {"update_id": mid, "message": {"message_id": mid, "chat": {"id": chat_id, "type": "private"},
                                          "date": 1795000000, "text": text, **extra}}


def pair(bot):
    bot.set_token(TOKEN)
    code = bot.status()["pair_code"]
    bot.handle_update(msg(f"/start {code}", mid=1))
    return code


def test_set_token_validates_format(bot):
    with pytest.raises(TelegramError):
        bot.set_token("not-a-token")
    st = bot.set_token(TOKEN)
    assert st["configured"] and not st["paired"] and len(st["pair_code"]) == 6
    assert st["pair_link"] == f"https://t.me/dday_test_bot?start={st['pair_code']}"


def test_pairing_requires_code(bot):
    bot.set_token(TOKEN)
    bot.handle_update(msg("hello", chat_id=7, mid=1))
    assert not bot.status()["paired"]
    assert "private" in bot.http_fake.sent()[-1]["text"]
    pair(bot)
    assert bot.status()["paired"] and bot.store.data["chat_id"] == 42
    assert "Paired" in bot.http_fake.sent()[-1]["text"]


def test_strangers_are_ignored_after_pairing(bot, box):
    pair(bot)
    n = len(bot.http_fake.calls)
    bot.handle_update(msg("Quiz tomorrow at 9", chat_id=999, mid=5))
    assert box.list() == [] and len(bot.http_fake.calls) == n


def test_message_goes_to_inbox_with_forward_date_and_reply(bot, box):
    pair(bot)
    bot.handle_update(msg("Quiz tomorrow at 9 AM", mid=7, forward_origin={"type": "user", "date": 1794900000}))
    item = box.list()[0]
    assert item["source"] == "telegram" and item["text"] == "Quiz tomorrow at 9 AM"
    assert item["reference_time"].startswith("2026-")  # from forward_origin.date
    assert item["meta"]["chat_id"] == 42 and item["meta"]["ack_message_id"]
    # processing edits the ack into a summary with a Save button
    bot.worker_ref.process(item["id"])
    edit = bot.http_fake.sent("editMessageText")[-1]
    assert "Submit DBMS assignment" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"save:{item['id']}"
    assert bot.store.data["offset"] == 8


def test_save_button_creates_events(bot, box):
    pair(bot)
    bot.handle_update(msg("Quiz", mid=3))
    item_id = box.list()[0]["id"]
    bot.worker_ref.process(item_id)
    bot.handle_update({"update_id": 4, "callback_query": {"id": "cq1", "data": f"save:{item_id}",
                       "message": {"message_id": 101, "chat": {"id": 42, "type": "private"}}}})
    assert bot.cal.calls and box.get(item_id)["status"] == "saved"
    assert "Saved 1" in bot.http_fake.sent("editMessageText")[-1]["text"]
    assert bot.http_fake.sent("answerCallbackQuery")[-1]["text"] == "Saved!"


def test_save_button_from_stranger_rejected(bot, box):
    pair(bot)
    item_id = ready_item(box)
    bot.handle_update({"update_id": 9, "callback_query": {"id": "cq2", "data": f"save:{item_id}",
                       "message": {"message_id": 5, "chat": {"id": 666, "type": "private"}}}})
    assert bot.cal.calls == []


def test_non_text_and_groups(bot, box):
    pair(bot)
    bot.handle_update({"update_id": 11, "message": {"message_id": 11, "chat": {"id": 42, "type": "private"}, "date": 1}})
    assert "only read text" in bot.http_fake.sent()[-1]["text"]
    bot.handle_update({"update_id": 12, "message": {"message_id": 12, "chat": {"id": 42, "type": "group"}, "text": "x"}})
    assert box.list() == []


def test_disconnect_clears(bot):
    pair(bot)
    bot.disconnect()
    assert bot.store.data is None and not bot.status()["configured"]


# ------------------------------------------------------------------ dashboard API
@pytest.fixture
def app_client(tmp_path, box):
    cfg = Config(data_dir=str(tmp_path), port=8765)
    cal = FakeCalendar()
    worker = inbox_mod.InboxWorker(box, lambda text, ref: {"tasks": [dict(TASK)], "warnings": []})
    tg = mock.Mock()
    tg.status.return_value = {"configured": False, "paired": False}
    services = Services(inbox=box, worker=worker, settings=SettingsStore(str(tmp_path / "s.json")), telegram=tg)
    auth = mock.Mock()
    auth.is_configured.return_value = True
    auth.is_connected.return_value = True
    app = server.create_app(cfg, extract_fn=mock.Mock(), calendar=cal, auth=auth,
                            ollama_session=mock.Mock(), services=services)
    c = app.test_client()
    c.cal, c.worker = cal, worker
    return c


APP_HEADERS = {"Origin": "http://127.0.0.1:8765", "X-DDay-Client": "app"}


def call(c, method, path, body=None, headers=APP_HEADERS):
    return getattr(c, method)(path, data=json.dumps(body) if body is not None else None,
                              content_type="application/json", headers=headers, base_url="http://127.0.0.1:8765")


def test_dashboard_page_served_with_csp(app_client):
    r = app_client.get("/app/", base_url="http://127.0.0.1:8765")
    assert r.status_code == 200 and b"D-Day" in r.data
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert app_client.get("/app/base.css", base_url="http://127.0.0.1:8765").status_code == 200
    assert app_client.get("/app/icons/icon48.png", base_url="http://127.0.0.1:8765").status_code == 200
    assert app_client.get("/app/../server.py", base_url="http://127.0.0.1:8765").status_code == 404


def test_dashboard_origin_allowed_but_websites_blocked(app_client):
    assert call(app_client, "get", "/api/inbox").status_code == 200
    bad = {"Origin": "https://evil.example", "X-DDay-Client": "app"}
    assert call(app_client, "get", "/api/inbox", headers=bad).status_code == 403
    assert call(app_client, "get", "/api/inbox", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 403


def test_paste_edit_save_flow(app_client, box):
    r = call(app_client, "post", "/api/inbox", {"text": "Submit DBMS assignment by Friday"})
    item_id = r.get_json()["id"]
    app_client.worker.process(item_id)
    items = call(app_client, "get", "/api/inbox").get_json()["items"]
    assert items[0]["status"] == "ready"
    r = call(app_client, "post", f"/api/inbox/{item_id}/tasks/t1", {"title": "Submit DBMS assignment 1"})
    assert r.get_json()["task"]["title"] == "Submit DBMS assignment 1"
    assert call(app_client, "post", f"/api/inbox/{item_id}/save", {}).status_code == 400  # needs confirmed
    r = call(app_client, "post", f"/api/inbox/{item_id}/save", {"confirmed": True})
    assert r.get_json()["status"] == "saved" and app_client.cal.calls
    # saved items leave the open inbox
    assert call(app_client, "get", "/api/inbox").get_json()["items"] == []


def test_dismiss_retry_and_settings(app_client, box):
    item_id = call(app_client, "post", "/api/inbox", {"text": "hello"}).get_json()["id"]
    assert call(app_client, "post", f"/api/inbox/{item_id}/dismiss").status_code == 200
    assert box.get(item_id)["status"] == "dismissed"
    assert call(app_client, "post", f"/api/inbox/{item_id}/retry").status_code == 200
    assert call(app_client, "post", "/api/inbox", {"text": "  "}).status_code == 400
    r = call(app_client, "post", "/api/settings", {"user_name": "Mohit", "timezone": "Asia/Kolkata"})
    assert r.get_json()["settings"]["user_name"] == "Mohit"
    assert call(app_client, "post", "/api/settings", {"timezone": "Mars/Base"}).status_code == 400


def test_autostart_toggle_api(app_client, monkeypatch, tmp_path):
    import autostart
    monkeypatch.setenv("APPDATA", str(tmp_path))
    created = []

    def fake_run(cmd, **kw):
        os_path = autostart.link_path()
        import os
        os.makedirs(os.path.dirname(os_path), exist_ok=True)
        open(os_path, "w").close()
        created.append(cmd)
    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    monkeypatch.setattr(autostart, "supported", lambda: True)
    r = call(app_client, "post", "/api/autostart", {"enabled": True})
    assert r.get_json()["enabled"] is True and "--start" in created[0][-1]
    r = call(app_client, "post", "/api/autostart", {"enabled": False})
    assert r.get_json()["enabled"] is False


def test_telegram_retries_transient_network_errors(bot, monkeypatch):
    import requests as rq
    import telegram_bot
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    real_post = bot.http_fake.post
    fails = {"n": 2}

    def flaky(url, json=None, timeout=None):
        if fails["n"]:
            fails["n"] -= 1
            raise rq.exceptions.ConnectionError("blip")
        return real_post(url, json=json, timeout=timeout)
    bot.http_fake.post = flaky
    assert bot.set_token(TOKEN)["configured"] is True  # succeeded on the 3rd try


def test_telegram_error_names_the_reason(bot, monkeypatch):
    import requests as rq
    import telegram_bot
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)

    def always_ssl(url, json=None, timeout=None):
        raise rq.exceptions.SSLError("bad cert")
    bot.http_fake.post = always_ssl
    with pytest.raises(TelegramError) as exc:
        bot.set_token(TOKEN)
    assert "SSL" in str(exc.value)


def test_telegram_rate_limit_reports_retry_after(bot):
    def limited(url, json=None, timeout=None):
        resp = mock.Mock(status_code=429)
        resp.json.return_value = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 32",
                                  "parameters": {"retry_after": 32}}
        return resp
    bot.http_fake.post = limited
    with pytest.raises(TelegramError) as exc:
        bot._call("getMe", token=TOKEN)
    assert exc.value.retry_after == 32 and "32 seconds" in str(exc.value)


def test_rate_limit_api_returns_429_with_wait(app_client):
    app_client.application.config["DDAY_SERVICES"].telegram.set_token.side_effect = TelegramError("wait", retry_after=32)
    r = call(app_client, "post", "/api/telegram/token", {"token": TOKEN})
    assert r.status_code == 429 and r.get_json()["error"]["retry_after"] == 32


def test_slow_or_throttled_check_still_connects(bot, monkeypatch):
    import requests as rq
    import telegram_bot
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def slow(url, json=None, timeout=None):
        calls["n"] += 1
        raise rq.exceptions.ReadTimeout("no answer")
    bot.http_fake.post = slow
    st = bot.set_token(TOKEN)
    assert calls["n"] == 1                 # a read timeout is not retried
    assert st["configured"] and len(st["pair_code"]) == 6 and st["bot_username"] is None
    assert "background" in st["last_error"]


def test_throttled_check_still_connects(bot):
    def limited(url, json=None, timeout=None):
        resp = mock.Mock(status_code=429)
        resp.json.return_value = {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 30}}
        return resp
    bot.http_fake.post = limited
    assert bot.set_token(TOKEN)["configured"] is True


def test_rejected_token_still_refused(bot):
    def unauthorized(url, json=None, timeout=None):
        resp = mock.Mock(status_code=401)
        resp.json.return_value = {"ok": False, "description": "Unauthorized"}
        return resp
    bot.http_fake.post = unauthorized
    with pytest.raises(TelegramError):
        bot.set_token(TOKEN)
    assert not bot.status()["configured"]


def test_conflict_with_other_program_is_explained(bot):
    bot.set_token(TOKEN)

    def conflict(url, json=None, timeout=None):
        resp = mock.Mock(status_code=409)
        resp.json.return_value = {"ok": False, "error_code": 409,
                                  "description": "Conflict: can't use getUpdates method while webhook is active"}
        return resp
    bot.http_fake.post = conflict
    with pytest.raises(TelegramError) as exc:
        bot._get_updates(bot.store.data)
    assert "another program" in str(exc.value) and exc.value.retry_after == 60


def test_new_listener_starts_even_if_old_one_is_finishing(box):
    worker = inbox_mod.InboxWorker(box, lambda text, ref: {"tasks": [], "warnings": []})
    b = TelegramBot(MemStore({"token": TOKEN}), box, worker, lambda i: {}, session=FakeHTTP())
    started = []
    b._run = lambda generation=0: started.append(generation)
    import threading
    alive = mock.Mock(is_alive=lambda: True)
    b._thread = alive
    b._stop.set()                       # "Use a different bot" stopped the old listener...
    b.start()                           # ...which is still finishing a request
    b._thread.join(timeout=2)
    assert started == [1] and not b._stop.is_set()


# ---------------------------------------------------------------- phone share app
CID = "1008780003887-abc123def456.apps.googleusercontent.com"


def test_share_setup_link_and_qr(app_client):
    r = call(app_client, "get", "/api/share")
    assert r.get_json()["setup_url"] is None
    assert app_client.get("/app/share-qr.svg", base_url="http://127.0.0.1:8765").status_code == 404
    bad = call(app_client, "post", "/api/share", {"share_client_id": "not-a-client-id"})
    assert bad.status_code == 400
    r = call(app_client, "post", "/api/share", {"share_client_id": CID})
    info = r.get_json()
    assert info["setup_url"] == f"https://mohitahujaa.github.io/D-Day/#setup={CID}"
    assert info["app_origin"] == "https://mohitahujaa.github.io"
    # loaded like an <img>: no X-DDay-Client header, no Origin
    qr = app_client.get("/app/share-qr.svg", base_url="http://127.0.0.1:8765")
    assert qr.status_code == 200 and qr.mimetype == "image/svg+xml" and b"<svg" in qr.data
