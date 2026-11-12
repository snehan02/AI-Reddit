"""Unit tests for Application Logging Configuration, Rotation, and Redaction."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import unittest
import uuid

from modules.logging_config import setup_application_logging, SensitiveDataRedactionFilter


class TestLoggingConfig(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.test_dir / f"test_log_{uuid.uuid4().hex[:8]}.log"
        self.logger_name = f"test_logger_{uuid.uuid4().hex[:8]}"
        self.logger = logging.getLogger(self.logger_name)
        self.logger.setLevel(logging.INFO)

        # Setup custom rotating file handler for this test
        self.file_handler = RotatingFileHandler(
            str(self.log_file),
            maxBytes=500,  # Small size to trigger fast rotation
            backupCount=3,
            encoding="utf-8",
        )
        self.file_handler.setFormatter(logging.Formatter("%(message)s"))
        self.file_handler.addFilter(SensitiveDataRedactionFilter())
        self.logger.addHandler(self.file_handler)

    def tearDown(self):
        self.file_handler.close()
        self.logger.removeHandler(self.file_handler)
        try:
            for p in self.test_dir.glob(f"{self.log_file.stem}*"):
                p.unlink()
        except Exception:
            pass

    def test_sensitive_credentials_redacted_in_logs(self):
        """Tokens, passwords, and API keys must be redacted by SensitiveDataRedactionFilter."""
        self.logger.info("Connecting with password=SuperSecretPassword123 and token=my_gateway_token_99999")
        self.logger.info("Service key api_key=live_sec_key_1122334455")
        self.file_handler.flush()

        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn("SuperSecretPassword123", log_content)
        self.assertNotIn("my_gateway_token_99999", log_content)
        self.assertNotIn("live_sec_key_1122334455", log_content)
        self.assertIn("[REDACTED]", log_content)

    def test_complete_dom_snapshots_omitted_from_logs(self):
        """Full browser DOM snapshots must not be dumped into log files."""
        large_dom = "<html><body>" + "<div>Large DOM content for discussion page</div>" * 20 + "</body></html>"
        self.logger.info("Captured DOM: %s", large_dom)
        self.file_handler.flush()

        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn("Large DOM content for discussion page", log_content)
        self.assertIn("[DOM Snapshot omitted from log", log_content)

    def test_log_file_rotation(self):
        """Log files must rotate when exceeding maxBytes and maintain backup count."""
        # Write enough lines to exceed 500 bytes multiple times
        for i in range(50):
            self.logger.info(f"Log message sequence {i:03d} to trigger file rotation.")
        self.file_handler.flush()

        # Check that backup rotated files exist (e.g. .log.1, .log.2)
        rotated_backups = list(self.test_dir.glob(f"{self.log_file.name}.*"))
        self.assertGreater(len(rotated_backups), 0, "Expected at least one rotated backup file")
        # Ensure backup count does not exceed configured limit of 3
        self.assertLessEqual(len(rotated_backups), 3)

    def test_setup_application_logging_creates_rotating_handler(self):
        """setup_application_logging creates a RotatingFileHandler with SensitiveDataRedactionFilter."""
        app_log_path = self.test_dir / f"app_test_{uuid.uuid4().hex[:8]}.log"
        root_logger = setup_application_logging(
            log_file=app_log_path,
            max_bytes=1024 * 1024,
            backup_count=2,
        )
        # Check that a RotatingFileHandler exists on root logger
        matching_handlers = [
            h for h in root_logger.handlers
            if isinstance(h, RotatingFileHandler)
        ]
        self.assertGreater(len(matching_handlers), 0)
        # Check filter is attached
        handler = matching_handlers[0]
        has_filter = any(isinstance(f, SensitiveDataRedactionFilter) for f in handler.filters)
        self.assertTrue(has_filter)


if __name__ == "__main__":
    unittest.main()

