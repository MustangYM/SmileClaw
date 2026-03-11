import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from gateway.adapters.telegram import TelegramAdapter


class TelegramAdapterTests(unittest.TestCase):

    def test_parse_update_text_message(self):
        adapter = TelegramAdapter("telegram", {"bot_token": "x"})
        adapter.account_id = "12345"
        update = {
            "update_id": 1001,
            "message": {
                "message_id": 77,
                "from": {"id": 888, "username": "alice"},
                "chat": {"id": 888, "type": "private"},
                "date": 1700000000,
                "text": "hello",
            },
        }
        msg = adapter._parse_update(update)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.channel, "telegram")
        self.assertEqual(msg.account_id, "12345")
        self.assertEqual(msg.event_id, "1001")
        self.assertEqual(msg.chat_id, "888")
        self.assertEqual(msg.sender_id, "888")
        self.assertEqual(msg.text, "hello")

    def test_poll_updates_updates_offset(self):
        adapter = TelegramAdapter("telegram", {"bot_token": "x", "poll_timeout": 1})
        adapter._running = True

        def fake_call(method, payload=None, timeout=30):
            if method == "getUpdates":
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 100,
                            "message": {
                                "from": {"id": 1, "first_name": "A"},
                                "chat": {"id": 2, "type": "private"},
                                "text": "ping",
                            },
                        }
                    ],
                }
            return {"ok": True, "result": {"id": 999}}

        adapter._api_call = fake_call
        out = adapter.poll_messages()
        self.assertEqual(len(out), 1)
        self.assertEqual(adapter.offset, 101)


if __name__ == "__main__":
    unittest.main()
