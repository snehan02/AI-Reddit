"""Unit tests for TaskValidator."""

import unittest
from modules.validator import TaskValidator


class TestTaskValidator(unittest.TestCase):
    def test_validate_url_valid(self):
        valid_urls = [
            "https://www.reddit.com/r/driving/comments/12345/test/",
            "http://reddit.com/r/DMV",
            "https://old.reddit.com/r/driving",
            "https://np.reddit.com/r/driving/comments/abc",
            "https://example.com",
            "https://example.com/test?query=param#fragment",
            "https://www.example.com/page",
        ]
        for url in valid_urls:
            valid, reason = TaskValidator.validate_url(url)
            self.assertTrue(valid, f"Expected {url} to be valid, got: {reason}")
            self.assertEqual(reason, "")

    def test_validate_url_deceptive_domains(self):
        deceptive_urls = [
            "https://reddit.com.attacker.com",
            "https://reddit.com.evil.com/path",
            "https://notreddit.com/r/driving",
            "https://fake-reddit.com",
            "https://reddit.com@attacker.com",
            "https://reddit.com:password@attacker.com",
            "https://attacker.com/reddit.com",
            "https://attacker.com?reddit.com",
            "https://reddit.com.com",
            "https://dcurbanmom.com/thread/123",
            "https://shoro.ai/test",
            "https://google.com",
        ]
        for url in deceptive_urls:
            valid, reason = TaskValidator.validate_url(url)
            self.assertFalse(valid, f"Expected {url} to be rejected")
            self.assertIn("invalid URL", reason)

    def test_validate_url_ip_addresses(self):
        ip_urls = [
            "http://10.0.0.1",
            "http://10.0.0.1:8080",
            "http://192.168.1.1",
            "http://172.16.0.1",
            "http://172.31.255.255",
            "http://8.8.8.8",
            "http://2130706433",
            "http://0x7f.0.0.1",
        ]
        for url in ip_urls:
            valid, reason = TaskValidator.validate_url(url)
            self.assertFalse(valid, f"Expected IP URL {url} to be rejected")
            self.assertIn("invalid URL", reason)

    def test_validate_url_localhost_and_loopback(self):
        localhost_urls = [
            "http://localhost",
            "http://localhost:8080",
            "http://localhost:18789",
            "http://app.localhost",
            "http://127.0.0.1",
            "http://127.0.0.1:18789",
            "http://127.1.2.3",
            "http://[::1]",
            "http://[::1]:8080",
        ]
        for url in localhost_urls:
            valid, reason = TaskValidator.validate_url(url)
            self.assertFalse(valid, f"Expected localhost URL {url} to be rejected")
            self.assertIn("invalid URL", reason)

    def test_validate_url_metadata_addresses(self):
        metadata_urls = [
            "http://169.254.169.254",
            "http://169.254.169.254/latest/meta-data/",
            "http://instance-data",
            "http://metadata.google.internal",
            "http://test.metadata.google.internal",
        ]
        for url in metadata_urls:
            valid, reason = TaskValidator.validate_url(url)
            self.assertFalse(valid, f"Expected metadata URL {url} to be rejected")
            self.assertIn("invalid URL", reason)

    def test_validate_url_malformed(self):
        malformed_urls = [
            "",
            "   ",
            None,
            "Any websites or helpful tools to retain information? : r/Washington",
            "just_a_string",
            "http://",
            "https://",
            "https://reddit.com/path with spaces",
            "http:///example.com",
        ]
        for url in malformed_urls:
            valid, reason = TaskValidator.validate_url(url)
            self.assertFalse(valid, f"Expected malformed URL '{url}' to be rejected")
            self.assertIn("invalid URL", reason)

    def test_validate_url_unsupported_schemes(self):
        unsupported = [
            "ftp://reddit.com",
            "javascript:alert(1)",
            "file:///C:/Windows/System32",
            "file:///etc/passwd",
            "data:text/html,<html>test</html>",
            "chrome://settings",
            "about:blank",
        ]
        for url in unsupported:
            valid, reason = TaskValidator.validate_url(url)
            self.assertFalse(valid, f"Expected unsupported scheme URL '{url}' to be rejected")
            self.assertIn("invalid URL", reason)

    def test_validate_url_nonstandard_ports(self):
        nonstandard_ports = [
            "https://reddit.com:8080",
            "https://reddit.com:18789",
            "https://example.com:8443",
            "http://reddit.com:22",
        ]
        for url in nonstandard_ports:
            valid, reason = TaskValidator.validate_url(url)
            self.assertFalse(valid, f"Expected non-standard port URL '{url}' to be rejected")
            self.assertIn("invalid URL", reason)

    def test_validate_account(self):
        self.assertTrue(TaskValidator.validate_account("schoolwaale01@gmail.com")[0])
        self.assertTrue(TaskValidator.validate_account("account_1")[0])

        # Blank accounts
        self.assertFalse(TaskValidator.validate_account("")[0])
        self.assertFalse(TaskValidator.validate_account("   ")[0])
        self.assertFalse(TaskValidator.validate_account(None)[0])
        self.assertFalse(TaskValidator.validate_account("None")[0])
        self.assertFalse(TaskValidator.validate_account("none")[0])

    def test_validate_reply_text(self):
        self.assertTrue(TaskValidator.validate_reply_text("This is an approved reply")[0])
        self.assertFalse(TaskValidator.validate_reply_text("")[0])
        self.assertFalse(TaskValidator.validate_reply_text("   ")[0])
        self.assertFalse(TaskValidator.validate_reply_text(None)[0])

    def test_validate_post_fields(self):
        self.assertTrue(TaskValidator.validate_post_fields("r/DMV", "Title", "Body")[0])
        self.assertFalse(TaskValidator.validate_post_fields("", "Title", "Body")[0])
        self.assertFalse(TaskValidator.validate_post_fields("r/DMV", "", "Body")[0])
        self.assertFalse(TaskValidator.validate_post_fields("r/DMV", "Title", "")[0])

    def test_check_existing_status(self):
        # Actionable statuses
        self.assertTrue(TaskValidator.check_existing_status(None)[0])
        self.assertTrue(TaskValidator.check_existing_status("")[0])
        self.assertTrue(TaskValidator.check_existing_status("To review")[0])
        self.assertTrue(TaskValidator.check_existing_status("Ready for review")[0])

        # Non-actionable statuses
        self.assertFalse(TaskValidator.check_existing_status("post deleted")[0])
        self.assertFalse(TaskValidator.check_existing_status("Locked post")[0])
        self.assertFalse(TaskValidator.check_existing_status("deleted by user")[0])
        self.assertFalse(TaskValidator.check_existing_status("Archived post. New comments cannot be posted")[0])
        self.assertFalse(TaskValidator.check_existing_status("Completed - human confirmed")[0])


if __name__ == "__main__":
    unittest.main()

