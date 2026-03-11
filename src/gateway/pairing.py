import uuid
from datetime import datetime, timedelta, timezone

from gateway.models import now_iso
from gateway.storage import GatewayStore


class PairingManager:

    def __init__(self, store: GatewayStore, pending_ttl_seconds: int = 86400):
        self.store = store
        self.pending_ttl_seconds = pending_ttl_seconds

    def principal_key(self, channel: str, account_id: str, sender_id: str) -> str:
        return f"{channel}:{account_id}:{sender_id}"

    def is_principal_allowed(self, principal: str) -> bool:
        row = self.store.get_principal(principal)
        return bool(row and row.get("status") == "approved")

    def ensure_pairing_request(self, channel: str, account_id: str, sender_id: str, sender_name: str) -> dict:
        principal = self.principal_key(channel, account_id, sender_id)
        if self.is_principal_allowed(principal):
            return {
                "principal": principal,
                "status": "approved",
            }

        existing = [
            item for item in self.store.list_pairing_requests(status="pending")
            if item.get("principal") == principal
        ]
        if existing:
            return existing[0]

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=self.pending_ttl_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        req = {
            "id": f"pair_{uuid.uuid4().hex[:8]}",
            "principal": principal,
            "channel": channel,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "status": "pending",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "expires_at": expires_at,
            "reason": "PAIRING_REQUIRED",
        }
        self.store.create_pairing_request(req)
        return req

    def approve(self, pairing_id: str) -> dict:
        req = self.store.get_pairing_request(pairing_id)
        if not req:
            raise ValueError("pairing request not found")

        ts = now_iso()
        self.store.update_pairing_status(pairing_id, "approved", ts)
        self.store.upsert_principal(
            principal=req["principal"],
            channel=req["channel"],
            sender_id=req["sender_id"],
            sender_name=req["sender_name"],
            status="approved",
            ts=ts,
        )
        return self.store.get_pairing_request(pairing_id)

    def reject(self, pairing_id: str) -> dict:
        req = self.store.get_pairing_request(pairing_id)
        if not req:
            raise ValueError("pairing request not found")
        self.store.update_pairing_status(pairing_id, "rejected", now_iso())
        return self.store.get_pairing_request(pairing_id)

    def revoke(self, principal: str):
        row = self.store.get_principal(principal)
        if not row:
            raise ValueError("principal not found")
        self.store.upsert_principal(
            principal=principal,
            channel=row["channel"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            status="revoked",
            ts=now_iso(),
        )

    def list_requests(self, status: str | None = None):
        return self.store.list_pairing_requests(status=status)

    def list_principals(self):
        return self.store.list_principals()
