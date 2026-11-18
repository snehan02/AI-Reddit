"""Audit Logger and SQLite Database Manager."""

from datetime import datetime
from pathlib import Path
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
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

            # 5. prepared_items table for persistent batch drafts across restarts
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS prepared_items (
                item_id TEXT PRIMARY KEY,
                task_id TEXT,
                account_id TEXT,
                profile_name TEXT,
                task_type TEXT NOT NULL,
                worksheet TEXT NOT NULL,
                excel_row INTEGER NOT NULL,
                title TEXT,
                content TEXT,
                destination_url TEXT,
                subreddit TEXT,
                flair TEXT,
                status TEXT NOT NULL DEFAULT 'Ready for Review',
                prepared_at TEXT NOT NULL,
                published_at TEXT,
                error_message TEXT
            );
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prepared_status ON prepared_items(status);
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prepared_account ON prepared_items(account_id);
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

    def save_prepared_item(
        self,
        item_id: str,
        task_id: str,
        account_id: str,
        profile_name: str,
        task_type: str,
        worksheet: str,
        excel_row: int,
        title: Optional[str],
        content: str,
        destination_url: Optional[str] = "",
        subreddit: Optional[str] = None,
        flair: Optional[str] = None,
        status: str = "Ready for Review",
        prepared_at: Optional[str] = None,
        published_at: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Persist a prepared post or comment draft for operator review."""
        now = prepared_at or get_current_timestamp(self.tz_name)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO prepared_items (
                item_id, task_id, account_id, profile_name, task_type,
                worksheet, excel_row, title, content, destination_url,
                subreddit, flair, status, prepared_at, published_at, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                account_id = excluded.account_id,
                profile_name = excluded.profile_name,
                title = excluded.title,
                content = excluded.content,
                destination_url = excluded.destination_url,
                subreddit = excluded.subreddit,
                flair = excluded.flair,
                status = excluded.status,
                prepared_at = excluded.prepared_at,
                published_at = excluded.published_at,
                error_message = excluded.error_message;
            """, (
                item_id, task_id, account_id, profile_name, task_type,
                worksheet, excel_row, title, content, destination_url,
                subreddit, flair, status, now, published_at, error_message
            ))
            conn.commit()

    def get_prepared_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single prepared item by item_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM prepared_items WHERE item_id = ?;", (item_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_prepared_items(
        self,
        status: Optional[str] = None,
        account_id: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve prepared items with optional filters, sorted by prepared_at DESC."""
        query = "SELECT * FROM prepared_items WHERE 1=1"
        params: List[Any] = []
        if status and status.lower() != "all":
            if status.lower() in ("ready", "ready for review"):
                query += " AND (status = 'Ready' OR status = 'Ready for Review')"
            else:
                query += " AND status = ?"
                params.append(status)
        if account_id and account_id.lower() != "all":
            query += " AND account_id = ?"
            params.append(account_id)
        if task_type and task_type.lower() != "all":
            query += " AND task_type = ?"
            params.append(task_type)
        query += " ORDER BY prepared_at DESC;"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def update_prepared_item_status(
        self,
        item_id: str,
        status: str,
        published_at: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update status of a prepared item upon review or publishing."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if published_at:
                cursor.execute("""
                UPDATE prepared_items
                SET status = ?, published_at = ?, error_message = ?
                WHERE item_id = ?;
                """, (status, published_at, error_message, item_id))
            else:
                cursor.execute("""
                UPDATE prepared_items
                SET status = ?, error_message = ?
                WHERE item_id = ?;
                """, (status, error_message, item_id))
            conn.commit()

    def get_last_published_timestamp(self, account_id: str) -> Optional[str]:
        """Get the latest published_at ISO timestamp for an account."""
        if not account_id:
            return None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT MAX(published_at)
            FROM prepared_items
            WHERE account_id = ? AND status = 'Complete' AND published_at IS NOT NULL;
            """, (account_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
            # Fallback to tasks table updated_at where task_status is a genuine publication
            cursor.execute("""
            SELECT MAX(updated_at)
            FROM tasks
            WHERE account_id = ? 
              AND task_status NOT LIKE 'Skipped%'
              AND (task_status = 'Complete' OR task_status LIKE 'Complete%' OR task_status LIKE 'Completed%');
            """, (account_id,))
            row2 = cursor.fetchone()
            return row2[0] if row2 and row2[0] else None

    def check_eight_hour_gap(self, account_id: str, min_gap_hours: float = 8.0) -> Tuple[bool, str]:
        """Check if at least min_gap_hours (default 8) have elapsed since last publication.
        
        Drafts and pending preparations do NOT trigger this limit; only completed publications do.
        """
        last_pub_str = self.get_last_published_timestamp(account_id)
        if not last_pub_str:
            return True, "No prior publications recorded for this account. Eligible to publish."

        try:
            # Handle ISO formats
            clean_str = last_pub_str.replace("Z", "+00:00")
            last_dt = datetime.fromisoformat(clean_str)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=ZoneInfo(self.tz_name))
            now_dt = datetime.now(ZoneInfo(self.tz_name))
            elapsed = (now_dt - last_dt).total_seconds() / 3600.0
            if elapsed < min_gap_hours:
                rem_mins = int((min_gap_hours - elapsed) * 60)
                next_eligible = last_dt.timestamp() + (min_gap_hours * 3600)
                next_eligible_dt = datetime.fromtimestamp(next_eligible, tz=ZoneInfo(self.tz_name))
                gap_label = f"{int(min_gap_hours)}-Hour" if min_gap_hours == int(min_gap_hours) else f"{min_gap_hours:.1f}-Hour"
                return False, (
                    f"{gap_label} Minimum Gap Rule: Account '{account_id}' published {elapsed:.1f}h ago. "
                    f"Must wait {rem_mins} more minutes. Next eligible publication time: "
                    f"{next_eligible_dt.strftime('%Y-%m-%d %H:%M:%S')}."
                )
            return True, f"Eligible: {elapsed:.1f} hours elapsed since last publication."
        except Exception as e:
            logger.warning("Could not parse last published timestamp '%s': %s", last_pub_str, e)
            return True, "Timestamp parse fallback: eligible to publish."

    def check_publication_gap(self, account_id: str, min_gap_hours: Optional[float] = None) -> Tuple[bool, str]:
        """Check if at least min_gap_hours have elapsed since last publication."""
        gap = 8.0 if min_gap_hours is None else float(min_gap_hours)
        return self.check_eight_hour_gap(account_id, min_gap_hours=gap)

    def count_account_published_total(self, account_id: str) -> int:
        """Count total published posts/comments for an account."""
        if not account_id:
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT COUNT(item_id)
            FROM prepared_items
            WHERE account_id = ? AND status = 'Complete';
            """, (account_id,))
            res = cursor.fetchone()
            return res[0] if res else 0

    def count_account_published_today(self, account_id: str) -> int:
        """Count published posts/comments for an account today in local timezone."""
        if not account_id:
            return 0
        now_dt = datetime.now(ZoneInfo(self.tz_name))
        today_prefix = now_dt.strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT COUNT(item_id)
            FROM prepared_items
            WHERE account_id = ? AND status = 'Complete' AND (published_at LIKE ? OR prepared_at LIKE ?);
            """, (account_id, f"{today_prefix}%", f"{today_prefix}%"))
            res = cursor.fetchone()
            count = res[0] if res else 0

            cursor.execute("""
            SELECT COUNT(task_id)
            FROM tasks
            WHERE account_id = ? 
              AND (task_status = 'Complete' OR task_status LIKE 'Complete%' OR task_status LIKE 'Completed%')
              AND updated_at LIKE ?;
            """, (account_id, f"{today_prefix}%"))
            res2 = cursor.fetchone()
            tasks_count = res2[0] if res2 else 0
            return max(count, tasks_count)



