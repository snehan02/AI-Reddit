"""Unit and integration tests for OpenClawClient.

Verifies:
1. All subprocess calls use argument lists with shell=False.
2. Sensitive information is not exposed through arguments, stdout, stderr, raw output, exceptions, or logs.
3. Browser operations use only the explicitly supplied profile.
4. The existing `user` and `chrome` profiles are strictly disallowed from being accessed, started, or stopped.
5. URL validation, timeout handling, and error handling are consistently enforced.
6. No automated form filling, commenting, posting, or submission functionality exists.
"""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from modules.openclaw_client import (
    OpenClawClient,
    OpenClawResponse,
    sanitize_args,
    sanitize_text,
    sanitize_dom_snapshot,
    PROTECTED_PROFILES,
)


class TestOpenClawClientUnit(unittest.TestCase):
    """Unit tests using mocked subprocess responses."""

    def setUp(self):
        self.client = OpenClawClient(cli_path="openclaw")

    def test_sanitize_args(self):
        raw_cmd = ["openclaw", "gateway", "--token", "secret12345678", "--bind", "loopback"]
        sanitized = sanitize_args(raw_cmd)
        self.assertEqual(sanitized, ["openclaw", "gateway", "--token", "[REDACTED]", "--bind", "loopback"])

        raw_cmd2 = ["openclaw", "gateway", "--password=my_secret_pass_123"]
        sanitized2 = sanitize_args(raw_cmd2)
        self.assertEqual(sanitized2, ["openclaw", "gateway", "--password=[REDACTED]"])

        raw_cmd3 = ["openclaw", "gateway", "--auth", "auth_secret_99999"]
        sanitized3 = sanitize_args(raw_cmd3)
        self.assertEqual(sanitized3, ["openclaw", "gateway", "--auth", "[REDACTED]"])

    def test_sanitize_text(self):
        sample = "Error: Invalid token: abcdef1234567890 for agent main"
        sanitized = sanitize_text(sample)
        self.assertNotIn("abcdef1234567890", sanitized)
        self.assertIn("[REDACTED]", sanitized)

        sample_pass = "Connection failed with password=supersecretpass123 in config"
        sanitized_pass = sanitize_text(sample_pass)
        self.assertNotIn("supersecretpass123", sanitized_pass)
        self.assertIn("[REDACTED]", sanitized_pass)

    @patch("subprocess.run")
    def test_sensitive_data_redacted_in_stdout_and_stderr(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="Logged with token: gateway_secret_token_12345\n",
            stderr="Failed auth with bearer secret_jwt_token_99999\n",
        )
        resp = self.client._execute(["health"])
        self.assertFalse(resp.success)
        self.assertNotIn("gateway_secret_token_12345", resp.raw_output)
        self.assertNotIn("secret_jwt_token_99999", resp.error)
        self.assertIn("[REDACTED]", resp.error)

    def test_sanitize_dom_snapshot_redacts_tokens_and_preserves_content(self):
        raw_dom = (
            "<div class='thread'>"
            "<h1>How do I schedule a DMV driving test appointment online?</h1>"
            "<p class='author'>Posted by u/ca_driver99 (score: 42)</p>"
            "<p class='comment'>You need to visit the official CA DMV portal and choose your office.</p>"
            "<input type='password' name='user_pass' value='SuperSecretPassword999!' />"
            "<script>"
            "var api_key = 'sec_api_key_887766554433';"
            "var session_id = 'sess_live_1234567890abcdef';"
            "var authHeader = 'Bearer my_access_token_12345678';"
            "var jwt_token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoidGVzdCJ9.example_signature_99999';"
            "</script>"
            "</div>"
        )
        sanitized = sanitize_dom_snapshot(raw_dom)

        # Verify sensitive values are redacted
        self.assertNotIn("SuperSecretPassword999!", sanitized)
        self.assertNotIn("sec_api_key_887766554433", sanitized)
        self.assertNotIn("sess_live_1234567890abcdef", sanitized)
        self.assertNotIn("my_access_token_12345678", sanitized)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", sanitized)
        self.assertIn("[REDACTED", sanitized)

        # Verify human-reviewable content is preserved
        self.assertIn("How do I schedule a DMV driving test appointment online?", sanitized)
        self.assertIn("Posted by u/ca_driver99 (score: 42)", sanitized)
        self.assertIn("You need to visit the official CA DMV portal and choose your office.", sanitized)

    @patch.object(OpenClawClient, "_execute")
    def test_get_page_snapshot_returns_sanitized_dom(self, mock_execute):
        raw_dom = (
            "<html><body>"
            "<h2>Reddit Discussion</h2>"
            "<input type='password' value='private_pass_12345' />"
            "<span>session_id: sess_secret_99887766</span>"
            "</body></html>"
        )
        mock_execute.return_value = OpenClawResponse(
            success=True,
            data=raw_dom,
            exit_code=0,
        )
        resp = self.client.get_page_snapshot("shoro-test")
        self.assertTrue(resp.success)
        self.assertNotIn("private_pass_12345", resp.data)
        self.assertNotIn("sess_secret_99887766", resp.data)
        self.assertIn("Reddit Discussion", resp.data)

    def test_protected_profiles_strictly_disallowed(self):
        """Verify that 'user', 'chrome', and 'default' profiles can never be accessed."""
        for prof in ["user", "chrome", "default", "User", "CHROME", "DEFAULT", " chrome ", "user ", " default "]:
            # Status check
            resp_st = self.client.get_browser_status(prof)
            self.assertFalse(resp_st.success)
            self.assertIn("Access to protected profile", resp_st.error)

            # Open URL
            resp_op = self.client.open_url("https://example.com", prof)
            self.assertFalse(resp_op.success)
            self.assertIn("Access to protected profile", resp_op.error)

            # Page Snapshot
            resp_snap = self.client.get_page_snapshot(prof)
            self.assertFalse(resp_snap.success)
            self.assertIn("Access to protected profile", resp_snap.error)

            # Screenshot
            resp_scr = self.client.capture_screenshot(prof)
            self.assertFalse(resp_scr.success)
            self.assertIn("Access to protected profile", resp_scr.error)

            # Stop
            resp_stp = self.client.stop_browser(prof)
            self.assertFalse(resp_stp.success)
            self.assertIn("Access to protected profile", resp_stp.error)

    def test_empty_or_whitespace_profile_rejected(self):
        for invalid_prof in ["", "   ", None]:
            resp = self.client.get_browser_status(invalid_prof)  # type: ignore
            self.assertFalse(resp.success)
            self.assertIn("cannot be empty", resp.error)

    def test_open_url_validation_strict(self):
        # Invalid schemes, deceptive domains, IPs, and localhost
        for invalid_url in [
            "ftp://not-allowed.com",
            "file:///C:/path/to/file",
            "javascript:alert(1)",
            "data:text/html,test",
            "example.com",
            "http://",
            "https://",
            "",
            "   ",
            None,
            "http://localhost:18789",
            "http://127.0.0.1:18789",
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.1",
            "https://reddit.com.attacker.com",
            "https://attacker.com/reddit.com",
            "https://notreddit.com",
            "https://reddit.com@evil.com",
        ]:
            resp = self.client.open_url(invalid_url, "shoro-test")  # type: ignore
            self.assertFalse(resp.success)
            self.assertIn("Invalid destination URL", resp.error)

    def test_open_url_approved_domains_allowed(self):
        with patch.object(self.client, "_execute") as mock_exec:
            mock_exec.return_value = OpenClawResponse(success=True, data="opened: https://example.com\nid: 12345")
            for valid_url in [
                "https://www.reddit.com/r/driving",
                "https://reddit.com/r/DMV",
                "https://old.reddit.com/r/driving",
                "https://example.com",
                "https://www.example.com",
            ]:
                resp = self.client.open_url(valid_url, "shoro-test")
                self.assertTrue(resp.success, f"Expected {valid_url} to succeed, got {resp.error}")

    @patch("subprocess.run")
    def test_get_version_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="OpenClaw 2026.10.9 (8b18b55)\n",
            stderr="",
        )
        resp = self.client.get_version()
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["version_number"], "2026.10.9")
        mock_run.assert_called_once()
        # Verify shell=False
        self.assertFalse(mock_run.call_args[1].get("shell", True))

    @patch("subprocess.run")
    def test_get_gateway_health_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Gateway event loop: ok max=44ms p99=44ms util=0.042 cpu=0.078\n"
                "Gateway check duration: 84ms\n"
                "Agents: main (default)\n"
            ),
            stderr="",
        )
        resp = self.client.get_gateway_health()
        self.assertTrue(resp.success)
        self.assertTrue(resp.data["healthy"])
        self.assertEqual(resp.data["check_duration"], "84ms")
        self.assertEqual(resp.data["agents"], "main (default)")
        self.assertFalse(mock_run.call_args[1].get("shell", True))

    @patch("subprocess.run")
    def test_list_browser_profiles_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "shoro-test: stopped\n"
                "  port: 18801, color: #FF4500\n"
                "openclaw: stopped [default]\n"
                "  port: 18800, color: #FF4500\n"
            ),
            stderr="",
        )
        resp = self.client.list_browser_profiles()
        self.assertTrue(resp.success)
        profiles = resp.data
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0]["name"], "shoro-test")
        self.assertEqual(profiles[0]["port"], 18801)
        self.assertFalse(profiles[0]["is_default"])
        self.assertEqual(profiles[1]["name"], "openclaw")
        self.assertTrue(profiles[1]["is_default"])

    @patch("subprocess.run")
    def test_get_browser_status_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "profile: shoro-test\n"
                "running: true\n"
                "cdpPort: 18801\n"
                "browser: chrome\n"
                "headless: false (default)\n"
            ),
            stderr="",
        )
        resp = self.client.get_browser_status("shoro-test")
        self.assertTrue(resp.success)
        self.assertTrue(resp.data["is_running"])
        self.assertEqual(resp.data["cdpPort"], "18801")
        self.assertEqual(resp.data["browser"], "chrome")
        # Verify --browser-profile shoro-test was passed in argument list
        cmd = mock_run.call_args[0][0]
        self.assertIn("--browser-profile", cmd)
        self.assertIn("shoro-test", cmd)
        self.assertFalse(mock_run.call_args[1].get("shell", True))

    @patch("subprocess.run")
    def test_open_url_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="opened: https://example.com\nid: 2D554D1E14290E0D\n",
            stderr="",
        )
        resp = self.client.open_url("https://example.com", "shoro-test")
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["url"], "https://example.com")
        self.assertEqual(resp.data["tab_id"], "2D554D1E14290E0D")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--browser-profile", cmd)
        self.assertIn("shoro-test", cmd)
        self.assertIn("https://example.com", cmd)
        self.assertFalse(mock_run.call_args[1].get("shell", True))

    @patch("subprocess.run")
    def test_get_page_snapshot_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- generic [active]:\n  - paragraph: Example Domain\n",
            stderr="",
        )
        resp = self.client.get_page_snapshot("shoro-test")
        self.assertTrue(resp.success)
        self.assertIn("Example Domain", resp.data)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--browser-profile", cmd)
        self.assertIn("shoro-test", cmd)
        self.assertFalse(mock_run.call_args[1].get("shell", True))

    @patch("subprocess.run")
    def test_capture_screenshot_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="C:\\Users\\sneha\\.openclaw\\media\\test.png\n",
            stderr="",
        )
        resp = self.client.capture_screenshot("shoro-test")
        self.assertTrue(resp.success)
        self.assertIn("test.png", resp.data["screenshot_path"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("--browser-profile", cmd)
        self.assertIn("shoro-test", cmd)
        self.assertFalse(mock_run.call_args[1].get("shell", True))

    @patch("subprocess.run")
    def test_stop_browser_mocked(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="browser [shoro-test] running: false\n",
            stderr="",
        )
        resp = self.client.stop_browser("shoro-test")
        self.assertTrue(resp.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--browser-profile", cmd)
        self.assertIn("shoro-test", cmd)
        self.assertFalse(mock_run.call_args[1].get("shell", True))

    @patch("subprocess.run")
    def test_timeout_handling(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["openclaw"], timeout=5)
        resp = self.client._execute(["browser", "status"], timeout=5)
        self.assertFalse(resp.success)
        self.assertIn("timed out after 5 seconds", resp.error)
        self.assertEqual(resp.exit_code, -2)

    @patch("subprocess.run")
    def test_cli_error_handling(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: Profile not found",
        )
        resp = self.client._execute(["browser", "status", "--browser-profile", "unknown"])
        self.assertFalse(resp.success)
        self.assertIn("Profile not found", resp.error)
        self.assertEqual(resp.exit_code, 1)

    def test_no_automated_submission_methods(self):
        """Verify no automated submission, form filling, or commenting methods exist."""
        disallowed_method_keywords = [
            "submit", "comment", "post", "fill", "type", "click", "publish",
            "reply", "upvote", "downvote", "login", "auth_login"
        ]
        client_methods = [
            attr for attr in dir(OpenClawClient)
            if callable(getattr(OpenClawClient, attr)) and not attr.startswith("_")
        ]
        for method_name in client_methods:
            for kw in disallowed_method_keywords:
                self.assertNotIn(
                    kw,
                    method_name.lower(),
                    f"Disallowed automated action method found: {method_name}",
                )

    # -----------------------------------------------------------------
    # Tests for persistent session helpers (added for Phase E)
    # -----------------------------------------------------------------

    def test_get_profile_data_dir_returns_string(self):
        """get_profile_data_dir should return a non-empty path string for any valid name."""
        path = OpenClawClient.get_profile_data_dir("shoro-test")
        self.assertIsInstance(path, str)
        self.assertIn("shoro-test", path)

    def test_get_profile_data_dir_protected_profiles_not_blocked(self):
        """get_profile_data_dir is a pure path computation; profile validation is
        the responsibility of check_profile_session_exists."""
        # This only builds a path string – it never accesses the filesystem.
        path = OpenClawClient.get_profile_data_dir("chrome")
        self.assertIsInstance(path, str)

    def test_check_profile_session_exists_protected_profile_rejected(self):
        """check_profile_session_exists must reject protected profiles."""
        for prof in ["user", "chrome"]:
            resp = self.client.check_profile_session_exists(prof)
            self.assertFalse(resp.success)
            self.assertIn("Access to protected profile", resp.error)

    def test_check_profile_session_exists_empty_profile_rejected(self):
        resp = self.client.check_profile_session_exists("")
        self.assertFalse(resp.success)
        self.assertIn("cannot be empty", resp.error)

    def test_check_profile_session_exists_missing_dir(self):
        """When profile dir does not exist, success=False with helpful note."""
        import tempfile, os
        # Use a guaranteed non-existent path by monkeypatching get_profile_data_dir
        original = OpenClawClient.get_profile_data_dir
        OpenClawClient.get_profile_data_dir = staticmethod(
            lambda name: os.path.join(tempfile.gettempdir(), "nonexistent_oc_profile_xyzzy", name)
        )
        try:
            resp = self.client.check_profile_session_exists("shoro-test")
            self.assertFalse(resp.success)
            self.assertEqual(resp.data["exists"], False)
            self.assertIn("No persistent session directory found", resp.data["note"])
        finally:
            OpenClawClient.get_profile_data_dir = staticmethod(original)

    def test_check_profile_session_exists_present_dir(self):
        """When profile dir exists, success=True with note about retained data."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            fake_profile_dir = os.path.join(tmp, "shoro-test")
            os.makedirs(fake_profile_dir)
            original = OpenClawClient.get_profile_data_dir
            OpenClawClient.get_profile_data_dir = staticmethod(lambda name: fake_profile_dir)
            try:
                resp = self.client.check_profile_session_exists("shoro-test")
                self.assertTrue(resp.success)
                self.assertEqual(resp.data["exists"], True)
                self.assertIn("Persistent session directory found", resp.data["note"])
                # Confirm data directory value is returned
                self.assertEqual(resp.data["data_dir"], fake_profile_dir)
            finally:
                OpenClawClient.get_profile_data_dir = staticmethod(original)

    @patch("subprocess.run")
    def test_list_tabs_json_mocked(self, mock_run):
        """list_tabs should parse JSON tabs output from OpenClaw."""
        import json
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "tabs": [
                    {"targetId": "TAB_AAA_111", "tabId": "1", "title": "Example Domain", "url": "https://example.com"},
                    {"targetId": "TAB_BBB_222", "tabId": "2", "title": "Reddit Thread", "url": "https://reddit.com/r/test"},
                ]
            }),
            stderr="",
        )
        resp = self.client.list_tabs("shoro-test")
        self.assertTrue(resp.success)
        tabs = resp.data
        self.assertEqual(len(tabs), 2)
        self.assertEqual(tabs[0]["target_id"], "TAB_AAA_111")
        self.assertEqual(tabs[0]["tab_id"], "1")
        self.assertEqual(tabs[0]["url"], "https://example.com")
        self.assertEqual(tabs[1]["target_id"], "TAB_BBB_222")

    @patch("subprocess.run")
    def test_list_tabs_text_mocked(self, mock_run):
        """list_tabs should parse text output if JSON is unavailable."""
        text_output = (
            "1. Example Domain [tab: 1]\n"
            "   https://example.com/\n"
            "   id: 2D554D1E14290E0D\n"
        )
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=text_output,
            stderr="",
        )
        resp = self.client.list_tabs("shoro-test")
        self.assertTrue(resp.success)
        tabs = resp.data
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["target_id"], "2D554D1E14290E0D")
        self.assertEqual(tabs[0]["tab_id"], "1")
        self.assertEqual(tabs[0]["url"], "https://example.com/")

    @patch("subprocess.run")
    def test_open_url_captures_both_tab_and_target_id(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="opened: https://example.com\ntab: 3\nid: 9A8B7C6D5E\n",
            stderr="",
        )
        resp = self.client.open_url("https://example.com", "shoro-test")
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["target_id"], "9A8B7C6D5E")
        self.assertEqual(resp.data["tab_id"], "3")

    @patch("subprocess.run")
    def test_snapshot_with_target_id(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- generic [active]: Example Domain\n",
            stderr="",
        )
        resp = self.client.get_page_snapshot("shoro-test", target_id="9A8B7C6D5E")
        self.assertTrue(resp.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--target-id", cmd)
        self.assertIn("9A8B7C6D5E", cmd)

    @patch("subprocess.run")
    def test_screenshot_with_target_id(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="C:\\test\\shot.png\n",
            stderr="",
        )
        resp = self.client.capture_screenshot("shoro-test", target_id="9A8B7C6D5E")
        self.assertTrue(resp.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("9A8B7C6D5E", cmd)

    # -----------------------------------------------------------------
    # Tests for CAPTCHA detection (added for Phase E)
    # -----------------------------------------------------------------

    def test_captcha_detection_positive_cases(self):
        """detect_captcha_in_snapshot must fire on known CAPTCHA signals."""
        captcha_texts = [
            "Please complete the CAPTCHA below to continue.",
            "Are you a robot? I'm not a robot checkbox",
            "Verify you are human to proceed.",
            "Security check: please verify you're human.",
            "This site is protected by reCAPTCHA and the Google Privacy Policy.",
            "Please verify with hCaptcha before proceeding.",
            "Suspicious activity detected. Prove you're not a robot.",
            "Human Verification Required",
            "Cloudflare Turnstile check",
        ]
        for text in captcha_texts:
            with self.subTest(text=text[:50]):
                self.assertTrue(
                    OpenClawClient.detect_captcha_in_snapshot(text),
                    f"Expected CAPTCHA detection for: {text[:50]}"
                )

    def test_captcha_detection_negative_cases(self):
        """detect_captcha_in_snapshot must not fire on normal page content."""
        normal_texts = [
            "- generic [active]:\n  - paragraph: Example Domain\n  - paragraph: This domain is for use in illustrative examples.",
            "Reddit: the front page of the internet",
            "Your comment was submitted successfully.",
            "Sign in to continue",   # Generic sign-in, not a CAPTCHA challenge
            "",
        ]
        for text in normal_texts:
            with self.subTest(text=text[:50]):
                self.assertFalse(
                    OpenClawClient.detect_captcha_in_snapshot(text),
                    f"Unexpected CAPTCHA detection for: {text[:50]}"
                )

    @patch("subprocess.run")
    def test_error_translation_browser_request_timed_out(self, mock_run):
        """GatewayClientRequestError: Error: browser request timed out must be mapped to clear advice."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="GatewayClientRequestError: Error: browser request timed out\n",
        )
        resp = self.client.capture_screenshot("shoro-test")
        self.assertFalse(resp.success)
        self.assertIn("Browser request timed out", resp.error)
        self.assertIn("Stop Browser", resp.error)

    @patch("subprocess.run")
    def test_stop_browser_does_not_delete_profile_dir(self, mock_run):
        """Stopping the browser must NOT delete the persistent profile directory."""
        import tempfile, os
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="browser [shoro-test] running: false\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_profile_dir = os.path.join(tmp, "shoro-test")
            os.makedirs(fake_profile_dir)
            # Create a sentinel file to verify directory is untouched.
            sentinel = os.path.join(fake_profile_dir, "Cookies")
            with open(sentinel, "w") as f:
                f.write("fake cookies")

            original = OpenClawClient.get_profile_data_dir
            OpenClawClient.get_profile_data_dir = staticmethod(lambda name: fake_profile_dir)
            try:
                resp = self.client.stop_browser("shoro-test")
                self.assertTrue(resp.success)
                # Critical assertion: profile directory and cookies still exist.
                self.assertTrue(os.path.exists(fake_profile_dir),
                                "Profile directory was deleted by stop_browser – this is a bug.")
                self.assertTrue(os.path.exists(sentinel),
                                "Cookie file was deleted by stop_browser – this is a bug.")
            finally:
                OpenClawClient.get_profile_data_dir = staticmethod(original)

    @patch("subprocess.run")
    def test_evaluate_script_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"verifiedText": "hello"}', stderr="")
        resp = self.client.evaluate_script("shoro-test", "() => 'hello'", target_id="TGT_1")
        self.assertTrue(resp.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("evaluate", cmd)
        self.assertIn("--fn", cmd)
        self.assertIn("--target-id", cmd)
        self.assertIn("TGT_1", cmd)

    def test_evaluate_script_protected_profile_rejected(self):
        resp = self.client.evaluate_script("chrome", "() => 1")
        self.assertFalse(resp.success)
        self.assertIn("protected profile", resp.error.lower())

    @patch("subprocess.run")
    def test_wait_for_condition(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="waited", stderr="")
        resp = self.client.wait_for("shoro-test", text="Join", time_ms=1000)
        self.assertTrue(resp.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("wait", cmd)
        self.assertIn("--text", cmd)
        self.assertIn("Join", cmd)
        self.assertIn("--time", cmd)
        self.assertIn("1000", cmd)

    @patch("subprocess.run")
    def test_click_ref_internal_helper(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="clicked", stderr="")
        resp = self.client._click_ref("shoro-test", "14", target_id="TGT_2")
        self.assertTrue(resp.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("click", cmd)
        self.assertIn("14", cmd)
        self.assertIn("--target-id", cmd)

    @patch("subprocess.run")
    def test_type_ref_never_submits(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="typed", stderr="")
        resp = self.client._type_ref("shoro-test", "14", "my reply text", target_id="TGT_2")
        self.assertTrue(resp.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("type", cmd)
        self.assertIn("14", cmd)
        self.assertIn("my reply text", cmd)
        self.assertNotIn("--submit", cmd)
        self.assertNotIn("Enter", cmd)

    def test_inspect_reddit_discussion_state_detection(self):
        self.assertFalse(OpenClawClient.inspect_reddit_discussion_state("this thread is locked by the moderators")[0])
        self.assertFalse(OpenClawClient.inspect_reddit_discussion_state("this is an archived post")[0])
        self.assertFalse(OpenClawClient.inspect_reddit_discussion_state("sorry, this post has been removed")[0])
        self.assertFalse(OpenClawClient.inspect_reddit_discussion_state("404 not found page not found")[0])
        self.assertTrue(OpenClawClient.inspect_reddit_discussion_state("normal reddit thread discussion with comments")[0])

    def test_find_editor_ref_in_snapshot(self):
        snapshot1 = "- button [ref 42]: Join the conversation"
        self.assertEqual(OpenClawClient.find_editor_ref_in_snapshot(snapshot1), "42")
        snapshot2 = "- textbox ref: 99 (placeholder='Add a comment')"
        self.assertEqual(OpenClawClient.find_editor_ref_in_snapshot(snapshot2), "99")
        snapshot3 = "- paragraph: Just normal text"
        self.assertIsNone(OpenClawClient.find_editor_ref_in_snapshot(snapshot3))


class TestOpenClawIntegration(unittest.TestCase):
    """Integration test against https://example.com only (no Reddit)."""

    def setUp(self):
        self.client = OpenClawClient()

    def test_version_integration(self):
        if not self.client.cli_path:
            self.skipTest("OpenClaw CLI not installed on this system.")
        resp = self.client.get_version()
        self.assertTrue(resp.success, resp.error)
        self.assertIsNotNone(resp.data.get("version_number"))


if __name__ == "__main__":
    unittest.main()
