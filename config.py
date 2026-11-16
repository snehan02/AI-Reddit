"""Configuration module for Shoro AI Reddit Operations."""

from dataclasses import dataclass
from pathlib import Path
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = Path("config/settings.json")
DEFAULT_ACCOUNTS_PATH = Path("config/accounts.json")


def is_force_test_mode_enabled() -> bool:
    """Check if the environment-level FORCE_TEST_MODE safeguard is active."""
    val = os.environ.get("FORCE_TEST_MODE", "").strip().lower()
    return val in ("true", "1", "yes", "on", "enabled")


@dataclass
class AppSettings:
    workbook_path: str = "data/Reddit Threads.xlsx"
    comment_sheet: str = "Driving DMV"
    post_sheet: str = "Posts"
    test_mode: bool = True
    daily_comment_cap: int = 5
    posts_per_account: int = 5
    min_post_interval_hours: float = 8.0
    review_task_pause_seconds: int = 30
    display_timezone: str = "Asia/Kolkata"
    require_human_outcome_confirmation: bool = True
    automated_submission_enabled: bool = False  # Strictly False in release 1
    google_spreadsheet_id: str = "1_AcKE7VErFV2sLIFqLWeGcBQy8AKmPBEmeSPkHjUkl4"
    google_sheets_webhook_url: str = ""
    database_path: str = "database/execution_state.db"
    backup_directory: str = "backups"
    log_file: str = "logs/application.log"
    log_max_bytes: int = 5 * 1024 * 1024  # 5 MB
    log_backup_count: int = 5

    def __post_init__(self) -> None:
        if is_force_test_mode_enabled():
            self.test_mode = True
        self.automated_submission_enabled = False

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "test_mode" and not value:
            if is_force_test_mode_enabled():
                logger.warning(
                    "Attempt to disable Test Mode rejected: FORCE_TEST_MODE environment safeguard is active."
                )
                value = True
        elif name == "automated_submission_enabled":
            value = False
        super().__setattr__(name, value)

    def is_force_test_mode_active(self) -> bool:
        """Return True if environment safeguard FORCE_TEST_MODE is currently active."""
        return is_force_test_mode_enabled()

    @classmethod
    def load(cls, file_path: Path | str = DEFAULT_SETTINGS_PATH) -> "AppSettings":
        path = Path(file_path)
        if not path.exists():
            logger.warning("Settings file %s not found. Using defaults.", path)
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
            # Enforce compliance gate: automated_submission_enabled must remain False
            data["automated_submission_enabled"] = False
            # Enforce environment safeguard if active
            if is_force_test_mode_enabled():
                data["test_mode"] = True
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception as exc:
            logger.error("Failed to load settings from %s: %s. Using defaults.", path, exc)
            return cls()

    def save(self, file_path: Path | str = DEFAULT_SETTINGS_PATH) -> None:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Force automated submission to False before saving
        data = {k: getattr(self, k) for k in self.__dataclass_fields__}
        data["automated_submission_enabled"] = False
        if is_force_test_mode_enabled():
            data["test_mode"] = True
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved settings to %s", path)

