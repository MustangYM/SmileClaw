import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from gateway.adapters.base import ChannelAdapter
from gateway.models import InboundMessage, now_iso


class TelegramAdapter(ChannelAdapter):

    def __init__(self, channel_name: str, config: dict):
        self.channel_name = channel_name
        self.config = config
        self._running = False
        self.bot_token = (config or {}).get("bot_token", "")
        self.poll_timeout = int((config or {}).get("poll_timeout", 20))
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.offset = None
        self.account_id = "telegram-bot"
        self.debug = os.getenv("GATEWAY_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}

    def name(self) -> str:
        return self.channel_name

    def _api_call(self, method: str, payload: dict | None = None, timeout: int = 30):
        if not self.bot_token or self.bot_token == "TOKEN":
            return {"ok": False, "description": "bot token not configured"}

        url = f"{self.api_base}/{method}"
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)

    def _fetch_bot_identity(self):
        try:
            result = self._api_call("getMe", payload={})
        except Exception as exc:
            if self.debug:
                print(f"[gateway.telegram] getMe failed: {exc}", file=sys.stderr, flush=True)
            return
        if not result or not result.get("ok"):
            if self.debug:
                print(f"[gateway.telegram] getMe not ok: {result}", file=sys.stderr, flush=True)
            return

        me = result.get("result", {})
        bot_id = me.get("id")
        if bot_id is not None:
            self.account_id = str(bot_id)
        if self.debug:
            print(
                f"[gateway.telegram] start account_id={self.account_id} poll_timeout={self.poll_timeout}",
                file=sys.stderr,
                flush=True,
            )

    def _parse_update(self, update: dict) -> InboundMessage | None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return None

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None

        chat = message.get("chat", {})
        from_user = message.get("from", {})

        chat_id = str(chat.get("id", ""))
        sender_id = str(from_user.get("id", ""))
        sender_name = from_user.get("username") or from_user.get("first_name") or "unknown"

        if not chat_id or not sender_id:
            return None

        thread_id = message.get("message_thread_id")
        if thread_id is not None:
            thread_id = str(thread_id)

        msg = InboundMessage(
            channel="telegram",
            account_id=self.account_id,
            chat_id=chat_id,
            event_id=str(update.get("update_id")),
            sender_id=sender_id,
            sender_name=str(sender_name),
            text=text.strip(),
            thread_id=thread_id,
            attachments=[],
            metadata={"raw_update_type": "message"},
            timestamp=now_iso(),
        )
        return msg

    def start(self):
        self._running = True
        self._fetch_bot_identity()

    def stop(self):
        self._running = False

    def poll_messages(self) -> list[InboundMessage]:
        if not self._running:
            return []

        payload = {
            "timeout": self.poll_timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if self.offset is not None:
            payload["offset"] = self.offset

        try:
            result = self._api_call("getUpdates", payload=payload, timeout=self.poll_timeout + 5)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            if self.debug:
                print(f"[gateway.telegram] getUpdates error: {exc}", file=sys.stderr, flush=True)
            return []
        except Exception as exc:
            if self.debug:
                print(f"[gateway.telegram] getUpdates unexpected error: {exc}", file=sys.stderr, flush=True)
            return []

        if not result or not result.get("ok"):
            if self.debug:
                print(f"[gateway.telegram] getUpdates not ok: {result}", file=sys.stderr, flush=True)
            return []

        updates = result.get("result", [])
        messages: list[InboundMessage] = []

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = update_id + 1

            parsed = self._parse_update(update)
            if parsed is not None:
                messages.append(parsed)

        if self.debug and (updates or messages):
            print(
                f"[gateway.telegram] updates={len(updates)} parsed_messages={len(messages)} next_offset={self.offset}",
                file=sys.stderr,
                flush=True,
            )

        return messages

    def send_response(self, chat_id: str, text: str, thread_id: str | None = None, actions: list[str] | None = None) -> None:
        if not self._running:
            return

        payload = {
            "chat_id": chat_id,
            "text": text or "",
        }
        if actions:
            buttons = [[{"text": action}] for action in actions]
            payload["reply_markup"] = {
                "keyboard": buttons,
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }
        if thread_id:
            try:
                payload["message_thread_id"] = int(thread_id)
            except ValueError:
                pass

        try:
            self._api_call("sendMessage", payload=payload, timeout=20)
        except Exception as exc:
            if self.debug:
                print(f"[gateway.telegram] sendMessage failed: {exc}", file=sys.stderr, flush=True)
            return
