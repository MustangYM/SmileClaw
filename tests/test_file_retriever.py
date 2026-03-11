import sys
import unittest
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.file_retriever import is_file_search_request, run_internal_file_search, _read_pdf_text


class _ApprovalsAllow:
    def check_shell_command(self, command):
        return {
            "requires_approval": False,
            "decision": "allow",
            "restricted_paths": [],
            "reason_codes": [],
            "risk_flags": [],
        }


class _ApprovalsNeedApproval:
    def check_shell_command(self, command):
        return {
            "requires_approval": True,
            "decision": "require_approval",
            "approval_id": "abc123",
            "restricted_paths": ["/Users/test/Documents"],
            "reason_codes": ["OUTSIDE_WORKSPACE"],
            "risk_flags": [],
        }


class FileRetrieverTests(unittest.TestCase):

    def test_intent_detection(self):
        self.assertTrue(is_file_search_request("在文稿里找简历"))
        self.assertTrue(is_file_search_request("find my resume"))
        self.assertFalse(is_file_search_request("帮我写一段代码"))

    def test_found_result_returns_user_summary(self):
        def fake_ask_llm(messages):
            prompt = messages[-1]["content"]
            if "schema" in prompt and "keywords" in prompt:
                return '{"keywords": ["简历"], "directories": ["~/Documents"], "extensions": ["pdf", "docx"]}'
            return '{"selected": ["/Users/test/Documents/张三_最终版.pdf"]}'

        def fake_execute_tool(name, args):
            self.assertEqual(name, "shell")
            return {
                "ok": True,
                "stdout": "/Users/test/Documents/张三_最终版.pdf\n",
                "stderr": "",
                "exit_code": 0,
            }

        out = run_internal_file_search(
            user_text="在文稿里面找一个简历",
            ask_llm_func=fake_ask_llm,
            execute_tool_func=fake_execute_tool,
            approvals=_ApprovalsAllow(),
        )

        self.assertTrue(out["handled"])
        self.assertEqual(out["status"], "found")
        self.assertIn("我找到这些可能相关的文件", out["message"])
        self.assertNotIn("tool", out["message"].lower())
        self.assertNotIn("command", out["message"].lower())

    def test_requires_approval_stops_execution(self):
        def fake_ask_llm(messages):
            return '{"keywords": ["简历"], "directories": ["~/Documents"], "extensions": ["pdf"]}'

        called = {"value": False}

        def fake_execute_tool(name, args):
            called["value"] = True
            return {}

        out = run_internal_file_search(
            user_text="找简历",
            ask_llm_func=fake_ask_llm,
            execute_tool_func=fake_execute_tool,
            approvals=_ApprovalsNeedApproval(),
        )

        self.assertTrue(out["handled"])
        self.assertEqual(out["status"], "approval_required")
        self.assertFalse(called["value"])

    def test_content_level_rerank_can_find_non_keyword_filename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx_path = root / "profile_final.docx"
            txt_path = root / "notes.txt"

            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>这是一份个人简历，包含工作经历和教育背景</w:t></w:r></w:p></w:body>"
                "</w:document>"
            )
            with zipfile.ZipFile(docx_path, "w") as zf:
                zf.writestr("word/document.xml", xml)
            txt_path.write_text("random note", encoding="utf-8")

            def fake_ask_llm(messages):
                prompt = messages[-1]["content"]
                if "schema" in prompt and "keywords" in prompt:
                    return json_plan
                return '{"selected": []}'

            json_plan = (
                '{"keywords": ["简历"], '
                f'"directories": ["{root}"], '
                '"extensions": ["docx", "txt"]}'
            )

            def fake_execute_tool(name, args):
                self.assertEqual(name, "shell")
                return {
                    "ok": True,
                    "stdout": f"{txt_path}\n{docx_path}\n",
                    "stderr": "",
                    "exit_code": 0,
                }

            out = run_internal_file_search(
                user_text="帮我找简历",
                ask_llm_func=fake_ask_llm,
                execute_tool_func=fake_execute_tool,
                approvals=_ApprovalsAllow(),
            )

            self.assertEqual(out["status"], "found")
            self.assertIn(str(docx_path), out["message"])

    def test_pdf_fallback_extract_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sample.pdf"
            path.write_bytes(b"%PDF-1.4\\n1 0 obj\\n<</Type /Catalog>>\\nendobj\\n")
            text = _read_pdf_text(path)
            self.assertIsInstance(text, str)

    def test_empty_plan_uses_dynamic_keywords_from_user_input(self):
        def fake_ask_llm(messages):
            prompt = messages[-1]["content"]
            if "JSON schema: {\"keywords\"" in prompt:
                return '{"keywords": [], "directories": [], "extensions": []}'
            if "内部目录解析器" in prompt:
                return '{"directories": ["/Users/coderchan/Documents"]}'
            return '{"selected": []}'

        captured = {"cmds": []}

        def fake_execute_tool(name, args):
            captured["cmds"].append(args["command"])
            return {
                "ok": True,
                "stdout": "/Users/coderchan/Documents/a.txt\n/Users/coderchan/Documents/b.md\n",
                "stderr": "",
                "exit_code": 0,
            }

        out = run_internal_file_search(
            user_text="帮我找合同扫描件",
            ask_llm_func=fake_ask_llm,
            execute_tool_func=fake_execute_tool,
            approvals=_ApprovalsAllow(),
        )

        self.assertEqual(out["status"], "found")
        self.assertIn("我找到这些可能相关的文件", out["message"])
        self.assertIn("/Users/coderchan/Documents/a.txt", out["message"])
        self.assertTrue(all("/Users/coderchan/Documents" in cmd for cmd in captured["cmds"]))
        self.assertTrue(any("*帮我找合同扫描件*" in cmd for cmd in captured["cmds"]))
        self.assertTrue(all("*简历*" not in cmd for cmd in captured["cmds"]))


if __name__ == "__main__":
    unittest.main()
