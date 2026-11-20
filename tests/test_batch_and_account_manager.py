"""Tests for Multi-Account Automation, Batch Preparation, Draft Persistence, and 8-Hour Rule."""

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from modules.account_manager import AccountManager, AccountProfile
from modules.audit_logger import AuditLogger, get_current_timestamp
from modules.workflow_controller import WorkflowController
from config import AppSettings


class TestAccountManagerAndBatch(unittest.TestCase):
    """Test suite verifying multi-account management, persistent profiles, 8h rule, and batch preparation."""

    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        import uuid
        self.db_path = self.test_dir / f"test_batch_{uuid.uuid4().hex[:8]}.db"
        self.audit_logger = AuditLogger(db_path=self.db_path)

    def tearDown(self):
        if self.db_path.exists():
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_account_manager_25_accounts_and_isolation(self):
        """Verify AccountManager supports 20-25 accounts with independent directories."""
        am = AccountManager()
        accounts = am.list_accounts()
        self.assertGreaterEqual(len(accounts), 20, "Must support at least 20-25 accounts.")

        # Verify each account has unique directory
        seen_dirs = set()
        for acc in accounts:
            norm_dir = os.path.normpath(am.get_profile_dir(acc.account_id))
            self.assertNotIn(norm_dir, seen_dirs, f"Collision detected for account directory: {norm_dir}")
            seen_dirs.add(norm_dir)

            # Check session status method
            st = am.check_session_status(acc.account_id)
            self.assertTrue(st["configured"])
            self.assertIn("session_status", st)

    def test_eight_hour_gap_rule(self):
        """Verify that completed publications enforce an 8-hour gap while drafts do not."""
        acc = "schoolwaale01@gmail.com"
        # Initially, no publications
        eligible, reason = self.audit_logger.check_eight_hour_gap(acc, min_gap_hours=8.0)
        self.assertTrue(eligible)
        self.assertIn("Eligible", reason)

        # Staging a draft (Ready for Review) does NOT trigger the 8-hour gap
        self.audit_logger.save_prepared_item(
            item_id="draft_1",
            task_id="task_1",
            account_id=acc,
            profile_name="Profile schoolwaale01",
            task_type="comment",
            worksheet="Driving DMV",
            excel_row=100,
            title="Draft comment",
            content="Sample text",
            destination_url="https://www.reddit.com/r/DMV/test",
            status="Ready for Review",
        )
        eligible_draft, _ = self.audit_logger.check_eight_hour_gap(acc, min_gap_hours=8.0)
        self.assertTrue(eligible_draft, "Drafts must not trigger 8-hour publishing lock")

        # Now mark as Complete with publication timestamp 2 hours ago
        tz = ZoneInfo(self.audit_logger.tz_name)
        two_hours_ago = (datetime.now(tz) - timedelta(hours=2)).isoformat()
        self.audit_logger.update_prepared_item_status(
            item_id="draft_1",
            status="Complete",
            published_at=two_hours_ago,
        )

        # Checking 8h gap must now be blocked
        eligible_pub, blocked_reason = self.audit_logger.check_eight_hour_gap(acc, min_gap_hours=8.0)
        self.assertFalse(eligible_pub, "Must block publishing if < 8 hours have elapsed")
        self.assertIn("8-Hour Minimum Gap Rule", blocked_reason)

        # Mark as Complete with publication timestamp 9 hours ago
        nine_hours_ago = (datetime.now(tz) - timedelta(hours=9)).isoformat()
        self.audit_logger.update_prepared_item_status(
            item_id="draft_1",
            status="Complete",
            published_at=nine_hours_ago,
        )
        eligible_9h, _ = self.audit_logger.check_eight_hour_gap(acc, min_gap_hours=8.0)
        self.assertTrue(eligible_9h, "Must allow publishing when >= 8 hours have elapsed")

    def test_draft_persistence_across_restarts(self):
        """Verify drafts saved in SQLite persist and can be retrieved, refilled, and updated."""
        item_id = "test_item_persisted_99"
        self.audit_logger.save_prepared_item(
            item_id=item_id,
            task_id="comment_Driving_DMV_99",
            account_id="schoolwaale02@gmail.com",
            profile_name="Profile schoolwaale02",
            task_type="comment",
            worksheet="Driving DMV",
            excel_row=99,
            title="CA DMV transfer question",
            content="California requires full documents verification.",
            destination_url="https://www.reddit.com/r/DMV/comments/test99",
            status="Ready for Review",
        )

        # Simulate new DB connection (app reload)
        new_logger = AuditLogger(db_path=self.db_path)
        items = new_logger.list_prepared_items(status="Ready for Review")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_id"], item_id)
        self.assertEqual(items[0]["account_id"], "schoolwaale02@gmail.com")
        self.assertEqual(items[0]["content"], "California requires full documents verification.")

    def test_batch_preparation_workflow(self):
        """Verify execute_batch_preparation stages items and formats summary banner correctly."""
        settings = AppSettings.load()
        ctrl = WorkflowController(settings)
        ctrl.audit_logger = self.audit_logger

        selected_accounts = ["schoolwaale01@gmail.com", "schoolwaale02@gmail.com"]

        with patch.object(ctrl, "sync_from_google_sheet", return_value=(True, "Synced")), \
             patch.object(ctrl, "prepare_reply_in_reddit", return_value=(True, "Staged", {"staged": True})), \
             patch.object(ctrl, "prepare_post_in_reddit", return_value=(True, "Staged", {"staged": True})), \
             patch.object(ctrl.excel_handler, "update_task_status", return_value=True), \
             patch("time.sleep", return_value=None):

            res = ctrl.execute_batch_preparation(
                account_ids=selected_accounts,
                mode="human_submit",
                post_limit_per_account=2,
                sync_gsheets=False,
            )

            self.assertTrue(res["success"])
            self.assertIn("Batch Preparation Completed", res["message"])
            self.assertIn("Ready for Review", res["message"])
            self.assertGreaterEqual(res["total_prepared"], 1)

            # Verify drafts are now in database
            drafts = self.audit_logger.list_prepared_items(status="Ready for Review")
            self.assertGreaterEqual(len(drafts), 1)

    def test_stop_batch_interrupts_execution(self):
        """Verify request_stop_batch immediately halts batch processing without corrupting state."""
        settings = AppSettings.load()
        ctrl = WorkflowController(settings)
        ctrl.audit_logger = self.audit_logger

        selected_accounts = ["schoolwaale01@gmail.com", "schoolwaale02@gmail.com"]

        def stop_on_first_call(*args, **kwargs):
            ctrl.request_stop_batch()
            return True, "Staged item 1", {"staged": True}

        with patch.object(ctrl, "sync_from_google_sheet", return_value=(True, "Synced")), \
             patch.object(ctrl, "prepare_reply_in_reddit", side_effect=stop_on_first_call), \
             patch.object(ctrl, "prepare_post_in_reddit", side_effect=stop_on_first_call), \
             patch.object(ctrl.excel_handler, "update_task_status", return_value=(True, "OK")), \
             patch("time.sleep", return_value=None):

            ctrl.reset_stop_batch()
            self.assertFalse(ctrl.stop_batch_requested)

            res = ctrl.execute_batch_preparation(
                account_ids=selected_accounts,
                mode="human_submit",
                post_limit_per_account=5,
                sync_gsheets=False,
            )

            # Must have stopped early after setting stop_batch_requested
            self.assertTrue(ctrl.stop_batch_requested)
            # Prepared count should be 1 (halted immediately after first item)
            self.assertEqual(res["total_prepared"], 1)

    def test_batch_account_error_resilience_and_continuation(self):
        """If one account throws an error, the batch must mark it Failed and continue to the next account."""
        settings = AppSettings.load()
        ctrl = WorkflowController(settings)
        ctrl.audit_logger = self.audit_logger

        selected_accounts = ["schoolwaale01@gmail.com", "schoolwaale02@gmail.com"]
        calls = []

        def side_effect_account_error(task, profile_name, **kwargs):
            calls.append(profile_name)
            if profile_name == "schoolwaale01@gmail.com":
                raise RuntimeError("Simulated browser crash for account 1")
            return True, "Successfully prepared for account 2", {"staged": True}

        with patch.object(ctrl, "sync_from_google_sheet", return_value=(True, "Synced")), \
             patch.object(ctrl, "prepare_reply_in_reddit", side_effect=side_effect_account_error), \
             patch.object(ctrl, "prepare_post_in_reddit", side_effect=side_effect_account_error), \
             patch.object(ctrl.excel_handler, "update_task_status", return_value=(True, "OK")), \
             patch("time.sleep", return_value=None):

            res = ctrl.execute_batch_preparation(
                account_ids=selected_accounts,
                mode="human_submit",
                post_limit_per_account=1,
                sync_gsheets=False,
            )

            # Both accounts were processed (account 1 failed, account 2 succeeded)
            self.assertIn("schoolwaale01@gmail.com", calls)
            self.assertIn("schoolwaale02@gmail.com", calls)
            self.assertGreaterEqual(len(res["errors"]), 1)
            self.assertGreaterEqual(res["total_prepared"], 1)

            # Failed item must be recorded in DB with error message preserved
            failed_items = self.audit_logger.list_prepared_items(status="Failed")
            self.assertGreaterEqual(len(failed_items), 1)
            self.assertIn("Simulated browser crash", failed_items[0]["error_message"])

    def test_draft_to_complete_status_transitions(self):
        """Verify items start as Draft in human submit, remain Draft, and transition to Complete on verification."""
        settings = AppSettings.load()
        ctrl = WorkflowController(settings)
        ctrl.audit_logger = self.audit_logger

        acc = "schoolwaale03@gmail.com"
        item_id = "test_trans_item_01"

        # 1. Save draft
        self.audit_logger.save_prepared_item(
            item_id=item_id,
            task_id="comment_Driving_DMV_401",
            account_id=acc,
            profile_name="Profile schoolwaale03",
            task_type="comment",
            worksheet="Driving DMV",
            excel_row=401,
            title="DMV Transfer Test",
            content="California transfer details",
            destination_url="https://www.reddit.com/r/DMV/comments/1wultuq/test",
            status="Draft",
        )

        drafts = self.audit_logger.list_prepared_items(status="Draft")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "Draft")

        # 2. Verify and mark complete
        with patch.object(ctrl.excel_handler, "update_task_status", return_value=(True, "OK")):
            ok, msg = ctrl.verify_and_mark_complete(item_id)
            self.assertTrue(ok)
            self.assertIn("Complete", msg)

        # 3. Check status is now Complete and publication timestamp is recorded
        completed = self.audit_logger.list_prepared_items(status="Complete")
        self.assertEqual(len(completed), 1)
        self.assertIsNotNone(completed[0]["published_at"])

    def test_batch_automatic_account_fallback_when_not_logged_in(self):
        """When an account appears not logged in or fails authentication, automatically switch to an active logged-in account."""
        settings = AppSettings.load()
        ctrl = WorkflowController(settings)
        ctrl.audit_logger = self.audit_logger

        acc_failing = "schoolwaale04@gmail.com"  # Has no cookies
        acc_working = "schoolwaale01@gmail.com"  # Has cookies

        selected_accounts = [acc_failing, acc_working]
        attempted_accounts = []

        def side_effect_auth_fallback(task, profile_name, **kwargs):
            attempted_accounts.append(profile_name)
            if profile_name == acc_failing:
                return False, "Reddit login required. Please log into Reddit in Chrome.", {"status": "NOT_LOGGED_IN"}
            return True, "Successfully prepared on fallback account", {"staged": True}

        with patch.object(ctrl, "sync_from_google_sheet", return_value=(True, "Synced")), \
             patch.object(ctrl, "prepare_reply_in_reddit", side_effect=side_effect_auth_fallback), \
             patch.object(ctrl, "prepare_post_in_reddit", side_effect=side_effect_auth_fallback), \
             patch.object(ctrl.excel_handler, "update_task_status", return_value=(True, "OK")), \
             patch("time.sleep", return_value=None):

            res = ctrl.execute_batch_preparation(
                account_ids=selected_accounts,
                mode="human_submit",
                post_limit_per_account=1,
                sync_gsheets=False,
            )

            self.assertTrue(res["success"])
            self.assertGreaterEqual(res["total_prepared"], 1)

            # Verify that the failing account was skipped/fallen back and the working account took over
            self.assertIn(acc_working, attempted_accounts)

            # Check that prepared items in DB were assigned to the working account
            drafts = self.audit_logger.list_prepared_items(status="Ready")
            if not drafts:
                drafts = self.audit_logger.list_prepared_items(status="Ready for Review")
            self.assertGreaterEqual(len(drafts), 1)
            self.assertTrue(any(d["account_id"] == acc_working for d in drafts))

    def test_configurable_publication_interval_gap(self):
        """Verify dynamic publication interval gap instead of hard-coded 8 hours."""
        acc = "schoolwaale03@gmail.com"
        tz = ZoneInfo(self.audit_logger.tz_name)

        # Mark an item as complete published 5 hours ago
        five_hours_ago = (datetime.now(tz) - timedelta(hours=5)).isoformat()
        self.audit_logger.save_prepared_item(
            item_id="config_gap_test",
            task_id="task_gap_test",
            account_id=acc,
            profile_name="Profile schoolwaale03",
            task_type="post",
            worksheet="Posts",
            excel_row=50,
            title="Interval test",
            content="Testing configurable gap",
            destination_url="https://www.reddit.com/r/test",
            status="Complete",
        )
        self.audit_logger.update_prepared_item_status(
            item_id="config_gap_test",
            status="Complete",
            published_at=five_hours_ago,
        )

        # With 8-hour gap (default): 5 hours elapsed is BLOCKED
        eligible_8h, reason_8h = self.audit_logger.check_eight_hour_gap(acc, min_gap_hours=8.0)
        self.assertFalse(eligible_8h)
        self.assertIn("8-Hour Minimum Gap Rule", reason_8h)

        # With 4-hour gap: 5 hours elapsed is ALLOWED (eligible)
        eligible_4h, reason_4h = self.audit_logger.check_eight_hour_gap(acc, min_gap_hours=4.0)
        self.assertTrue(eligible_4h)
        self.assertIn("Eligible", reason_4h)

        # With 12-hour gap: 5 hours elapsed is BLOCKED
        eligible_12h, reason_12h = self.audit_logger.check_publication_gap(acc, min_gap_hours=12.0)
        self.assertFalse(eligible_12h)
        self.assertIn("12-Hour Minimum Gap Rule", reason_12h)


if __name__ == "__main__":
    unittest.main()
