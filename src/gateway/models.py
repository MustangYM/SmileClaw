from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class InboundMessage:
    channel: str
    account_id: str
    chat_id: str
    event_id: str
    sender_id: str
    sender_name: str
    text: str
    thread_id: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=now_iso)


@dataclass
class BridgeRequest:
    request_id: str
    correlation_id: str
    session_key: str
    text: str
    attachments: list[dict[str, Any]]
    metadata: dict[str, Any]
    timestamp: str


@dataclass
class BridgeResponse:
    request_id: str
    run_id: str
    status: str
    message: str
    error_code: str | None
    timestamp: str


@dataclass
class BridgeEvent:
    event_id: str
    run_id: str
    status: str
    message: str
    error_code: str | None
    timestamp: str
