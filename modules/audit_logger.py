"""Audit Logger and SQLite Database Manager."""

from datetime import datetime
from pathlib import Path
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from modules.openclaw_client import sanitize_text

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("database/execution_state.db")


def get_current_timestamp(tz_name: str = "Asia/Kolkata") -> str:
    """Return ISO 8601 formatted timestamp with timezone."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).isoformat()


class AuditLogger:
    """Manages SQLite tables and audit events for Shoro AI Reddit operations."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH, tz_name: str = "Asia/Kolkata"):
        self.db_path = Path(db_path)
        self.tz_name = tz_name
        self.init_database()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def init_database(self) -> None:
        """Create tables per PRD FR-11 if they do not exist and configure WAL mode."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Enable SQLite Write-Ahead Logging (WAL) and busy timeout
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.execute("PRAGMA synchronous=NORMAL;")

            # 1. tasks table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_fingerprint TEXT NOT NULL,
                workbook_name TEXT NOT NULL,
                worksheet_name TEXT NOT NULL,
                excel_row_number INTEGER NOT NULL,
                task_type TEXT NOT NULL,
                account_id TEXT,
                destination_url TEXT,
                task_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_fingerprint ON tasks(task_fingerprint);
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_account_status ON tasks(account_id, task_status);
            """)

            # 2. execution_events table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS execution_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                event_type TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                operator_action TEXT,
                outcome TEXT,
                error_code TEXT,
                error_details TEXT
            );
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_task_id ON execution_events(task_id);
            """)

            # 3. account_profiles table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS account_profiles (
                account_id TEXT PRIMARY KEY,
                chrome_profile_name TEXT NOT NULL,
                user_data_directory TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

            # 4. run_sessions table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS run_sessions (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                run_mode TEXT NOT NULL,
                run_status TEXT NOT NULL,
                total_tasks INTEGER DEFAULT 0,
                completed_tasks INTEGER DEFAULT 0,
                skipped_tasks INTEGER DEFAULT 0,
                failed_tasks INTEGER DEFAULT 0
            );
            """)
            conn.commit()

    def log_task(
        self,
        task_id: str,
        task_fingerprint: str,
        workbook_name: str,
        worksheet_name: str,
        excel_row_number: int,
        task_type: str,
        account_id: Optional[str],
        destination_url: Optional[str],
        task_status: str,
    ) -> None:
        """Upsert a task into the tasks table."""
        now = get_current_timestamp(self.tz_name)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO tasks (
                task_id, task_fingerprint, workbook_name, worksheet_name,
                excel_row_number, task_type, account_id, destination_url,
                task_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                task_status = excluded.task_status,
                updated_at = excluded.updated_at;
            """, (
                task_id, task_fingerprint, workbook_name, worksheet_name,
                excel_row_number, task_type, account_id, destination_url,
                task_status, now, now
            ))
            conn.commit()

    def log_event(
        self,
        task_id: Optional[str],
        event_type: str,
        operator_action: Optional[str] = None,
        outcome: Optional[str] = None,
        error_code: Optional[str] = None,
        error_details: Optional[str] = None,
    ) -> int:
        """Append an event to execution_events.
        
        Strictly enforces that complete DOM snapshots and sensitive credentials
        are never stored in SQLite audit logs.
        """
        def _clean_audit_field(val: Optional[str], max_len: int = 500) -> Optional[str]:
            if val is None:
                return None
            s = str(val).strip()
            # Redact known credentials
            s = sanitize_text(s)
            # Detect and omit complete DOM snapshots or HTML documents
            s_lower = s.lower()
            if ("<html" in s_lower or "<body" in s_lower or "<!doctype" in s_lower or ("<div" in s_lower and len(s) > 200)):
                return f"[DOM snapshot omitted - length: {len(s)} chars]"
            if len(s) > max_len:
                return s[:max_len] + f"... [truncated, total {len(s)} chars]"
            return s

        clean_outcome = _clean_audit_field(outcome, max_len=500)
        clean_action = _clean_audit_field(operator_action, max_len=100)
        clean_details = _clean_audit_field(error_details, max_len=500)

        now = get_current_timestamp(self.tz_name)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO execution_events (
                task_id, event_type, event_timestamp, operator_action,
                outcome, error_code, error_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, event_type, now, clean_action,
                clean_outcome, error_code, clean_details
            ))
            conn.commit()
            return cursor.lastrowid

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent execution events."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT event_id, task_id, event_type, event_timestamp,
                   operator_action, outcome, error_code, error_details
            FROM execution_events
            ORDER BY event_id DESC
            LIMIT ?;
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def count_account_completed_today(self, account_id: str) -> int:
        """Count human-confirmed completed tasks for account today in display timezone."""
        if not account_id:
            return 0
        now = datetime.now(ZoneInfo(self.tz_name))
        today_prefix = now.strftime("%Y-%m-%d")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Count events where event_type is CONFIRMED_COMPLETED on today's date for this account
            cursor.execute("""
            SELECT COUNT(e.event_id)
            FROM execution_events e
            JOIN tasks t ON e.task_id = t.task_id
            WHERE t.account_id = ?
              AND e.outcome = 'Completed - verified'
              AND e.event_timestamp LIKE ?;
            """, (account_id, f"{today_prefix}%"))
            result = cursor.fetchone()
            return result[0] if result else 0

