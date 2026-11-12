"""Application Logging Configuration with Rotation and Redaction."""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
from typing import Optional

from modules.openclaw_client import sanitize_text

# Regex to detect raw HTML/DOM snapshots in log messages
HTML_DOM_INDICATORS = re.compile(r"(?i)<(?:html|!doctype|body|div|span|script|style)[^>]*>")


class SensitiveDataRedactionFilter(logging.Filter):
    """Filter that redacts sensitive credentials and truncates complete DOM snapshots in logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        def _clean_str(s: str) -> str:
            # 1. Redact credentials, tokens, and secrets
            s = sanitize_text(s)
            # 2. Prevent logging complete browser DOM snapshots
            if len(s) > 300 and HTML_DOM_INDICATORS.search(s):
                return f"[DOM Snapshot omitted from log - length: {len(s)} chars]"
            return s

        if isinstance(record.msg, str):
            record.msg = _clean_str(record.msg)

        # Also sanitize string arguments if present
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _clean_str(str(v)) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(_clean_str(str(a)) if isinstance(a, str) else a for a in record.args)

        return True


def setup_application_logging(
    log_file: str | Path = "logs/application.log",
    max_bytes: int = 5 * 1024 * 1024,  # 5 MB
    backup_count: int = 5,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure rotating application logger with sensitive data redaction."""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Check if a RotatingFileHandler targeting this log path is already configured
    normalized_target = str(log_path.resolve()).lower()
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            base_fn = getattr(handler, "baseFilename", "")
            if base_fn and str(Path(base_fn).resolve()).lower() == normalized_target:
                return root_logger

    file_handler = RotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SensitiveDataRedactionFilter())
    root_logger.addHandler(file_handler)

    return root_logger
