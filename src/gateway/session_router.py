from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class QueuedRequest:
    request_id: str
    enqueued_at: datetime


class SessionRouter:

    def __init__(self, max_queue_length_per_session: int = 3, queued_ttl_seconds: int = 300):
        self.max_queue_length = max_queue_length_per_session
        self.queued_ttl_seconds = queued_ttl_seconds
        self.active_run_by_session: dict[str, str] = {}
        self.queues: dict[str, deque[QueuedRequest]] = defaultdict(deque)

    def _now(self):
        return datetime.now(timezone.utc)

    def _cleanup_expired(self, session_key: str):
        queue = self.queues[session_key]
        cutoff = self._now() - timedelta(seconds=self.queued_ttl_seconds)
        while queue and queue[0].enqueued_at < cutoff:
            queue.popleft()

    def can_start_now(self, session_key: str) -> bool:
        return session_key not in self.active_run_by_session

    def mark_active(self, session_key: str, run_id: str):
        self.active_run_by_session[session_key] = run_id

    def clear_active(self, session_key: str):
        self.active_run_by_session.pop(session_key, None)

    def enqueue(self, session_key: str, request_id: str) -> bool:
        self._cleanup_expired(session_key)
        queue = self.queues[session_key]
        if len(queue) >= self.max_queue_length:
            return False
        queue.append(QueuedRequest(request_id=request_id, enqueued_at=self._now()))
        return True

    def pop_next(self, session_key: str) -> str | None:
        self._cleanup_expired(session_key)
        queue = self.queues.get(session_key)
        if not queue:
            return None
        if not queue:
            return None
        item = queue.popleft()
        return item.request_id

    def queue_length(self, session_key: str) -> int:
        self._cleanup_expired(session_key)
        return len(self.queues[session_key])
