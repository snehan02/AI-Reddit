"""Integration tests for WorkflowController."""

import unittest
import uuid
from pathlib import Path
from config import AppSettings
from modules.workflow_controller import WorkflowController
from modules.state_manager import RunStatus


class TestWorkflowController(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        unique_id = uuid.uuid4().hex[:8]
        self.db_path = str(self.test_dir / f"test_wf_{unique_id}.db")

        self.settings = AppSettings(
            workbook_path="data/Reddit Threads.xlsx",
            comment_sheet="Driving DMV",
            post_sheet="Posts",
            test_mode=True,
            daily_comment_cap=5,
            database_path=self.db_path,
            backup_directory=str(self.test_dir / "backups"),
        )
        self.controller = WorkflowController(self.settings)

    def tearDown(self):
        try:
            p = Path(self.db_path)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def test_workflow_initialization_and_counts(self):
        report = self.controller.initialize()
        self.assertTrue(report.file_exists)
        self.assertTrue(report.is_valid_for_comments)
        self.assertFalse(report.is_valid_for_posts)  # Missing Account posted

        stats = self.controller.stats
        self.assertGreaterEqual(stats["total_comments"], 489)
        self.assertEqual(stats["total_comments"], len(self.controller.comment_tasks))
        # With all accounts disabled by default, 0 tasks are eligible
        self.assertEqual(stats["eligible_comments"], 0)
        self.assertEqual(stats["skipped_comments"], len(self.controller.comment_tasks))
        self.assertEqual(stats["total_posts"], len(self.controller.post_tasks))
        # posts eligible in account-independent mode, or 0 if accounts required
        self.assertIn(stats["eligible_posts"], (0, 5, 14, len(self.controller.post_tasks)))
        self.assertIn(stats["total_queue"], (0, 5, 14, len(self.controller.post_tasks)))

    def test_workflow_session_and_record_outcome(self):
        # Enable one verified account for workflow testing
        self.controller.account_manager.load()
        if "pankajtulshyan@gmail.com" in self.controller.account_manager.accounts:
            self.controller.account_manager.accounts["pankajtulshyan@gmail.com"].enabled = True

        self.controller.initialize(reload_accounts=False)
        run_id = self.controller.start_run()
        self.assertTrue(run_id.startswith("run_"))
        self.assertEqual(self.controller.state_manager.run_status, RunStatus.RUNNING)

        task = self.controller.get_next_task()
        self.assertIsNotNone(task)
        self.assertTrue(task.is_eligible)
        self.assertEqual(task.account_id, "pankajtulshyan@gmail.com")

        # In Test Mode, recording outcome should succeed without altering live workbook
        success, msg = self.controller.record_outcome(task, "Completed - verified")
        self.assertTrue(success)
        self.assertIn("Test Mode", msg)
        self.assertEqual(task.status, "Test Mode - review verified")

        self.controller.stop_run()
        self.assertEqual(self.controller.state_manager.run_status, RunStatus.STOPPED)

    def test_open_in_openclaw_url_security_rejection(self):
        """Verify workflow controller blocks unapproved URLs from browser navigation."""
        bad_urls = [
            "http://localhost:8080",
            "http://127.0.0.1:18789",
            "http://169.254.169.254/latest/meta-data/",
            "https://reddit.com.attacker.com",
            "https://attacker.com/reddit.com",
            "javascript:alert(1)",
        ]
        for url in bad_urls:
            success, msg, data = self.controller.open_in_openclaw(url, "shoro-test")
            self.assertFalse(success, f"Expected navigation to {url} to be blocked")
            self.assertIn("invalid URL", msg)
            self.assertIsNone(data)

        # Verify audit log recorded rejection
        events = self.controller.audit_logger.get_recent_events(limit=10)
        invalid_events = [e for e in events if e.get("error_code") == "INVALID_URL"]
        self.assertTrue(len(invalid_events) > 0, "Expected INVALID_URL error event in audit log")

    def test_force_test_mode_environment_safeguard(self):
        """Verify FORCE_TEST_MODE locks test mode ON and prevents live completion."""
        import os
        os.environ["FORCE_TEST_MODE"] = "true"
        try:
            self.assertTrue(self.settings.is_force_test_mode_active())

            # Attempt to set test_mode to False should be rejected
            self.settings.test_mode = False
            self.assertTrue(self.settings.test_mode, "test_mode must remain True when FORCE_TEST_MODE is set")

            # Enable one account for task processing
            self.controller.account_manager.load()
            if "pankajtulshyan@gmail.com" in self.controller.account_manager.accounts:
                self.controller.account_manager.accounts["pankajtulshyan@gmail.com"].enabled = True
            self.controller.initialize(reload_accounts=False)

            task = self.controller.get_next_task()
            self.assertIsNotNone(task)

            # Even if settings.test_mode were somehow set to False, record_outcome checks FORCE_TEST_MODE
            success, msg = self.controller.record_outcome(task, "Completed - verified")
            self.assertTrue(success)
            self.assertIn("Test Mode", msg)
            self.assertEqual(task.status, "Test Mode - review verified")
        finally:
            os.environ.pop("FORCE_TEST_MODE", None)

    def test_excel_handler_blocks_under_force_test_mode(self):
        """Verify ExcelHandler refuses to update workbook if FORCE_TEST_MODE is active."""
        import os
        os.environ["FORCE_TEST_MODE"] = "1"
        try:
            success, msg = self.controller.excel_handler.update_task_status(
                worksheet_name="Driving DMV",
                excel_row=2,
                new_status="Completed - human confirmed",
            )
            self.assertFalse(success)
            self.assertIn("FORCE_TEST_MODE environment safeguard is active", msg)
        finally:
            os.environ.pop("FORCE_TEST_MODE", None)


if __name__ == "__main__":
    unittest.main()

