"""Unit tests for DuplicateChecker."""

import unittest
import uuid
from pathlib import Path
from modules.audit_logger import AuditLogger
from modules.duplicate_checker import DuplicateChecker
from modules.task_builder import CommentTask


class TestDuplicateChecker(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / f"test_dup_{uuid.uuid4().hex[:8]}.db"
        self.logger = AuditLogger(self.db_path)
        self.checker = DuplicateChecker(self.db_path)

    def tearDown(self):
        try:
            if self.db_path.exists():
                self.db_path.unlink()
        except Exception:
            pass

    def test_detect_completed_duplicate(self):
        task = CommentTask(
            worksheet="Driving DMV",
            excel_row=15,
            account_id="acc1@gmail.com",
            destination_url="https://reddit.com/r/driving",
            reply_text="Some helpful text",
        )
        # Initially not duplicate
        is_dup, reason = self.checker.check_task(task)
        self.assertFalse(is_dup)

        # Mark completed in DB
        self.logger.log_task(
            task_id=task.task_id,
            task_fingerprint=task.fingerprint,
            workbook_name="Reddit Threads.xlsx",
            worksheet_name=task.worksheet,
            excel_row_number=task.excel_row,
            task_type="comment",
            account_id=task.account_id,
            destination_url=task.destination_url,
            task_status="Completed - human confirmed",
        )

        is_dup2, reason2 = self.checker.check_task(task)
        self.assertTrue(is_dup2)
        self.assertIn("previously processed and completed by this application", reason2.lower())


if __name__ == "__main__":
    unittest.main()
