"""
D-Day local service.

    python server.py            # listens on http://127.0.0.1:8765

Endpoints (JSON, called by the browser extension):
    GET  /api/health             service / Ollama / Google status
    POST /api/extract            selected text -> reviewable tasks (local LLM)
    POST /api/events/preview     reviewed task -> exact Calendar payload (no API call)
    POST /api/events             reviewed + confirmed task -> Google Calendar event
    POST /api/auth/disconnect    revoke and delete the stored Google token
Browser pages:
    GET  /                       status page
    GET  /auth/start             begin Google sign-in
    GET  /oauth/callback         OAuth redirect target

Security: binds to 127.0.0.1 only; rejects foreign Host headers (DNS
rebinding) and any /api call that is not from a browser-extension origin
carrying the X-DDay-Client header (blocks drive-by requests from websites).
Selected text and tokens are never logged.
"""

import html
import logging
import logging.handlers
import os
import re
import sys
import threading
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import Flask, jsonify, redirect, request

import app_api
import calendar_client
import extractor
import pipeline
from config import Config
from token_store import TokenStore

log = logging.getLogger("dday")

MAX_SELECTION_CHARS = 20000
EXTENSION_ORIGIN_RE = re.compile(r"^(chrome|moz|ms-browser)-extension://([a-z0-9-]+)/?$", re.IGNORECASE)


def _error(code: str, message: str, status: int, **extra):
    body = {"ok": False, "error": {"code": code, "message": message, **extra}}
    return jsonify(body), status


def unload_model(session: Optional[requests.Session] = None) -> None:
    """Ask Ollama to drop the model from RAM now (keep_alive=0). Best effort."""
    try:
        (session or requests).post(extractor.OLLAMA_URL,
                                   json={"model": extractor.MODEL, "keep_alive": 0}, timeout=5)
    except Exception:
        pass


def create_app(config: Optional[Config] = None, extract_fn=None, calendar=None,
               auth: Optional[calendar_client.GoogleAuth] = None,
               ollama_session: Optional[requests.Session] = None,
               exit_fn=None, services: Optional["app_api.Services"] = None) -> Flask:
    config = config or Config()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.config["JSON_SORT_KEYS"] = False

    if auth is None:
        auth = calendar_client.GoogleAuth(config.resolve_client_secrets, TokenStore(config.token_path))
    if calendar is None:
        calendar = calendar_client.CalendarClient(auth, calendar_client.EventLedger(config.ledger_path))
    ollama_http = ollama_session or requests.Session()

    allowed_hosts = {f"127.0.0.1:{config.port}", f"localhost:{config.port}", "127.0.0.1", "localhost"}
    # The dashboard is served by this service, so its own origin may call the API.
    own_origins = {f"http://127.0.0.1:{config.port}", f"http://localhost:{config.port}"}

    def origin_allowed(origin: Optional[str]) -> bool:
        if not origin:
            return False
        if origin in own_origins:
            return True
        m = EXTENSION_ORIGIN_RE.match(origin)
        if not m:
            return False
        return not config.allowed_extension_ids or m.group(2) in config.allowed_extension_ids

    # ------------------------------------------------------------------ guards
    @app.before_request
    def guard():
        if request.host not in allowed_hosts:
            return _error("forbidden_host", "Requests must be addressed to 127.0.0.1.", 403)
        if not request.path.startswith("/api/"):
            return None
        origin = request.headers.get("Origin")
        if request.method == "OPTIONS":
            if origin_allowed(origin):
                return app.response_class(status=204)
            return _error("forbidden_origin", "Origin not allowed.", 403)
        if origin and not origin_allowed(origin):
            return _error("forbidden_origin", "Only the D-Day extension may call this API.", 403)
        if request.headers.get("X-DDay-Client") not in ("extension", "app"):
            return _error("forbidden_client", "Missing X-DDay-Client header.", 403)
        return None

    @app.after_request
    def headers(resp):
        origin = request.headers.get("Origin")
        if request.path.startswith("/api/") and origin_allowed(origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-DDay-Client"
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
            resp.headers["Access-Control-Max-Age"] = "600"
            resp.headers["Vary"] = "Origin"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if not request.path.startswith("/app"):
            resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/app"):
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'")
            if request.path.endswith((".js", ".css", ".png", ".webmanifest")):
                resp.headers["Cache-Control"] = "no-cache"
        elif resp.mimetype == "text/html":
            resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        log.info("%s %s -> %s", request.method, request.path, resp.status_code)
        return resp

    @app.errorhandler(413)
    def too_large(_):
        return _error("too_large", "Selection is too large.", 413)

    @app.errorhandler(Exception)
    def unhandled(exc):
        if hasattr(exc, "code") and isinstance(getattr(exc, "code"), int):
            return _error("http_error", str(exc), exc.code)
        log.exception("Unhandled error")
        return _error("internal_error", "Unexpected error in the D-Day service. See its console.", 500)

    # ------------------------------------------------------------------ helpers
    def json_body() -> Dict[str, Any]:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def ollama_status() -> Dict[str, Any]:
        try:
            r = ollama_http.get(extractor.OLLAMA_BASE_URL + "/api/tags", timeout=2)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            have = extractor.MODEL in names or f"{extractor.MODEL}:latest" in names
            return {"reachable": True, "model": extractor.MODEL, "model_installed": have}
        except Exception:
            return {"reachable": False, "model": extractor.MODEL, "model_installed": False}

    # ------------------------------------------------------------------ API
    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "d-day",
            "version": "1.0.0",
            "ollama": ollama_status(),
            "google": {"configured": bool(auth.is_configured()), "connected": bool(auth.is_connected())},
            "default_timezone": config.default_timezone,
            "auth_url": f"http://{config.host}:{config.port}/auth/start",
        })

    @app.post("/api/extract")
    def extract():
        body = json_body()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error("empty_selection", "No text was selected. Select some text and try again.", 400)
        if len(text) > MAX_SELECTION_CHARS:
            return _error("too_large", f"Selection is longer than {MAX_SELECTION_CHARS} characters.", 413)
        tz_name = body.get("timezone") or config.default_timezone
        ref = None
        if body.get("reference_time"):
            try:
                ref = datetime.fromisoformat(str(body["reference_time"]).replace("Z", "+00:00"))
            except ValueError:
                return _error("bad_reference_time", "reference_time must be ISO 8601.", 400)
        untimed = body.get("untimed_deadline") if body.get("untimed_deadline") in ("all_day", "end_of_day") else "all_day"
        log.info("extract: %d chars", len(text))
        try:
            reader = body.get("reader") if isinstance(body.get("reader"), str) else None
            result = pipeline.extract_tasks(text, reference_datetime=ref, timezone=tz_name,
                                            untimed_deadline=untimed, extract_fn=extract_fn,
                                            reader=(reader or "").strip()[:60] or None,
                                            auto_resolve=body.get("auto_resolve") is not False)
        except extractor.ExtractionError as exc:
            status = {"ollama_unavailable": 503, "model_not_found": 503,
                      "ollama_timeout": 504}.get(exc.code, 502)
            return _error(exc.code, str(exc), status)
        return jsonify({"ok": True, **result})

    @app.post("/api/events/preview")
    def preview():
        body = json_body()
        try:
            built = calendar_client.build_event_payload(body.get("task"), body.get("settings"))
        except calendar_client.InvalidTask as exc:
            return _error(exc.code, str(exc), 400)
        return jsonify({"ok": True, **built})

    @app.post("/api/events")
    def create_event():
        body = json_body()
        if body.get("confirmed") is not True:
            return _error("not_confirmed", "Events are only created after the user confirms.", 400)
        try:
            result = calendar.create_event(
                body.get("task"), body.get("settings"),
                idempotency_key=body.get("idempotency_key"),
                allow_duplicate=body.get("allow_duplicate") is True,
            )
        except calendar_client.CalendarError as exc:
            extra = {k: v for k, v in exc.extra.items() if k in ("html_link", "event_id")}
            if exc.code == "auth_required" or exc.code == "google_not_configured":
                extra["auth_url"] = f"http://{config.host}:{config.port}/auth/start"
            return _error(exc.code, str(exc), exc.http_status, **extra)
        status = 201 if result["status"] == "created" else 200
        return jsonify({"ok": True, **result}), status

    @app.get("/api/events")
    def list_events():
        """Upcoming D-Day events, read live from Google Calendar."""
        try:
            items = calendar.list_upcoming(request.args.get("calendar_id") or "primary")
        except calendar_client.CalendarError as exc:
            extra = {}
            if exc.code in ("auth_required", "google_not_configured"):
                extra["auth_url"] = f"http://{config.host}:{config.port}/auth/start"
            return _error(exc.code, str(exc), exc.http_status, **extra)
        return jsonify({"ok": True, "events": items})

    if services is None:
        services = app_api.build_services(config.data_dir, calendar, extract_fn, config.port, auth=auth)
    app.config["DDAY_SERVICES"] = services
    app_api.register(app, services, calendar, _error, config.ledger_path)

    @app.post("/api/shutdown")
    def shutdown():
        """Turn D-Day off from the extension: free the model's RAM and stop this service."""
        unload_model(ollama_http)
        log.info("Shutting down on request from the extension")
        threading.Timer(0.5, exit_fn or (lambda: os._exit(0))).start()
        return jsonify({"ok": True, "stopping": True})

    @app.post("/api/auth/disconnect")
    def disconnect():
        auth.disconnect()
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ pages
    def page(title: str, message: str, ok: bool = True, status: int = 200):
        color = "#137333" if ok else "#b3261e"
        body = (
            "<!doctype html><meta charset='utf-8'><title>D-Day</title>"
            "<body style='font:15px system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1f1f1f'>"
            f"<h2 style='color:{color}'>{html.escape(title)}</h2><p>{html.escape(message)}</p>"
            "<p style='color:#5f6368'>You can close this tab.</p></body>"
        )
        return app.response_class(body, status=status, mimetype="text/html")

    @app.get("/")
    def index():
        ost = ollama_status()
        g = "connected" if auth.is_connected() else ("not connected" if auth.is_configured() else "not configured")
        o = "running" if ost["reachable"] else "not reachable"
        if ost["reachable"] and not ost["model_installed"]:
            o += f" (model {ost['model']} missing)"
        return page("D-Day service is running",
                    f"Ollama: {o}. Google Calendar: {g}. Default timezone: {config.default_timezone}.")

    @app.get("/auth/start")
    def auth_start():
        try:
            return redirect(auth.authorization_url(config.redirect_uri))
        except calendar_client.CalendarError as exc:
            return page("Google Calendar is not set up", str(exc), ok=False, status=503)

    @app.get("/oauth/callback")
    def oauth_callback():
        try:
            auth.handle_callback(request.args.get("state"), request.args.get("code"), request.args.get("error"))
        except calendar_client.CalendarError as exc:
            return page("Could not connect Google Calendar", str(exc), ok=False, status=400)
        return page("Google Calendar connected",
                    "D-Day can now add events you confirm. Return to the D-Day window and press Save again.")

    return app


def main():
    config = Config()
    os.makedirs(config.data_dir, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(
        os.path.join(config.data_dir, "service.log"), maxBytes=512 * 1024, backupCount=1, encoding="utf-8")]
    if sys.stderr is not None:  # None when started windowless (pythonw)
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app(config)
    app.config["DDAY_SERVICES"].start()  # inbox worker + Telegram polling
    print(f"D-Day service on http://{config.host}:{config.port}  (data: {config.data_dir})")
    print(f"Dashboard: http://{config.host}:{config.port}/app")
    print(f"Model: {extractor.MODEL} via {extractor.OLLAMA_BASE_URL}")
    secrets_path = config.resolve_client_secrets()
    print("Google OAuth client:", secrets_path or "NOT CONFIGURED (see README)")
    try:
        from waitress import serve  # optional, more robust than the dev server
        serve(app, host=config.host, port=config.port, threads=4)
    except ImportError:
        app.run(host=config.host, port=config.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
