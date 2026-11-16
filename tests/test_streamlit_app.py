"""Tests for Streamlit dashboard rendering and integration."""

import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest


class TestStreamlitDashboard(unittest.TestCase):
    """Test dashboard initialization and UI element rendering."""

    @patch("modules.workflow_controller.WorkflowController.get_openclaw_overview")
    def test_app_loads_successfully(self, mock_overview):
        """Verify app runs from start to finish without unhandled exceptions."""
        mock_overview.return_value = {
            "version": {"raw_version": "OpenClaw 2026.10.9", "version_number": "2026.10.9"},
            "version_error": None,
            "health": {"healthy": True, "event_loop": "ok", "check_duration": "30ms", "agents": "main (default)"},
            "health_error": None,
            "is_healthy": True,
            "profiles": [
                {"name": "shoro-test", "status": "stopped", "is_default": False, "port": 18801},
                {"name": "openclaw", "status": "stopped", "is_default": True, "port": 18800},
                {"name": "user", "status": "stopped [existing-session]", "is_default": False, "port": None},
                {"name": "chrome", "status": "stopped [extension]", "is_default": False, "port": None},
            ],
            "profiles_error": None,
            "shoro_status": {"profile": "shoro-test", "is_running": False},
            "shoro_running": False,
            "shoro_error": None,
        }

        at = AppTest.from_file("app.py", default_timeout=60)
        at.run()
        self.assertFalse(at.exception, f"App crashed with exception: {at.exception}")

        # Check titles and tabs
        tabs_text = [t.label for t in at.tabs]
        self.assertIn("🎯 Review & Batch Operations", tabs_text)
        self.assertIn("👥 Account Manager", tabs_text)
        self.assertIn("📋 Real-Time Task Queue", tabs_text)
        self.assertIn("🦞 OpenClaw Console", tabs_text)

        # Check sidebar OpenClaw Gateway section
        sidebar_markdown = " ".join([m.value for m in at.sidebar.markdown])
        self.assertIn("OpenClaw Gateway", sidebar_markdown)
        self.assertIn("127.0.0.1:18789", sidebar_markdown)
        self.assertIn("shoro-test", sidebar_markdown)

    @patch("modules.workflow_controller.WorkflowController.get_openclaw_overview")
    def test_force_test_mode_ui_safeguard(self, mock_overview):
        """Verify UI locks Test Mode toggle and shows environment safeguard badge."""
        import os
        mock_overview.return_value = {
            "version": {"raw_version": "OpenClaw 2026.10.9", "version_number": "2026.10.9"},
            "version_error": None,
            "health": {"healthy": True, "event_loop": "ok", "check_duration": "30ms", "agents": "main (default)"},
            "health_error": None,
            "is_healthy": True,
            "profiles": [],
            "profiles_error": None,
            "shoro_status": None,
            "shoro_running": False,
            "shoro_error": None,
        }
        os.environ["FORCE_TEST_MODE"] = "true"
        try:
            at = AppTest.from_file("app.py", default_timeout=60)
            at.run()
            self.assertFalse(at.exception, f"App crashed with exception: {at.exception}")

            # Check that Test Mode toggle is disabled and True
            toggles = at.sidebar.toggle
            test_mode_toggle = [t for t in toggles if t.label == "Test Mode"]
            self.assertTrue(len(test_mode_toggle) > 0)
            self.assertTrue(test_mode_toggle[0].value)
            self.assertTrue(test_mode_toggle[0].disabled)

            # Check that caption displays environment lock
            captions = [c.value for c in at.sidebar.caption]
            self.assertTrue(any("FORCE_TEST_MODE=true" in c for c in captions))
        finally:
            os.environ.pop("FORCE_TEST_MODE", None)


if __name__ == "__main__":
    unittest.main()
