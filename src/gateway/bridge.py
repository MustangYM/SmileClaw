import json
from dataclasses import dataclass

from agent.agent import Agent
from gateway.models import BridgeRequest, BridgeResponse, now_iso
from memory.memory import Memory


@dataclass
class SessionContext:
    memory: Memory
    agent: Agent


class AgentBridge:

    def __init__(self):
        self.sessions: dict[str, SessionContext] = {}

    def _new_context(self) -> SessionContext:
        memory = Memory()
        agent = Agent(memory)
        return SessionContext(memory=memory, agent=agent)

    def _get_context(self, session_key: str) -> SessionContext:
        if session_key not in self.sessions:
            self.sessions[session_key] = self._new_context()
        return self.sessions[session_key]

    def _serialize_context(self, context: SessionContext) -> str:
        payload = {
            "messages": context.memory.get(),
            "pending_approval_id": context.agent.pending_approval_id,
            "denied_commands_in_turn": sorted(list(context.agent.denied_commands_in_turn)),
            "user_turn_count": context.agent.user_turn_count,
            "approval_pending": context.agent.approvals.pending,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _load_checkpoint(self, session_key: str, checkpoint: str | None) -> SessionContext:
        if not checkpoint:
            return self._get_context(session_key)

        data = json.loads(checkpoint)
        memory = Memory()
        for item in data.get("messages", []):
            memory.add(item.get("role", "assistant"), item.get("content", ""))

        agent = Agent(memory)
        agent.pending_approval_id = data.get("pending_approval_id")
        agent.denied_commands_in_turn = set(data.get("denied_commands_in_turn", []))
        agent.user_turn_count = data.get("user_turn_count", 0)
        agent.approvals.pending = data.get("approval_pending", {})

        context = SessionContext(memory=memory, agent=agent)
        self.sessions[session_key] = context
        return context

    def _last_assistant_message(self, context: SessionContext) -> str:
        for msg in reversed(context.memory.get()):
            if msg.get("role") == "assistant":
                return msg.get("content", "")
        return ""

    def _approval_summary(self, context: SessionContext, approval_id: str | None) -> str:
        if not approval_id:
            return "检测到敏感操作，等待审批。"

        pending = context.agent.approvals.pending.get(approval_id, {})
        raw_paths = pending.get("restricted_paths", []) if isinstance(pending, dict) else []
        seen = set()
        paths = []
        for item in raw_paths:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            paths.append(text)
            if len(paths) >= 3:
                break

        if not paths:
            path_text = "- 工作区外路径"
        else:
            path_text = "\n".join(f"- {p}" for p in paths)

        return (
            "检测到需要访问工作区之外的路径，执行前需要审批。\n"
            "涉及路径：\n"
            f"{path_text}\n"
            "请点击下方按钮选择。"
        )

    def execute(self, request: BridgeRequest, checkpoint: str | None = None) -> tuple[BridgeResponse, str | None, str | None]:
        context = self._load_checkpoint(request.session_key, checkpoint)
        context.memory.add("user", request.text)

        context.agent.run()

        if context.agent.has_pending_approval():
            approval_id = context.agent.pending_approval_id
            approval_prompt = self._approval_summary(context, approval_id)
            response = BridgeResponse(
                request_id=request.request_id,
                run_id=request.metadata["run_id"],
                status="waiting_approval",
                message=approval_prompt,
                error_code="APPROVAL_PENDING",
                timestamp=now_iso(),
            )
            return response, self._serialize_context(context), approval_id

        context.agent.finalize_run_if_idle()
        last_assistant = self._last_assistant_message(context)

        response = BridgeResponse(
            request_id=request.request_id,
            run_id=request.metadata["run_id"],
            status="completed",
            message=last_assistant,
            error_code=None,
            timestamp=now_iso(),
        )
        return response, self._serialize_context(context), None

    def resolve_approval(self, session_key: str, run_id: str, approval_id: str, decision: str, checkpoint: str | None) -> tuple[BridgeResponse, str | None]:
        context = self._load_checkpoint(session_key, checkpoint)
        if context.agent.pending_approval_id != approval_id:
            context.agent.pending_approval_id = approval_id

        decision_map = {
            "approve": "allow_once",
            "approve_always": "allow_always",
            "reject": "deny",
        }
        context.agent.resolve_approval(decision_map.get(decision, "deny"))
        context.agent.run()

        if context.agent.has_pending_approval():
            approval_prompt = self._approval_summary(context, context.agent.pending_approval_id)
            return BridgeResponse(
                request_id=run_id,
                run_id=run_id,
                status="waiting_approval",
                message=approval_prompt,
                error_code="APPROVAL_PENDING",
                timestamp=now_iso(),
            ), self._serialize_context(context)

        context.agent.finalize_run_if_idle()
        last_assistant = self._last_assistant_message(context)

        if decision in {"approve", "approve_always"} and not last_assistant.strip():
            last_assistant = "已批准并执行完成。"

        return BridgeResponse(
            request_id=run_id,
            run_id=run_id,
            status="completed" if decision in {"approve", "approve_always"} else "failed",
            message=last_assistant if decision in {"approve", "approve_always"} else "Approval denied locally.",
            error_code=None if decision in {"approve", "approve_always"} else "APPROVAL_DENIED",
            timestamp=now_iso(),
        ), self._serialize_context(context)
