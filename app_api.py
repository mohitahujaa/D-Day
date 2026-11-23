"""
Dashboard + inbox + Telegram routes for the local D-Day service.

The dashboard is a plain local web page served at http://127.0.0.1:<port>/app
(works offline; installable as an app from Chrome's menu).
"""

import os
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from flask import Flask, jsonify, request, send_from_directory, abort

import autostart
import calendar_client
from gtasks_source import GoogleTasksSource
import inbox as inbox_mod
import pipeline
from settings_store import SettingsStore
from telegram_bot import TelegramBot, TelegramError
from token_store import TokenStore

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
EXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extension")
STATIC_TYPES = {".html", ".js", ".css", ".png", ".svg", ".webmanifest", ".ico"}


@dataclass
class Services:
    inbox: inbox_mod.Inbox
    worker: inbox_mod.InboxWorker
    settings: SettingsStore
    telegram: Optional[TelegramBot]
    gtasks: Optional[GoogleTasksSource] = None

    def start(self) -> None:
        self.inbox.purge_old()
        self.worker.start()
        if self.telegram:
            self.telegram.start()
        if self.gtasks:
            self.gtasks.start()


def build_services(data_dir: str, calendar, extract_fn=None, port: int = 8765, auth=None) -> Services:
    os.makedirs(data_dir, exist_ok=True)
    settings = SettingsStore(os.path.join(data_dir, "settings.json"))
    inbox = inbox_mod.Inbox(os.path.join(data_dir, "inbox.db"))

    def extract(text: str, reference_time: Optional[str]) -> Dict[str, Any]:
        s = settings.get()
        ref = datetime.fromisoformat(reference_time) if reference_time else None
        return pipeline.extract_tasks(text, reference_datetime=ref, timezone=s["timezone"],
                                      untimed_deadline=s["untimed_deadline"], extract_fn=extract_fn,
                                      reader=s["user_name"] or None, auto_resolve=s["auto_resolve"])

    worker = inbox_mod.InboxWorker(inbox, extract)

    def save(item_id: int) -> Dict[str, Any]:
        return inbox_mod.save_item(inbox, calendar, item_id, {"timezone": settings.get()["timezone"]})

    telegram = TelegramBot(TokenStore(os.path.join(data_dir, "telegram.bin")), inbox, worker, save,
                           dashboard_url=f"http://127.0.0.1:{port}/app")
    gtasks = GoogleTasksSource(auth, inbox, worker, os.path.join(data_dir, "gtasks.json")) if auth else None
    return Services(inbox=inbox, worker=worker, settings=settings, telegram=telegram, gtasks=gtasks)


def register(app: Flask, services: Services, calendar, error: Callable, ledger_path: str) -> None:

    def body() -> Dict[str, Any]:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def cal_settings() -> Dict[str, Any]:
        return {"timezone": services.settings.get()["timezone"]}

    # ---------------------------------------------------------------- pages
    @app.get("/app")
    @app.get("/app/")
    def app_index():
        return send_from_directory(APP_DIR, "index.html")

    @app.get("/app/<path:name>")
    def app_static(name):
        if name == "base.css":
            return send_from_directory(EXT_DIR, "styles.css")
        if name.startswith("icons/"):
            return send_from_directory(EXT_DIR, name)
        if os.path.splitext(name)[1] not in STATIC_TYPES or "/" in name or "\\" in name:
            abort(404)
        resp = send_from_directory(APP_DIR, name)
        if name == "sw.js":
            resp.headers["Service-Worker-Allowed"] = "/app/"
        return resp

    # ---------------------------------------------------------------- inbox
    @app.get("/api/inbox")
    def inbox_list():
        which = request.args.get("status", "open")
        statuses = inbox_mod.OPEN_STATUSES if which == "open" else None
        return jsonify({"ok": True, "items": services.inbox.list(statuses, limit=100),
                        "counts": services.inbox.counts()})

    @app.post("/api/inbox")
    def inbox_add():
        text = body().get("text")
        if not isinstance(text, str) or not text.strip():
            return error("empty_selection", "Paste a message first.", 400)
        if len(text) > 20000:
            return error("too_large", "Message is longer than 20,000 characters.", 413)
        item_id = services.inbox.add("paste", text, reference_time=body().get("reference_time"))
        services.worker.enqueue(item_id)
        return jsonify({"ok": True, "id": item_id}), 201

    @app.get("/api/inbox/<int:item_id>")
    def inbox_get(item_id):
        item = services.inbox.get(item_id)
        return jsonify({"ok": True, "item": item}) if item else error("not_found", "No such item.", 404)

    @app.post("/api/inbox/<int:item_id>/tasks/<task_id>")
    def inbox_edit(item_id, task_id):
        try:
            task = services.inbox.edit_task(item_id, task_id, body())
        except KeyError as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_task", str(exc), 400)
        return jsonify({"ok": True, "task": task})

    @app.post("/api/inbox/<int:item_id>/save")
    def inbox_save(item_id):
        if body().get("confirmed") is not True:
            return error("not_confirmed", "Events are only created after the user confirms.", 400)
        ids = body().get("task_ids")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            return error("bad_request", "task_ids must be a list of ids.", 400)
        try:
            res = inbox_mod.save_item(services.inbox, calendar, item_id, cal_settings(), ids)
        except KeyError as exc:
            return error("not_found", str(exc), 404)
        return jsonify({"ok": True, **res})

    @app.post("/api/inbox/<int:item_id>/dismiss")
    def inbox_dismiss(item_id):
        if not services.inbox.get(item_id):
            return error("not_found", "No such item.", 404)
        services.inbox.update(item_id, status="dismissed")
        return jsonify({"ok": True})

    @app.post("/api/inbox/<int:item_id>/retry")
    def inbox_retry(item_id):
        if not services.inbox.get(item_id):
            return error("not_found", "No such item.", 404)
        services.inbox.update(item_id, status="queued", error=None)
        services.worker.enqueue(item_id)
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- history
    @app.get("/api/history")
    def history():
        try:
            with open(ledger_path, encoding="utf-8") as fh:
                entries = list((json.load(fh).get("by_key") or {}).values())
        except (OSError, ValueError):
            entries = []
        entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        return jsonify({"ok": True, "events": entries[:100]})

    # ---------------------------------------------------------------- settings
    @app.get("/api/settings")
    def settings_get():
        return jsonify({"ok": True, "settings": services.settings.get()})

    @app.post("/api/settings")
    def settings_set():
        try:
            return jsonify({"ok": True, "settings": services.settings.update(body())})
        except ValueError as exc:
            return error("invalid_setting", str(exc), 400)

    @app.get("/api/autostart")
    def autostart_get():
        return jsonify({"ok": True, "supported": autostart.supported(), "enabled": autostart.is_enabled()})

    @app.post("/api/autostart")
    def autostart_set():
        try:
            autostart.enable() if body().get("enabled") else autostart.disable()
        except Exception as exc:
            return error("autostart_failed", f"Couldn't change the startup setting: {exc}", 500)
        return jsonify({"ok": True, "supported": autostart.supported(), "enabled": autostart.is_enabled()})

    # ---------------------------------------------------------------- phone share app
    def share_info() -> Dict[str, Any]:
        s = services.settings.get()
        url = s["share_app_url"]
        cid = s["share_client_id"]
        return {"client_id": cid, "app_url": url, "app_origin": "/".join(url.split("/")[:3]),
                "setup_url": f"{url}#setup={cid}" if cid else None}

    @app.get("/api/share")
    def share_get():
        return jsonify({"ok": True, **share_info()})

    @app.post("/api/share")
    def share_set():
        patch = {k: v for k, v in body().items() if k in ("share_client_id", "share_app_url")}
        try:
            services.settings.update(patch)
        except ValueError as exc:
            return error("invalid_setting", str(exc), 400)
        return jsonify({"ok": True, **share_info()})

    @app.get("/api/share/qr.svg")
    def share_qr():
        info = share_info()
        if not info["setup_url"]:
            return error("not_configured", "Add the share app's client ID first.", 404)
        import io
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(info["setup_url"], image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return app.response_class(buf.getvalue(), mimetype="image/svg+xml")

    # ---------------------------------------------------------------- google tasks (phone inbox)
    @app.get("/api/gtasks")
    def gtasks_status():
        if not services.gtasks:
            return error("not_available", "Google Tasks isn't available.", 404)
        return jsonify({"ok": True, **services.gtasks.status()})

    @app.post("/api/gtasks")
    def gtasks_set():
        if not services.gtasks:
            return error("not_available", "Google Tasks isn't available.", 404)
        return jsonify({"ok": True, **services.gtasks.set_enabled(body().get("enabled", True) is not False)})

    @app.post("/api/gtasks/check")
    def gtasks_check():
        if not services.gtasks:
            return error("not_available", "Google Tasks isn't available.", 404)
        services.gtasks.check_now()
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- telegram
    @app.get("/api/telegram")
    def telegram_status():
        return jsonify({"ok": True, **services.telegram.status()})

    @app.post("/api/telegram/token")
    def telegram_token():
        try:
            return jsonify({"ok": True, **services.telegram.set_token(body().get("token", ""))})
        except TelegramError as exc:
            if exc.retry_after:
                return error("telegram_rate_limited", str(exc), 429, retry_after=exc.retry_after)
            return error("telegram_error", str(exc), 400)

    @app.post("/api/telegram/disconnect")
    def telegram_disconnect():
        services.telegram.disconnect()
        return jsonify({"ok": True})
