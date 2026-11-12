"""Unit and integration tests for Dashboard and WorkflowController OpenClaw integration."""

import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

from config import AppSettings
from modules.openclaw_client import OpenClawResponse
from modules.task_builder import CommentTask
from modules.workflow_controller import WorkflowController


class TestWorkflowOpenClawIntegration(unittest.TestCase):
    """Test OpenClaw integration methods on WorkflowController."""

    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        unique_id = uuid.uuid4().hex[:8]
        self.db_path = str(self.test_dir / f"test_oc_wf_{unique_id}.db")

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

    def test_get_openclaw_overview(self):
        self.mock_client.get_version.return_value = OpenClawResponse(
            success=True,
            data={"raw_version": "OpenClaw 2026.10.9", "version_number": "2026.10.9"},
        )
        self.mock_client.get_gateway_health.return_value = OpenClawResponse(
            success=True,
            data={"healthy": True, "event_loop": "ok", "check_duration": "30ms"},
        )
        self.mock_client.list_browser_profiles.return_value = OpenClawResponse(
            success=True,
            data=[
                {"name": "shoro-test", "status": "stopped", "is_default": False, "port": 18801},
                {"name": "openclaw", "status": "stopped", "is_default": True, "port": 18800},
            ],
        )
        self.mock_client.get_browser_status.return_value = OpenClawResponse(
            success=True,
            data={"profile": "shoro-test", "is_running": False},
        )

        overview = self.controller.get_openclaw_overview()
        self.assertTrue(overview["is_healthy"])
        self.assertEqual(overview["version"]["version_number"], "2026.10.9")
        self.assertEqual(len(overview["profiles"]), 2)
        self.assertFalse(overview["shoro_running"])

    def test_open_in_openclaw_allowed_profile(self):
        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True,
            data={"url": "https://example.com", "profile": "shoro-test", "tab_id": "ABC1234"},
        )

        dummy_task = CommentTask(
            task_id="task_test_01",
            worksheet="Driving DMV",
            excel_row=2,
            account_id="test@example.com",
            thread_title="Test Thread",
            destination_url="https://example.com",
            reply_text="Test reply",
            is_eligible=True,
            status="To review",
        )

        success, msg, data = self.controller.open_in_openclaw(
            url="https://example.com",
            profile_name="shoro-test",
            task=dummy_task,
        )
        self.assertTrue(success)
        self.assertIn("Navigated shoro-test", msg)
        self.mock_client.open_url.assert_called_once_with(url="https://example.com", profile_name="shoro-test")

        # Verify audit event was logged
        events = self.controller.audit_logger.get_recent_events(limit=5)
        nav_events = [e for e in events if e.get("event_type") == "OPENCLAW_NAVIGATE"]
        self.assertTrue(len(nav_events) > 0)
        self.assertEqual(nav_events[0]["task_id"], "task_test_01")

    def test_open_in_openclaw_disallowed_profile(self):
        for disallowed in ["user", "chrome", "openclaw", "random_profile"]:
            success, msg, data = self.controller.open_in_openclaw(
                url="https://example.com",
                profile_name=disallowed,
            )
            self.assertFalse(success)
            self.assertIn("locked", msg.lower())
            self.assertIsNone(data)

    def test_get_openclaw_snapshot(self):
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=True,
            data="- generic [active]: Example Domain",
        )
        success, msg, data = self.controller.get_openclaw_snapshot("shoro-test")
        self.assertTrue(success)
        self.assertIn("Example Domain", data)

        # Disallowed profile
        success_bad, msg_bad, _ = self.controller.get_openclaw_snapshot("user")
        self.assertFalse(success_bad)
        self.assertIn("locked", msg_bad)

    def test_capture_openclaw_screenshot(self):
        self.mock_client.capture_screenshot.return_value = OpenClawResponse(
            success=True,
            data={"profile": "shoro-test", "screenshot_path": "C:\\test\\screen.png"},
        )
        success, msg, path = self.controller.capture_openclaw_screenshot("shoro-test")
        self.assertTrue(success)
        self.assertEqual(path, "C:\\test\\screen.png")

        # Disallowed profile
        success_bad, msg_bad, _ = self.controller.capture_openclaw_screenshot("chrome")
        self.assertFalse(success_bad)
        self.assertIn("locked", msg_bad)

    def test_stop_openclaw_browser(self):
        self.mock_client.stop_browser.return_value = OpenClawResponse(
            success=True,
            data="browser [shoro-test] stopped",
        )
        success, msg = self.controller.stop_openclaw_browser("shoro-test")
        self.assertTrue(success)
        self.assertIn("stopped", msg)

        # Verify audit event was logged
        events = self.controller.audit_logger.get_recent_events(limit=5)
        stop_events = [e for e in events if e.get("event_type") == "OPENCLAW_STOP"]
        self.assertTrue(len(stop_events) > 0)

        # Disallowed profile
        success_bad, msg_bad = self.controller.stop_openclaw_browser("user")
        self.assertFalse(success_bad)
        self.assertIn("locked", msg_bad)

    def test_navigation_stores_tab_id(self):
        """When navigating to a URL, newly opened tab and target ID must be stored."""
        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True,
            data={"url": "https://example.com", "profile": "shoro-test", "tab_id": "2", "target_id": "TARGET_XYZ_789"},
        )
        success, msg, data = self.controller.open_in_openclaw("https://example.com", "shoro-test")
        self.assertTrue(success)
        self.assertEqual(self.controller.active_target_id, "TARGET_XYZ_789")
        self.assertEqual(self.controller.active_tab_id, "2")

    def test_resolve_active_tab_existing(self):
        """When stored tab is still present in open tabs, it is verified and reused."""
        self.controller.active_target_id = "TARGET_XYZ_789"
        self.controller.active_tab_id = "2"
        self.mock_client.get_browser_status.return_value = OpenClawResponse(
            success=True,
            data={"is_running": True},
        )
        self.mock_client.list_tabs.return_value = OpenClawResponse(
            success=True,
            data=[
                {"target_id": "TARGET_OLD_000", "tab_id": "1", "url": "https://example.com/old"},
                {"target_id": "TARGET_XYZ_789", "tab_id": "2", "url": "https://example.com"},
            ],
        )
        chosen_id, err = self.controller.resolve_active_tab("shoro-test")
        self.assertIsNone(err)
        self.assertEqual(chosen_id, "TARGET_XYZ_789")

    def test_resolve_active_tab_stale_auto_resolves(self):
        """When stored tab no longer exists, resolve_active_tab auto-resolves to latest active tab."""
        self.controller.active_target_id = "TARGET_OLD_STALE"
        self.controller.active_tab_id = "99"
        self.mock_client.get_browser_status.return_value = OpenClawResponse(
            success=True,
            data={"is_running": True},
        )
        self.mock_client.list_tabs.return_value = OpenClawResponse(
            success=True,
            data=[
                {"target_id": "TARGET_NEW_111", "tab_id": "1", "url": "about:blank"},
                {"target_id": "TARGET_NEW_222", "tab_id": "2", "url": "https://example.com"},
            ],
        )
        chosen_id, err = self.controller.resolve_active_tab("shoro-test")
        self.assertIsNone(err)
        self.assertEqual(chosen_id, "TARGET_NEW_222")
        self.assertEqual(self.controller.active_target_id, "TARGET_NEW_222")

    def test_resolve_active_tab_when_browser_stopped(self):
        """When browser is stopped, returns clear message asking operator to start/open browser."""
        self.controller.active_target_id = "TARGET_ANY"
        self.mock_client.get_browser_status.return_value = OpenClawResponse(
            success=True,
            data={"is_running": False},
        )
        chosen_id, err = self.controller.resolve_active_tab("shoro-test")
        self.assertIsNone(chosen_id)
        self.assertIsNotNone(err)
        self.assertIn("stopped", err.lower())
        self.assertIsNone(self.controller.active_target_id)

    def test_stop_browser_clears_tab_references(self):
        """Stopping browser clears outdated tab references."""
        self.controller.active_target_id = "TARGET_ABC"
        self.controller.active_tab_id = "5"
        self.mock_client.stop_browser.return_value = OpenClawResponse(
            success=True,
            data="stopped",
        )
        self.controller.stop_openclaw_browser("shoro-test")
        self.assertIsNone(self.controller.active_target_id)
        self.assertIsNone(self.controller.active_tab_id)

    def test_simultaneous_browser_request_rejected(self):
        """Simultaneous browser operations are locked and rejected with a helpful message."""
        acquired = self.controller._browser_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            success, msg, path = self.controller.capture_openclaw_screenshot("shoro-test")
            self.assertFalse(success)
            self.assertIn("in progress", msg.lower())

            success_snap, msg_snap, text = self.controller.get_openclaw_snapshot("shoro-test")
            self.assertFalse(success_snap)
            self.assertIn("in progress", msg_snap.lower())
        finally:
            self.controller._browser_lock.release()

    def test_snapshot_timeout_clears_stale_target_and_advises_restart(self):
        """When snapshot times out, stale tab references are cleared and restart is advised."""
        self.controller.active_target_id = "STALE_123"
        self.controller.active_tab_id = "t1"
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=False,
            error="Browser request timed out. The browser session may be unresponsive.",
        )
        success, msg, snap = self.controller.get_openclaw_snapshot("shoro-test")
        self.assertFalse(success)
        self.assertIn("timed out", msg.lower())
        self.assertIn("stop browser", msg.lower())
        self.assertIsNone(self.controller.active_target_id)
        self.assertIsNone(self.controller.active_tab_id)

    def test_screenshot_timeout_clears_stale_target_and_advises_restart(self):
        """When screenshot times out, stale tab references are cleared and restart is advised."""
        self.controller.active_target_id = "STALE_123"
        self.controller.active_tab_id = "t1"
        self.mock_client.capture_screenshot.return_value = OpenClawResponse(
            success=False,
            error="Browser request timed out. The browser session may be unresponsive.",
        )
        success, msg, path = self.controller.capture_openclaw_screenshot("shoro-test")
        self.assertFalse(success)
        self.assertIn("timed out", msg.lower())
        self.assertIn("stop browser", msg.lower())
        self.assertIsNone(self.controller.active_target_id)
        self.assertIsNone(self.controller.active_tab_id)


class TestOpenClawAuditLogging(unittest.TestCase):
    """Verify that all OpenClaw browser operations generate correct SQLite audit events."""

    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        unique_id = uuid.uuid4().hex[:8]
        self.db_path = str(self.test_dir / f"test_audit_events_{unique_id}.db")

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
        self.dummy_task = CommentTask(
            task_id="task_audit_001",
            worksheet="Driving DMV",
            excel_row=5,
            account_id="audit_tester@example.com",
            thread_title="Audit Test Thread",
            destination_url="https://example.com/test",
            reply_text="Audit test reply text",
            is_eligible=True,
            status="To review",
        )

    def tearDown(self):
        try:
            p = Path(self.db_path)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def test_audit_open_url_success_and_failure(self):
        """Verify OPENCLAW_NAVIGATE and OPENCLAW_ERROR audit logging for URL navigation."""
        # 1. Success case
        self.mock_client.open_url.return_value = OpenClawResponse(
            success=True,
            data={"url": "https://example.com", "profile": "shoro-test", "tab_id": "TAB_101"},
        )
        success, msg, data = self.controller.open_in_openclaw(
            url="https://example.com",
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertTrue(success)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        nav_ev = next((e for e in events if e["event_type"] == "OPENCLAW_NAVIGATE"), None)
        self.assertIsNotNone(nav_ev)
        self.assertEqual(nav_ev["task_id"], "task_audit_001")
        self.assertEqual(nav_ev["operator_action"], "OPEN_URL")
        self.assertIn("Navigated 'shoro-test'", nav_ev["outcome"])
        self.assertIn("TAB_101", nav_ev["error_details"])
        self.assertTrue(len(nav_ev["event_timestamp"]) > 10)

        # 2. Failure case
        self.mock_client.open_url.return_value = OpenClawResponse(
            success=False,
            error="Gateway connection refused",
        )
        success_fail, msg_fail, _ = self.controller.open_in_openclaw(
            url="https://example.com",
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertFalse(success_fail)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        err_ev = next((e for e in events if e["event_type"] == "OPENCLAW_ERROR" and e["operator_action"] == "OPEN_URL"), None)
        self.assertIsNotNone(err_ev)
        self.assertEqual(err_ev["task_id"], "task_audit_001")
        self.assertEqual(err_ev["error_code"], "NAVIGATION_FAILED")
        self.assertIn("Gateway connection refused", err_ev["error_details"])

    def test_audit_screenshot_success_and_failure(self):
        """Verify OPENCLAW_SCREENSHOT and OPENCLAW_ERROR audit logging for screenshots."""
        # 1. Success case
        self.mock_client.capture_screenshot.return_value = OpenClawResponse(
            success=True,
            data={"profile": "shoro-test", "screenshot_path": "C:\\media\\screenshot_01.png"},
        )
        success, msg, path = self.controller.capture_openclaw_screenshot(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertTrue(success)
        self.assertEqual(path, "C:\\media\\screenshot_01.png")

        events = self.controller.audit_logger.get_recent_events(limit=5)
        scr_ev = next((e for e in events if e["event_type"] == "OPENCLAW_SCREENSHOT"), None)
        self.assertIsNotNone(scr_ev)
        self.assertEqual(scr_ev["task_id"], "task_audit_001")
        self.assertEqual(scr_ev["operator_action"], "CAPTURE_SCREENSHOT")
        self.assertIn("Screenshot captured successfully", scr_ev["outcome"])
        self.assertIn("screenshot_01.png", scr_ev["error_details"])
        self.assertTrue(len(scr_ev["event_timestamp"]) > 10)

        # 2. Failure case (Browser stopped)
        self.mock_client.capture_screenshot.return_value = OpenClawResponse(
            success=False,
            error="Browser is not running or stopped",
        )
        success_fail, msg_fail, _ = self.controller.capture_openclaw_screenshot(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertFalse(success_fail)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        err_ev = next((e for e in events if e["event_type"] == "OPENCLAW_ERROR" and e["operator_action"] == "CAPTURE_SCREENSHOT"), None)
        self.assertIsNotNone(err_ev)
        self.assertEqual(err_ev["task_id"], "task_audit_001")
        self.assertEqual(err_ev["error_code"], "BROWSER_STOPPED")
        self.assertIn("not running or stopped", err_ev["error_details"])

        # 3. Timeout case
        self.mock_client.capture_screenshot.return_value = OpenClawResponse(
            success=False,
            error="Error: browser request timed out",
        )
        success_timeout, msg_timeout, _ = self.controller.capture_openclaw_screenshot(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertFalse(success_timeout)
        events = self.controller.audit_logger.get_recent_events(limit=5)
        timeout_ev = next((e for e in events if e["error_code"] == "TIMEOUT" and e["operator_action"] == "CAPTURE_SCREENSHOT"), None)
        self.assertIsNotNone(timeout_ev)

    def test_audit_snapshot_success_and_failure(self):
        """Verify OPENCLAW_SNAPSHOT and OPENCLAW_ERROR audit logging for DOM snapshots."""
        # 1. Success case
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=True,
            data="- generic [active]: Example Domain Header\n- paragraph: Normal content",
        )
        self.mock_client.detect_captcha_in_snapshot.return_value = False

        success, msg, text = self.controller.get_openclaw_snapshot(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertTrue(success)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        snap_ev = next((e for e in events if e["event_type"] == "OPENCLAW_SNAPSHOT"), None)
        self.assertIsNotNone(snap_ev)
        self.assertEqual(snap_ev["task_id"], "task_audit_001")
        self.assertEqual(snap_ev["operator_action"], "DOM_SNAPSHOT")
        self.assertIn("Retrieved DOM snapshot", snap_ev["outcome"])
        self.assertTrue(len(snap_ev["event_timestamp"]) > 10)

        # 2. CAPTCHA detected case
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=True,
            data="cf-turnstile bot check please solve captcha",
        )
        self.mock_client.detect_captcha_in_snapshot.return_value = True

        success_cap, msg_cap, _ = self.controller.get_openclaw_snapshot(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertTrue(success_cap)
        self.assertIn("CAPTCHA", msg_cap)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        cap_ev = next((e for e in events if e["event_type"] == "CAPTCHA_DETECTED"), None)
        self.assertIsNotNone(cap_ev)
        self.assertEqual(cap_ev["task_id"], "task_audit_001")
        self.assertEqual(cap_ev["operator_action"], "DOM_SNAPSHOT")
        self.assertEqual(cap_ev["error_code"], "CAPTCHA_SUSPECTED")

        # 3. Failure case
        self.mock_client.get_page_snapshot.return_value = OpenClawResponse(
            success=False,
            error="Target closed before snapshot",
        )
        self.mock_client.get_browser_status.return_value = OpenClawResponse(success=True, data={"is_running": True})
        self.mock_client.list_tabs.return_value = OpenClawResponse(success=False, error="Cannot list tabs")

        success_fail, msg_fail, _ = self.controller.get_openclaw_snapshot(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertFalse(success_fail)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        err_ev = next((e for e in events if e["event_type"] == "OPENCLAW_ERROR" and e["operator_action"] == "DOM_SNAPSHOT"), None)
        self.assertIsNotNone(err_ev)
        self.assertEqual(err_ev["task_id"], "task_audit_001")

    def test_audit_stop_browser_success_and_failure(self):
        """Verify OPENCLAW_STOP and OPENCLAW_ERROR audit logging for stop browser."""
        # 1. Success case
        self.mock_client.stop_browser.return_value = OpenClawResponse(
            success=True,
            data="browser [shoro-test] stopped",
        )
        success, msg = self.controller.stop_openclaw_browser(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertTrue(success)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        stop_ev = next((e for e in events if e["event_type"] == "OPENCLAW_STOP"), None)
        self.assertIsNotNone(stop_ev)
        self.assertEqual(stop_ev["task_id"], "task_audit_001")
        self.assertEqual(stop_ev["operator_action"], "STOP_BROWSER")
        self.assertIn("Stopped OpenClaw browser", stop_ev["outcome"])
        self.assertTrue(len(stop_ev["event_timestamp"]) > 10)

        # 2. Failure case
        self.mock_client.stop_browser.return_value = OpenClawResponse(
            success=False,
            error="Failed to kill process PID 9999",
        )
        success_fail, msg_fail = self.controller.stop_openclaw_browser(
            profile_name="shoro-test",
            task=self.dummy_task,
        )
        self.assertFalse(success_fail)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        err_ev = next((e for e in events if e["event_type"] == "OPENCLAW_ERROR" and e["operator_action"] == "STOP_BROWSER"), None)
        self.assertIsNotNone(err_ev)
        self.assertEqual(err_ev["task_id"], "task_audit_001")
        self.assertEqual(err_ev["error_code"], "STOP_FAILED")
        self.assertIn("PID 9999", err_ev["error_details"])

    def test_audit_locked_profile_and_concurrency_rejections(self):
        """Verify audit logging when an operation is rejected due to profile locks or concurrency."""
        # 1. Disallowed profile rejection
        success, msg, _ = self.controller.open_in_openclaw("https://example.com", profile_name="chrome")
        self.assertFalse(success)

        events = self.controller.audit_logger.get_recent_events(limit=5)
        err_ev = next((e for e in events if e["error_code"] == "PROFILE_LOCKED"), None)
        self.assertIsNotNone(err_ev)
        self.assertEqual(err_ev["operator_action"], "OPEN_URL")
        self.assertIn("Only 'shoro-test' is allowed", err_ev["outcome"])

        # 2. Concurrency rejection
        acquired = self.controller._browser_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            success_c, msg_c, _ = self.controller.capture_openclaw_screenshot("shoro-test")
            self.assertFalse(success_c)
            events = self.controller.audit_logger.get_recent_events(limit=5)
            lock_ev = next((e for e in events if e["error_code"] == "CONCURRENCY_LOCK"), None)
            self.assertIsNotNone(lock_ev)
            self.assertEqual(lock_ev["operator_action"], "CAPTURE_SCREENSHOT")
        finally:
            self.controller._browser_lock.release()


if __name__ == "__main__":
    unittest.main()
