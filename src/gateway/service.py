import json
import re
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from gateway.audit import AuditLogger, sender_hash
from gateway.bridge import AgentBridge
from gateway.config import GatewayConfig
from gateway.models import BridgeRequest, BridgeResponse, InboundMessage, now_iso
from gateway.pairing import PairingManager
from gateway.session_router import SessionRouter
from gateway.storage import GatewayStore


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


class GatewayService:

    def __init__(self, config: GatewayConfig, store: GatewayStore, workspace_root: str):
        self.config = config
        self.store = store
        self.audit = AuditLogger(workspace_root)
        self.bridge = AgentBridge()
        self.pairing = PairingManager(store)
        self.router = SessionRouter(max_queue_length_per_session=3, queued_ttl_seconds=300)
        self.pending_requests: dict[str, InboundMessage] = {}
        self.pending_request_meta: dict[str, dict] = {}
        self.run_by_request: dict[str, str] = {}
        self.queued_by_session: dict[str, list[str]] = defaultdict(list)
        self.run_recovery_window = timedelta(minutes=10)
        self.approval_wait_timeout = timedelta(seconds=config.approval_wait_timeout)
        self._recover_runs()

    def _recover_runs(self):
        now = datetime.now(timezone.utc)
        for row in self.store.list_runs_by_status(("running", "waiting_approval", "accepted")):
            status = row["status"]
            run_id = row["run_id"]
            session_key = row["session_key"]
            last_event_at = _parse_iso(row["last_event_at"])
            checkpoint = row.get("checkpoint")

            if status == "waiting_approval":
                if now - last_event_at > self.approval_wait_timeout:
                    self.store.update_run_state(
                        run_id,
                        status="timeout",
                        updated_at=now_iso(),
                        last_event_at=now_iso(),
                        error_code="AGENT_TIMEOUT",
                        message="Approval wait timeout.",
                    )
                continue

            if status in {"running", "accepted"}:
                if checkpoint and now - last_event_at <= self.run_recovery_window:
                    self.store.update_run_state(
                        run_id,
                        status="interrupted",
                        updated_at=now_iso(),
                        last_event_at=now_iso(),
                        message="Run interrupted; checkpoint available for manual resume.",
                    )
                else:
                    self.store.update_run_state(
                        run_id,
                        status="interrupted",
                        updated_at=now_iso(),
                        last_event_at=now_iso(),
                        message="Run interrupted and cannot be resumed automatically.",
                    )

            self.router.clear_active(session_key)

    def _session_key(self, msg: InboundMessage) -> str:
        if msg.thread_id:
            thread_scope = msg.thread_id
        elif msg.chat_id == msg.sender_id:
            thread_scope = msg.sender_id
        else:
            thread_scope = msg.chat_id
        return f"{msg.channel}:{msg.account_id}:{msg.chat_id}:{thread_scope}"

    def _principal(self, msg: InboundMessage) -> str:
        return self.pairing.principal_key(msg.channel, msg.account_id, msg.sender_id)

    def _new_run_id(self) -> str:
        return str(uuid.uuid4())

    def _new_request_id(self) -> str:
        return str(uuid.uuid4())

    def _new_correlation_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _find_pending_approval_for_session(self, session_key: str) -> dict | None:
        active = self.store.list_active_runs_by_session(session_key)
        waiting_runs = [
            row for row in active
            if row.get("status") == "waiting_approval" and row.get("approval_id")
        ]
        for row in reversed(waiting_runs):
            approval_id = row.get("approval_id")
            item = self.store.get_approval_queue_item(approval_id)
            if item and item.get("status") == "pending":
                return {"approval_id": approval_id, "run_id": row["run_id"]}
        return None

    def _parse_approval_command(self, text: str) -> tuple[str, str | None] | None:
        normalized = (text or "").strip()
        if not normalized:
            return None

        direct_map = {
            "✅ 同意一次": ("approve", None),
            "🟢 当前文件总是允许": ("approve_always", None),
            "❌ 拒绝": ("reject", None),
        }
        if normalized in direct_map:
            return direct_map[normalized]

        approve_pattern = re.compile(r"^(?:/)?(?:approve|allow|同意|批准)(?:\s+([A-Za-z0-9_-]+))?$", re.IGNORECASE)
        approve_always_pattern = re.compile(r"^(?:/)?(?:approve_always|allow_always|始终同意|总是允许|永久允许)(?:\s+([A-Za-z0-9_-]+))?$", re.IGNORECASE)
        reject_pattern = re.compile(r"^(?:/)?(?:reject|deny|拒绝|驳回)(?:\s+([A-Za-z0-9_-]+))?$", re.IGNORECASE)

        approve_always_match = approve_always_pattern.match(normalized)
        if approve_always_match:
            return "approve_always", approve_always_match.group(1)

        approve_match = approve_pattern.match(normalized)
        if approve_match:
            return "approve", approve_match.group(1)

        reject_match = reject_pattern.match(normalized)
        if reject_match:
            return "reject", reject_match.group(1)

        return None

    def _format_approval_help(self) -> str:
        return "请直接点击下方按钮进行审批。"

    def _handle_approval_command(self, msg: InboundMessage, session_key: str, decision: str, specified_approval_id: str | None) -> BridgeResponse:
        request_id = self._new_request_id()
        pending = self._find_pending_approval_for_session(session_key)
        now = now_iso()

        if not pending:
            run_id = self._new_run_id()
            self.store.put_processed_event(
                msg.channel,
                msg.account_id,
                msg.event_id,
                request_id,
                run_id,
                now,
                (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            return self._error_response(
                request_id=request_id,
                run_id=run_id,
                message="当前没有待审批请求。",
                error_code="NO_PENDING_APPROVAL",
            )

        approval_id = pending["approval_id"]
        run_id = pending["run_id"]

        if specified_approval_id and specified_approval_id != approval_id:
            self.store.put_processed_event(
                msg.channel,
                msg.account_id,
                msg.event_id,
                request_id,
                run_id,
                now,
                (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            return self._error_response(
                request_id=request_id,
                run_id=run_id,
                message=f"approval_id 不匹配。当前待审批为：{approval_id}",
                error_code="APPROVAL_ID_MISMATCH",
            )

        self.store.put_processed_event(
            msg.channel,
            msg.account_id,
            msg.event_id,
            request_id,
            run_id,
            now,
            (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
        response = self.resolve_approval(approval_id, decision)
        response.request_id = request_id
        return response

    def _log_event(self, msg: InboundMessage, request_id: str, run_id: str, status: str):
        self.audit.log(
            {
                "event_id": msg.event_id,
                "request_id": request_id,
                "run_id": run_id,
                "status": status,
                "channel": msg.channel,
                "sender_hash": sender_hash(f"{msg.channel}:{msg.sender_id}"),
                "text": msg.text,
            }
        )

    def _ack_response(self, request_id: str, run_id: str) -> BridgeResponse:
        return BridgeResponse(
            request_id=request_id,
            run_id=run_id,
            status="accepted",
            message="Accepted",
            error_code=None,
            timestamp=now_iso(),
        )

    def _error_response(self, request_id: str, run_id: str, message: str, error_code: str, status: str = "failed") -> BridgeResponse:
        return BridgeResponse(
            request_id=request_id,
            run_id=run_id,
            status=status,
            message=message,
            error_code=error_code,
            timestamp=now_iso(),
        )

    def _internal_error_user_message(self) -> str:
        return "执行时出现异常，我已记录错误。请稍后重试，或换一种表述后再试一次。"

    def handle_inbound(self, msg: InboundMessage) -> BridgeResponse:
        now = now_iso()
        self.store.cleanup_processed_events(now)
        duplicate = self.store.get_processed_event(msg.channel, msg.account_id, msg.event_id)
        if duplicate:
            run = self.store.get_run_state(duplicate["run_id"])
            status = run["status"] if run else "accepted"
            return BridgeResponse(
                request_id=duplicate["request_id"],
                run_id=duplicate["run_id"],
                status=status,
                message="Duplicate event. Returning existing run.",
                error_code="DUPLICATE_EVENT",
                timestamp=now_iso(),
            )

        principal = self._principal(msg)
        pairing_state = self.pairing.ensure_pairing_request(msg.channel, msg.account_id, msg.sender_id, msg.sender_name)
        if pairing_state.get("status") != "approved":
            run_id = self._new_run_id()
            request_id = self._new_request_id()
            self.store.put_processed_event(
                msg.channel,
                msg.account_id,
                msg.event_id,
                request_id,
                run_id,
                now_iso(),
                (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            return self._error_response(
                request_id=request_id,
                run_id=run_id,
                message="Pairing required before command execution.",
                error_code="PAIRING_REQUIRED",
            )

        session_key = self._session_key(msg)
        approval_command = self._parse_approval_command(msg.text)
        if approval_command:
            decision, specified_approval_id = approval_command
            return self._handle_approval_command(
                msg=msg,
                session_key=session_key,
                decision=decision,
                specified_approval_id=specified_approval_id,
            )

        request_id = self._new_request_id()
        correlation_id = self._new_correlation_id()
        run_id = self._new_run_id()

        self.store.put_processed_event(
            msg.channel,
            msg.account_id,
            msg.event_id,
            request_id,
            run_id,
            now_iso(),
            (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )

        if not self.router.can_start_now(session_key):
            queued = self.router.enqueue(session_key, request_id)
            if not queued:
                return self._error_response(
                    request_id=request_id,
                    run_id=run_id,
                    message="Session queue is full.",
                    error_code="SESSION_BUSY",
                )
            self.pending_requests[request_id] = msg
            self.pending_request_meta[request_id] = {
                "run_id": run_id,
                "session_key": session_key,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "queued_at": now_iso(),
            }
            self.store.create_run_state(
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "session_key": session_key,
                    "status": "accepted",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "last_event_at": now_iso(),
                    "approval_id": None,
                    "checkpoint": None,
                    "message": "Queued behind active session run.",
                    "error_code": None,
                }
            )
            self._log_event(msg, request_id, run_id, "accepted")
            return self._ack_response(request_id, run_id)

        return self._execute_request(msg, request_id, run_id, session_key, correlation_id, principal)

    def _execute_request(self, msg: InboundMessage, request_id: str, run_id: str, session_key: str, correlation_id: str, principal: str) -> BridgeResponse:
        self.router.mark_active(session_key, run_id)
        self.store.create_run_state(
            {
                "run_id": run_id,
                "request_id": request_id,
                "session_key": session_key,
                "status": "running",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "last_event_at": now_iso(),
                "approval_id": None,
                "checkpoint": None,
                "message": "Running",
                "error_code": None,
            }
        )
        self._log_event(msg, request_id, run_id, "running")

        bridge_req = BridgeRequest(
            request_id=request_id,
            correlation_id=correlation_id,
            session_key=session_key,
            text=msg.text,
            attachments=msg.attachments,
            metadata={"run_id": run_id, "channel": msg.channel, "principal": principal},
            timestamp=now_iso(),
        )

        try:
            response, checkpoint, approval_id = self.bridge.execute(bridge_req)
        except Exception as exc:
            print("[gateway] bridge.execute exception:", repr(exc))
            traceback.print_exc()
            self.router.clear_active(session_key)
            self.store.update_run_state(
                run_id,
                status="failed",
                updated_at=now_iso(),
                last_event_at=now_iso(),
                message=str(exc),
                error_code="GATEWAY_INTERNAL_ERROR",
            )
            return self._error_response(
                request_id,
                run_id,
                self._internal_error_user_message(),
                "GATEWAY_INTERNAL_ERROR",
            )

        if response.status == "waiting_approval":
            response.message = f"{response.message}\n\n{self._format_approval_help()}"
            self.store.update_run_state(
                run_id,
                status="waiting_approval",
                updated_at=now_iso(),
                last_event_at=now_iso(),
                approval_id=approval_id,
                checkpoint=checkpoint,
                message=response.message,
                error_code=response.error_code,
            )
            self.store.create_approval_queue_item(
                {
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "session_key": session_key,
                    "status": "pending",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "command": msg.text,
                    "principal": principal,
                }
            )
            return response

        self.store.update_run_state(
            run_id,
            status=response.status,
            updated_at=now_iso(),
            last_event_at=now_iso(),
            checkpoint=checkpoint,
            message=response.message,
            error_code=response.error_code,
        )
        self.router.clear_active(session_key)
        self._drain_queue(session_key)
        return response

    def _drain_queue(self, session_key: str):
        if not self.router.can_start_now(session_key):
            return

        next_request_id = self.router.pop_next(session_key)
        if not next_request_id:
            return

        msg = self.pending_requests.pop(next_request_id, None)
        meta = self.pending_request_meta.pop(next_request_id, None)
        if not msg or not meta:
            return

        self._execute_request(
            msg=msg,
            request_id=meta["request_id"],
            run_id=meta["run_id"],
            session_key=meta["session_key"],
            correlation_id=meta["correlation_id"],
            principal=self._principal(msg),
        )

    def list_pending_approvals(self):
        return self.store.list_approval_queue(status="pending")

    def resolve_approval(self, approval_id: str, decision: str) -> BridgeResponse:
        item = self.store.get_approval_queue_item(approval_id)
        if not item:
            return self._error_response(approval_id, "unknown", "Approval request not found.", "APPROVAL_DENIED")

        run = self.store.get_run_state(item["run_id"])
        if not run:
            return self._error_response(approval_id, item["run_id"], "Run not found.", "AGENT_RUN_FAILED")

        session_key = run["session_key"]
        checkpoint = run.get("checkpoint")
        try:
            response, next_checkpoint = self.bridge.resolve_approval(
                session_key=session_key,
                run_id=item["run_id"],
                approval_id=approval_id,
                decision=decision,
                checkpoint=checkpoint,
            )
        except Exception as exc:
            print("[gateway] bridge.resolve_approval exception:", repr(exc))
            traceback.print_exc()
            self.store.update_approval_queue_item(
                approval_id,
                "rejected" if decision == "reject" else "pending",
                now_iso(),
            )
            self.store.update_run_state(
                item["run_id"],
                status="failed",
                updated_at=now_iso(),
                last_event_at=now_iso(),
                message=str(exc),
                error_code="GATEWAY_INTERNAL_ERROR",
            )
            self.router.clear_active(session_key)
            return self._error_response(
                approval_id,
                item["run_id"],
                self._internal_error_user_message(),
                "GATEWAY_INTERNAL_ERROR",
            )

        self.store.update_approval_queue_item(approval_id, "approved" if decision == "approve" else "rejected", now_iso())
        self.store.update_run_state(
            item["run_id"],
            status=response.status,
            updated_at=now_iso(),
            last_event_at=now_iso(),
            checkpoint=next_checkpoint,
            message=response.message,
            error_code=response.error_code,
        )

        if response.status != "waiting_approval":
            self.router.clear_active(session_key)
            self._drain_queue(session_key)

        return response

    def get_run(self, run_id: str):
        return self.store.get_run_state(run_id)

    def list_runs(self, statuses: tuple[str, ...] = ("accepted", "running", "waiting_approval", "completed", "failed", "timeout", "cancelled", "interrupted")):
        return self.store.list_runs_by_status(statuses)
