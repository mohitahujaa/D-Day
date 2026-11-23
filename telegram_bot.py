"""
Telegram bot: send or forward messages from your phone into the D-Day inbox.

* The PC polls Telegram (long polling): no open ports, no server, free.
* Only ONE Telegram account can use the bot: it must first send the 6-digit
  pairing code shown in the dashboard. Everyone else is ignored.
* For each message the bot replies with what it found and a
  "Save to Calendar" button; pressing it is the user's confirmation.
* The bot token is stored encrypted (DPAPI) like the Google token.

Telegram keeps undelivered updates for about 24 hours, so messages sent while
the PC is off are picked up when it comes back within a day.
"""

import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import requests

from token_store import TokenStore

log = logging.getLogger("dday.telegram")
API = "https://api.telegram.org"
MAX_MSG = 3900


class TelegramError(Exception):
    def __init__(self, message: str, retry_after: Optional[int] = None, slow: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.slow = slow  # the request reached Telegram but the answer didn't come back in time


class TelegramBot:
    def __init__(self, store: TokenStore, inbox, worker, save_fn: Callable[[int], Dict[str, Any]],
                 session: Optional[requests.Session] = None, api_base: str = API,
                 dashboard_url: str = "http://127.0.0.1:8765/app"):
        self.store = store
        self.inbox = inbox
        self.worker = worker
        self.save_fn = save_fn
        self.http = session or requests.Session()
        self.api_base = api_base
        self.dashboard_url = dashboard_url
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._generation = 0  # bumped on every start; an old listener exits when it sees a newer one
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None
        worker.listeners.append(self.on_item_processed)

    # ---------------------------------------------------------------- config
    def _cfg(self) -> Dict[str, Any]:
        return self.store.load() or {}

    def _save_cfg(self, cfg: Dict[str, Any]) -> None:
        self.store.save(cfg)

    def status(self) -> Dict[str, Any]:
        cfg = self._cfg()
        out = {
            "configured": bool(cfg.get("token")),
            "paired": bool(cfg.get("chat_id")),
            "bot_username": cfg.get("bot_username"),
            "bot_id": (cfg.get("token") or "").split(":")[0] or None,  # public part of the token
            "running": bool(self._thread and self._thread.is_alive()),
            "last_error": self.last_error,
        }
        if cfg.get("token") and not cfg.get("chat_id"):
            out["pair_code"] = cfg.get("pair_code")
            if cfg.get("bot_username"):
                out["pair_link"] = f"https://t.me/{cfg['bot_username']}?start={cfg.get('pair_code')}"
        return out

    def set_token(self, token: str) -> Dict[str, Any]:
        token = (token or "").strip()
        if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,50}", token):
            raise TelegramError("That doesn't look like a bot token. Copy the whole token BotFather sent "
                                "(it looks like 123456789:AA...).")
        note = None
        try:
            me = self._call("getMe", token=token, timeout=25) or {}
        except TelegramError as exc:
            # 429 means Telegram recognised the token; a slow answer means it reached
            # Telegram. Either way, finish connecting in the background instead of
            # making the user click again (which only adds more requests).
            if not (exc.retry_after or exc.slow):
                raise
            me = {}
            note = "Telegram is slow right now; D-Day will finish connecting in the background."
        cfg = {"token": token, "bot_username": me.get("username"), "chat_id": None,
               "pair_code": f"{secrets.randbelow(10**6):06d}", "offset": 0}
        self._save_cfg(cfg)
        self.last_error = note
        self.start()
        return self.status()

    def disconnect(self) -> None:
        self.stop()
        self.store.clear()
        self.last_error = None

    # ---------------------------------------------------------------- HTTP
    def _call(self, method: str, token: Optional[str] = None, timeout: float = 15, **params) -> Any:
        token = token or self._cfg().get("token")
        if not token:
            raise TelegramError("Telegram is not set up.")
        resp, last = None, None
        for attempt in range(3):
            try:
                resp = self.http.post(f"{self.api_base}/bot{token}/{method}", json=params, timeout=timeout)
                break
            except requests.exceptions.RequestException as exc:
                last = exc
                log.warning("Telegram %s failed (attempt %d): %s", method, attempt + 1, type(exc).__name__)
                if isinstance(exc, requests.exceptions.ReadTimeout):
                    break  # it reached Telegram; repeating it now only triggers "too many requests"
                time.sleep(1.5 * (attempt + 1))
        if resp is None:
            reason = {
                requests.exceptions.SSLError: "a secure-connection (SSL) error, often caused by antivirus or a proxy",
                requests.exceptions.ProxyError: "a proxy error",
                requests.exceptions.ConnectTimeout: "a timeout connecting to api.telegram.org",
                requests.exceptions.ReadTimeout: "Telegram taking too long to answer",
            }.get(type(last), "a connection error")
            raise TelegramError(f"Can't reach Telegram ({reason}). Check your internet connection and try again.",
                                slow=isinstance(last, requests.exceptions.ReadTimeout))
        log.info("Telegram %s -> HTTP %s", method, resp.status_code)  # never log the token
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"Telegram returned HTTP {resp.status_code}.")
        if not body.get("ok"):
            if resp.status_code == 401:
                raise TelegramError("Telegram rejected the bot token. Create a new token with BotFather.")
            if resp.status_code == 429:
                wait = int((body.get("parameters") or {}).get("retry_after") or 30)
                raise TelegramError(f"Telegram asked D-Day to wait {wait} seconds (too many requests).", retry_after=wait)
            raise TelegramError(f"Telegram error: {str(body.get('description', ''))[:200]}")
        return body.get("result")

    def send(self, chat_id: int, text: str, buttons=None, reply_to: Optional[int] = None) -> Optional[int]:
        params: Dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_MSG], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return (self._call("sendMessage", **params) or {}).get("message_id")
        except TelegramError as exc:
            self.last_error = str(exc)
            return None

    def edit(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        params: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_MSG],
                                  "disable_web_page_preview": True,
                                  "reply_markup": {"inline_keyboard": buttons or []}}
        try:
            self._call("editMessageText", **params)
        except TelegramError as exc:
            if "not modified" not in str(exc):
                self.last_error = str(exc)

    # ---------------------------------------------------------------- polling
    def start(self) -> None:
        if not self._cfg().get("token"):
            return
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return  # already listening
        # A previous listener may still be finishing a request after "Use a different bot";
        # start a fresh one anyway. The old one notices the newer generation and exits.
        self._stop.clear()
        self._generation += 1
        self._thread = threading.Thread(target=self._run, args=(self._generation,), name="dday-telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, generation: int = 0) -> None:
        backoff = 5
        next_name_lookup = 0.0
        while not self._stop.is_set() and generation == self._generation:
            cfg = self._cfg()
            if not cfg.get("token"):
                return
            try:
                updates = self._get_updates(cfg)  # long poll: at most ~2 requests a minute when idle
                backoff = 5
                self.last_error = None
            except TelegramError as exc:
                self.last_error = str(exc)
                if exc.retry_after:
                    # Throttled: asking again as soon as allowed keeps resetting Telegram's timer,
                    # so wait longer each time (up to 3 minutes).
                    self.last_error = ("Telegram is limiting requests from this internet connection right now. "
                                       "D-Day keeps trying quietly in the background.")
                # Asking again right at the end of the wait can extend Telegram's limit, so
                # wait twice as long as asked, growing up to 5 minutes.
                self._stop.wait(max((exc.retry_after or 0) * 2, backoff))
                backoff = min(backoff * 2, 300)
                continue
            if generation != self._generation:
                return
            for upd in updates:
                self.handle_update(upd)
            # The bot's @name is only for display; look it up rarely, never in a tight loop.
            if not cfg.get("bot_username") and time.time() >= next_name_lookup:
                next_name_lookup = time.time() + 600
                try:
                    me = self._call("getMe", timeout=20) or {}
                    with self._lock:
                        cfg = self._cfg()
                        cfg["bot_username"] = me.get("username")
                        self._save_cfg(cfg)
                except TelegramError:
                    pass

    def _get_updates(self, cfg: Dict[str, Any]):
        token = cfg["token"]
        try:
            resp = self.http.post(f"{self.api_base}/bot{token}/getUpdates",
                                  json={"offset": cfg.get("offset", 0), "timeout": 30,
                                        "allowed_updates": ["message", "callback_query"]}, timeout=45)
            log.info("Telegram getUpdates -> HTTP %s", resp.status_code)
            body = resp.json()
        except requests.exceptions.ReadTimeout:
            raise TelegramError("Telegram is slow to answer; retrying.")
        except (requests.exceptions.RequestException, ValueError):
            raise TelegramError("Can't reach Telegram (no internet?).")
        if not body.get("ok"):
            wait = (body.get("parameters") or {}).get("retry_after")
            if resp.status_code == 409:
                raise TelegramError("This bot is already being used by another program (for example an n8n "
                                    "workflow or a webhook). Create a separate bot for D-Day with BotFather.",
                                    retry_after=60)
            if resp.status_code == 401:
                raise TelegramError("Telegram rejected the bot token. Create a new token with BotFather.",
                                    retry_after=300)
            raise TelegramError(f"Telegram error: {str(body.get('description', ''))[:200]}",
                                retry_after=int(wait) if wait else None)
        return body.get("result") or []

    def _advance(self, update_id: int) -> None:
        with self._lock:
            cfg = self._cfg()
            if update_id + 1 > cfg.get("offset", 0):
                cfg["offset"] = update_id + 1
                self._save_cfg(cfg)

    # ---------------------------------------------------------------- handling
    def handle_update(self, upd: Dict[str, Any]) -> None:
        try:
            if "message" in upd:
                self._on_message(upd["message"])
            elif "callback_query" in upd:
                self._on_callback(upd["callback_query"])
        except Exception:
            log.exception("Telegram update failed")
        finally:
            self._advance(int(upd.get("update_id", 0)))

    def _on_message(self, msg: Dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        chat_id = chat.get("id")
        cfg = self._cfg()
        text = (msg.get("text") or msg.get("caption") or "").strip()

        if not cfg.get("chat_id"):
            code = re.sub(r"^/start\s*", "", text).strip()
            if code and code == cfg.get("pair_code"):
                cfg["chat_id"] = chat_id
                cfg.pop("pair_code", None)
                self._save_cfg(cfg)
                self.send(chat_id, "✅ Paired with D-Day on your PC.\n\nForward or send me any message with a "
                                   "deadline (WhatsApp, SMS, email…). I'll find the tasks and you can save them "
                                   "to Google Calendar with one tap.")
            else:
                self.send(chat_id, "This D-Day bot is private. Send the 6-digit pairing code shown in the "
                                   "D-Day dashboard (Settings → Phone).")
            return
        if chat_id != cfg.get("chat_id"):
            return  # someone else: ignore silently

        if text in ("/start", "/help"):
            self.send(chat_id, "Forward or paste a message with a deadline and I'll add it to your D-Day inbox.\n"
                               "/pending: items waiting to be saved")
            return
        if text == "/pending":
            items = [i for i in self.inbox.list(("ready",), limit=10) if any(t.get("ui") != "saved" for t in i["tasks"])]
            if not items:
                self.send(chat_id, "Nothing waiting. 🎉")
            for it in items:
                self.send(chat_id, self._summary(it), buttons=self._buttons(it))
            return
        if not text:
            self.send(chat_id, "I can only read text for now. Send or forward the message as text.")
            return

        # When forwarded, use the original send time so "tomorrow" means the right day.
        origin = msg.get("forward_origin") or {}
        ts = origin.get("date") or msg.get("forward_date") or msg.get("date") or time.time()
        ref = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        item_id = self.inbox.add("telegram", text, reference_time=ref,
                                 meta={"chat_id": chat_id, "message_id": msg.get("message_id")})
        ack = self.send(chat_id, "📥 Got it. Reading it on your PC…", reply_to=msg.get("message_id"))
        self.inbox.update_meta(item_id, ack_message_id=ack)
        self.worker.enqueue(item_id)

    def _on_callback(self, cq: Dict[str, Any]) -> None:
        cfg = self._cfg()
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        answer = ""
        try:
            if not cfg.get("chat_id") or chat_id != cfg.get("chat_id"):
                answer = "Not allowed."
                return
            m = re.fullmatch(r"(save|dismiss):(\d+)", cq.get("data") or "")
            if not m:
                return
            action, item_id = m.group(1), int(m.group(2))
            item = self.inbox.get(item_id)
            if not item:
                answer = "That message is no longer in the inbox."
                return
            if action == "dismiss":
                self.inbox.update(item_id, status="dismissed")
                self.edit(chat_id, msg["message_id"], self._summary(self.inbox.get(item_id)) + "\n\n🗑 Dismissed.")
                answer = "Dismissed"
                return
            res = self.save_fn(item_id)
            item = res["item"]
            failed = [r for r in res["results"] if not r["ok"]]
            footer = ""
            if res["results"] and not failed:
                footer = f"\n\n✅ Saved {len(res['results'])} to Google Calendar."
                answer = "Saved!"
            elif failed:
                footer = "\n\n⚠️ " + failed[0]["message"] + "\nYour tasks are kept; open the dashboard to retry."
                answer = "Couldn't save"
            else:
                footer = "\n\nNothing selected to save. Edit on your PC: " + self.dashboard_url
                answer = "Nothing to save"
            self.edit(chat_id, msg["message_id"], self._summary(item) + footer,
                      buttons=None if not failed else self._buttons(item))
        finally:
            try:
                self._call("answerCallbackQuery", callback_query_id=cq.get("id"), text=answer[:190])
            except TelegramError:
                pass

    # ---------------------------------------------------------------- replies
    def on_item_processed(self, item: Optional[Dict[str, Any]]) -> None:
        if not item or item.get("source") != "telegram":
            return
        meta = item.get("meta") or {}
        chat_id = meta.get("chat_id")
        if not chat_id:
            return
        text, buttons = self._summary(item), self._buttons(item)
        if meta.get("ack_message_id"):
            self.edit(chat_id, meta["ack_message_id"], text, buttons)
        else:
            self.send(chat_id, text, buttons)

    @staticmethod
    def _when(t: Dict[str, Any]) -> str:
        if not t.get("due_date"):
            return "no date yet"
        d = datetime.strptime(t["due_date"], "%Y-%m-%d").strftime("%a %d %b")
        return d + (f", {t['due_time']}" if t.get("due_time") else "")

    def _summary(self, item: Dict[str, Any]) -> str:
        if item["status"] == "failed":
            return f"⚠️ Couldn't read that message: {item.get('error') or 'unknown error'}"
        tasks = item.get("tasks") or []
        if not tasks:
            return "🤷 No deadlines or events found in that message."
        lines = []
        for t in tasks:
            mark = "✅" if t.get("ui") == "saved" else ("☑️" if t.get("selected") else "▫️")
            label = t.get("area_label") or ""
            lines.append(f"{mark} {t.get('title')}\n     {self._when(t)}" + (f" · {label}" if label else ""))
            extra = (t.get("ambiguities") or [])[:1]
            if extra and t.get("ui") != "saved":
                lines.append(f"     ⚠️ {extra[0]}")
        selected = [t for t in tasks if t.get("selected") and t.get("ui") != "saved"]
        hint = "" if selected else f"\n\nNothing is ticked. Review it on your PC: {self.dashboard_url}"
        return "\n".join(lines) + hint

    def _buttons(self, item: Dict[str, Any]):
        if item["status"] in ("failed", "saved", "dismissed"):
            return None
        n = sum(1 for t in item.get("tasks") or [] if t.get("selected") and t.get("ui") != "saved")
        row = []
        if n:
            row.append({"text": f"✅ Save {n} to Calendar", "callback_data": f"save:{item['id']}"})
        row.append({"text": "🗑 Dismiss", "callback_data": f"dismiss:{item['id']}"})
        return [row]
