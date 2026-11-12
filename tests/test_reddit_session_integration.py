"""Unit and integration tests for Reddit Session Status, Profile Persistence, and Workflow."""

import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import MagicMock, patch
import uuid

from config import AppSettings
from modules.account_manager import AccountManager
from modules.openclaw_client import OpenClawClient, OpenClawResponse
from modules.workflow_controller import WorkflowController, RedditSessionStatus
from modules.task_builder import CommentTask


class TestRedditSessionIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(self.test_dir / f"test_session_{uuid.uuid4().hex[:8]}.db")

        # Mock OpenClawClient
        self.mock_client = MagicMock()

        self.settings = AppSettings(
            workbook_path="data/Reddit Threads.xlsx",
            comment_sheet="Driving DMV",
            post_sheet="Posts",
            test_mode=True,
            daily_comment_cap=5,
            database_path=self.db_path,
            backup_directory=str(self.test_dir / "backups"),
        )
        self.controller = WorkflowController(
            settings=self.settings,
            openclaw_client=self.mock_client,
        )

    def tearDown(self):
        try:
            p = Path(self.db_path)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def test_session_status_distinguishes_three_states(self):
        """Verify session status strictly distinguishes directory, browser, and verified login."""
        # Setup: Directory exists, but browser stopped
        self.mock_client.get_browser_status.return_value = OpenClawResponse(
            success=True,
            data={"is_running": False},
        )
        self.mock_client.get_profile_data_dir.return_value = "chrome_profiles/schoolwaale01"

        status = self.controller.check_reddit_session_status("schoolwaale01@gmail.com")
        self.assertTrue(status.directory_exists)
        self.assertFalse(status.browser_running)
        # CRITICAL: Must NEVER claim user is logged in based only on directory existence
        self.assertFalse(status.reddit_authenticated)
        self.assertIn("Browser Stopped", status.status_label)
        self.assertIn("cannot be verified based on directory existence alone", status.details)

        # State 2: Browser running on Reddit login page (unauthenticated)
        self.mock_client.get_browser_status.return_value = OpenClawResponse(
            success=True,
            data={"is_running": True},
        )
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=True,
            data='<html><body><button data-testid="login-button">Log In</button></body></html>',
        )
        self.mock_client.verify_reddit_auth_in_dom.return_value = (
            False, None, "Unauthenticated: Reddit login prompt / button detected on page."
        )

        status_running_unauth = self.controller.check_reddit_session_status("schoolwaale01@gmail.com")
        self.assertTrue(status_running_unauth.directory_exists)
        self.assertTrue(status_running_unauth.browser_running)
        self.assertFalse(status_running_unauth.reddit_authenticated)
        self.assertEqual(status_running_unauth.status_label, "Not Logged In")

        # State 3: Browser running on Reddit with verified active session
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=True,
            data='<html><body><shreddit-app current-user="schoolwaale_user"></shreddit-app></body></html>',
        )
        self.mock_client.verify_reddit_auth_in_dom.return_value = (
            True, "schoolwaale_user", "Verified active Reddit session for user 'schoolwaale_user'"
        )

        status_auth = self.controller.check_reddit_session_status("schoolwaale01@gmail.com")
        self.assertTrue(status_auth.directory_exists)
        self.assertTrue(status_auth.browser_running)
        self.assertTrue(status_auth.reddit_authenticated)
        self.assertEqual(status_auth.authenticated_username, "schoolwaale_user")
        self.assertEqual(status_auth.status_label, "Authenticated")

    def test_verify_reddit_auth_in_dom_method(self):
        """Verify DOM analysis distinguishes logged-in vs logged-out Reddit pages."""
        # Logged out
        logged_out_html = '<div class="header"><button data-testid="login-button">Log In</button><a href="/login">Sign In</a></div>'
        is_auth, user, msg = OpenClawClient.verify_reddit_auth_in_dom(logged_out_html)
        self.assertFalse(is_auth)
        self.assertIsNone(user)
        self.assertIn("login prompt", msg.lower())

        # Logged in with current-user attribute
        logged_in_html = '<shreddit-app current-user="test_mod_99" aria-label="User account: test_mod_99"></shreddit-app>'
        is_auth2, user2, msg2 = OpenClawClient.verify_reddit_auth_in_dom(logged_in_html)
        self.assertTrue(is_auth2)
        self.assertEqual(user2, "test_mod_99")

        # Logged in with user dropdown menu
        dropdown_html = '<div data-testid="user-dropdown" aria-label="open user menu"><span>Account Menu</span></div>'
        is_auth3, user3, msg3 = OpenClawClient.verify_reddit_auth_in_dom(dropdown_html)
        self.assertTrue(is_auth3)

        # Empty / non-Reddit content
        is_auth4, _, _ = OpenClawClient.verify_reddit_auth_in_dom("<html><body><h1>Example Domain</h1></body></html>")
        self.assertFalse(is_auth4)

    def test_profile_directory_preserved_on_browser_stop(self):
        """Profile directory must NOT be deleted when browser stops."""
        # Enable account in memory for this controller instance
        self.controller.account_manager.load()
        if "schoolwaale01@gmail.com" in self.controller.account_manager.accounts:
            self.controller.account_manager.accounts["schoolwaale01@gmail.com"].enabled = True

        profile_dir = Path("chrome_profiles/schoolwaale01")
        profile_dir.mkdir(parents=True, exist_ok=True)
        # Create a test marker file inside the directory
        marker_file = profile_dir / "test_session_marker.txt"
        marker_file.write_text("session_active", encoding="utf-8")

        self.mock_client.stop_browser.return_value = OpenClawResponse(success=True, data="stopped")
        ok, msg = self.controller.stop_openclaw_browser("Profile schoolwaale01")
        self.assertTrue(ok)

        # Directory and marker file must remain intact
        self.assertTrue(profile_dir.exists())
        self.assertTrue(marker_file.exists())
        self.assertEqual(marker_file.read_text(encoding="utf-8"), "session_active")

        # Clean marker file
        marker_file.unlink()

    def test_excel_to_browser_workflow_eligible_task(self):
        """Verify reading next eligible task, validating account and URL, and opening in isolated profile."""
        # Enable schoolwaale01 in-memory for workflow execution test
        self.controller.account_manager.load()
        if "schoolwaale01@gmail.com" in self.controller.account_manager.accounts:
            self.controller.account_manager.accounts["schoolwaale01@gmail.com"].enabled = True
        self.controller.initialize(reload_accounts=False)
        # Find next eligible task for schoolwaale01
        task = self.controller.get_next_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.account_id, "schoolwaale01@gmail.com")
        self.assertTrue(task.is_eligible)
        self.assertTrue(task.destination_url.startswith("https://www.reddit.com/r/DMV/"))
        self.assertTrue(len(task.reply_text) > 10)

        # Mock OpenClaw navigation
        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True,
            data={"url": task.destination_url, "profile": "Profile schoolwaale01", "tab_id": "TAB_EXCEL_01"},
        )

        ok, msg, data = self.controller.open_in_openclaw(
            url=task.destination_url,
            profile_name="Profile schoolwaale01",
            task=task,
        )
        self.assertTrue(ok)
        self.mock_client.open_url.assert_called_once_with(
            url=task.destination_url,
            profile_name="Profile schoolwaale01",
        )

        # Verify audit trail recorded OPENCLAW_NAVIGATE
        events = self.controller.audit_logger.get_recent_events(limit=5)
        nav_ev = next((e for e in events if e["event_type"] == "OPENCLAW_NAVIGATE"), None)
        self.assertIsNotNone(nav_ev)
        self.assertEqual(nav_ev["task_id"], task.task_id)

    def test_duplicate_task_prevention(self):
        """Duplicate tasks previously completed must be rejected from re-processing."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_999",
            worksheet="Driving DMV",
            excel_row=999,
            account_id="schoolwaale01@gmail.com",
            thread_title="Test Duplicate Thread",
            destination_url="https://www.reddit.com/r/DMV/comments/dup123/",
            reply_text="Duplicate test reply",
            is_eligible=True,
            status="To review",
        )
        # Record task as completed in SQLite
        self.controller.audit_logger.log_task(
            task_id=dummy_task.task_id,
            task_fingerprint=dummy_task.fingerprint,
            workbook_name="Reddit Threads.xlsx",
            worksheet_name=dummy_task.worksheet,
            excel_row_number=dummy_task.excel_row,
            task_type=dummy_task.task_type,
            account_id=dummy_task.account_id,
            destination_url=dummy_task.destination_url,
            task_status="Completed - human confirmed",
        )

        # Check duplicate
        is_dup, reason = self.controller.duplicate_checker.check_task(dummy_task)
        self.assertTrue(is_dup)
        self.assertIn("duplicate", reason.lower())

    def test_disabled_account_task_displayed_but_profile_launch_blocked(self):
        """When all accounts are disabled, tasks can be inspected but account profiles cannot be opened."""
        # Initialize with standard config where all 17 accounts are disabled
        self.controller.initialize()

        # 1. Confirm all accounts are disabled
        enabled_accs = [a for a in self.controller.account_manager.accounts.values() if a.enabled]
        self.assertEqual(len(enabled_accs), 0)

        # 2. Task for Row 380 is loaded and displayable for review
        task_380 = next((t for t in self.controller.comment_tasks if t.excel_row == 380), None)
        self.assertIsNotNone(task_380)
        self.assertEqual(task_380.account_id, "schoolwaale01@gmail.com")
        self.assertFalse(task_380.is_eligible)
        self.assertTrue(any(w in task_380.skip_reason.lower() for w in ["disabled", "completed", "non-actionable"]))
        self.assertTrue(len(task_380.reply_text) > 0)
        self.assertTrue(len(task_380.suggested_reply) > 0)

        # 3. Attempting to open the disabled account's profile fails
        self.mock_client.open_url.return_value = OpenClawResponse(success=True, data={})
        ok_prof, msg_prof, _ = self.controller.open_in_openclaw(
            url=task_380.destination_url,
            profile_name="Profile schoolwaale01",
            task=task_380,
        )
        self.assertFalse(ok_prof)
        self.assertIn("locked", msg_prof.lower())
        self.assertIn("shoro-test", msg_prof.lower())

        # 4. Attempting to open via open_task_for_review fails
        ok_rev, msg_rev = self.controller.open_task_for_review(task_380)
        self.assertFalse(ok_rev)
        self.assertIn("disabled", msg_rev.lower())

        # 5. Attempting to open browser for manual login fails
        ok_login, msg_login = self.controller.open_browser_for_manual_login("schoolwaale01@gmail.com")
        self.assertFalse(ok_login)
        self.assertIn("disabled", msg_login.lower())

        # 6. Permitted test profile 'shoro-test' CAN be opened
        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True, data={"url": "https://example.com", "tab_id": "TEST_TAB"}
        )
        ok_test, msg_test, _ = self.controller.open_in_openclaw(
            url="https://example.com",
            profile_name="shoro-test",
        )
        self.assertTrue(ok_test)

        # 7. Protected profiles remain strictly rejected
        for p in ["user", "chrome", "default"]:
            ok_prot, _, _ = self.controller.open_in_openclaw(
                url="https://example.com",
                profile_name=p,
            )
            self.assertFalse(ok_prot)

    def test_open_browser_for_manual_login(self):
        """open_browser_for_manual_login opens login URL in isolated profile."""
        # Enable schoolwaale01 in-memory for testing manual login launcher
        self.controller.account_manager.load()
        if "schoolwaale01@gmail.com" in self.controller.account_manager.accounts:
            self.controller.account_manager.accounts["schoolwaale01@gmail.com"].enabled = True

        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True,
            data={"url": "https://www.reddit.com/login", "profile": "Profile schoolwaale01", "tab_id": "LOGIN_TAB"},
        )
        ok, msg = self.controller.open_browser_for_manual_login("schoolwaale01@gmail.com")
        self.assertTrue(ok)
        self.assertIn("manual Reddit login", msg)
        self.mock_client.open_url.assert_called_with(
            url="https://www.reddit.com/login",
            profile_name="Profile schoolwaale01",
        )

    def test_prepare_reply_in_reddit_success(self):
        """Prepare reply locates editor, activates, inserts exact reply, and verifies text without submitting."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            thread_title="CA behind-the-wheel drive test",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/ca_behindthewheel/",
            reply_text="In California, you typically need to wait at least two weeks before rescheduling.",
            is_eligible=True,
            status="To review",
        )

        # 1. Navigation succeeds
        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True, data={"tab_id": "TAB_380", "target_id": "TGT_380"}
        )

        # 2. Snapshot shows logged in user and 'Join the conversation'
        snapshot_html = (
            '<shreddit-app current-user="schoolwaale01">\n'
            '  <div>Thread content</div>\n'
            '  <button [ref 14]: Join the conversation>\n'
            '</shreddit-app>'
        )
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=True, data=snapshot_html
        )
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (True, "")
        self.mock_client.verify_reddit_auth_in_dom.return_value = (True, "schoolwaale01", "Logged in")
        self.mock_client.find_editor_ref_in_snapshot.return_value = "14"
        self.mock_client._click_ref.return_value = OpenClawResponse(success=True, data="clicked")

        # 3. Evaluate script activates, inserts, and returns exact verified text
        import json
        self.mock_client.evaluate_script.return_value = OpenClawResponse(
            success=True,
            data=json.dumps({"success": True, "verifiedText": dummy_task.reply_text}),
        )

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertTrue(ok)
        self.assertEqual(msg, "Reply inserted - Ready for manual submission")
        self.assertEqual(details["status"], "READY_FOR_MANUAL_SUBMISSION")
        self.assertEqual(dummy_task.status, "Reply inserted - Ready for manual submission")
        self.assertEqual(details["verified_text"], dummy_task.reply_text)

        # Verify audit logging
        events = self.controller.audit_logger.get_recent_events(limit=5)
        prep_event = next((e for e in events if e["event_type"] == "PREPARE_REPLY"), None)
        self.assertIsNotNone(prep_event)
        self.assertEqual(prep_event["outcome"], "Reply inserted - Ready for manual submission")

    def test_prepare_reply_in_reddit_strictly_never_submits(self):
        """Prepare reply must insert and verify exact text, but strictly NEVER automatically submit."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            thread_title="CA behind-the-wheel drive test",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/ca_behindthewheel/",
            reply_text="In California, you typically need to wait at least two weeks before rescheduling.",
            is_eligible=True,
            status="To review",
        )

        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True, data={"tab_id": "TAB_380", "target_id": "TGT_380"}
        )
        snapshot_html = (
            '<shreddit-app current-user="schoolwaale01">\n'
            '  <comment-body-header>\n'
            '    <button [ref 14]: Join the conversation>\n'
            '  </comment-body-header>\n'
            '</shreddit-app>'
        )
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (True, "")
        self.mock_client.verify_reddit_auth_in_dom.return_value = (True, "schoolwaale01", "Logged in")
        self.mock_client.find_editor_ref_in_snapshot.return_value = "14"
        self.mock_client._click_ref.return_value = OpenClawResponse(success=True, data="clicked")

        import json
        self.mock_client.evaluate_script.return_value = OpenClawResponse(
            success=True,
            data=json.dumps({
                "success": True,
                "verifiedText": dummy_task.reply_text,
                "submitted": False,
            }),
        )

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertTrue(ok)
        self.assertEqual(msg, "Reply inserted - Ready for manual submission")
        self.assertEqual(details["status"], "READY_FOR_MANUAL_SUBMISSION")
        self.assertEqual(dummy_task.status, "Reply inserted - Ready for manual submission")
        self.assertFalse(details["submitted"])

    def test_no_submission_action_can_occur_in_workflow(self):
        """Confirm that automatic submission logic does not exist and cannot be executed."""
        self.assertFalse(hasattr(self.controller, "auto_submit_reply_to_reddit"))


    def test_prepare_reply_in_reddit_comment_editor_not_found(self):
        """When 'Join the conversation' is absent, reports 'Comment editor not found.'"""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/ca_test/",
            reply_text="Valid reply text.",
            is_eligible=True,
        )
        self.mock_client.open_url.return_value = OpenClawResponse(success=True, data={})
        # Snapshot without editor
        snapshot_html = '<shreddit-app current-user="schoolwaale01"><div>No comments allowed here</div></shreddit-app>'
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (True, "")
        self.mock_client.verify_reddit_auth_in_dom.return_value = (True, "schoolwaale01", "Logged in")
        self.mock_client.find_editor_ref_in_snapshot.return_value = None

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertFalse(ok)
        self.assertEqual(msg, "Comment editor not found.")
        self.assertEqual(details["status"], "EDITOR_NOT_FOUND")

    def test_prepare_reply_in_reddit_locked_discussion_skipped(self):
        """Locked discussion must be skipped without typing."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/locked/",
            reply_text="Valid reply text.",
            is_eligible=True,
        )
        self.mock_client.open_url.return_value = OpenClawResponse(success=True, data={})
        snapshot_html = '<div>This thread is locked by the moderators</div>'
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (False, "Discussion is locked. Skipping task.")

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertFalse(ok)
        self.assertIn("locked", msg.lower())
        self.assertEqual(details["status"], "NON_ACTIONABLE")

    def test_prepare_reply_in_reddit_archived_discussion_skipped(self):
        """Archived discussion must be skipped without typing."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/archived/",
            reply_text="Valid reply text.",
            is_eligible=True,
        )
        self.mock_client.open_url.return_value = OpenClawResponse(success=True, data={})
        snapshot_html = '<div>This is an archived post. New comments cannot be posted.</div>'
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (False, "Discussion is archived. Skipping task.")

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertFalse(ok)
        self.assertIn("archived", msg.lower())
        self.assertEqual(details["status"], "NON_ACTIONABLE")

    def test_prepare_reply_in_reddit_deleted_discussion_skipped(self):
        """Deleted or removed discussion must be skipped without typing."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/deleted/",
            reply_text="Valid reply text.",
            is_eligible=True,
        )
        self.mock_client.open_url.return_value = OpenClawResponse(success=True, data={})
        snapshot_html = '<div>Sorry, this post has been removed by the moderators</div>'
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (False, "Discussion is deleted or unavailable. Skipping task.")

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertFalse(ok)
        self.assertIn("deleted", msg.lower())
        self.assertEqual(details["status"], "NON_ACTIONABLE")

    def test_prepare_reply_in_reddit_wrong_account_stops(self):
        """If active account in browser does not match task assignment, stop immediately."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/test/",
            reply_text="Valid reply text.",
            is_eligible=True,
        )
        self.mock_client.open_url.return_value = OpenClawResponse(success=True, data={})
        snapshot_html = '<shreddit-app current-user="different_unauthorized_user"></shreddit-app>'
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (True, "")
        self.mock_client.verify_reddit_auth_in_dom.return_value = (True, "different_unauthorized_user", "Logged in")

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertFalse(ok)
        self.assertIn("wrong account", msg.lower())
        self.assertEqual(details["status"], "WRONG_ACCOUNT")

    def test_prepare_reply_in_reddit_text_mismatch_error(self):
        """If editor content does not match expected Excel reply, report an error."""
        import json
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/test/",
            reply_text="Exact expected text from Excel Column K.",
            is_eligible=True,
        )
        self.mock_client.open_url.return_value = OpenClawResponse(success=True, data={})
        snapshot_html = '<shreddit-app current-user="schoolwaale01"><button>Join the conversation</button></shreddit-app>'
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (True, "")
        self.mock_client.verify_reddit_auth_in_dom.return_value = (True, "schoolwaale01", "Logged in")
        self.mock_client.find_editor_ref_in_snapshot.return_value = None

        # Return mismatched text
        self.mock_client.evaluate_script.return_value = OpenClawResponse(
            success=True,
            data=json.dumps({"success": True, "verifiedText": "Some completely different text in editor"}),
        )

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertFalse(ok)
        self.assertIn("does not match", msg.lower())
        self.assertEqual(details["status"], "TEXT_MISMATCH")

    def test_prepare_reply_in_reddit_locked_profile_rejected(self):
        """Locked/disabled profile must be rejected by is_profile_allowed."""
        dummy_task = CommentTask(
            task_id="comment_Driving_DMV_380",
            worksheet="Driving DMV",
            excel_row=380,
            account_id="schoolwaale01@gmail.com",
            chrome_profile_name="Profile schoolwaale01",
            destination_url="https://www.reddit.com/r/DMV/comments/16lfl20/test/",
            reply_text="Valid reply.",
            is_eligible=True,
        )
        # Attempting to use disabled profile
        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="Profile schoolwaale01")
        self.assertFalse(ok)
        self.assertIn("locked", msg.lower())
        self.assertEqual(details["status"], "PROFILE_LOCKED")


if __name__ == "__main__":
    unittest.main()

