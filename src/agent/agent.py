import json
import re
import uuid
from pathlib import Path

from agent.events import RuntimeEventEmitter
from agent.file_retriever import run_internal_file_search
from llm.llm import ask_llm
from tools.registry import execute_tool
from security.approval import ApprovalManager


def _extract_tool_json(reply):
    text = (reply or "").strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        if isinstance(data, dict) and "tool" in data:
            return data
    except json.JSONDecodeError:
        pass

    blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "tool" in data:
                return data
        except json.JSONDecodeError:
            continue

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        maybe_json = text[first:last + 1]
        try:
            data = json.loads(maybe_json)
            if isinstance(data, dict) and "tool" in data:
                return data
        except json.JSONDecodeError:
            pass

    return None


class Agent:

    def __init__(self, memory):
        self.memory = memory
        workspace_root = Path(__file__).resolve().parents[2]
        self.approvals = ApprovalManager(workspace_root)
        self.events = RuntimeEventEmitter(workspace_root)
        self.pending_approval_id = None
        self.denied_commands_in_turn = set()
        self.user_turn_count = 0
        self.last_completion_emitted = False
        self.last_internal_retriever_turn = 0

    def has_pending_approval(self):
        return self.pending_approval_id is not None

    def _ensure_run_started(self):
        if not self.events.has_active_run():
            self.events.start_run()
        self.last_completion_emitted = False

    def finalize_run_if_idle(self):
        if self.pending_approval_id:
            return
        if self.last_completion_emitted:
            return
        self.events.emit(
            "run_completed",
            {"status": "completed"}
        )
        self.events.end_run()
        self.last_completion_emitted = True

    def _emit_error(self, stage, message, correlation_id=None, error_code=None, retryable=False):
        self.events.emit(
            "runtime_error",
            {
                "stage": stage,
                "message": message,
                "retryable": retryable
            },
            correlation_id=correlation_id,
            error_code=error_code
        )

    def _sync_turn_state(self, messages):
        current_user_turn_count = sum(
            1 for item in messages if item.get("role") == "user"
        )
        if current_user_turn_count != self.user_turn_count:
            self.user_turn_count = current_user_turn_count
            self.denied_commands_in_turn.clear()
            self.events.start_run()
            self.last_completion_emitted = False
            self.last_internal_retriever_turn = 0

    def _latest_user_message(self, messages):
        for item in reversed(messages):
            if item.get("role") == "user":
                return item.get("content", "")
        return ""

    def _handle_internal_file_search_if_needed(self, messages):
        if self.user_turn_count <= 0:
            return False
        if self.last_internal_retriever_turn == self.user_turn_count:
            return False

        user_text = self._latest_user_message(messages)
        result = run_internal_file_search(
            user_text=user_text,
            ask_llm_func=ask_llm,
            execute_tool_func=execute_tool,
            approvals=self.approvals,
            log_func=print,
        )

        if not result.get("handled"):
            return False

        self.last_internal_retriever_turn = self.user_turn_count

        if result.get("status") == "approval_required":
            self.pending_approval_id = result.get("approval_id")
            message = result.get("message") or "检测到目录访问风险，等待审批。"
            self.memory.add("assistant", message)
            print("LLM:", message)
            self.events.emit(
                "approval_requested",
                {
                    "approval_id": self.pending_approval_id,
                    "tool_name": "shell",
                    "reason": "restricted_paths",
                    "restricted_paths": result.get("paths", []),
                },
                correlation_id=self.pending_approval_id or str(uuid.uuid4())[:8]
            )
            return True

        reply = result.get("message", "")
        if reply:
            self.memory.add("assistant", reply)
            print("LLM:", reply)
            self.events.emit("assistant_message", {"text": reply})
        return True

    def _format_tool_result(self, result):
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    def _execute_tool(self, tool_name, args, reply, correlation_id):
        self.events.emit(
            "tool_call_started",
            {"tool_name": tool_name},
            correlation_id=correlation_id
        )
        result = execute_tool(tool_name, args)
        print("Tool result:", result)
        self.events.emit(
            "tool_call_finished",
            {
                "tool_name": tool_name,
                "ok": result.get("ok", False) if isinstance(result, dict) else False,
                "exit_code": result.get("exit_code") if isinstance(result, dict) else None,
                "duration_ms": result.get("duration_ms", 0) if isinstance(result, dict) else 0,
                "output_truncated": result.get("output_truncated", False) if isinstance(result, dict) else False
            },
            correlation_id=correlation_id,
            error_code=result.get("error_code") if isinstance(result, dict) else None
        )
        self.memory.add("assistant", reply)
        self.memory.add("assistant", f"Tool result: {self._format_tool_result(result)}")
        return result

    def resolve_approval(self, decision):
        if not self.pending_approval_id:
            return

        resolved = self.approvals.resolve(self.pending_approval_id, decision)
        if not resolved.get("ok"):
            msg = "审批状态异常，无法继续执行。"
            self.memory.add("assistant", msg)
            print("LLM:", msg)
            self._emit_error(
                stage="approval_resolve",
                message=msg,
                error_code="APPROVAL_STATE_ERROR",
                retryable=False
            )
            self.pending_approval_id = None
            return

        request = resolved["request"]
        self.pending_approval_id = None
        correlation_id = request.get("approval_id", str(uuid.uuid4())[:8])
        self.events.emit(
            "approval_resolved",
            {
                "approval_id": request.get("approval_id"),
                "decision": decision
            },
            correlation_id=correlation_id
        )
        for audit in resolved.get("audit_events", []):
            self.events.emit(
                audit.get("type", "approval_audit"),
                {
                    "approval_id": request.get("approval_id"),
                    "rule_id": audit.get("rule_id")
                },
                correlation_id=correlation_id
            )

        if decision == "deny":
            self.denied_commands_in_turn.add(request["command"])
            msg = (
                f"审批已拒绝（id={request['approval_id']}），"
                f"已取消命令：{request['command']}"
            )
            self.memory.add("assistant", msg)
            print("LLM:", msg)
            self.events.emit(
                "assistant_message",
                {"text": msg},
                correlation_id=correlation_id
            )
            return

        replay = json.dumps(
            {"tool": request["tool"], "args": {"command": request["command"]}},
            ensure_ascii=False
        )
        self._execute_tool(
            request["tool"],
            {"command": request["command"]},
            replay,
            correlation_id=correlation_id
        )

    def run(self):

        self._ensure_run_started()
        step_guard = 0
        while True:
            step_guard += 1
            if step_guard > 20:
                guard_msg = "检测到重复受阻操作，本次事务已停止，等待新的用户指令。"
                self.memory.add("assistant", guard_msg)
                print("LLM:", guard_msg)
                self._emit_error(
                    stage="run_loop",
                    message=guard_msg,
                    error_code="LOOP_GUARD",
                    retryable=False
                )
                break

            if self.pending_approval_id:
                print("LLM: 当前存在待审批操作，等待用户决策。")
                break

            messages = self.memory.get()
            self._sync_turn_state(messages)
            if self._handle_internal_file_search_if_needed(messages):
                if self.pending_approval_id:
                    print("LLM: 当前存在待审批操作，等待用户决策。")
                break
            print("准备调用LLM Messages:", messages)

            reply = ask_llm(messages)

            print("LLM:", reply)

            data = _extract_tool_json(reply)
            if isinstance(data, dict) and "tool" in data:
                tool_name = data.get("tool")
                args = data.get("args", {})
                correlation_id = str(uuid.uuid4())[:8]
                approval_required = False

                if tool_name == "shell":
                    command = args.get("command", "")
                    check = self.approvals.check_shell_command(command)
                    approval_required = bool(check.get("requires_approval"))
                    self.events.emit(
                        "tool_call_requested",
                        {
                            "tool_name": tool_name,
                            "args": args,
                            "risk_level": "high",
                            "approval_required": approval_required
                        },
                        correlation_id=correlation_id
                    )
                    if check.get("decision") == "deny":
                        self.denied_commands_in_turn.add(command)
                        denied_result = {
                            "ok": False,
                            "policy_denied": True,
                            "command": command,
                            "message": "策略已拒绝该命令执行。",
                            "reason_codes": check.get("reason_codes", [])
                        }
                        self.memory.add("assistant", reply)
                        self.memory.add(
                            "assistant",
                            f"Tool result: {self._format_tool_result(denied_result)}"
                        )
                        print("Tool result:", denied_result)
                        self.events.emit(
                            "tool_call_finished",
                            {
                                "tool_name": tool_name,
                                "ok": False,
                                "exit_code": None,
                                "duration_ms": 0,
                                "output_truncated": False
                            },
                            correlation_id=correlation_id,
                            error_code="POLICY_DENY"
                        )
                        continue

                    if check.get("requires_approval"):
                        if command in self.denied_commands_in_turn:
                            denied_result = {
                                "ok": False,
                                "approval_denied": True,
                                "command": command,
                                "message": "该命令在当前事务中已被拒绝，不再重复申请权限。"
                            }
                            self.memory.add("assistant", reply)
                            self.memory.add(
                                "assistant",
                                f"Tool result: {self._format_tool_result(denied_result)}"
                            )
                            print("Tool result:", denied_result)
                            self.events.emit(
                                "tool_call_finished",
                                {
                                    "tool_name": tool_name,
                                    "ok": False,
                                    "exit_code": None,
                                    "duration_ms": 0,
                                    "output_truncated": False
                                },
                                correlation_id=correlation_id,
                                error_code="APPROVAL_DENY"
                            )
                            continue

                        approval_id = check["approval_id"]
                        paths = check.get("restricted_paths", [])
                        path_text = "、".join(paths) if paths else "未知路径"

                        approval_message = (
                            "检测到该命令将访问工作区之外的路径，执行前需要审批。\n"
                            f"approval_id: {approval_id}\n"
                            f"command: {command}\n"
                            f"paths: {path_text}\n"
                            f"reason_codes: {check.get('reason_codes', [])}\n"
                            f"risk_flags: {check.get('risk_flags', [])}\n"
                            "请选择：allow_once / allow_always / deny"
                        )
                        self.memory.add("assistant", approval_message)
                        print("LLM:", approval_message)
                        self.pending_approval_id = approval_id
                        self.events.emit(
                            "approval_requested",
                            {
                                "approval_id": approval_id,
                                "tool_name": tool_name,
                                "reason": "restricted_paths",
                                "restricted_paths": paths
                            },
                            correlation_id=approval_id
                        )
                        break
                else:
                    self.events.emit(
                        "tool_call_requested",
                        {
                            "tool_name": tool_name,
                            "args": args,
                            "risk_level": "normal",
                            "approval_required": False
                        },
                        correlation_id=correlation_id
                    )

                self._execute_tool(tool_name, args, reply, correlation_id=correlation_id)
                continue

            self.memory.add("assistant", reply)
            self.events.emit(
                "assistant_message",
                {"text": reply}
            )

            break
