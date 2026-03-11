import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")

    class _DummyOpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai_stub.OpenAI = _DummyOpenAI
    sys.modules["openai"] = openai_stub

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")

    def _dummy_load_dotenv(*args, **kwargs):
        return None

    dotenv_stub.load_dotenv = _dummy_load_dotenv
    sys.modules["dotenv"] = dotenv_stub

from gateway.config import GatewayConfig, ChannelConfig
from gateway.models import InboundMessage
from gateway.service import GatewayService
from gateway.storage import GatewayStore
from gateway.app import run_gateway


class GatewayCoreTests(unittest.TestCase):

    def setUp(self):
        self._load_policy_patcher = patch("security.approval.ApprovalManager._load_policy", return_value=None)
        self._save_policy_patcher = patch("security.approval.ApprovalManager._save_policy", return_value=None)
        self._load_policy_patcher.start()
        self._save_policy_patcher.start()
        self.addCleanup(self._load_policy_patcher.stop)
        self.addCleanup(self._save_policy_patcher.stop)

    def _new_service(self):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        cfg = GatewayConfig(
            request_timeout=30,
            session_ttl_dm=24 * 3600,
            session_ttl_group=8 * 3600,
            approval_wait_timeout=24 * 3600,
            channels={
                "telegram": ChannelConfig(enabled=True, bot_token="x", dm_policy="pairing")
            },
        )
        store = GatewayStore(root / ".smileclaw" / "gateway.db")
        service = GatewayService(cfg, store, root)
        return temp_dir, service

    def test_pairing_required_for_unknown_user(self):
        tmp, service = self._new_service()
        try:
            msg = InboundMessage(
                channel="telegram",
                account_id="acct",
                chat_id="u1",
                event_id="e1",
                sender_id="u1",
                sender_name="Alice",
                text="hello",
            )
            resp = service.handle_inbound(msg)
            self.assertEqual(resp.error_code, "PAIRING_REQUIRED")
            self.assertEqual(resp.status, "failed")
            pending = service.pairing.list_requests(status="pending")
            self.assertEqual(len(pending), 1)
        finally:
            tmp.cleanup()

    def test_duplicate_event_returns_existing_run(self):
        tmp, service = self._new_service()
        try:
            req = service.pairing.ensure_pairing_request("telegram", "acct", "u1", "Alice")
            service.pairing.approve(req["id"])

            tool_call = json.dumps({"tool": "shell", "args": {"command": "echo hi"}})
            msg = InboundMessage(
                channel="telegram",
                account_id="acct",
                chat_id="u1",
                event_id="event-1",
                sender_id="u1",
                sender_name="Alice",
                text="say hi",
            )
            with patch("agent.agent.ask_llm", side_effect=[tool_call, "done"]):
                first = service.handle_inbound(msg)
            with patch("agent.agent.ask_llm", side_effect=[tool_call]):
                second = service.handle_inbound(msg)

            self.assertEqual(first.run_id, second.run_id)
            self.assertIn(second.status, {"completed", "accepted", "running", "waiting_approval"})
        finally:
            tmp.cleanup()

    def test_waiting_approval_and_resume_same_run(self):
        tmp, service = self._new_service()
        try:
            req = service.pairing.ensure_pairing_request("telegram", "acct", "u1", "Alice")
            service.pairing.approve(req["id"])

            blocked_cmd = json.dumps({"tool": "shell", "args": {"command": "cat /private/tmp/gateway_test_out_1.txt"}})
            msg = InboundMessage(
                channel="telegram",
                account_id="acct",
                chat_id="u1",
                event_id="event-2",
                sender_id="u1",
                sender_name="Alice",
                text="read external",
            )
            with patch("agent.agent.ask_llm", side_effect=[blocked_cmd]):
                waiting = service.handle_inbound(msg)

            self.assertEqual(waiting.status, "waiting_approval")
            self.assertNotIn("approval_id:", waiting.message)
            self.assertIn("请直接点击下方按钮进行审批。", waiting.message)
            self.assertNotIn("command:", waiting.message)
            approvals = service.list_pending_approvals()
            self.assertEqual(len(approvals), 1)
            approval_id = approvals[0]["approval_id"]

            with patch("agent.agent.ask_llm", side_effect=["stop"]):
                resumed = service.resolve_approval(approval_id, "reject")
            self.assertEqual(resumed.run_id, waiting.run_id)
            self.assertIn(resumed.status, {"failed", "completed"})
        finally:
            tmp.cleanup()

    def test_approve_always_via_inbound_message(self):
        tmp, service = self._new_service()
        try:
            req = service.pairing.ensure_pairing_request("telegram", "acct", "u1", "Alice")
            service.pairing.approve(req["id"])

            blocked_cmd = json.dumps({"tool": "shell", "args": {"command": "cat /private/tmp/gateway_test_out_2.txt"}})
            initial_msg = InboundMessage(
                channel="telegram",
                account_id="acct",
                chat_id="u1",
                event_id="event-approval-3",
                sender_id="u1",
                sender_name="Alice",
                text="read external",
            )
            with patch("agent.agent.ask_llm", side_effect=[blocked_cmd]):
                waiting = service.handle_inbound(initial_msg)

            self.assertEqual(waiting.status, "waiting_approval")
            approvals = service.list_pending_approvals()
            self.assertEqual(len(approvals), 1)

            approve_msg = InboundMessage(
                channel="telegram",
                account_id="acct",
                chat_id="u1",
                event_id="event-approval-4",
                sender_id="u1",
                sender_name="Alice",
                text="🟢 当前文件总是允许",
            )
            with patch("agent.agent.ask_llm", side_effect=["done"]):
                resolved = service.handle_inbound(approve_msg)

            self.assertEqual(service.list_pending_approvals(), [])
            self.assertEqual(resolved.status, "completed")
        finally:
            tmp.cleanup()

    def test_approve_via_inbound_message(self):
        tmp, service = self._new_service()
        try:
            req = service.pairing.ensure_pairing_request("telegram", "acct", "u1", "Alice")
            service.pairing.approve(req["id"])

            blocked_cmd = json.dumps({"tool": "shell", "args": {"command": "cat /private/tmp/gateway_test_allow_always/out.txt"}})
            initial_msg = InboundMessage(
                channel="telegram",
                account_id="acct",
                chat_id="u1",
                event_id="event-approval-1",
                sender_id="u1",
                sender_name="Alice",
                text="read external",
            )
            with patch("agent.agent.ask_llm", side_effect=[blocked_cmd]):
                waiting = service.handle_inbound(initial_msg)

            self.assertEqual(waiting.status, "waiting_approval")
            approvals = service.list_pending_approvals()
            self.assertEqual(len(approvals), 1)

            approve_msg = InboundMessage(
                channel="telegram",
                account_id="acct",
                chat_id="u1",
                event_id="event-approval-2",
                sender_id="u1",
                sender_name="Alice",
                text="✅ 同意一次",
            )
            with patch("agent.agent.ask_llm", side_effect=["done"]):
                resolved = service.handle_inbound(approve_msg)

            self.assertEqual(service.list_pending_approvals(), [])
            self.assertIn(resolved.status, {"completed", "failed"})
        finally:
            tmp.cleanup()

    def test_run_gateway_skips_duplicate_event_response(self):
        msg = InboundMessage(
            channel="telegram",
            account_id="acct",
            chat_id="u1",
            event_id="event-dup",
            sender_id="u1",
            sender_name="Alice",
            text="hello",
        )

        class _StubAdapter:
            def __init__(self):
                self.sent = []
                self._iter = 0

            def start(self):
                return None

            def stop(self):
                return None

            def poll_messages(self):
                self._iter += 1
                if self._iter == 1:
                    return [msg]
                raise KeyboardInterrupt()

            def send_response(self, chat_id: str, text: str, thread_id: str | None = None, actions=None):
                self.sent.append((chat_id, text, thread_id, actions))

        class _StubService:
            def handle_inbound(self, _):
                from gateway.models import BridgeResponse, now_iso
                return BridgeResponse(
                    request_id="req1",
                    run_id="run1",
                    status="completed",
                    message="Duplicate event. Returning existing run.",
                    error_code="DUPLICATE_EVENT",
                    timestamp=now_iso(),
                )

        adapter = _StubAdapter()
        service = _StubService()

        with patch("gateway.app.build_service", return_value=service), patch("gateway.app.build_adapters", return_value=[adapter]), patch("gateway.app.time.sleep", return_value=None):
            try:
                run_gateway("docs/gateway.yaml")
            except KeyboardInterrupt:
                pass

        self.assertEqual(adapter.sent, [])


if __name__ == "__main__":
    unittest.main()
