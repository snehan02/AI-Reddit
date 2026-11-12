"""Unit tests for StateManager."""

import unittest
import uuid
from pathlib import Path
from modules.audit_logger import AuditLogger
from modules.state_manager import StateManager, RunStatus


class TestStateManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        unique_id = uuid.uuid4().hex[:8]
        self.db_path = self.test_dir / f"test_state_{unique_id}.db"
        self.state_file = self.test_dir / f"test_session_state_{unique_id}.json"

        self.logger = AuditLogger(self.db_path)
        self.state_mgr = StateManager(self.logger, daily_cap=2, state_file=self.state_file)

    def tearDown(self):
        try:
            if self.db_path.exists():
                self.db_path.unlink()
            if self.state_file.exists():
                self.state_file.unlink()
        except Exception:
            pass

    def test_session_lifecycle(self):
        run_id = self.state_mgr.start_session(test_mode=True, total_tasks=10)
        self.assertEqual(self.state_mgr.run_status, RunStatus.RUNNING)

        self.state_mgr.pause_session()
        self.assertEqual(self.state_mgr.run_status, RunStatus.PAUSED)

        self.state_mgr.resume_session()
        self.assertEqual(self.state_mgr.run_status, RunStatus.RUNNING)

        self.state_mgr.stop_session()
        self.assertEqual(self.state_mgr.run_status, RunStatus.STOPPED)

    def test_daily_cap_enforcement(self):
        acc = "user1@gmail.com"
        # 0 comments: allowed
        allowed, count, msg = self.state_mgr.check_daily_cap(acc)
        self.assertTrue(allowed)
        self.assertEqual(count, 0)

        # Log 2 completions
        for i in range(2):
            self.logger.log_task(
                task_id=f"t_{i}",
                task_fingerprint=f"fp_{i}",
                workbook_name="wb",
                worksheet_name="Driving DMV",
                excel_row_number=i + 2,
                task_type="comment",
                account_id=acc,
                destination_url="https://reddit.com",
                task_status="Completed - human confirmed",
            )
            self.logger.log_event(
                task_id=f"t_{i}",
                event_type="CONFIRMED_COMPLETED",
                outcome="Completed - verified",
            )

        allowed2, count2, msg2 = self.state_mgr.check_daily_cap(acc)
        self.assertFalse(allowed2)
        self.assertEqual(count2, 2)
        self.assertIn("Daily cap reached", msg2)


if __name__ == "__main__":
    unittest.main()
