"""Duplicate task detection and fingerprint reconciliation."""

from pathlib import Path
import sqlite3
from typing import Tuple
import logging

from modules.task_builder import CommentTask, PostTask

logger = logging.getLogger(__name__)


class DuplicateChecker:
    """Detects previously completed tasks using SQLite fingerprints."""

    def __init__(self, db_path: Path | str = "database/execution_state.db"):
        self.db_path = Path(db_path)

    def check_task(self, task: CommentTask | PostTask) -> Tuple[bool, str]:
        """Check if task was previously confirmed complete by this application.
        
        Returns:
            (is_duplicate, explanation)
        """
        if not self.db_path.exists():
            return False, ""

        fp = task.fingerprint
        if not fp:
            return False, ""

        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT task_id, task_status, updated_at
                FROM tasks
                WHERE task_fingerprint = ?;
                """, (fp,))
                rows = cursor.fetchall()

                for row in rows:
                    t_id, t_status, updated_at = row[0], row[1], row[2]
                    if "completed" in str(t_status).lower() or "verified" in str(t_status).lower():
                        return True, (
                            f"Skipped - duplicate: Task previously processed and completed by this application "
                            f"(Task ID: {t_id}, Status: '{t_status}', Completed at: {updated_at})"
                        )
        except Exception as exc:
            logger.error("Error querying duplicate history: %s", exc)
            return False, ""

        return False, ""

