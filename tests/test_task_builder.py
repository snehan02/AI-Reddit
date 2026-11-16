"""Unit tests for TaskBuilder, CommentManager, and PostManager with synthetic workbooks."""

import unittest
import uuid
import shutil
from pathlib import Path
import openpyxl

from modules.account_manager import AccountManager
from modules.audit_logger import AuditLogger
from modules.comment_manager import CommentManager
from modules.duplicate_checker import DuplicateChecker
from modules.post_manager import PostManager
from modules.task_builder import (
    CommentTask,
    PostTask,
    compute_comment_fingerprint,
    compute_post_fingerprint,
)


class TestTaskBuilder(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.unique_id = uuid.uuid4().hex[:8]

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_comment_fingerprint_deterministic(self):
        fp1 = compute_comment_fingerprint("Driving DMV", 10, "https://reddit.com/r/driving", "acc1", "reply text")
        fp2 = compute_comment_fingerprint("Driving DMV", 10, "https://reddit.com/r/driving", "acc1", "reply text")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)

        fp3 = compute_comment_fingerprint("Driving DMV", 10, "https://reddit.com/r/driving", "acc1", "different")
        self.assertNotEqual(fp1, fp3)

    def test_post_fingerprint_deterministic(self):
        fp1 = compute_post_fingerprint("Posts", 2, "r/DMV", "acc1", "Title", "Body")
        fp2 = compute_post_fingerprint("Posts", 2, "r/DMV", "acc1", "Title", "Body")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)

    def test_exact_comment_preservation(self):
        exact_text = "  Multi-line reply\nLine 2 with \"quotes\" and 'apostrophes' & symbols: 100% update.\n\nFree practice!  "
        task = CommentTask(
            worksheet="Driving DMV",
            excel_row=12,
            account_id="user1@example.com",
            destination_url="https://reddit.com/r/driving/comments/123",
            reply_text=exact_text,
        )
        self.assertEqual(task.reply_text, exact_text)
        self.assertEqual(task.worksheet, "Driving DMV")
        self.assertEqual(task.excel_row, 12)

    def test_synthetic_comment_validation_matrix(self):
        """Test all edge cases using an isolated synthetic workbook."""
        synth_wb_path = self.test_dir / f"synth_comments_{self.unique_id}.xlsx"
        synth_acc_path = self.test_dir / f"synth_accounts_{self.unique_id}.json"
        synth_db_path = self.test_dir / f"synth_audit_{self.unique_id}.db"

        # 1. Setup account manager with verified and disabled accounts
        acc_mgr = AccountManager(synth_acc_path)
        acc_mgr.add_or_update("verified@example.com", "Profile Verified", "chrome_profiles/verified", enabled=True)
        acc_mgr.add_or_update("disabled@example.com", "Profile Disabled", "chrome_profiles/disabled", enabled=False)

        # 2. Setup duplicate checker
        logger = AuditLogger(synth_db_path)
        dup_checker = DuplicateChecker(synth_db_path)

        # Pre-record a completed task in DB to trigger duplicate detection
        logger.log_task(
            task_id="comment_Driving_DMV_11",
            task_fingerprint=compute_comment_fingerprint("Driving DMV", 11, "https://reddit.com/r/driving", "verified@example.com", "Duplicate test reply"),
            workbook_name="synth.xlsx",
            worksheet_name="Driving DMV",
            excel_row_number=11,
            task_type="comment",
            account_id="verified@example.com",
            destination_url="https://reddit.com/r/driving",
            task_status="Completed - human confirmed",
        )

        # 3. Create synthetic workbook
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Driving DMV"
        headers = [
            "Last checked", "State / Province", "Thread title", "Thread link",
            "Engagement", "Why it matters / What changed", "Suggested reply",
            "Content idea", "My status", "Revised suggested reply",
            "Shoro AI reply", "Account posted", "Status"
        ]
        ws.append(headers)

        rows = [
            # Row 2: Fully valid
            ["chk", "WA", "Valid Title", "https://reddit.com/r/driving/1", "eng", "why", "sug", "idea", "stat", "rev", "Exact approved reply #1", "verified@example.com", "To review"],
            # Row 3: Blank account
            ["chk", "WA", "Title 2", "https://reddit.com/r/driving/2", "eng", "why", "sug", "idea", "stat", "rev", "Reply #2", None, "To review"],
            # Row 4: Disabled account
            ["chk", "WA", "Title 3", "https://reddit.com/r/driving/3", "eng", "why", "sug", "idea", "stat", "rev", "Reply #3", "disabled@example.com", "To review"],
            # Row 5: Unconfigured account
            ["chk", "WA", "Title 4", "https://reddit.com/r/driving/4", "eng", "why", "sug", "idea", "stat", "rev", "Reply #4", "unconfigured@example.com", "To review"],
            # Row 6: Invalid/malformed URL (just title text in link col)
            ["chk", "WA", "Title 5", "This is just text not a web URL", "eng", "why", "sug", "idea", "stat", "rev", "Reply #5", "verified@example.com", "To review"],
            # Row 7: Blank URL
            ["chk", "WA", "Title 6", "", "eng", "why", "sug", "idea", "stat", "rev", "Reply #6", "verified@example.com", "To review"],
            # Row 8: Missing reply content
            ["chk", "WA", "Title 7", "https://reddit.com/r/driving/7", "eng", "why", "sug", "idea", "stat", "rev", "", "verified@example.com", "To review"],
            # Row 9: Non-actionable: Locked post
            ["chk", "WA", "Title 8", "https://reddit.com/r/driving/8", "eng", "why", "sug", "idea", "stat", "rev", "Reply #8", "verified@example.com", "Locked post"],
            # Row 10: Non-actionable: Post deleted
            ["chk", "WA", "Title 9", "https://reddit.com/r/driving/9", "eng", "why", "sug", "idea", "stat", "rev", "Reply #9", "verified@example.com", "post deleted"],
            # Row 11: Duplicate task
            ["chk", "WA", "Title 10", "https://reddit.com/r/driving", "eng", "why", "sug", "idea", "stat", "rev", "Duplicate test reply", "verified@example.com", "To review"],
        ]
        for r in rows:
            ws.append(r)
        wb.save(synth_wb_path)
        wb.close()

        # 4. Run CommentManager
        cm = CommentManager(synth_wb_path, sheet_name="Driving DMV", account_manager=acc_mgr, duplicate_checker=dup_checker)
        tasks, stats = cm.load_tasks()

        self.assertEqual(stats["total"], 10)
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["skipped"], 9)

        # Verify Row 2 (the valid row)
        t_valid = tasks[0]
        self.assertTrue(t_valid.is_eligible)
        self.assertEqual(t_valid.excel_row, 2)
        self.assertEqual(t_valid.reply_text, "Exact approved reply #1")
        self.assertEqual(t_valid.chrome_profile_name, "Profile Verified")

        # Verify skip reasons on invalid rows
        self.assertFalse(tasks[1].is_eligible)  # Row 3
        self.assertIn("blank account", tasks[1].skip_reason.lower())

        self.assertFalse(tasks[2].is_eligible)  # Row 4
        self.assertIn("disabled", tasks[2].skip_reason.lower())

        self.assertFalse(tasks[3].is_eligible)  # Row 5
        self.assertIn("not configured", tasks[3].skip_reason.lower())

        self.assertFalse(tasks[4].is_eligible)  # Row 6
        self.assertIn("invalid url", tasks[4].skip_reason.lower())

        self.assertFalse(tasks[5].is_eligible)  # Row 7
        self.assertIn("invalid url", tasks[5].skip_reason.lower())

        self.assertFalse(tasks[6].is_eligible)  # Row 8
        self.assertIn("missing content", tasks[6].skip_reason.lower())

        self.assertFalse(tasks[7].is_eligible)  # Row 9
        self.assertIn("non-actionable", tasks[7].skip_reason.lower())

        self.assertFalse(tasks[8].is_eligible)  # Row 10
        self.assertIn("non-actionable", tasks[8].skip_reason.lower())

        self.assertFalse(tasks[9].is_eligible)  # Row 11
        self.assertIn("duplicate", tasks[9].skip_reason.lower())

        # Verify grouped skip reasons
        by_reason = stats["skipped_by_reason"]
        self.assertTrue(len(by_reason) >= 5)

    def test_synthetic_posts_missing_account_column(self):
        """Verify Posts worksheet without Account posted skips all tasks with schema error."""
        synth_wb_path = self.test_dir / f"synth_posts_no_acc_{self.unique_id}.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Posts"
        # 7 columns (no Account posted)
        ws.append(["Date drafted", "Subreddit", "Region", "Post title", "Post body", "Shoro link", "Status"])
        for i in range(5):
            ws.append([f"2026-10-2{i}", f"r/Sub{i}", "Reg", f"Title {i}", f"Body {i}", "https://shoro.ai", "To review"])
        wb.save(synth_wb_path)
        wb.close()

        pm = PostManager(synth_wb_path, sheet_name="Posts")
        tasks, stats = pm.load_tasks()

        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["eligible"], 0)
        self.assertEqual(stats["skipped"], 5)
        self.assertIn("Account posted", stats["schema_error"])
        for t in tasks:
            self.assertFalse(t.is_eligible)
            self.assertIn("Account posted", t.skip_reason)
            self.assertEqual(t.worksheet, "Posts")

    def test_synthetic_posts_with_account_column(self):
        """Verify Posts worksheet when Account posted is present."""
        synth_wb_path = self.test_dir / f"synth_posts_with_acc_{self.unique_id}.xlsx"
        synth_acc_path = self.test_dir / f"synth_accounts_posts_{self.unique_id}.json"

        acc_mgr = AccountManager(synth_acc_path)
        acc_mgr.add_or_update("author@example.com", "Profile Author", "chrome_profiles/author", enabled=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Posts"
        # 8 columns (includes Account posted)
        ws.append(["Date drafted", "Subreddit", "Region", "Post title", "Post body", "Shoro link", "Account posted", "Status"])
        ws.append(["2026-10-22", "r/DMV", "US", "Valid Post Title", "Valid Post Body Content", "https://shoro.ai", "author@example.com", "To review"])
        ws.append(["2026-10-22", "r/DMV", "US", "Missing Acc Title", "Body Content", "https://shoro.ai", "", "To review"])
        wb.save(synth_wb_path)
        wb.close()

        pm = PostManager(synth_wb_path, sheet_name="Posts", account_manager=acc_mgr)
        tasks, stats = pm.load_tasks()

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertTrue(tasks[0].is_eligible)
        self.assertEqual(tasks[0].title, "Valid Post Title")
        self.assertEqual(tasks[0].body, "Valid Post Body Content")
        self.assertFalse(tasks[1].is_eligible)
        self.assertIn("blank account", tasks[1].skip_reason.lower())

    def test_reply_column_mapping_distinction(self):
        """Verify that Col F, Col G, Col J, and Col K are distinctly mapped without confusion."""
        synth_wb_path = self.test_dir / f"synth_col_distinction_{self.unique_id}.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Driving DMV"
        headers = [
            "Last checked", "State / Province", "Thread title", "Thread link",
            "Engagement", "Why it matters / What changed", "Suggested reply",
            "Content idea", "My status", "Revised suggested reply",
            "Shoro AI reply", "Account posted", "Status"
        ]
        ws.append(headers)
        ws.append([
            "2026-10-22", "CA", "Title DMV", "https://reddit.com/r/DMV/comments/123",
            "High", "MATTERS_F_TEXT", "SUGGESTED_G_TEXT",
            "Idea", "To review", "REVISED_J_TEXT",
            "SHORO_AI_K_TEXT", "user@example.com", "To review"
        ])
        wb.save(synth_wb_path)
        wb.close()

        cm = CommentManager(synth_wb_path, sheet_name="Driving DMV")
        tasks, stats = cm.load_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]

        # Col K is the primary approved text (reply_text)
        self.assertEqual(task.reply_text, "SHORO_AI_K_TEXT")
        # Col G is the original draft suggested reply
        self.assertEqual(task.suggested_reply, "SUGGESTED_G_TEXT")
        # Col J is the revised draft suggested reply
        self.assertEqual(task.revised_suggested_reply, "REVISED_J_TEXT")
        # Col F is Why it matters / What changed
        self.assertEqual(task.why_it_matters, "MATTERS_F_TEXT")

    def test_production_row_380_column_mapping(self):
        """Verify production workbook Row 380 maps Col F, G, J, K distinctly."""
        prod_path = Path("data/Reddit Threads.xlsx")
        if not prod_path.exists():
            self.skipTest("Production workbook not found")

        cm = CommentManager(prod_path, sheet_name="Driving DMV")
        tasks, stats = cm.load_tasks()

        task_380 = next((t for t in tasks if t.excel_row == 380), None)
        self.assertIsNotNone(task_380)
        self.assertIn(task_380.account_id, ["schoolwaale01@gmail.com", "shoro_test"])
        self.assertEqual(task_380.destination_url, "https://www.reddit.com/r/DMV/comments/1wultuq/why_did_i_get_told_to_call_a_number/")

        # Verify Column K: Shoro AI reply
        self.assertTrue(task_380.reply_text.startswith("Why did California DMV tell me to call a number during my license transfer?"))
        # Verify Column G: Suggested reply
        self.assertTrue(task_380.suggested_reply.startswith("That tracks with how California handles out-of-state moves."))
        # Verify Column J: Revised suggested reply
        self.assertTrue(task_380.revised_suggested_reply.startswith("A knowledge test can be part of transferring an out-of-state licence to California"))
        # Verify Column F: Why it matters / What changed
        self.assertTrue(task_380.why_it_matters.startswith("TIER 2 licensing logistics"))

        # Confirm all 4 columns contain distinct strings
        self.assertNotEqual(task_380.reply_text, task_380.suggested_reply)
        self.assertNotEqual(task_380.reply_text, task_380.why_it_matters)
        self.assertNotEqual(task_380.suggested_reply, task_380.revised_suggested_reply)


if __name__ == "__main__":
    unittest.main()
