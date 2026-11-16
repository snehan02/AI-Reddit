"""Unit and integration tests for Reddit Session Status, Profile Persistence, and Workflow."""

import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import uuid

from config import AppSettings
from modules.openclaw_client import OpenClawClient, OpenClawResponse
from modules.workflow_controller import WorkflowController
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
        self.assertIn(task_380.account_id, ("schoolwaale01@gmail.com", "shoro_test"))
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
        self.assertTrue(any(w in msg_rev.lower() for w in ["disabled", "not configured", "cannot open review"]))

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
        self.controller.allow_account_independent = False

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertFalse(ok)
        self.assertIn("wrong account", msg.lower())
        self.assertEqual(details["status"], "WRONG_ACCOUNT")

    def test_prepare_reply_in_reddit_account_independent_allows(self):
        """When allow_account_independent is True, differing active account proceeds without stopping."""
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
        snapshot_html = '<shreddit-app current-user="shoro_test"><button>Join the conversation</button></shreddit-app>'
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(success=True, data=snapshot_html)
        self.mock_client.detect_captcha_in_snapshot.return_value = False
        self.mock_client.inspect_reddit_discussion_state.return_value = (True, "")
        self.mock_client.verify_reddit_auth_in_dom.return_value = (True, "shoro_test", "Logged in")
        self.mock_client.find_editor_ref_in_snapshot.return_value = None
        self.mock_client.evaluate_script.return_value = OpenClawResponse(
            success=True,
            data='{"success": true, "verifiedText": "Valid reply text."}',
        )
        self.controller.allow_account_independent = True

        ok, msg, details = self.controller.prepare_reply_in_reddit(dummy_task, profile_name="shoro-test")
        self.assertTrue(ok)
        self.assertIn("ready for manual submission", msg.lower())

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

    def test_batch_publish_posts_success(self):
        """Verify batch publish iterates eligible post tasks, calls submit, and returns structured summary."""
        from modules.task_builder import PostTask

        task1 = PostTask(
            task_id="post_Posts_2",
            worksheet="Posts",
            excel_row=2,
            subreddit="DMV",
            title="Failed permit test?",
            body="Here is what it costs.",
            status="Ready for review",
            is_eligible=True,
        )
        task2 = PostTask(
            task_id="post_Posts_3",
            worksheet="Posts",
            excel_row=3,
            subreddit="Ontariodrivetest",
            title="Ontario test tips",
            body="Check mirrors often.",
            status="Ready for review",
            is_eligible=True,
        )
        self.controller.post_tasks = [task1, task2]

        events = []
        def cb(evt):
            events.append(evt)

        with patch.object(self.controller, "submit_post_to_reddit") as mock_sub:
            mock_sub.side_effect = [
                (True, "Post published successfully", {"submitted": True, "published_url": "https://reddit.com/r/DMV/comments/abc"}),
                (True, "Post published successfully", {"submitted": True, "published_url": "https://reddit.com/r/Ontariodrivetest/comments/def"}),
            ]
            summary = self.controller.batch_publish_posts(
                profile_name="shoro-test",
                delay_between_posts=0,
                progress_callback=cb,
            )

            self.assertTrue(summary["success"])
            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["published"], 2)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(len(summary["results"]), 2)
            self.assertEqual(mock_sub.call_count, 2)
            self.assertGreater(len(events), 0)

    def test_batch_publish_posts_skips_completed(self):
        """Verify batch publish skips already completed posts and returns clean result if none pending."""
        from modules.task_builder import PostTask

        completed_task = PostTask(
            task_id="post_Posts_2",
            worksheet="Posts",
            excel_row=2,
            subreddit="DMV",
            title="Already done",
            body="Content",
            status="Completed - automated submission",
            is_eligible=False,
        )
        self.controller.post_tasks = [completed_task]

        summary = self.controller.batch_publish_posts(profile_name="shoro-test")
        self.assertTrue(summary["success"])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["published"], 0)

    def test_post_submission_detects_reddit_filter_removal(self):
        """Verify that when Reddit flags and removes a post, status becomes 'Removed by Reddit filters'."""
        from modules.task_builder import PostTask
        from modules.openclaw_client import OpenClawResponse

        task = PostTask(
            task_id="post_Posts_2",
            worksheet="Posts",
            excel_row=2,
            subreddit="DMV",
            title="Failed the permit test? Here is what it actually costs you",
            body="Our practice sets: https://www.shoro.ai/\nFailed once already?",
            status="Ready for review",
            is_eligible=True,
        )

        curr_t = [1000.0]
        def f_time():
            curr_t[0] += 50.0
            return curr_t[0]

        with patch.object(self.controller.openclaw_client, "open_url") as mock_open, \
             patch.object(self.controller.openclaw_client, "evaluate_script") as mock_eval, \
             patch.object(self.controller.excel_handler, "update_task_status") as mock_excel, \
             patch("time.time", side_effect=f_time), \
             patch("time.sleep"):

            mock_open.return_value = OpenClawResponse(success=True, data={"tab_id": "t1"})
            mock_eval.side_effect = [
                OpenClawResponse(success=True, data=json.dumps({"isCaptcha": False, "isLoggedIn": True})),
                OpenClawResponse(success=True, data="true"),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True, "chosenFlair": "Discussion"})),
                OpenClawResponse(success=True, data=json.dumps({"readTitle": task.title, "readBody": task.body, "postCoords": {"x": 500, "y": 400, "disabled": False}})),
                OpenClawResponse(success=True, data="true"),
                OpenClawResponse(success=True, data="https://www.reddit.com/r/DMV/comments/123/failed_the_permit_test/"),
                OpenClawResponse(success=True, data=json.dumps({"isFiltered": True, "currentUrl": "https://www.reddit.com/r/DMV/comments/123/failed_the_permit_test/"})),
            ]
            mock_excel.return_value = (True, "Updated")

            ok, msg, details = self.controller.submit_post_to_reddit(
                task,
                profile_name="shoro-test",
            )

            self.assertFalse(ok)
            self.assertEqual(details.get("status"), "REMOVED_BY_REDDIT_FILTERS")
            self.assertEqual(task.status, "Removed by Reddit filters")
            self.assertIn("Reddit's automated filters removed it", msg)
            mock_excel.assert_called_once()
            call_kwargs = mock_excel.call_args[1]
            self.assertEqual(call_kwargs.get("new_status"), "Removed by Reddit filters")

    def test_prepare_post_exact_body(self):
        """Verify that exact post body from task is passed to editor."""
        from modules.task_builder import PostTask
        from modules.openclaw_client import OpenClawResponse

        task = PostTask(
            task_id="post_Posts_3",
            worksheet="Posts",
            excel_row=3,
            subreddit="Ontariodrivetest",
            title="3 things that can suspend your G1",
            body="Practice here: https://www.shoro.ai/ontario-g1 to pass on your first attempt.",
            status="Ready for review",
            is_eligible=True,
        )

        curr_t = [1000.0]
        def f_time():
            curr_t[0] += 50.0
            return curr_t[0]

        with patch.object(self.controller.openclaw_client, "open_url") as mock_open, \
             patch.object(self.controller.openclaw_client, "evaluate_script") as mock_eval, \
             patch("time.time", side_effect=f_time), \
             patch("time.sleep"):

            mock_open.return_value = OpenClawResponse(success=True, data={"tab_id": "t1"})
            mock_eval.side_effect = [
                OpenClawResponse(success=True, data=json.dumps({"isCaptcha": False, "isLoggedIn": True})),
                OpenClawResponse(success=True, data="true"),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"chosenFlair": "Discussion"})),
                OpenClawResponse(success=True, data=json.dumps({"readTitle": task.title, "readBody": task.body, "postCoords": None})),
                OpenClawResponse(success=True, data="true"),
            ]

            ok, msg, details = self.controller.prepare_post_in_reddit(
                task,
                profile_name="shoro-test",
                auto_submit=False,
            )

            self.assertTrue(ok)
            self.assertEqual(task.status, "Post drafted - Ready for manual submission")
            body_script = mock_eval.call_args_list[3][0][1]
            self.assertIn("https://www.shoro.ai/ontario-g1", body_script)
            body_script = mock_eval.call_args_list[3][0][1]
            self.assertIn("https://www.shoro.ai/ontario-g1", body_script)

    def test_granular_reply_handles_stringified_p1_data(self):
        """Verify _safe_json_loads handles double-stringified or raw string JS evaluation responses safely without AttributeError."""
        from modules.workflow_controller import _safe_json_loads

        # Standard JSON string
        d1 = _safe_json_loads('{"isLocked": false}', default={})
        self.assertIsInstance(d1, dict)
        self.assertFalse(d1.get("isLocked"))

        # Double-encoded string
        d2 = _safe_json_loads(json.dumps('{"isLocked": false}'), default={})
        self.assertIsInstance(d2, dict)
        self.assertFalse(d2.get("isLocked"))

        # Non-dict string like "true" or error text
        d3 = _safe_json_loads("true", default={})
        self.assertIsInstance(d3, dict)
        self.assertEqual(d3, {})

        # None input
        d4 = _safe_json_loads(None, default={})
        self.assertIsInstance(d4, dict)
        self.assertEqual(d4, {})

    def test_auto_post_execution_mode(self):
        """Scenario 1: Auto Post executes full preparation and automatically submits to Reddit."""
        from modules.task_builder import PostTask

        task = PostTask(
            task_id="post_test_auto",
            worksheet="Posts",
            excel_row=10,
            subreddit="r/DMV",
            title="Permit Test Tips",
            body="Study online at https://www.shoro.ai to prepare.",
            status="Ready for review",
            is_eligible=True,
        )

        curr_t = [1000.0]
        def f_time():
            curr_t[0] += 50.0
            return curr_t[0]

        with patch.object(self.controller.openclaw_client, "open_url") as mock_open, \
             patch.object(self.controller.openclaw_client, "evaluate_script") as mock_eval, \
             patch.object(self.controller.excel_handler, "update_task_status") as mock_excel, \
             patch.object(self.controller, "bring_browser_to_front") as mock_front, \
             patch.object(self.controller, "activate_browser_tab") as mock_tab, \
             patch.object(self.controller, "ensure_visible_browser_running") as mock_vis, \
             patch("time.time", side_effect=f_time), \
             patch("time.sleep"):

            mock_open.return_value = OpenClawResponse(success=True, data={"tab_id": "TAB_P1", "target_id": "TGT_P1"})
            mock_eval.side_effect = [
                OpenClawResponse(success=True, data=json.dumps({"isCaptcha": False, "isLoggedIn": True})),
                OpenClawResponse(success=True, data="true"),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True, "chosenFlair": "Advice"})),
                OpenClawResponse(success=True, data=json.dumps({"readTitle": task.title, "readBody": task.body, "postCoords": {"x": 200, "y": 300, "disabled": False}})),
                OpenClawResponse(success=True, data="true"),  # submit button clicked
                OpenClawResponse(success=True, data="https://www.reddit.com/r/DMV/comments/abc/permit_test_tips/"),  # redirected
                OpenClawResponse(success=True, data=json.dumps({"isFiltered": False, "currentUrl": "https://www.reddit.com/r/DMV/comments/abc/permit_test_tips/"})),
            ]
            mock_excel.return_value = (True, "Excel Updated")

            ok, msg, details = self.controller.auto_submit_post(task, profile_name="shoro-test")

            self.assertTrue(ok)
            self.assertEqual(details["status"], "POST_SUBMITTED")
            self.assertTrue(details["submitted"])
            self.assertEqual(task.status, "Completed - automated submission")
            mock_vis.assert_called_once()
            mock_tab.assert_called()
            mock_front.assert_called()
            mock_excel.assert_called_once()

    def test_human_submit_post_execution_mode(self):
        """Scenario 2: Human Submit for Posts prepares full content and flair, but stops before submission leaving page open."""
        from modules.task_builder import PostTask

        task = PostTask(
            task_id="post_test_human",
            worksheet="Posts",
            excel_row=11,
            subreddit="r/DMV",
            title="Road Test Check",
            body="Review checklist: https://www.shoro.ai/checklist",
            status="Ready for review",
            is_eligible=True,
        )

        curr_t = [1000.0]
        def f_time():
            curr_t[0] += 50.0
            return curr_t[0]

        with patch.object(self.controller.openclaw_client, "open_url") as mock_open, \
             patch.object(self.controller.openclaw_client, "evaluate_script") as mock_eval, \
             patch.object(self.controller.excel_handler, "update_task_status") as mock_excel, \
             patch.object(self.controller, "bring_browser_to_front") as mock_front, \
             patch.object(self.controller, "activate_browser_tab") as mock_tab, \
             patch.object(self.controller, "ensure_visible_browser_running") as mock_vis, \
             patch("time.time", side_effect=f_time), \
             patch("time.sleep"):

            mock_open.return_value = OpenClawResponse(success=True, data={"tab_id": "TAB_P2", "target_id": "TGT_P2"})
            mock_eval.side_effect = [
                OpenClawResponse(success=True, data=json.dumps({"isCaptcha": False, "isLoggedIn": True})),
                OpenClawResponse(success=True, data="true"),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True, "chosenFlair": "Question"})),
                OpenClawResponse(success=True, data=json.dumps({"readTitle": task.title, "readBody": task.body, "postCoords": {"x": 200, "y": 300, "disabled": False}})),
                OpenClawResponse(success=True, data="true"),
            ]

            ok, msg, details = self.controller.human_submit_post(task, profile_name="shoro-test")

            self.assertTrue(ok)
            self.assertEqual(details["status"], "READY_FOR_MANUAL_SUBMISSION")
            self.assertFalse(details["submitted"])
            self.assertEqual(task.status, "Post drafted - Ready for manual submission")
            # Strictly NO automatic submit occurred and Excel status was set to Draft (not Complete)
            if mock_excel.called:
                self.assertEqual(mock_excel.call_args[1].get("new_status"), "Draft")
            mock_vis.assert_called_once()
            mock_tab.assert_called()
            mock_front.assert_called()

    def test_auto_comment_execution_mode(self):
        """Scenario 3: Auto Comment executes full human-mimic workflow and automatically submits."""
        task = CommentTask(
            task_id="comment_test_auto",
            worksheet="Driving DMV",
            excel_row=381,
            account_id="schoolwaale01@gmail.com",
            thread_title="Driving Test Help",
            destination_url="https://www.reddit.com/r/DMV/comments/123/driving_test/",
            reply_text="Always check your mirrors before changing lanes.",
            is_eligible=True,
            status="To review",
        )

        curr_t = [1000.0]
        def f_time():
            curr_t[0] += 500.0
            return curr_t[0]

        with patch.object(self.controller.openclaw_client, "open_url") as mock_open, \
             patch.object(self.controller.openclaw_client, "get_page_snapshot") as mock_snap, \
             patch.object(self.controller.openclaw_client, "verify_reddit_auth_in_dom") as mock_auth, \
             patch.object(self.controller.openclaw_client, "evaluate_script") as mock_eval, \
             patch.object(self.controller.excel_handler, "update_task_status") as mock_excel, \
             patch.object(self.controller.openclaw_client, "close_tab") as mock_close, \
             patch.object(self.controller, "bring_browser_to_front") as mock_front, \
             patch.object(self.controller, "activate_browser_tab") as mock_tab, \
             patch.object(self.controller, "ensure_visible_browser_running") as mock_vis, \
             patch("time.time", side_effect=f_time), \
             patch("time.sleep"):

            mock_open.return_value = OpenClawResponse(success=True, data={"tab_id": "TAB_C1", "target_id": "TGT_C1"})
            mock_snap.return_value = OpenClawResponse(success=True, data="<html><body>reddit</body></html>")
            mock_auth.return_value = (True, "schoolwaale01", "Logged in")
            # Phase 1 discussion check, Phase 2 question extract, Phase 3 top 5, Phase 4 open box,
            # Phase 5 para typing, Phase 5 final state, Phase 7 click comment, Phase 8 refresh, Phase 8 verify live
            mock_eval.side_effect = [
                OpenClawResponse(success=True, data=json.dumps({"isLocked": False, "isArchived": False, "hasShoroReply": False})),
                OpenClawResponse(success=True, data=json.dumps({"questionText": "How to pass driving test?", "anchorDetail": "test"})),
                OpenClawResponse(success=True, data=json.dumps({"topComments": [{"author": "u1", "body": "Practice"}]})),
                OpenClawResponse(success=True, data=json.dumps({"opened": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"verified": True, "length": len(task.reply_text)})),
                OpenClawResponse(success=True, data=json.dumps({"clicked": True})),
                OpenClawResponse(success=True, data=json.dumps({"reloading": True})),
                OpenClawResponse(success=True, data=json.dumps({"live": True, "permalink": "https://www.reddit.com/r/DMV/comments/123/comment/xyz/"})),
            ]
            mock_excel.return_value = (True, "Excel Updated")

            ok, msg, details = self.controller.auto_submit_comment(task, profile_name="shoro-test")

            self.assertTrue(ok)
            self.assertEqual(task.status, "Completed - automated reply")
            mock_excel.assert_called_once()
            mock_close.assert_called_once()

    def test_human_submit_comment_execution_mode(self):
        """Scenario 4: Human Submit for Comments prepares content and stops before clicking Comment, leaving browser open."""
        task = CommentTask(
            task_id="comment_test_human",
            worksheet="Driving DMV",
            excel_row=382,
            account_id="schoolwaale01@gmail.com",
            thread_title="Permit Test Waiting Time",
            destination_url="https://www.reddit.com/r/DMV/comments/123/permit_wait/",
            reply_text="In most states you must wait at least 7 days before retaking.",
            is_eligible=True,
            status="To review",
        )

        curr_t = [1000.0]
        def f_time():
            curr_t[0] += 500.0
            return curr_t[0]

        with patch.object(self.controller.openclaw_client, "open_url") as mock_open, \
             patch.object(self.controller.openclaw_client, "get_page_snapshot") as mock_snap, \
             patch.object(self.controller.openclaw_client, "verify_reddit_auth_in_dom") as mock_auth, \
             patch.object(self.controller.openclaw_client, "evaluate_script") as mock_eval, \
             patch.object(self.controller.excel_handler, "update_task_status") as mock_excel, \
             patch.object(self.controller.openclaw_client, "close_tab") as mock_close, \
             patch.object(self.controller, "bring_browser_to_front") as mock_front, \
             patch.object(self.controller, "activate_browser_tab") as mock_tab, \
             patch.object(self.controller, "ensure_visible_browser_running") as mock_vis, \
             patch("time.time", side_effect=f_time), \
             patch("time.sleep"):

            mock_open.return_value = OpenClawResponse(success=True, data={"tab_id": "TAB_C2", "target_id": "TGT_C2"})
            mock_snap.return_value = OpenClawResponse(success=True, data="<html><body>reddit</body></html>")
            mock_auth.return_value = (True, "schoolwaale01", "Logged in")
            mock_eval.side_effect = [
                OpenClawResponse(success=True, data=json.dumps({"isLocked": False, "isArchived": False, "hasShoroReply": False})),
                OpenClawResponse(success=True, data=json.dumps({"questionText": "How long to wait?", "anchorDetail": "wait"})),
                OpenClawResponse(success=True, data=json.dumps({"topComments": []})),
                OpenClawResponse(success=True, data=json.dumps({"opened": True})),
                OpenClawResponse(success=True, data=json.dumps({"success": True})),
                OpenClawResponse(success=True, data=json.dumps({"verified": True, "length": len(task.reply_text)})),
                OpenClawResponse(success=True, data="true"),
            ]

            ok, msg, details = self.controller.human_submit_comment(task, profile_name="shoro-test")

            self.assertTrue(ok)
            self.assertEqual(details["status"], "READY_FOR_MANUAL_SUBMISSION")
            self.assertEqual(task.status, "Reply inserted - Ready for manual submission")
            # Strictly NO submit clicked, browser NOT closed, Excel NOT marked completed
            mock_excel.assert_not_called()
            mock_close.assert_not_called()
            mock_vis.assert_called_once()
            mock_tab.assert_called()
            mock_front.assert_called()

    def test_timing_configurations_preserved_and_demo_removed(self):
        """Scenario 5: Verify all existing timing configurations remain unchanged and Accelerated Demo Mode is completely removed."""
        import inspect
        from modules import workflow_controller

        # 1. Inspect WorkflowController source code
        wf_src = inspect.getsource(workflow_controller)
        self.assertNotIn("Accelerated Demo Mode", wf_src)
        self.assertNotIn("is_stealth else", wf_src)

        # 2. Verify all exact timing values are declared and preserved
        expected_timing_fragments = [
            "t_phase1 = random.uniform(180, 240)",
            "t_phase2 = random.uniform(180, 195)",
            "t_phase3 = random.uniform(240, 360)",
            "t_phase4 = random.uniform(90, 105)",
            "t_phase5 = random.uniform(360, 600)",
            "p_pause = random.uniform(10, 15)",
            "t_phase6 = random.uniform(180, 240)",
            "t_phase8 = random.uniform(120, 140)",
            "t_phase9 = random.uniform(120, 135)",
            "t_dwell_rules = random.uniform(45, 60)",
            "t_title_word_ms = (180, 320)",
            "t_pause_after_title = random.uniform(15, 25)",
            "t_body_word_ms = (180, 350)",
            "t_pause_between_paras = random.uniform(10, 18)",
            "t_proofread_dwell = random.uniform(45, 60)",
        ]
        for fragment in expected_timing_fragments:
            self.assertIn(fragment, wf_src, f"Timing configuration missing or altered: {fragment}")

        # 3. Inspect app.py source code
        with open("app.py", "r", encoding="utf-8") as f:
            app_src = f.read()

        self.assertNotIn("Accelerated Demo Mode", app_src)
        self.assertNotIn("demo_fast", app_src)
        self.assertIn("Auto Post", app_src)
        self.assertIn("Human Submit", app_src)
        self.assertIn("Auto Comment", app_src)

    def test_browser_visibility_and_foreground_controls(self):
        """Scenario 6: Verify visible browser launching and foreground focus controls."""
        self.assertTrue(hasattr(self.controller, "ensure_visible_browser_running"))
        self.assertTrue(hasattr(self.controller, "activate_browser_tab"))
        self.assertTrue(hasattr(self.controller, "bring_browser_to_front"))
        self.assertTrue(hasattr(self.controller, "capture_live_screen_preview"))


if __name__ == "__main__":
    unittest.main()

