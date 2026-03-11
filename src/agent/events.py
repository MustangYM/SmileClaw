import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


class RuntimeEventEmitter:

    def __init__(self, workspace_root):
        self.workspace_root = Path(workspace_root).resolve()
        self.events_path = self.workspace_root / ".smileclaw" / "events.jsonl"
        self.current_run_id = None
        self.sequence = 0

    def _now_iso(self):
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def start_run(self):
        self.current_run_id = str(uuid.uuid4())
        self.sequence = 0
        return self.current_run_id

    def has_active_run(self):
        return self.current_run_id is not None

    def end_run(self):
        self.current_run_id = None
        self.sequence = 0

    def emit(self, event_type, payload, correlation_id=None, error_code=None):
        if not self.current_run_id:
            self.start_run()

        self.sequence += 1
        event = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "timestamp": self._now_iso(),
            "run_id": self.current_run_id,
            "correlation_id": correlation_id or str(uuid.uuid4())[:8],
            "payload": payload,
            "error_code": error_code
        }

        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")

        return event
