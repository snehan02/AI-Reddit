"""Workflow Controller coordinating validation, queue, review navigation, pacing, and outcomes."""

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import json
import logging
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config import AppSettings, is_force_test_mode_enabled
from modules.account_manager import AccountManager, AccountProfile
from modules.audit_logger import AuditLogger, get_current_timestamp
from modules.browser_manager import BrowserReviewManager
from modules.comment_manager import CommentManager
from modules.duplicate_checker import DuplicateChecker
from modules.excel_handler import ExcelHandler
from modules.openclaw_client import OpenClawClient, sanitize_dom_snapshot
from modules.post_manager import PostManager
from modules.schema_inspector import SchemaInspector, WorkbookSchemaReport
from modules.state_manager import StateManager, RunStatus
from modules.task_builder import CommentTask, PostTask
from modules.validator import TaskValidator

logger = logging.getLogger(__name__)


@dataclass
class RedditSessionStatus:
    account_id: str
    profile_name: str
    profile_dir: str
    directory_exists: bool
    browser_running: bool
    reddit_authenticated: bool
    authenticated_username: Optional[str] = None
    status_label: str = "Unverified"
    details: str = ""


class WorkflowController:
    """Master controller orchestrating the Shoro AI Reddit operations pipeline."""

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        account_manager: Optional[AccountManager] = None,
        openclaw_client: Optional[OpenClawClient] = None,
    ):
        self.settings = settings or AppSettings.load()
        self.schema_inspector = SchemaInspector(self.settings.workbook_path)
        self.account_manager = account_manager or AccountManager("config/accounts.json")
        self.openclaw_client = openclaw_client or OpenClawClient()
        self.audit_logger = AuditLogger(self.settings.database_path, self.settings.display_timezone)
        self.state_manager = StateManager(
            self.audit_logger,
            daily_cap=self.settings.daily_comment_cap,
        )
        self.browser_manager = BrowserReviewManager(test_mode=self.settings.test_mode)
        self.excel_handler = ExcelHandler(
            workbook_path=self.settings.workbook_path,
            backup_dir=self.settings.backup_directory,
            tz_name=self.settings.display_timezone,
        )
        self.duplicate_checker = DuplicateChecker(self.settings.database_path)

        self.schema_report: Optional[WorkbookSchemaReport] = None
        self.comment_tasks: List[CommentTask] = []
        self.post_tasks: List[PostTask] = []
        self.task_queue: List[CommentTask | PostTask] = []
        self.stats: Dict[str, Any] = {}
        self.current_task: Optional[CommentTask | PostTask] = None
        self.active_tab_id: Optional[str] = None
        self.active_target_id: Optional[str] = None
        self._browser_lock = threading.Lock()

    def initialize(self, reload_accounts: bool = True) -> WorkbookSchemaReport:
        """Run workbook inspection and prepare task queue."""
        logger.info("Initializing workflow controller...")
        # 1. Inspect schema
        self.schema_report = self.schema_inspector.inspect()

        # 2. Load and validate account mappings
        if reload_accounts:
            self.account_manager.load()

        # 3. Load comment tasks
        cm = CommentManager(
            self.settings.workbook_path,
            self.settings.comment_sheet,
            account_manager=self.account_manager,
            duplicate_checker=self.duplicate_checker,
        )
        self.comment_tasks, comment_stats = cm.load_tasks()

        # 4. Load post tasks
        pm = PostManager(
            self.settings.workbook_path,
            self.settings.post_sheet,
            account_manager=self.account_manager,
            duplicate_checker=self.duplicate_checker,
            allow_account_independent=True,
        )
        self.post_tasks, post_stats = pm.load_tasks()

        # 5. Persist task state into database
        for task in self.comment_tasks:
            if task.fingerprint:
                self.audit_logger.log_task(
                    task_id=task.task_id,
                    task_fingerprint=task.fingerprint,
                    workbook_name=Path(self.settings.workbook_path).name,
                    worksheet_name=task.worksheet,
                    excel_row_number=task.excel_row,
                    task_type=task.task_type,
                    account_id=task.account_id,
                    destination_url=task.destination_url,
                    task_status=task.status,
                )

        for post in self.post_tasks:
            if post.fingerprint:
                self.audit_logger.log_task(
                    task_id=post.task_id,
                    task_fingerprint=post.fingerprint,
                    workbook_name=Path(self.settings.workbook_path).name,
                    worksheet_name=post.worksheet,
                    excel_row_number=post.excel_row,
                    task_type=post.task_type,
                    account_id=post.account_id,
                    destination_url=f"https://www.reddit.com/{post.subreddit}",
                    task_status=post.status,
                )

        # Build combined active queue
        self.task_queue = [t for t in self.comment_tasks + self.post_tasks if t.is_eligible]

        # Calculate statistics
        eligible_comments = sum(1 for t in self.comment_tasks if t.is_eligible)
        eligible_posts = sum(1 for t in self.post_tasks if t.is_eligible)
        self.stats = {
            "total_comments": len(self.comment_tasks),
            "eligible_comments": eligible_comments,
            "skipped_comments": len(self.comment_tasks) - eligible_comments,
            "total_posts": len(self.post_tasks),
            "eligible_posts": eligible_posts,
            "skipped_posts": len(self.post_tasks) - eligible_posts,
            "total_queue": len(self.task_queue),
        }

        logger.info("Workflow initialization complete: %s", self.stats)
        return self.schema_report

    def start_run(self) -> str:
        """Start run session."""
        effective_test_mode = self.settings.test_mode or is_force_test_mode_enabled()
        self.browser_manager.test_mode = effective_test_mode
        return self.state_manager.start_session(
            test_mode=effective_test_mode,
            total_tasks=len(self.task_queue),
        )

    def pause_run(self) -> None:
        """Pause run session."""
        self.state_manager.pause_session()

    def resume_run(self) -> None:
        """Resume run session."""
        self.state_manager.resume_session()

    def stop_run(self) -> None:
        """Stop run session."""
        self.state_manager.stop_session()
        self.browser_manager.close_active()

    def get_next_task(self) -> Optional[CommentTask | PostTask]:
        """Fetch next actionable task respecting daily limits."""
        for task in self.task_queue:
            if task.status in ("Ready for review", "To review") and task.is_eligible:
                # Check account daily cap
                if task.account_id:
                    allowed, count, msg = self.state_manager.check_daily_cap(task.account_id)
                    if not allowed:
                        logger.warning(msg)
                        continue
                self.current_task = task
                self.state_manager.current_task_id = task.task_id
                self.state_manager.persist_state()
                return task
        return None

    def open_task_for_review(self, task: CommentTask | PostTask) -> Tuple[bool, str]:
        """Open the task destination in the mapped Chrome profile for operator review."""
        if not task.account_id:
            return False, "Cannot open review: Account is blank."

        profile, err = self.account_manager.resolve_account(task.account_id)
        if not profile:
            return False, f"Cannot open review: {err}"

        dest_url = task.destination_url
        if task.task_type == "post":
            dest_url = f"https://www.reddit.com/{task.subreddit}/submit"

        is_valid_url, url_err = TaskValidator.validate_url(dest_url)
        if not is_valid_url:
            self.audit_logger.log_event(
                task_id=task.task_id,
                event_type="BROWSER_ERROR",
                operator_action="OPEN_BROWSER",
                outcome="Destination URL rejected by security policy",
                error_code="INVALID_URL",
                error_details=url_err,
            )
            return False, f"Cannot open review: {url_err}"

        success, msg = self.browser_manager.open_for_review(
            url=dest_url,
            user_data_dir=profile.user_data_directory,
            profile_name=profile.chrome_profile_name,
        )

        if success:
            task.status = "Opened for review"
            self.audit_logger.log_event(
                task_id=task.task_id,
                event_type="OPENED_FOR_REVIEW",
                operator_action="OPEN_BROWSER",
                outcome=f"Opened in profile '{profile.chrome_profile_name}' ({dest_url})",
            )
        else:
            self.audit_logger.log_event(
                task_id=task.task_id,
                event_type="BROWSER_ERROR",
                operator_action="OPEN_BROWSER",
                outcome="Failed to open browser",
                error_details=msg,
            )

        return success, msg

    def get_openclaw_overview(self) -> Dict[str, Any]:
        """Fetch overall OpenClaw CLI and Gateway status."""
        v_resp = self.openclaw_client.get_version()
        h_resp = self.openclaw_client.get_gateway_health()

        is_healthy = bool(h_resp.success and h_resp.data and h_resp.data.get("healthy"))
        profiles = []
        profiles_error = None
        shoro_status = None
        shoro_running = False
        shoro_error = None

        if is_healthy:
            p_resp = self.openclaw_client.list_browser_profiles()
            if p_resp.success and p_resp.data:
                profiles = p_resp.data
            else:
                profiles_error = p_resp.error

            st_resp = self.openclaw_client.get_browser_status("shoro-test")
            if st_resp.success and st_resp.data:
                shoro_status = st_resp.data
                shoro_running = bool(st_resp.data.get("is_running"))
            else:
                shoro_error = st_resp.error
        else:
            profiles_error = "Gateway is offline or unreachable."

        return {
            "version": v_resp.data if v_resp.success else None,
            "version_error": v_resp.error,
            "health": h_resp.data if h_resp.success else None,
            "health_error": h_resp.error,
            "is_healthy": is_healthy,
            "profiles": profiles,
            "profiles_error": profiles_error,
            "shoro_status": shoro_status,
            "shoro_running": shoro_running,
            "shoro_error": shoro_error,
        }

    def is_profile_allowed(self, profile_name: str) -> bool:
        """Check if profile is an allowed isolated profile and not protected."""
        if not profile_name or not isinstance(profile_name, str) or not profile_name.strip():
            return False
        clean = profile_name.strip()
        from modules.openclaw_client import PROTECTED_PROFILES
        if clean.lower() in PROTECTED_PROFILES:
            return False
        if clean == "shoro-test":
            return True
        enabled_profiles = {
            p.chrome_profile_name for p in self.account_manager.list_accounts() if p.enabled
        }
        return clean in enabled_profiles

    def resolve_active_tab(self, profile_name: str = "shoro-test") -> Tuple[Optional[str], Optional[str]]:
        """Verify stored tab or resolve latest active browser tab.
        
        Requirements:
        - If browser is stopped, returns (None, clear message asking operator to start/reopen).
        - Verifies whether stored tab ID still exists in open tabs.
        - If stored tab no longer exists, retrieves current tabs and resolves to active tab.
        - Clears outdated references if no tabs exist or browser stopped.
        """
        if not self.is_profile_allowed(profile_name):
            return None, f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed."

        # Check if browser is running
        status_resp = self.openclaw_client.get_browser_status(profile_name)
        if status_resp and hasattr(status_resp, "success") and status_resp.success is True and isinstance(status_resp.data, dict):
            if status_resp.data.get("is_running") is False:
                self.active_tab_id = None
                self.active_target_id = None
                return None, "Browser is stopped. Please open a discussion URL or click 'Open in Browser' to start the browser."

        # Fetch current open tabs
        tabs_resp = self.openclaw_client.list_tabs(profile_name)
        if tabs_resp and hasattr(tabs_resp, "success"):
            if tabs_resp.success is False:
                err = str(tabs_resp.error or "")
                if any(k in err.lower() for k in ["not running", "stopped", "closed", "connect", "econnrefused", "no profile"]):
                    self.active_tab_id = None
                    self.active_target_id = None
                    return None, "Browser is stopped. Please open a discussion URL or click 'Open in Browser' to start the browser."
                if self.active_target_id or self.active_tab_id:
                    return self.active_target_id or self.active_tab_id, None
                return None, f"Failed to list browser tabs: {err}"

            tabs = tabs_resp.data if isinstance(tabs_resp.data, list) else None
            if tabs is not None:
                if not tabs:
                    self.active_tab_id = None
                    self.active_target_id = None
                    return None, "No active browser tabs found. Please click 'Open in Browser' to navigate to a discussion."

                # Check if stored tab still exists in the live tabs list
                candidate_ids = {self.active_target_id, self.active_tab_id} - {None, ""}
                found_tab = None
                for t in tabs:
                    t_id = t.get("target_id")
                    tab_num = t.get("tab_id")
                    if (t_id and t_id in candidate_ids) or (tab_num and tab_num in candidate_ids):
                        found_tab = t
                        break

                if found_tab:
                    chosen = found_tab.get("target_id") or found_tab.get("tab_id")
                    return chosen, None

                # Stored tab is stale -> auto-resolve to latest active tab
                valid_tabs = [t for t in tabs if t.get("url") and "about:blank" not in t.get("url", "")]
                selected_tab = valid_tabs[-1] if valid_tabs else tabs[-1]
                self.active_target_id = selected_tab.get("target_id") or selected_tab.get("tab_id")
                self.active_tab_id = selected_tab.get("tab_id") or self.active_target_id
                logger.info(
                    "Stale tab reference auto-resolved to active tab: target=%s (url=%s)",
                    self.active_target_id, selected_tab.get("url")
                )
                return self.active_target_id or self.active_tab_id, None

        # Fallback if list_tabs returned mock/non-list data:
        return self.active_target_id or self.active_tab_id, None

    def open_in_openclaw(
        self,
        url: str,
        profile_name: str = "shoro-test",
        task: Optional[CommentTask | PostTask] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Navigate OpenClaw browser profile to target URL for human review.
        
        Strict safety:
        - Only allows operations on the isolated 'shoro-test' profile.
        - Validates HTTP/HTTPS URL syntax.
        - Stores the newly opened tab ID for subsequent inspection.
        - Avoids simultaneous conflicting browser requests via lock.
        - Never submits forms or posts comments automatically.
        """
        task_id = task.task_id if task else (self.current_task.task_id if self.current_task else "SESSION")

        is_valid_url, url_err = TaskValidator.validate_url(url)
        if not is_valid_url:
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="OPEN_URL",
                outcome="Navigation rejected by URL security policy",
                error_code="INVALID_URL",
                error_details=url_err,
            )
            return False, url_err, None

        if not self.is_profile_allowed(profile_name):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="OPEN_URL",
                outcome=f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed.",
                error_code="PROFILE_LOCKED",
                error_details=f"Attempted profile: {profile_name}",
            )
            return False, f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed.", None

        if not self._browser_lock.acquire(blocking=False):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="OPEN_URL",
                outcome="Operation rejected: Browser busy",
                error_code="CONCURRENCY_LOCK",
                error_details="Another browser operation is currently in progress",
            )
            return False, "Another browser operation is currently in progress. Please wait a moment for it to complete.", None

        try:
            resp = self.openclaw_client.open_url(url=url, profile_name=profile_name)

            if resp.success:
                if isinstance(resp.data, dict):
                    self.active_target_id = resp.data.get("target_id") or resp.data.get("tab_id")
                    self.active_tab_id = resp.data.get("tab_id") or self.active_target_id
                tab_id = self.active_target_id or self.active_tab_id
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_NAVIGATE",
                    operator_action="OPEN_URL",
                    outcome=f"Navigated '{profile_name}' to {url}",
                    error_details=f"Tab ID: {tab_id}",
                )
                return True, f"Navigated {profile_name} to {url}", resp.data
            else:
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="OPEN_URL",
                    outcome=f"Failed to navigate '{profile_name}' to {url}",
                    error_code="NAVIGATION_FAILED",
                    error_details=resp.error,
                )
                return False, resp.error or "Failed to open URL in OpenClaw browser", None
        finally:
            self._browser_lock.release()

    def launch_visible_browser_for_login(
        self,
        profile_name: str = "shoro-test",
        url: str = "https://www.reddit.com/login",
    ) -> Tuple[bool, str]:
        """Launch a visible native Chrome desktop window using the exact bot profile directory.

        This opens an interactive Google Chrome window directly on the operator's desktop,
        allowing manual login, 2FA, or CAPTCHA resolution.
        """
        import os
        import subprocess
        import psutil
        import time

        data_dir = self.openclaw_client.get_profile_data_dir(profile_name)
        if not data_dir:
            data_dir = os.path.expanduser(f"~/.openclaw/browser/{profile_name}/user-data")

        try:
            if not os.path.exists(data_dir):
                os.makedirs(data_dir, exist_ok=True)
        except Exception as e:
            logger.debug("Could not create data_dir (may already exist): %s", e)

        # 1. Terminate any stale background processes holding shoro-test and clear lockfile
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'chrome' in p.info['name'].lower():
                    cmd = p.info.get('cmdline') or []
                    if any(profile_name in str(arg).lower() for arg in cmd):
                        p.kill()
            except Exception:
                pass

        time.sleep(0.3)
        lock_path = os.path.join(data_dir, "lockfile")
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except Exception:
                pass

        # 2. Prefer launching via Windows Explorer shell (os.startfile) with helper bat if available
        bat_candidates = [
            r"d:\Shoro AI Reddit\launch_login_browser.bat",
            os.path.expandvars(r"%USERPROFILE%\OneDrive\Desktop\Login to Reddit.bat"),
            os.path.expandvars(r"%USERPROFILE%\Desktop\Login to Reddit.bat"),
        ]
        bat_file = next((b for b in bat_candidates if os.path.exists(b)), None)

        if os.name == "nt" and bat_file and hasattr(os, "startfile"):
            try:
                os.startfile(bat_file)
                return True, (
                    "Desktop Chrome login window launched! If it did not pop up to the front, "
                    "double-click the 'Login to Reddit.bat' shortcut created on your Desktop."
                )
            except Exception as exc:
                logger.warning("os.startfile on bat failed: %s; trying direct Chrome spawn", exc)

        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        chrome_exe = next((p for p in chrome_candidates if os.path.exists(p)), None)
        if not chrome_exe:
            resp = self.openclaw_client.open_url(url, profile_name)
            return resp.success, resp.error or "Opened in OpenClaw browser"

        try:
            cmd = [
                chrome_exe,
                f"--user-data-dir={data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                url,
            ]
            subprocess.Popen(cmd)
            return True, (
                "Visible Chrome window opened! Please log in to Reddit (Shoro-AI-02) in the window. "
                "You can also use 'Login to Reddit.bat' on your Desktop."
            )
        except Exception as exc:
            logger.error("Failed to launch visible Chrome window: %s", exc)
            return False, f"Failed to launch Chrome window: {exc}"

    def get_openclaw_snapshot(
        self,
        profile_name: str = "shoro-test",
        task: Optional[CommentTask | PostTask] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """Retrieve page snapshot from OpenClaw browser.

        Uses direct inspection with auto-recovery for stale or missing tabs.
        Locks against concurrent execution to prevent port/CDP conflicts.
        Also checks for CAPTCHA indicators and logs a warning event so the
        operator is alerted to handle verification manually.
        """
        task_id = task.task_id if task else (self.current_task.task_id if self.current_task else "SESSION")

        if not self.is_profile_allowed(profile_name):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="DOM_SNAPSHOT",
                outcome=f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed.",
                error_code="PROFILE_LOCKED",
                error_details=f"Attempted profile: {profile_name}",
            )
            return False, f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed.", None

        if not self._browser_lock.acquire(blocking=False):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="DOM_SNAPSHOT",
                outcome="Operation rejected: Browser busy",
                error_code="CONCURRENCY_LOCK",
                error_details="Another browser operation is currently in progress",
            )
            return False, "Another browser operation is currently in progress. Please wait a moment for it to complete.", None

        try:
            # Attempt snapshot with current active target (or active tab if not set)
            target = self.active_target_id or self.active_tab_id
            resp = self.openclaw_client.get_page_snapshot(profile_name, target_id=target)

            # If direct call failed, inspect error and auto-recover if tab is stale/missing
            if not resp.success:
                err_str = (resp.error or "").lower()
                if any(k in err_str for k in ["not running", "stopped", "closed"]):
                    self.active_tab_id = None
                    self.active_target_id = None
                    msg = "Browser is stopped. Please open a discussion URL or click 'Open in Browser' to start the browser."
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="DOM_SNAPSHOT",
                        outcome="Browser is stopped",
                        error_code="BROWSER_STOPPED",
                        error_details=resp.error or msg,
                    )
                    return False, msg, None

                if "timed out" in err_str:
                    self.active_tab_id = None
                    self.active_target_id = None
                    msg = (
                        "Browser request timed out. The browser session may be unresponsive "
                        "(e.g., after system sleep or a crashed tab). Please click 'Stop Browser' "
                        "and reopen the discussion URL."
                    )
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="DOM_SNAPSHOT",
                        outcome="Browser request timed out",
                        error_code="TIMEOUT",
                        error_details=resp.error or msg,
                    )
                    return False, msg, None

                # Attempt auto-resolving to latest active tab and retry once
                logger.warning("Snapshot attempt failed with error: %s. Resolving live tabs...", resp.error)
                new_target, resolve_err = self.resolve_active_tab(profile_name)
                if resolve_err:
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="DOM_SNAPSHOT",
                        outcome="Failed to resolve active tab for snapshot",
                        error_code="RESOLVE_TAB_FAILED",
                        error_details=resolve_err,
                    )
                    return False, resolve_err, None
                if new_target:
                    resp = self.openclaw_client.get_page_snapshot(profile_name, target_id=new_target)

            if resp.success:
                snapshot_text = sanitize_dom_snapshot(str(resp.data or ""))
                if self.openclaw_client.detect_captcha_in_snapshot(snapshot_text):
                    logger.warning("CAPTCHA indicator detected in page snapshot for profile '%s'.", profile_name)
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="CAPTCHA_DETECTED",
                        operator_action="DOM_SNAPSHOT",
                        outcome="CAPTCHA or login verification screen suspected. Manual handling required.",
                        error_code="CAPTCHA_SUSPECTED",
                        error_details=f"Snapshot length: {len(snapshot_text)} chars",
                    )
                    return True, "⚠️ CAPTCHA or verification screen detected. Please handle manually in the browser.", snapshot_text

                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_SNAPSHOT",
                    operator_action="DOM_SNAPSHOT",
                    outcome=f"Retrieved DOM snapshot ({len(snapshot_text)} characters)",
                    error_details=None,
                )
                return True, "Snapshot retrieved successfully", snapshot_text

            err_msg = resp.error or "Failed to retrieve snapshot"
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="DOM_SNAPSHOT",
                outcome="Failed to retrieve snapshot",
                error_code="SNAPSHOT_FAILED",
                error_details=err_msg,
            )
            return False, err_msg, None
        finally:
            self._browser_lock.release()

    def capture_openclaw_screenshot(
        self,
        profile_name: str = "shoro-test",
        task: Optional[CommentTask | PostTask] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """Capture screenshot from OpenClaw browser.

        Uses direct inspection with auto-recovery for stale or missing tabs.
        Locks against concurrent execution to prevent port/CDP conflicts.
        """
        task_id = task.task_id if task else (self.current_task.task_id if self.current_task else "SESSION")

        if not self.is_profile_allowed(profile_name):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="CAPTURE_SCREENSHOT",
                outcome=f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed.",
                error_code="PROFILE_LOCKED",
                error_details=f"Attempted profile: {profile_name}",
            )
            return False, f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed.", None

        if not self._browser_lock.acquire(blocking=False):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="CAPTURE_SCREENSHOT",
                outcome="Operation rejected: Browser busy",
                error_code="CONCURRENCY_LOCK",
                error_details="Another browser operation is currently in progress",
            )
            return False, "Another browser operation is currently in progress. Please wait a moment for it to complete.", None

        try:
            # Attempt screenshot with current active target (or active tab if not set)
            target = self.active_target_id or self.active_tab_id
            resp = self.openclaw_client.capture_screenshot(profile_name, target_id=target)

            # If direct call failed, inspect error and auto-recover if tab is stale/missing
            if not resp.success:
                err_str = (resp.error or "").lower()
                if any(k in err_str for k in ["not running", "stopped", "closed"]):
                    self.active_tab_id = None
                    self.active_target_id = None
                    msg = "Browser is stopped. Please open a discussion URL or click 'Open in Browser' to start the browser."
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="CAPTURE_SCREENSHOT",
                        outcome="Browser is stopped",
                        error_code="BROWSER_STOPPED",
                        error_details=resp.error or msg,
                    )
                    return False, msg, None

                if "timed out" in err_str:
                    self.active_tab_id = None
                    self.active_target_id = None
                    msg = (
                        "Browser request timed out. The browser session may be unresponsive "
                        "(e.g., after system sleep or a crashed tab). Please click 'Stop Browser' "
                        "and reopen the discussion URL."
                    )
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="CAPTURE_SCREENSHOT",
                        outcome="Browser request timed out",
                        error_code="TIMEOUT",
                        error_details=resp.error or msg,
                    )
                    return False, msg, None

                # Attempt auto-resolving to latest active tab and retry once
                logger.warning("Screenshot attempt failed with error: %s. Resolving live tabs...", resp.error)
                new_target, resolve_err = self.resolve_active_tab(profile_name)
                if resolve_err:
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="CAPTURE_SCREENSHOT",
                        outcome="Failed to resolve active tab for screenshot",
                        error_code="RESOLVE_TAB_FAILED",
                        error_details=resolve_err,
                    )
                    return False, resolve_err, None
                if new_target:
                    resp = self.openclaw_client.capture_screenshot(profile_name, target_id=new_target)

            if resp.success and resp.data:
                path = resp.data.get("screenshot_path", "")
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_SCREENSHOT",
                    operator_action="CAPTURE_SCREENSHOT",
                    outcome="Screenshot captured successfully",
                    error_details=f"Saved to {path}" if path else None,
                )
                return True, "Screenshot captured successfully", path

            err_msg = resp.error or "Failed to capture screenshot"
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="CAPTURE_SCREENSHOT",
                outcome="Failed to capture screenshot",
                error_code="SCREENSHOT_FAILED",
                error_details=err_msg,
            )
            return False, err_msg, None
        finally:
            self._browser_lock.release()

    def stop_openclaw_browser(
        self,
        profile_name: str = "shoro-test",
        task: Optional[CommentTask | PostTask] = None,
    ) -> Tuple[bool, str]:
        """Stop OpenClaw browser instance.

        Clears outdated tab references.
        The persistent user-data directory is intentionally preserved so that
        cookies and session state survive restarts.
        """
        task_id = task.task_id if task else (self.current_task.task_id if self.current_task else "SESSION")

        if not self.is_profile_allowed(profile_name):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="STOP_BROWSER",
                outcome=f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed.",
                error_code="PROFILE_LOCKED",
                error_details=f"Attempted profile: {profile_name}",
            )
            return False, f"Profile '{profile_name}' is locked. Only 'shoro-test' is allowed."

        if not self._browser_lock.acquire(blocking=False):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="STOP_BROWSER",
                outcome="Operation rejected: Browser busy",
                error_code="CONCURRENCY_LOCK",
                error_details="Another browser operation is currently in progress",
            )
            return False, "Another browser operation is currently in progress. Please wait a moment for it to complete."

        try:
            # Clear outdated tab references
            self.active_tab_id = None
            self.active_target_id = None

            resp = self.openclaw_client.stop_browser(profile_name)
            if resp.success:
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_STOP",
                    operator_action="STOP_BROWSER",
                    outcome=f"Stopped OpenClaw browser for profile '{profile_name}' (session data retained)",
                    error_details=None,
                )
                return True, f"Browser profile '{profile_name}' stopped. Session data retained for next restart."
            else:
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="STOP_BROWSER",
                    outcome=f"Failed to stop OpenClaw browser for profile '{profile_name}'",
                    error_code="STOP_FAILED",
                    error_details=resp.error,
                )
                return False, resp.error or f"Failed to stop browser profile '{profile_name}'"
        finally:
            self._browser_lock.release()

    def get_profile_session_status(self, profile_name: str = "shoro-test") -> dict:
        """Return persistence status for the given profile.

        Checks whether the profile's user-data directory exists on disk.
        Returns a dict with keys: profile, data_dir, exists, note.
        Never reads, copies, or exposes credential data.
        """
        if not self.is_profile_allowed(profile_name):
            return {
                "profile": profile_name,
                "data_dir": None,
                "exists": False,
                "note": f"Profile '{profile_name}' is locked. Only 'shoro-test' is inspectable.",
            }
        resp = self.openclaw_client.check_profile_session_exists(profile_name)
        return resp.data or {
            "profile": profile_name,
            "data_dir": None,
            "exists": False,
            "note": resp.error or "Unknown",
        }

    def check_snapshot_for_captcha(self, snapshot_text: str) -> bool:
        """Return True if the snapshot text suggests a CAPTCHA or verification screen."""
        return self.openclaw_client.detect_captcha_in_snapshot(snapshot_text)

    def record_outcome(
        self,
        task: CommentTask | PostTask,
        outcome: str,
        notes: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Record human-confirmed outcome after operator manual action."""
        now_iso = get_current_timestamp(self.settings.display_timezone)
        is_test_mode = self.settings.test_mode or is_force_test_mode_enabled()

        logger.info(
            "Recording human outcome for Task %s: '%s' (Test Mode: %s)",
            task.task_id, outcome, is_test_mode
        )

        if outcome == "Completed - verified":
            if is_test_mode:
                task.status = "Test Mode - review verified"
                task.is_eligible = False
                msg = "Test Mode: Human verified review. No Reddit submission made or marked completed in workbook."
                self.audit_logger.log_event(
                    task_id=task.task_id,
                    event_type="TEST_MODE_VERIFIED",
                    operator_action="CONFIRM_VERIFIED",
                    outcome="Test Mode - review verified (no live completion)",
                    error_details=notes,
                )
            else:
                # Production update
                task.status = "Completed - human confirmed"
                task.is_eligible = False
                task.timestamp = now_iso
                # Update Excel
                success, excel_msg = self.excel_handler.update_task_status(
                    worksheet_name=task.worksheet,
                    excel_row=task.excel_row,
                    new_status="Completed - human confirmed",
                    timestamp_str=now_iso,
                )
                if not success:
                    task.status = "Needs manual verification"
                    msg = f"Excel update failed: {excel_msg}"
                else:
                    msg = f"Task confirmed completed and workbook updated. {excel_msg}"

                self.audit_logger.log_event(
                    task_id=task.task_id,
                    event_type="CONFIRMED_COMPLETED",
                    operator_action="CONFIRM_COMPLETED",
                    outcome="Completed - verified",
                    error_details=notes,
                )
        elif outcome == "Submission outcome uncertain":
            task.status = "Needs manual verification"
            task.is_eligible = False
            msg = "Task flagged for manual verification."
            self.audit_logger.log_event(
                task_id=task.task_id,
                event_type="OUTCOME_UNCERTAIN",
                operator_action="MARK_UNCERTAIN",
                outcome="Needs manual verification",
                error_details=notes,
            )
        else:
            task.status = f"Operator: {outcome}"
            task.is_eligible = False
            msg = f"Recorded outcome: {outcome}"
            self.audit_logger.log_event(
                task_id=task.task_id,
                event_type="OPERATOR_OUTCOME",
                operator_action="RECORD_OUTCOME",
                outcome=outcome,
                error_details=notes,
            )

        # Update task in SQLite tasks table
        if task.fingerprint:
            self.audit_logger.log_task(
                task_id=task.task_id,
                task_fingerprint=task.fingerprint,
                workbook_name=Path(self.settings.workbook_path).name,
                worksheet_name=task.worksheet,
                excel_row_number=task.excel_row,
                task_type=task.task_type,
                account_id=task.account_id,
                destination_url=task.destination_url if task.task_type == "comment" else f"https://www.reddit.com/{task.subreddit}",
                task_status=task.status,
            )

        self.state_manager.current_task_id = None
        self.state_manager.persist_state()

        return True, msg

    def check_reddit_session_status(self, account_id: str) -> RedditSessionStatus:
        """Inspect and distinguish: profile directory exists, browser session running, and verified Reddit login.

        Strict compliance:
        - Never claims user is logged in based solely on directory existence.
        - Never stores passwords, cookies, or session tokens in JSON, Excel, or SQLite.
        - Records verification outcome in SQLite audit trail.
        """
        clean_id = (account_id or "").strip()
        profile = self.account_manager.accounts.get(clean_id)
        if not profile:
            return RedditSessionStatus(
                account_id=account_id,
                profile_name="",
                profile_dir="",
                directory_exists=False,
                browser_running=False,
                reddit_authenticated=False,
                status_label="Account Not Configured",
                details=f"Account profile not configured: '{clean_id}'",
            )

        prof_name = profile.chrome_profile_name
        udir = profile.user_data_directory

        # 1. Directory existence check
        dir_path = Path(udir)
        openclaw_dir = Path(self.openclaw_client.get_profile_data_dir(prof_name) or udir)
        dir_exists = dir_path.exists() or openclaw_dir.exists()
        actual_dir_str = str(dir_path if dir_path.exists() else openclaw_dir)

        # 2. Browser session existence check
        b_status = self.openclaw_client.get_browser_status(prof_name)
        browser_running = bool(b_status.success and b_status.data and b_status.data.get("is_running"))

        # 3. Reddit authentication verified check
        reddit_auth = False
        auth_user = None

        if not browser_running:
            status_label = "Browser Stopped (Login Unverified)"
            details = (
                f"Profile directory exists at '{actual_dir_str}'. Browser is stopped. "
                "Reddit authentication cannot be verified based on directory existence alone."
                if dir_exists
                else f"Profile directory does not exist at '{actual_dir_str}' and browser is stopped."
            )
        else:
            snap_resp = self.openclaw_client.get_page_snapshot(prof_name)
            if snap_resp.success and snap_resp.data:
                is_auth, uname, auth_msg = self.openclaw_client.verify_reddit_auth_in_dom(str(snap_resp.data))
                reddit_auth = is_auth
                auth_user = uname
                status_label = "Authenticated" if is_auth else "Not Logged In"
                details = auth_msg
            else:
                status_label = "Browser Running (Snapshot Failed)"
                details = snap_resp.error or "Failed to inspect page snapshot for authentication verification."

        self.audit_logger.log_event(
            task_id=f"ACC_{account_id}",
            event_type="SESSION_STATUS_CHECK",
            operator_action="CHECK_SESSION",
            outcome=f"{status_label} [dir_exists={dir_exists}, running={browser_running}, auth={reddit_auth}]",
            error_details=details[:500],
        )

        return RedditSessionStatus(
            account_id=account_id,
            profile_name=prof_name,
            profile_dir=actual_dir_str,
            directory_exists=dir_exists,
            browser_running=browser_running,
            reddit_authenticated=reddit_auth,
            authenticated_username=auth_user,
            status_label=status_label,
            details=details,
        )

    def open_browser_for_manual_login(self, account_id: str) -> Tuple[bool, str]:
        """Open the isolated browser profile to Reddit login screen for manual operator authentication."""
        profile, err = self.account_manager.resolve_account(account_id)
        if not profile:
            return False, f"Cannot open browser: {err}"

        # Ensure isolated profile directory exists on disk
        Path(profile.user_data_directory).mkdir(parents=True, exist_ok=True)

        login_url = "https://www.reddit.com/login"
        success, msg, data = self.open_in_openclaw(
            url=login_url,
            profile_name=profile.chrome_profile_name,
        )
        if success:
            return True, f"Opened {profile.chrome_profile_name} for manual Reddit login. Please complete login in the browser window."
        return False, msg

    def prepare_reply_in_reddit(
        self,
        task: CommentTask,
        profile_name: Optional[str] = None,
        auto_submit: bool = False,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Search 'Join the conversation' in thread, activate editor, insert exact reply from Column K, verify text, and optionally submit.

        - Reads destination URL, account, and reply text from Excel task.
        - Deep-searches for 'Join the conversation' and activates Lexical composer.
        - Inserts formatted multi-paragraph reply.
        - Verifies exact character match with Column K.
        - If auto_submit is True: clicks 'Comment' button and confirms submission.
        - If auto_submit is False: stops immediately before submission for manual review.
        """
        import json
        import time

        if not task:
            return False, "No task provided.", {"status": "NO_TASK"}

        task_id = task.task_id or f"comment_{task.worksheet}_{task.excel_row}"

        if not task.destination_url:
            return False, "Task has no destination URL.", {"status": "INVALID_URL"}

        is_valid_url, url_err = TaskValidator.validate_url(task.destination_url)
        if not is_valid_url:
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="PREPARE_REPLY",
                outcome="URL rejected by security policy",
                error_code="INVALID_URL",
                error_details=url_err,
            )
            return False, f"Invalid destination URL: {url_err}", {"status": "INVALID_URL"}

        reply_text = task.reply_text
        if not reply_text or not str(reply_text).strip():
            return False, "Task has no prepared reply text in 'Shoro AI reply' column.", {"status": "EMPTY_REPLY"}

        prof_name = profile_name or task.chrome_profile_name or "shoro-test"
        if not self.is_profile_allowed(prof_name):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="PREPARE_REPLY",
                outcome=f"Profile '{prof_name}' is locked. Only 'shoro-test' is allowed.",
                error_code="PROFILE_LOCKED",
                error_details=f"Attempted profile: {prof_name}",
            )
            return False, f"Profile '{prof_name}' is locked. Only 'shoro-test' is allowed.", {"status": "PROFILE_LOCKED"}

        if not self._browser_lock.acquire(blocking=False):
            self.audit_logger.log_event(
                task_id=task_id,
                event_type="OPENCLAW_ERROR",
                operator_action="PREPARE_REPLY",
                outcome="Operation rejected: Browser busy",
                error_code="CONCURRENCY_LOCK",
            )
            return False, "Another browser operation is currently in progress. Please wait a moment.", {"status": "BROWSER_BUSY"}

        try:
            # 1. Open discussion in browser profile
            nav_resp = self.openclaw_client.open_url(url=task.destination_url, profile_name=prof_name)
            if not nav_resp.success:
                err_msg = nav_resp.error or "Failed to open discussion URL in OpenClaw browser"
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome=f"Failed to navigate: {err_msg}",
                    error_code="NAVIGATION_FAILED",
                )
                return False, err_msg, {"status": "NAVIGATION_FAILED"}

            if isinstance(nav_resp.data, dict):
                self.active_tab_id = nav_resp.data.get("tab_id")
                self.active_target_id = nav_resp.data.get("target_id")
            # Prefer actual CDP target ID if valid hex, else use None for active tab
            target_cdp_id = self.active_target_id if (self.active_target_id and len(str(self.active_target_id).strip()) > 10) else None
            tab_id = target_cdp_id

            # 2. Human touch: Randomized reading & page settling delay (2 to 15 seconds)
            reading_delay = random.uniform(3.0, 7.5)
            logger.info("Human touch: Pausing %.2fs to simulate human reading the thread...", reading_delay)
            time.sleep(reading_delay)

            # 3. Retrieve DOM page snapshot with auto-recovery
            snap_resp = self.openclaw_client.get_page_snapshot(prof_name, target_id=tab_id)
            if not snap_resp.success:
                # Retry targeting active tab directly without target_id
                logger.warning("Snapshot with target_id '%s' failed (%s). Retrying active tab...", tab_id, snap_resp.error)
                snap_resp = self.openclaw_client.get_page_snapshot(prof_name)

            if not snap_resp.success:
                # If snapshot timed out on heavy dynamic Reddit page, recover text content via evaluate_script
                logger.warning("Snapshot timed out. Recovering page content via evaluate_script...")
                eval_snap = self.openclaw_client.evaluate_script(
                    prof_name,
                    "() => document.body ? (document.body.innerText || document.body.textContent || '') : ''",
                    target_id=tab_id,
                )
                if eval_snap.success and eval_snap.data:
                    snapshot_text = str(eval_snap.data)
                else:
                    return False, f"Failed to retrieve page snapshot: {snap_resp.error}", {"status": "SNAPSHOT_FAILED"}
            else:
                snapshot_text = str(snap_resp.data or "")

            # 4. Check for CAPTCHA
            if self.check_snapshot_for_captcha(snapshot_text):
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome="CAPTCHA or verification screen detected. Manual handling required.",
                    error_code="CAPTCHA_SUSPECTED",
                )
                return False, "⚠️ CAPTCHA or verification screen detected. Please handle manually in the browser.", {"status": "CAPTCHA_DETECTED"}

            # 5. Check Discussion State (Locked, Archived, Deleted, Unavailable)
            is_actionable, state_reason = self.openclaw_client.inspect_reddit_discussion_state(snapshot_text)
            if not is_actionable:
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome=state_reason,
                    error_code="NON_ACTIONABLE_DISCUSSION",
                )
                return False, state_reason, {"status": "NON_ACTIONABLE", "reason": state_reason}

            # 6. Verify Active Account and Reddit Authentication
            is_test_url = "example.com" in (task.destination_url or "").lower()
            active_user = None
            if not is_test_url:
                is_auth, active_user, auth_msg = self.openclaw_client.verify_reddit_auth_in_dom(snapshot_text)
                # If unverified or page still hydrating skeleton (< 250 chars), wait and retry snapshot
                for _ in range(2):
                    if is_auth and len(snapshot_text) >= 250:
                        break
                    time.sleep(2)
                    snap_retry = self.openclaw_client.get_page_snapshot(prof_name, target_id=tab_id)
                    if snap_retry.success and snap_retry.data:
                        snapshot_text = str(snap_retry.data)
                        is_auth, active_user, auth_msg = self.openclaw_client.verify_reddit_auth_in_dom(snapshot_text)

                if not is_auth:
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="PREPARE_REPLY",
                        outcome="Reddit authentication required. Please log in manually.",
                        error_code="NOT_LOGGED_IN",
                    )
                    return False, "Reddit authentication required. Please log in to Reddit manually.", {"status": "NOT_LOGGED_IN"}

            if task.account_id and active_user:
                expected_user = None
                prof = self.account_manager.accounts.get(task.account_id)
                if prof and hasattr(prof, "reddit_username") and prof.reddit_username:
                    expected_user = prof.reddit_username.lower().strip()
                elif "@" in task.account_id:
                    expected_user = task.account_id.split("@")[0].lower().strip()
                else:
                    expected_user = task.account_id.replace("u/", "").lower().strip()

                if expected_user and active_user.lower().strip() != expected_user and expected_user not in active_user.lower():
                    err_acc = f"Wrong account is active in browser: logged in as '{active_user}', expected '{task.account_id}'. Stopping."
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="OPENCLAW_ERROR",
                        operator_action="PREPARE_REPLY",
                        outcome=err_acc,
                        error_code="WRONG_ACCOUNT",
                    )
                    return False, err_acc, {"status": "WRONG_ACCOUNT", "active_user": active_user, "expected_user": task.account_id}

            # 7. Locate comment-entry area with 'Join the conversation' or <comment-body-header>
            editor_ref = self.openclaw_client.find_editor_ref_in_snapshot(snapshot_text)
            has_join_conv = (
                "join the conversation" in snapshot_text.lower()
                or "add a comment" in snapshot_text.lower()
                or "shreddit-comment-composer" in snapshot_text.lower()
                or "comment-body-header" in snapshot_text.lower()
                or "faceplate-textarea-input" in snapshot_text.lower()
            )

            # If editor ref exists in snapshot, click it to activate
            if editor_ref:
                self.openclaw_client._click_ref(prof_name, editor_ref, target_id=tab_id)

            # 8. Deep-search entire thread DOM (including open Shadow DOMs), activate, paste into content box, and STOP before submission
            reply_json = json.dumps(task.reply_text)

            js_script = (
                "async () => {\n"
                "  const expected = " + reply_json + ";\n"
                "  const wait = ms => new Promise(r => setTimeout(r, ms));\n"
                "\n"
                "  // 1. Human touch: Step-by-step natural screen scrolling\n"
                "  const scrollSteps = Math.floor(Math.random() * 2) + 2;\n"
                "  for (let s = 0; s < scrollSteps; s++) {\n"
                "    const scrollDelta = Math.floor(Math.random() * 160) + 140;\n"
                "    window.scrollBy({ top: scrollDelta, behavior: 'smooth' });\n"
                "    await wait(Math.floor(Math.random() * 200) + 180);\n"
                "  }\n"
                "\n"
                "  // 2. Locate existing editor or trigger\n"
                "  let editor = document.querySelector('div[slot=\"rte\"][contenteditable=\"true\"]') ||\n"
                "               document.querySelector('shreddit-composer div[contenteditable=\"true\"]') ||\n"
                "               document.querySelector('div[contenteditable=\"true\"]:not([class*=\"recaptcha\"])');\n"
                "\n"
                "  if (!editor) {\n"
                "    const trigger = document.querySelector('faceplate-textarea-input[data-testid=\"trigger-button\"], [placeholder*=\"conversation\" i], [aria-label*=\"Join the conversation\" i], faceplate-textarea-input');\n"
                "    if (trigger) {\n"
                "      try {\n"
                "        trigger.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                "        await wait(250);\n"
                "        trigger.click();\n"
                "        const inner = trigger.querySelector('button, textarea, [placeholder*=\"conversation\" i]');\n"
                "        if (inner) inner.click();\n"
                "      } catch (e) {}\n"
                "    }\n"
                "\n"
                "    // Poll for up to 3 seconds for editor to mount\n"
                "    for (let i = 0; i < 30; i++) {\n"
                "      await wait(100);\n"
                "      editor = document.querySelector('div[slot=\"rte\"][contenteditable=\"true\"]') ||\n"
                "               document.querySelector('shreddit-composer div[contenteditable=\"true\"]') ||\n"
                "               document.querySelector('div[contenteditable=\"true\"]:not([class*=\"recaptcha\"])');\n"
                "      if (editor) break;\n"
                "    }\n"
                "  }\n"
                "\n"
                "  if (!editor) {\n"
                "    const composers = document.querySelectorAll('shreddit-comment-composer, comment-body-header, faceplate-textarea-input, comment-composer-host');\n"
                "    for (const c of composers) {\n"
                "      const root = c.shadowRoot || c;\n"
                "      editor = root.querySelector('div[contenteditable=\"true\"], textarea, [name=\"content\"]');\n"
                "      if (editor) break;\n"
                "    }\n"
                "  }\n"
                "\n"
                "  if (!editor) {\n"
                "    return JSON.stringify({ success: false, error: 'Comment editor could not be identified confidently.' });\n"
                "  }\n"
                "\n"
                "  // 3. Human touch: Focus & manual progressive typing simulation\n"
                "  try {\n"
                "    editor.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                "    editor.focus();\n"
                "  } catch (e) {}\n"
                "  await wait(200);\n"
                "\n"
                "  const words = expected.split(' ');\n"
                "  let progressiveText = '';\n"
                "  const batchSize = Math.floor(Math.random() * 3) + 4;\n"
                "  for (let i = 0; i < words.length; i++) {\n"
                "    progressiveText += (i > 0 ? ' ' : '') + words[i];\n"
                "    if (i % batchSize === 0 || i === words.length - 1 || words[i].includes('\\n')) {\n"
                "      if (editor.__lexicalEditor && typeof editor.__lexicalEditor.parseEditorState === 'function') {\n"
                "        const paras = progressiveText.split('\\n\\n').map(p => ({\n"
                "          children: [{\n"
                "            detail: 0,\n"
                "            format: 0,\n"
                "            mode: 'normal',\n"
                "            style: '',\n"
                "            text: p,\n"
                "            type: 'text',\n"
                "            version: 1\n"
                "          }],\n"
                "          direction: 'ltr',\n"
                "          format: '',\n"
                "          indent: 0,\n"
                "          type: 'paragraph',\n"
                "          version: 1\n"
                "        }));\n"
                "        const stateObj = {\n"
                "          root: {\n"
                "            children: paras,\n"
                "            direction: 'ltr',\n"
                "            format: '',\n"
                "            indent: 0,\n"
                "            type: 'root',\n"
                "            version: 1\n"
                "          }\n"
                "        };\n"
                "        editor.__lexicalEditor.setEditorState(editor.__lexicalEditor.parseEditorState(JSON.stringify(stateObj)));\n"
                "      } else {\n"
                "        editor.innerText = progressiveText;\n"
                "      }\n"
                "      editor.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: words[i] }));\n"
                "      await wait(Math.floor(Math.random() * 20) + 15);\n"
                "    }\n"
                "  }\n"
                "\n"
                "  // Ensure exact final state matching Column K character-for-character\n"
                "  if (editor.__lexicalEditor && typeof editor.__lexicalEditor.parseEditorState === 'function') {\n"
                "    const finalParas = expected.split('\\n\\n').map(p => ({\n"
                "      children: [{\n"
                "        detail: 0,\n"
                "        format: 0,\n"
                "        mode: 'normal',\n"
                "        style: '',\n"
                "        text: p,\n"
                "        type: 'text',\n"
                "        version: 1\n"
                "      }],\n"
                "      direction: 'ltr',\n"
                "      format: '',\n"
                "      indent: 0,\n"
                "      type: 'paragraph',\n"
                "      version: 1\n"
                "    }));\n"
                "    const finalState = {\n"
                "      root: {\n"
                "        children: finalParas,\n"
                "        direction: 'ltr',\n"
                "        format: '',\n"
                "        indent: 0,\n"
                "        type: 'root',\n"
                "        version: 1\n"
                "      }\n"
                "    };\n"
                "    editor.__lexicalEditor.setEditorState(editor.__lexicalEditor.parseEditorState(JSON.stringify(finalState)));\n"
                "  } else {\n"
                "    editor.innerHTML = '<p class=\"first:mt-0 last:mb-0\"><span data-lexical-text=\"true\">' + expected + '</span></p>';\n"
                "    editor.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: expected }));\n"
                "    editor.dispatchEvent(new Event('change', { bubbles: true }));\n"
                "  }\n"
                "  await wait(200);\n"
                "\n"
                "  // 4. Read back and verify\n"
                "  const readBack = ((editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT')\n"
                "    ? editor.value\n"
                "    : (editor.innerText || editor.textContent || '')).trim();\n"
                "\n"
                "  // 5. If auto_submit is enabled, human proofreading pause (1.5 to 2.5 seconds) before submit\n"
                "  let submitted = false;\n"
                "  if (" + ("true" if auto_submit else "false") + ") {\n"
                "    await wait(Math.floor(Math.random() * 1000) + 1500);\n"
                "    const composer = document.querySelector('shreddit-composer, shreddit-comment-composer');\n"
                "    let submitBtn = null;\n"
                "    if (composer) {\n"
                "      submitBtn = composer.querySelector('button[slot=\"submit-button\"], button[type=\"submit\"]') ||\n"
                "                  Array.from(composer.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'comment');\n"
                "    }\n"
                "    if (!submitBtn) {\n"
                "      submitBtn = Array.from(document.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'comment');\n"
                "    }\n"
                "    if (submitBtn && !submitBtn.disabled) {\n"
                "      submitBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                "      await wait(300);\n"
                "      submitBtn.click();\n"
                "      await wait(1500);\n"
                "      submitted = true;\n"
                "    }\n"
                "  }\n"
                "\n"
                "  return JSON.stringify({\n"
                "    success: true,\n"
                "    verifiedText: readBack,\n"
                "    submitted: submitted,\n"
                "  });\n"
                "}"
            )
            eval_resp = self.openclaw_client.evaluate_script(prof_name, js_script, target_id=tab_id, timeout=120)
            eval_data = eval_resp.data
            while isinstance(eval_data, str):
                try:
                    eval_data = json.loads(eval_data)
                except Exception:
                    break

            # If not found immediately, retry evaluate_script if neither editor_ref nor has_join_conv was in snapshot
            if not editor_ref and not has_join_conv and (not eval_resp.success or not isinstance(eval_data, dict) or not eval_data.get("success")):
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome="Comment editor not found in DOM snapshot.",
                    error_code="EDITOR_NOT_FOUND",
                )
                return False, "Comment editor not found.", {"status": "EDITOR_NOT_FOUND"}

            # If editor was not found on first attempt, retry up to 2 times with a 1.5s delay to allow dynamic web components to finish hydrating
            if not eval_resp.success or not isinstance(eval_data, dict) or not eval_data.get("success"):
                for _ in range(2):
                    time.sleep(1.5)
                    eval_resp = self.openclaw_client.evaluate_script(prof_name, js_script, target_id=tab_id, timeout=120)
                    eval_data = eval_resp.data
                    while isinstance(eval_data, str):
                        try:
                            eval_data = json.loads(eval_data)
                        except Exception:
                            break
                    if eval_resp.success and isinstance(eval_data, dict) and eval_data.get("success"):
                        break

            if not eval_resp.success or not isinstance(eval_data, dict) or not eval_data.get("success"):
                is_auth_err = isinstance(eval_data, dict) and eval_data.get("isAuthError")
                err_reason = eval_data.get("error") if isinstance(eval_data, dict) else (eval_resp.error or "Comment editor could not be identified confidently.")
                err_code = "NOT_LOGGED_IN" if is_auth_err else "INSERTION_FAILED"
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome=f"Editor activation/insertion failed: {err_reason}",
                    error_code=err_code,
                )
                return False, err_reason, {"status": err_code, "error": err_reason}

            verified_text = str(eval_data.get("verifiedText") or "")
            # 9. Verify that editor contains exact expected text
            norm_verified = re.sub(r"\s+", " ", verified_text).strip()
            norm_expected = re.sub(r"\s+", " ", task.reply_text).strip()
            if norm_verified != norm_expected and verified_text.strip() != task.reply_text.strip():
                err_mismatch = f"Reply text does not match Excel value: expected '{task.reply_text[:60]}...', found '{verified_text[:60]}...'"
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome=err_mismatch,
                    error_code="TEXT_MISMATCH",
                )
                return False, err_mismatch, {"status": "TEXT_MISMATCH", "expected": task.reply_text, "found": verified_text}

            # 10. Verification successful - Handle submission status
            was_submitted = bool(eval_data.get("submitted", False))
            excel_update_msg = None
            if was_submitted:
                now_iso = get_current_timestamp(self.settings.display_timezone)
                task.status = "Completed - automated submission"
                task.is_eligible = False
                task.timestamp = now_iso
                outcome_text = "Reply submitted successfully to Reddit and status updated in Excel"
                event_action = "SUBMIT_REPLY"
                event_type = "AUTOMATED_SUBMISSION"
                status_code = "SUBMITTED"

                # Update Excel status immediately
                excel_ok, excel_msg = self.excel_handler.update_task_status(
                    worksheet_name=task.worksheet,
                    excel_row=task.excel_row,
                    new_status="Completed - automated submission",
                    timestamp_str=now_iso,
                )
                excel_update_msg = excel_msg
                if not excel_ok:
                    logger.warning("Notice on Excel update for row %s: %s", task.excel_row, excel_msg)
                else:
                    logger.info("Excel updated successfully for row %s: %s", task.excel_row, excel_msg)
            else:
                task.status = "Reply inserted - Ready for manual submission"
                outcome_text = "Reply inserted - Ready for manual submission"
                event_action = "INSERT_REPLY"
                event_type = "PREPARE_REPLY"
                status_code = "READY_FOR_MANUAL_SUBMISSION"

            self.audit_logger.log_event(
                task_id=task_id,
                event_type=event_type,
                operator_action=event_action,
                outcome=outcome_text,
                error_details=f"Verified reply text: {len(verified_text)} chars from Column K. Submitted: {was_submitted}. Excel: {excel_update_msg}",
            )
            return True, outcome_text, {
                "status": status_code,
                "task_id": task_id,
                "excel_row": task.excel_row,
                "worksheet": task.worksheet,
                "account_id": task.account_id,
                "destination_url": task.destination_url,
                "verified_text": verified_text,
                "submitted": was_submitted,
                "excel_update": excel_update_msg,
            }
        finally:
            self._browser_lock.release()

    def submit_reply_to_reddit(
        self,
        task: CommentTask,
        profile_name: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Fetch URL, account, and exact reply from Excel task, open discussion, activate editor, insert reply, and automatically submit to Reddit."""
        return self.prepare_reply_in_reddit(task, profile_name=profile_name, auto_submit=True)

    def prepare_post_in_reddit(
        self,
        task: PostTask,
        profile_name: Optional[str] = None,
        auto_submit: bool = False,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Fetch Subreddit, Title, and Body from Excel task, open submission page, type title and body, and optionally submit.

        Account-independent: Works with any active authenticated session in the target profile.
        Includes human-touch anti-bot delays, screen scrolling, natural typing simulation, and Excel status sync.
        """
        task_id = task.task_id if task else "SESSION"
        prof_name = profile_name or "shoro-test"
        dest_url = task.destination_url

        if not self.is_profile_allowed(prof_name):
            prof_name = "shoro-test"

        if not self._browser_lock.acquire(blocking=False):
            return False, "Another browser operation is currently in progress. Please wait a moment.", {"status": "CONCURRENCY_LOCK"}

        try:
            # 1. Open subreddit submission page in browser
            nav_resp = self.openclaw_client.open_url(url=dest_url, profile_name=prof_name)
            if not nav_resp.success:
                err_msg = nav_resp.error or "Failed to open submit URL in OpenClaw browser"
                return False, f"Navigation failed: {err_msg}", {"status": "NAVIGATION_FAILED", "error": err_msg}

            if isinstance(nav_resp.data, dict):
                self.active_target_id = nav_resp.data.get("target_id") or nav_resp.data.get("tab_id")
                self.active_tab_id = nav_resp.data.get("tab_id") or self.active_target_id
            tab_id = self.active_tab_id or self.active_target_id

            # 2. Capture DOM snapshot to inspect state
            snap_resp = self.openclaw_client.get_page_snapshot(prof_name)
            if not snap_resp.success or not snap_resp.data:
                return False, f"Could not inspect submit page: {snap_resp.error}", {"status": "SNAPSHOT_FAILED"}

            snapshot_text = str(snap_resp.data)

            # 3. Check for CAPTCHA / bot block
            if self.openclaw_client.detect_captcha_in_snapshot(snapshot_text):
                return False, "CAPTCHA detected on page. Manual intervention required.", {"status": "CAPTCHA_DETECTED"}

            # 4. Verify Reddit authentication (account-independent: verify active session exists)
            is_auth, active_uname, auth_expl = self.openclaw_client.verify_reddit_auth_in_dom(snapshot_text)
            if not is_auth:
                return False, f"Reddit login required: {auth_expl}", {"status": "NOT_LOGGED_IN", "error": auth_expl}

            # 5. Anti-bot human delay: Pre-reading community rules
            time.sleep(random.uniform(2.5, 4.5))

            # 6. Build automated injection script for Title, Body, and Post Button
            title_json = json.dumps(task.title)
            body_json = json.dumps(task.body)

            js_script = (
                "async () => {\n"
                "  const expectedTitle = " + title_json + ";\n"
                "  const expectedBody = " + body_json + ";\n"
                "  const wait = ms => new Promise(r => setTimeout(r, ms));\n"
                "\n"
                "  // 1. Natural page scroll simulation\n"
                "  const scrollSteps = Math.floor(Math.random() * 2) + 2;\n"
                "  for (let s = 0; s < scrollSteps; s++) {\n"
                "    const scrollDelta = Math.floor(Math.random() * 120) + 100;\n"
                "    window.scrollBy({ top: scrollDelta, behavior: 'smooth' });\n"
                "    await wait(Math.floor(Math.random() * 150) + 150);\n"
                "  }\n"
                "\n"
                "  // 2. Locate and fill Title field\n"
                "  let titleEl = null;\n"
                "  const titleHost = document.querySelector('post-composer-title');\n"
                "  if (titleHost) {\n"
                "    const root = titleHost.shadowRoot || titleHost;\n"
                "    titleEl = root.querySelector('textarea, input');\n"
                "  }\n"
                "  if (!titleEl) {\n"
                "    titleEl = document.querySelector('textarea[name=\"title\"], [placeholder*=\"Title\" i], input[name=\"title\"]');\n"
                "  }\n"
                "  if (!titleEl) {\n"
                "    return JSON.stringify({ success: false, error: 'Post title field not found on page.' });\n"
                "  }\n"
                "\n"
                "  titleEl.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                "  titleEl.focus();\n"
                "  await wait(200);\n"
                "\n"
                "  // Human typing simulation into Title\n"
                "  const titleWords = expectedTitle.split(' ');\n"
                "  let curTitle = '';\n"
                "  for (let i = 0; i < titleWords.length; i++) {\n"
                "    curTitle += (i > 0 ? ' ' : '') + titleWords[i];\n"
                "    titleEl.value = curTitle;\n"
                "    titleEl.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: titleWords[i] }));\n"
                "    await wait(Math.floor(Math.random() * 20) + 15);\n"
                "  }\n"
                "  titleEl.value = expectedTitle;\n"
                "  titleEl.dispatchEvent(new Event('input', { bubbles: true }));\n"
                "  titleEl.dispatchEvent(new Event('change', { bubbles: true }));\n"
                "  await wait(300);\n"
                "\n"
                "  // 3. Locate and fill Post Body editor\n"
                "  let bodyEl = document.querySelector('div[slot=\"rte\"][contenteditable=\"true\"]') ||\n"
                "               document.querySelector('shreddit-composer[name=\"body\"] div[contenteditable=\"true\"]') ||\n"
                "               document.querySelector('div[contenteditable=\"true\"]:not([class*=\"recaptcha\"])');\n"
                "\n"
                "  if (bodyEl) {\n"
                "    bodyEl.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                "    bodyEl.focus();\n"
                "    await wait(200);\n"
                "\n"
                "    // Natural typing simulation into Body\n"
                "    const bodyWords = expectedBody.split(' ');\n"
                "    let curBody = '';\n"
                "    const batchSize = Math.floor(Math.random() * 3) + 4;\n"
                "    for (let i = 0; i < bodyWords.length; i++) {\n"
                "      curBody += (i > 0 ? ' ' : '') + bodyWords[i];\n"
                "      if (i % batchSize === 0 || i === bodyWords.length - 1 || bodyWords[i].includes('\\n')) {\n"
                "        if (bodyEl.__lexicalEditor && typeof bodyEl.__lexicalEditor.parseEditorState === 'function') {\n"
                "          const paras = curBody.split('\\n\\n').map(p => ({\n"
                "            children: [{\n"
                "              detail: 0,\n"
                "              format: 0,\n"
                "              mode: 'normal',\n"
                "              style: '',\n"
                "              text: p,\n"
                "              type: 'text',\n"
                "              version: 1\n"
                "            }],\n"
                "            direction: 'ltr',\n"
                "            format: '',\n"
                "            indent: 0,\n"
                "            type: 'paragraph',\n"
                "            version: 1\n"
                "          }));\n"
                "          const stateObj = { root: { children: paras, direction: 'ltr', format: '', indent: 0, type: 'root', version: 1 } };\n"
                "          bodyEl.__lexicalEditor.setEditorState(bodyEl.__lexicalEditor.parseEditorState(JSON.stringify(stateObj)));\n"
                "        } else {\n"
                "          bodyEl.innerText = curBody;\n"
                "        }\n"
                "        bodyEl.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: bodyWords[i] }));\n"
                "        await wait(Math.floor(Math.random() * 20) + 15);\n"
                "      }\n"
                "    }\n"
                "\n"
                "    // Ensure exact final state matching Column K/Post Body\n"
                "    if (bodyEl.__lexicalEditor && typeof bodyEl.__lexicalEditor.parseEditorState === 'function') {\n"
                "      const finalParas = expectedBody.split('\\n\\n').map(p => ({\n"
                "        children: [{\n"
                "          detail: 0,\n"
                "          format: 0,\n"
                "          mode: 'normal',\n"
                "          style: '',\n"
                "          text: p,\n"
                "          type: 'text',\n"
                "          version: 1\n"
                "        }],\n"
                "        direction: 'ltr',\n"
                "        format: '',\n"
                "        indent: 0,\n"
                "        type: 'paragraph',\n"
                "        version: 1\n"
                "      }));\n"
                "      const finalState = { root: { children: finalParas, direction: 'ltr', format: '', indent: 0, type: 'root', version: 1 } };\n"
                "      bodyEl.__lexicalEditor.setEditorState(bodyEl.__lexicalEditor.parseEditorState(JSON.stringify(finalState)));\n"
                "    } else {\n"
                "      bodyEl.innerHTML = '<p>' + expectedBody.replace(/\\n/g, '<br>') + '</p>';\n"
                "      bodyEl.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true }));\n"
                "      bodyEl.dispatchEvent(new Event('change', { bubbles: true }));\n"
                "    }\n"
                "  }\n"
                "  await wait(300);\n"
                "\n"
                "  // 4. Handle required flair if present and necessary\n"
                "  const flairBtn = Array.from(document.querySelectorAll('button')).find(b => (b.innerText || '').includes('Add flair'));\n"
                "  if (flairBtn && (flairBtn.innerText || '').includes('*')) {\n"
                "    try {\n"
                "      flairBtn.click();\n"
                "      await wait(500);\n"
                "      const firstPill = document.querySelector('r-post-flair-modal [role=\"radio\"], r-post-flair-modal li, [role=\"dialog\"] [role=\"radio\"]');\n"
                "      if (firstPill) {\n"
                "        firstPill.click();\n"
                "        await wait(300);\n"
                "      }\n"
                "      const applyBtn = Array.from(document.querySelectorAll('button')).find(b => ['apply', 'done', 'save'].includes((b.innerText || '').trim().toLowerCase()));\n"
                "      if (applyBtn) {\n"
                "        applyBtn.click();\n"
                "        await wait(400);\n"
                "      }\n"
                "    } catch (e) {}\n"
                "  }\n"
                "\n"
                "  // 5. Read back verified title and body\n"
                "  const readTitle = (titleEl.value || titleEl.innerText || '').trim();\n"
                "  const readBody = bodyEl ? (bodyEl.innerText || bodyEl.textContent || '').trim() : '';\n"
                "\n"
                "  // 6. If auto_submit is enabled, human proofreading pause before clicking Post\n"
                "  let submitted = false;\n"
                "  let publishedUrl = window.location.href;\n"
                "  if (" + ("true" if auto_submit else "false") + ") {\n"
                "    await wait(Math.floor(Math.random() * 1000) + 1500);\n"
                "    const postBtn = Array.from(document.querySelectorAll('button')).find(b => {\n"
                "      const txt = (b.innerText || '').trim().toLowerCase();\n"
                "      return txt === 'post' || b.getAttribute('slot') === 'submit-button';\n"
                "    });\n"
                "    if (postBtn && !postBtn.disabled) {\n"
                "      postBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                "      await wait(300);\n"
                "      postBtn.click();\n"
                "      await wait(3500);\n"
                "      submitted = true;\n"
                "      publishedUrl = window.location.href;\n"
                "    }\n"
                "  }\n"
                "\n"
                "  return JSON.stringify({\n"
                "    success: true,\n"
                "    verifiedTitle: readTitle,\n"
                "    verifiedBody: readBody,\n"
                "    submitted: submitted,\n"
                "    publishedUrl: publishedUrl,\n"
                "  });\n"
                "}"
            )

            eval_resp = self.openclaw_client.evaluate_script(prof_name, js_script, target_id=tab_id, timeout=120)
            eval_data = eval_resp.data
            while isinstance(eval_data, str):
                try:
                    eval_data = json.loads(eval_data)
                except Exception:
                    break

            if not eval_resp.success or not isinstance(eval_data, dict) or not eval_data.get("success"):
                err_reason = eval_data.get("error") if isinstance(eval_data, dict) else (eval_resp.error or "Post preparation failed.")
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_POST",
                    outcome=f"Post preparation failed: {err_reason}",
                    error_code="POST_FAILED",
                )
                return False, err_reason, {"status": "POST_FAILED", "error": err_reason}

            verified_title = str(eval_data.get("verifiedTitle") or "")
            verified_body = str(eval_data.get("verifiedBody") or "")
            was_submitted = bool(eval_data.get("submitted", False))
            published_url = str(eval_data.get("publishedUrl") or dest_url)

            excel_update_msg = None
            if was_submitted:
                now_iso = get_current_timestamp(self.settings.display_timezone)
                task.status = "Completed - automated submission"
                task.is_eligible = False
                task.timestamp = now_iso
                outcome_text = "Post published successfully to Reddit and status updated in Excel"
                event_action = "SUBMIT_POST"
                event_type = "AUTOMATED_POST_SUBMISSION"
                status_code = "POST_SUBMITTED"

                # Update Excel status
                excel_ok, excel_msg = self.excel_handler.update_task_status(
                    worksheet_name=task.worksheet,
                    excel_row=task.excel_row,
                    new_status="Completed - automated submission",
                    timestamp_str=now_iso,
                )
                excel_update_msg = excel_msg
            else:
                task.status = "Post drafted - Ready for manual submission"
                outcome_text = "Post drafted - Ready for manual submission"
                event_action = "DRAFT_POST"
                event_type = "PREPARE_POST"
                status_code = "READY_FOR_MANUAL_SUBMISSION"

            self.audit_logger.log_event(
                task_id=task_id,
                event_type=event_type,
                operator_action=event_action,
                outcome=outcome_text,
                error_details=f"Title: '{verified_title[:40]}'. Submitted: {was_submitted}. Published URL: {published_url}",
            )
            return True, outcome_text, {
                "status": status_code,
                "task_id": task_id,
                "excel_row": task.excel_row,
                "worksheet": task.worksheet,
                "subreddit": task.subreddit,
                "title": verified_title,
                "submitted": was_submitted,
                "published_url": published_url,
                "excel_update": excel_update_msg,
            }
        finally:
            self._browser_lock.release()

    def submit_post_to_reddit(
        self,
        task: PostTask,
        profile_name: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Fetch Subreddit, Title, and Body from Excel task, open submission page, type title and body, and automatically submit to Reddit."""
        return self.prepare_post_in_reddit(task, profile_name=profile_name, auto_submit=True)


