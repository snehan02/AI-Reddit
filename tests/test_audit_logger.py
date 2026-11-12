"""Unit tests for AuditLogger."""

import concurrent.futures
from pathlib import Path
import sqlite3
import unittest
import uuid

from modules.audit_logger import AuditLogger, get_current_timestamp


class TestAuditLogger(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / f"test_audit_{uuid.uuid4().hex[:8]}.db"
        self.logger = AuditLogger(self.db_path)

    def tearDown(self):
        try:
            # Close any connections and cleanup test database
            for suffix in ["", "-wal", "-shm"]:
                p = Path(str(self.db_path) + suffix)
                if p.exists():
                    p.unlink()
        except Exception:
            pass

    def test_init_and_tables_created(self):
        with self.logger._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cursor.fetchall()]
            self.assertIn("tasks", tables)
            self.assertIn("execution_events", tables)
            self.assertIn("account_profiles", tables)
            self.assertIn("run_sessions", tables)

    def test_wal_mode_and_busy_timeout_enabled(self):
        """Verify SQLite Write-Ahead Logging (WAL) and busy timeout are configured."""
        with self.logger._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode;")
            journal_mode = cursor.fetchone()[0]
            self.assertEqual(journal_mode.lower(), "wal")

            cursor.execute("PRAGMA busy_timeout;")
            busy_timeout = cursor.fetchone()[0]
            self.assertGreaterEqual(busy_timeout, 5000)

    def test_concurrent_database_access(self):
        """Verify multiple threads can concurrently write tasks and events without locking errors."""
        def worker(thread_idx: int):
            for i in range(10):
                task_id = f"worker_{thread_idx}_task_{i}"
                self.logger.log_task(
                    task_id=task_id,
                    task_fingerprint=f"fp_{thread_idx}_{i}",
                    workbook_name="Reddit Threads.xlsx",
                    worksheet_name="Driving DMV",
                    excel_row_number=100 + i,
                    task_type="comment",
                    account_id="acc_concurrent@gmail.com",
                    destination_url="https://reddit.com/r/dmv",
                    task_status="Ready for review",
                )
                self.logger.log_event(
                    task_id=task_id,
                    event_type="TEST_CONCURRENT",
                    operator_action="READ",
                    outcome="Success",
                )
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, idx) for idx in range(5)]
            results = [f.result() for f in futures]
            self.assertTrue(all(results))

        events = self.logger.get_recent_events(limit=100)
        self.assertEqual(len(events), 50)

    def test_dom_snapshot_omitted_and_secrets_redacted_in_audit_logs(self):
        """Verify complete DOM snapshots and sensitive credentials are never stored in SQLite."""
        raw_dom = "<html><head><title>Test</title></head><body>" + "<div>content</div>" * 50 + "</body></html>"
        self.logger.log_event(
            task_id="t_dom_sec",
            event_type="OPENCLAW_SNAPSHOT",
            operator_action="DOM_SNAPSHOT",
            outcome="Retrieved DOM snapshot",
            error_details=raw_dom,
        )

        secret_err = "Failed with password=super_secret_admin_pass_123 and token=my_secret_token_abcdefg"
        self.logger.log_event(
            task_id="t_dom_sec",
            event_type="OPENCLAW_ERROR",
            operator_action="LOGIN",
            outcome=secret_err,
        )

        events = self.logger.get_recent_events(limit=5)
        # Check DOM snapshot was omitted
        dom_event = next(e for e in events if e["event_type"] == "OPENCLAW_SNAPSHOT")
        self.assertNotIn("<html>", dom_event["error_details"])
        self.assertIn("[DOM snapshot omitted", dom_event["error_details"])

        # Check secret was redacted
        sec_event = next(e for e in events if e["event_type"] == "OPENCLAW_ERROR")
        self.assertNotIn("super_secret_admin_pass_123", sec_event["outcome"])
        self.assertNotIn("my_secret_token_abcdefg", sec_event["outcome"])
        self.assertIn("[REDACTED]", sec_event["outcome"])

    def test_log_task_and_event(self):
        self.logger.log_task(
            task_id="t1",
            task_fingerprint="fp123",
            workbook_name="Reddit Threads.xlsx",
            worksheet_name="Driving DMV",
            excel_row_number=10,
            task_type="comment",
            account_id="acc1@gmail.com",
            destination_url="https://reddit.com",
            task_status="Ready for review",
        )
        self.logger.log_event(
            task_id="t1",
            event_type="CONFIRMED_COMPLETED",
            operator_action="CONFIRM",
            outcome="Completed - verified",
        )

        events = self.logger.get_recent_events(limit=5)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["task_id"], "t1")
        self.assertEqual(events[0]["outcome"], "Completed - verified")

    def test_daily_counter(self):
        self.assertEqual(self.logger.count_account_completed_today("acc1@gmail.com"), 0)
        self.logger.log_task(
            task_id="t2",
            task_fingerprint="fp2",
            workbook_name="Reddit Threads.xlsx",
            worksheet_name="Driving DMV",
            excel_row_number=11,
            task_type="comment",
            account_id="acc1@gmail.com",
            destination_url="https://reddit.com",
            task_status="Completed - human confirmed",
        )
        self.logger.log_event(
            task_id="t2",
            event_type="CONFIRMED_COMPLETED",
            outcome="Completed - verified",
        )
        self.assertEqual(self.logger.count_account_completed_today("acc1@gmail.com"), 1)


if __name__ == "__main__":
    unittest.main()
