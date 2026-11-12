"""State and session lifecycle manager."""

from datetime import datetime
from enum import Enum
from pathlib import Path
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from modules.audit_logger import AuditLogger, get_current_timestamp

logger = logging.getLogger(__name__)


class RunStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


class StateManager:
    """Tracks active sessions, in-flight tasks, daily activity limits, and resume state."""

    def __init__(
        self,
        audit_logger: AuditLogger,
        daily_cap: int = 5,
        state_file: Path | str = "database/session_state.json",
    ):
        self.audit_logger = audit_logger
        self.daily_cap = daily_cap
        self.state_file = Path(state_file)
        self.current_run_id: Optional[str] = None
        self.run_status: RunStatus = RunStatus.IDLE
        self.current_task_id: Optional[str] = None
        self.test_mode: bool = True
        self.load_state()

    def load_state(self) -> None:
        """Load session state from file if exists."""
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.current_run_id = data.get("current_run_id")
            saved_status = data.get("run_status", RunStatus.IDLE.value)
            # If application was RUNNING when saved/terminated, recover safely
            if saved_status == RunStatus.RUNNING.value:
                logger.warning(
                    "Recovering from previous ungraceful termination. Setting status to PAUSED."
                )
                self.run_status = RunStatus.PAUSED
            else:
                self.run_status = RunStatus(saved_status)
            self.current_task_id = data.get("current_task_id")
            self.test_mode = data.get("test_mode", True)
        except Exception as exc:
            logger.error("Failed to load session state: %s", exc)

    def persist_state(self) -> None:
        """Atomically persist state to json file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "current_run_id": self.current_run_id,
            "run_status": self.run_status.value,
            "current_task_id": self.current_task_id,
            "test_mode": self.test_mode,
            "updated_at": get_current_timestamp(self.audit_logger.tz_name),
        }
        tmp_file = self.state_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(self.state_file)

    def start_session(self, test_mode: bool = True, total_tasks: int = 0) -> str:
        """Start a new run session."""
        self.current_run_id = f"run_{uuid.uuid4().hex[:8]}"
        self.run_status = RunStatus.RUNNING
        self.test_mode = test_mode
        self.current_task_id = None

        now = get_current_timestamp(self.audit_logger.tz_name)
        with self.audit_logger._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO run_sessions (
                run_id, started_at, run_mode, run_status, total_tasks
            ) VALUES (?, ?, ?, ?, ?)
            """, (
                self.current_run_id,
                now,
                "TEST MODE" if test_mode else "PRODUCTION",
                self.run_status.value,
                total_tasks,
            ))
            conn.commit()

        self.persist_state()
        self.audit_logger.log_event(
            task_id=None,
            event_type="SESSION_STARTED",
            operator_action="START",
            outcome=f"Started run session {self.current_run_id} (Test Mode: {test_mode})",
        )
        return self.current_run_id

    def pause_session(self) -> None:
        """Pause active run session."""
        self.run_status = RunStatus.PAUSED
        self.persist_state()
        self.audit_logger.log_event(
            task_id=self.current_task_id,
            event_type="SESSION_PAUSED",
            operator_action="PAUSE",
            outcome=f"Paused run session {self.current_run_id}",
        )

    def resume_session(self) -> None:
        """Resume paused run session."""
        self.run_status = RunStatus.RUNNING
        self.persist_state()
        self.audit_logger.log_event(
            task_id=self.current_task_id,
            event_type="SESSION_RESUMED",
            operator_action="RESUME",
            outcome=f"Resumed run session {self.current_run_id}",
        )

    def stop_session(self) -> None:
        """Safely stop run session."""
        self.run_status = RunStatus.STOPPED
        now = get_current_timestamp(self.audit_logger.tz_name)
        if self.current_run_id:
            with self.audit_logger._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                UPDATE run_sessions
                SET run_status = ?, ended_at = ?
                WHERE run_id = ?;
                """, (self.run_status.value, now, self.current_run_id))
                conn.commit()

        self.persist_state()
        self.audit_logger.log_event(
            task_id=self.current_task_id,
            event_type="SESSION_STOPPED",
            operator_action="STOP",
            outcome=f"Stopped run session {self.current_run_id}",
        )

    def check_daily_cap(self, account_id: str) -> Tuple[bool, int, str]:
        """Check if account has reached daily comment cap.
        
        Returns:
            (is_allowed, current_count, message)
        """
        count = self.audit_logger.count_account_completed_today(account_id)
        if count >= self.daily_cap:
            return (
                False,
                count,
                f"Daily cap reached for account '{account_id}': {count}/{self.daily_cap} comments completed today.",
            )
        return True, count, f"Account '{account_id}': {count}/{self.daily_cap} comments today."

