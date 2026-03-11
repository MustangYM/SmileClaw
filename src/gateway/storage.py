import sqlite3
from contextlib import contextmanager
from pathlib import Path


class GatewayStore:

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, channel, account_id)
                );

                CREATE TABLE IF NOT EXISTS pairing_requests (
                    id TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    reason TEXT
                );

                CREATE TABLE IF NOT EXISTS approved_principals (
                    principal TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approval_queue (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    command TEXT,
                    principal TEXT
                );

                CREATE TABLE IF NOT EXISTS run_state (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_event_at TEXT NOT NULL,
                    approval_id TEXT,
                    checkpoint TEXT,
                    message TEXT,
                    error_code TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_run_state_session_status
                ON run_state(session_key, status);

                CREATE INDEX IF NOT EXISTS idx_approval_queue_status
                ON approval_queue(status);
                """
            )

    def get_processed_event(self, channel: str, account_id: str, event_id: str):
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM processed_events
                WHERE channel = ? AND account_id = ? AND event_id = ?
                """,
                (channel, account_id, event_id),
            ).fetchone()
        return dict(row) if row else None

    def put_processed_event(self, channel: str, account_id: str, event_id: str, request_id: str, run_id: str, first_seen_at: str, expires_at: str):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_events(
                    event_id, channel, account_id, request_id, run_id, first_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, channel, account_id, request_id, run_id, first_seen_at, expires_at),
            )

    def cleanup_processed_events(self, now_iso: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM processed_events WHERE expires_at <= ?", (now_iso,))

    def create_pairing_request(self, req: dict):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO pairing_requests(id, principal, channel, sender_id, sender_name, status, created_at, updated_at, expires_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req["id"], req["principal"], req["channel"], req["sender_id"], req["sender_name"],
                    req["status"], req["created_at"], req["updated_at"], req.get("expires_at"), req.get("reason")
                ),
            )

    def list_pairing_requests(self, status: str | None = None):
        with self._conn() as conn:
            if status:
                rows = conn.execute("SELECT * FROM pairing_requests WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM pairing_requests ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def update_pairing_status(self, req_id: str, status: str, updated_at: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE pairing_requests SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, req_id),
            )

    def get_pairing_request(self, req_id: str):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM pairing_requests WHERE id = ?", (req_id,)).fetchone()
        return dict(row) if row else None

    def upsert_principal(self, principal: str, channel: str, sender_id: str, sender_name: str, status: str, ts: str):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO approved_principals(principal, channel, sender_id, sender_name, status, approved_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(principal) DO UPDATE SET
                    status = excluded.status,
                    sender_name = excluded.sender_name,
                    updated_at = excluded.updated_at
                """,
                (principal, channel, sender_id, sender_name, status, ts, ts),
            )

    def get_principal(self, principal: str):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM approved_principals WHERE principal = ?", (principal,)).fetchone()
        return dict(row) if row else None

    def list_principals(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM approved_principals ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def create_run_state(self, row: dict):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO run_state(run_id, request_id, session_key, status, created_at, updated_at, last_event_at, approval_id, checkpoint, message, error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["run_id"], row["request_id"], row["session_key"], row["status"], row["created_at"],
                    row["updated_at"], row["last_event_at"], row.get("approval_id"), row.get("checkpoint"),
                    row.get("message"), row.get("error_code")
                ),
            )

    def update_run_state(self, run_id: str, **fields):
        if not fields:
            return
        columns = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        values.append(run_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE run_state SET {columns} WHERE run_id = ?", values)

    def get_run_state(self, run_id: str):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM run_state WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_active_runs_by_session(self, session_key: str):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM run_state WHERE session_key = ? AND status IN ('accepted', 'running', 'waiting_approval') ORDER BY created_at ASC",
                (session_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_runs_by_status(self, statuses: tuple[str, ...]):
        placeholders = ",".join("?" for _ in statuses)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM run_state WHERE status IN ({placeholders}) ORDER BY updated_at ASC",
                statuses,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_approval_queue_item(self, row: dict):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO approval_queue(approval_id, run_id, session_key, status, created_at, updated_at, command, principal)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["approval_id"], row["run_id"], row["session_key"], row["status"], row["created_at"],
                    row["updated_at"], row.get("command"), row.get("principal")
                ),
            )

    def update_approval_queue_item(self, approval_id: str, status: str, updated_at: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE approval_queue SET status = ?, updated_at = ? WHERE approval_id = ?",
                (status, updated_at, approval_id),
            )

    def get_approval_queue_item(self, approval_id: str):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM approval_queue WHERE approval_id = ?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def list_approval_queue(self, status: str | None = None):
        with self._conn() as conn:
            if status:
                rows = conn.execute("SELECT * FROM approval_queue WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM approval_queue ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]
