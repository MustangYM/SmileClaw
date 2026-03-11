import hashlib
import json
import re
from pathlib import Path


PII_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:\+?\d[\d\-\s]{7,}\d)\b"),
]
TOKEN_PATTERNS = [
    re.compile(r"(?i)(token|secret|webhook)[\s:=]+[^\s,]+"),
    re.compile(r"https?://[^\s]*?(token|signature|sig|secret)=[^\s&]+", re.IGNORECASE),
]


def _mask_text(text: str) -> str:
    output = text
    for pattern in TOKEN_PATTERNS:
        output = pattern.sub("[REDACTED]", output)
    for pattern in PII_PATTERNS:
        output = pattern.sub("[REDACTED]", output)
    output = re.sub(r"https?://[^\s]+", "[URL]", output)
    return output


def sender_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class AuditLogger:

    def __init__(self, workspace_root: str | Path):
        root = Path(workspace_root)
        self.log_dir = root / ".smileclaw" / "audit"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.log_dir / "events.jsonl"

    def log(self, record: dict):
        sanitized = {}
        for key, value in record.items():
            if isinstance(value, str):
                sanitized[key] = _mask_text(value)
            elif isinstance(value, dict):
                sanitized[key] = {
                    k: (_mask_text(v) if isinstance(v, str) else v)
                    for k, v in value.items()
                }
            else:
                sanitized[key] = value

        with self.events_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(sanitized, ensure_ascii=False) + "\n")
