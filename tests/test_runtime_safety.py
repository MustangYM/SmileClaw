import json
import types
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Minimal stubs to avoid importing external SDKs in runtime unit tests.
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

from agent.agent import Agent  # noqa: E402
from memory.memory import Memory  # noqa: E402
from security.approval import ApprovalManager  # noqa: E402
from security.policy import ShellPolicyEngine  # noqa: E402


class RuntimeSafetyTests(unittest.TestCase):

    def setUp(self):
        self._load_policy_patcher = patch("security.approval.ApprovalManager._load_policy", return_value=None)
        self._save_policy_patcher = patch("security.approval.ApprovalManager._save_policy", return_value=None)
        self._load_policy_patcher.start()
        self._save_policy_patcher.start()
        self.addCleanup(self._load_policy_patcher.stop)
        self.addCleanup(self._save_policy_patcher.stop)

    def test_cd_traversal_requires_approval(self):
        manager = ApprovalManager(PROJECT_ROOT)
        result = manager.check_shell_command("cd .. && pwd")
        self.assertTrue(result["requires_approval"])
        self.assertEqual(result["decision"], "require_approval")
        self.assertIn("OUTSIDE_WORKSPACE", result.get("reason_codes", []))

    def test_denied_command_does_not_reprompt_in_same_turn(self):
        memory = Memory()
        memory.add("user", "读取外部目录")
        agent = Agent(memory)

        tool_call = json.dumps(
            {
                "tool": "shell",
                "args": {"command": "cat /Users/coderchan/Desktop/OutsideOnly/file.txt"}
            },
            ensure_ascii=False
        )

        with patch("agent.agent.ask_llm", side_effect=[tool_call]):
            agent.run()

        self.assertTrue(agent.has_pending_approval())

        agent.resolve_approval("deny")
        self.assertFalse(agent.has_pending_approval())

        with patch("agent.agent.ask_llm", side_effect=[tool_call, "停止"]):
            agent.run()

        # Ensure no second approval request was created.
        self.assertFalse(agent.has_pending_approval())

        assistant_contents = [
            item["content"] for item in memory.get() if item["role"] == "assistant"
        ]
        self.assertTrue(
            any("不再重复申请权限" in text for text in assistant_contents),
            "Expected denied-loop protection message not found."
        )

    def test_expanduser_runtime_error_fallback(self):
        engine = ShellPolicyEngine(PROJECT_ROOT)
        with patch("pathlib.Path.expanduser", side_effect=RuntimeError("Could not determine home directory.")):
            result = engine.evaluate(
                command="cat ~/secret.txt",
                allowed_roots=[str(PROJECT_ROOT)],
                denied_commands=set(),
            )
        self.assertEqual(result["decision"], "require_approval")
        self.assertIn("OUTSIDE_WORKSPACE", result["reason_codes"])

    def test_agent_uses_internal_retriever_reply(self):
        memory = Memory()
        memory.add("user", "在文稿里找一份简历")
        agent = Agent(memory)

        with patch(
            "agent.agent.run_internal_file_search",
            return_value={
                "handled": True,
                "status": "found",
                "message": "我找到这些可能相关的文件：\\n1. /Users/test/Documents/resume.pdf",
            },
        ), patch("agent.agent.ask_llm", side_effect=AssertionError("ask_llm should not be called")):
            agent.run()

        assistant_contents = [
            item["content"] for item in memory.get() if item["role"] == "assistant"
        ]
        self.assertTrue(any("我找到这些可能相关的文件" in text for text in assistant_contents))


if __name__ == "__main__":
    unittest.main()
