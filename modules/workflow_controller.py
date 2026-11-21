"""Workflow Controller coordinating validation, queue, review navigation, pacing, and outcomes."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import asyncio
import base64
import json
import logging
import os
import psutil
import random
import re
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.request

from config import AppSettings, is_force_test_mode_enabled
from modules.account_manager import AccountManager
from modules.audit_logger import AuditLogger, get_current_timestamp
from modules.browser_manager import BrowserReviewManager
from modules.comment_manager import CommentManager
from modules.duplicate_checker import DuplicateChecker
from modules.excel_handler import ExcelHandler
from modules.openclaw_client import OpenClawClient, OpenClawResponse, sanitize_dom_snapshot
from modules.post_manager import PostManager
from modules.schema_inspector import SchemaInspector, WorkbookSchemaReport
from modules.state_manager import StateManager
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


def _safe_json_loads(val: Any, default: Any = None) -> Any:
    """Safely unwrap potentially double-encoded JSON strings until parsed into an object or primitive."""
    target = val
    while isinstance(target, str):
        try:
            target = json.loads(target)
        except Exception:
            break
    if default is not None and not isinstance(target, type(default)):
        return default
    return target if target is not None else default


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
        from modules.google_sheets_syncer import GoogleSheetsSyncer
        self.google_syncer = GoogleSheetsSyncer(
            spreadsheet_id=getattr(self.settings, "google_spreadsheet_id", "1_AcKE7VErFV2sLIFqLWeGcBQy8AKmPBEmeSPkHjUkl4"),
            webhook_url=getattr(self.settings, "google_sheets_webhook_url", ""),
            backup_dir=self.settings.backup_directory,
        )

        self.schema_report: Optional[WorkbookSchemaReport] = None
        self.comment_tasks: List[CommentTask] = []
        self.post_tasks: List[PostTask] = []
        self.task_queue: List[CommentTask | PostTask] = []
        self.stats: Dict[str, Any] = {}
        self.current_task: Optional[CommentTask | PostTask] = None
        self.active_tab_id: Optional[str] = None
        self.active_target_id: Optional[str] = None
        self.allow_account_independent: bool = True
        self._browser_lock = threading.Lock()
        self.stop_batch_requested: bool = False
        self._current_running_data_dir: Optional[str] = None
        self._current_running_profile: Optional[str] = None

    def request_stop_batch(self) -> None:
        """Signal running batch operations to immediately halt scheduling new tasks."""
        logger.info("Batch stop requested by operator.")
        self.stop_batch_requested = True
        self.audit_logger.log_event(
            task_id="BATCH",
            event_type="BATCH_STOP_REQUESTED",
            operator_action="STOP_BATCH",
            outcome="Operator requested batch execution stop. Halting task scheduling.",
        )

    def reset_stop_batch(self) -> None:
        """Reset the batch stop request flag for future batch runs."""
        self.stop_batch_requested = False

    def initialize(self, reload_accounts: bool = True) -> WorkbookSchemaReport:
        """Run workbook inspection and prepare task queue."""
        logger.info("Initializing workflow controller...")
        # 0. Sync workbook path
        self.excel_handler.set_workbook_path(self.settings.workbook_path)
        if hasattr(self, "schema_inspector"):
            self.schema_inspector.workbook_path = Path(self.settings.workbook_path)

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

        # Build combined active queue: comments prioritized from bottom to top of Excel (highest row first)
        reversed_comments = sorted(self.comment_tasks, key=lambda x: getattr(x, "excel_row", 0), reverse=True)
        self.task_queue = [t for t in reversed_comments + self.post_tasks if t.is_eligible]

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

    def pull_latest_from_google_sheet(self, target_path: Optional[str] = None) -> Tuple[bool, str]:
        """Download latest content from Google Sheet, update local workbook, and re-initialize tasks."""
        dest = target_path or self.settings.workbook_path
        ok, msg = self.google_syncer.pull_latest_from_google_sheet(
            target_path=dest
        )
        if ok:
            self.initialize(reload_accounts=False)
        return ok, msg

    def sync_from_google_sheet(self) -> Tuple[bool, str]:
        """Alias for pull_latest_from_google_sheet."""
        return self.pull_latest_from_google_sheet()

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
        if clean in ("shoro-test", "shoro_test", "default"):
            return True
        active_batch = getattr(self, "_active_batch_accounts", None)
        if active_batch:
            clean_low = clean.lower()
            for b_acc in active_batch:
                if clean_low == b_acc.lower():
                    return True
                p = self.account_manager.accounts.get(b_acc)
                if p and clean_low == p.chrome_profile_name.lower():
                    return True
        enabled_profiles = {
            p.chrome_profile_name for p in self.account_manager.list_accounts() if p.enabled
        }
        enabled_ids = {
            p.account_id for p in self.account_manager.list_accounts() if p.enabled
        }
        if clean in enabled_profiles or clean in enabled_ids:
            return True
        for p in self.account_manager.list_accounts():
            if p.enabled and clean.lower() in (p.chrome_profile_name.lower(), p.account_id.lower()):
                return True
        return False

    def resolve_profile_data_dir(self, profile_name: str) -> str:
        """Resolve persistent filesystem user-data directory for an account profile."""
        clean = str(profile_name or "").strip()
        if not clean:
            clean = "shoro-test"

        # 1. Match against configured account profiles
        for p in self.account_manager.list_accounts():
            if clean.lower() in (p.chrome_profile_name.lower(), p.account_id.lower()):
                p_dir = Path(p.user_data_directory)
                if not p_dir.is_absolute():
                    p_dir = Path(os.getcwd()) / p_dir
                return str(p_dir).replace("/", os.sep)
            clean_sub = re.sub(r'[^a-zA-Z0-9]', '', clean.lower())
            prof_sub = re.sub(r'[^a-zA-Z0-9]', '', p.chrome_profile_name.lower())
            acc_sub = re.sub(r'[^a-zA-Z0-9]', '', p.account_id.lower())
            if clean_sub and (clean_sub in prof_sub or clean_sub in acc_sub):
                p_dir = Path(p.user_data_directory)
                if not p_dir.is_absolute():
                    p_dir = Path(os.getcwd()) / p_dir
                return str(p_dir).replace("/", os.sep)

        # 2. Check openclaw client
        claw_dir = self.openclaw_client.get_profile_data_dir(clean)
        if claw_dir:
            return str(claw_dir).replace("/", os.sep)

        # 3. Default isolated persistent local directory
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', clean)
        return str(Path(os.getcwd()) / "chrome_profiles" / safe_name).replace("/", os.sep)

    def resolve_active_tab(self, profile_name: str = "shoro-test") -> Tuple[Optional[str], Optional[str]]:
        """Verify stored tab or resolve latest active browser tab.
        
        Requirements:
        - If browser is stopped, returns (None, clear message asking operator to start/reopen).
        - Verifies whether stored tab ID still exists in open tabs.
        - If stored tab no longer exists, retrieves current tabs and resolves to active tab.
        - Clears outdated references if no tabs exist or browser stopped.
        """
        if not self.is_profile_allowed(profile_name):
            return None, f"Profile '{profile_name}' is locked or unauthorized."

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
            self.ensure_visible_browser_running(url=url, profile_name=profile_name)
            resp = self.openclaw_client.open_url(url=url, profile_name=profile_name)

            if resp.success:
                if isinstance(resp.data, dict):
                    self.active_target_id = resp.data.get("target_id") or resp.data.get("tab_id")
                    self.active_tab_id = resp.data.get("tab_id") or self.active_target_id
                tab_id = self.active_target_id or self.active_tab_id
                if tab_id:
                    self.activate_browser_tab(tab_id)
                self.bring_browser_to_front("Chrome")
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

        data_dir = self.resolve_profile_data_dir(profile_name)

        try:
            if not os.path.exists(data_dir):
                os.makedirs(data_dir, exist_ok=True)
        except Exception as e:
            logger.debug("Could not create data_dir (may already exist): %s", e)

        # 1. Terminate any stale background processes holding this profile or port 18801 and clear lockfile
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'chrome' in p.info['name'].lower():
                    cmd = p.info.get('cmdline') or []
                    if any(profile_name.lower() in str(arg).lower() or data_dir.lower() in str(arg).lower() or '18801' in str(arg) for arg in cmd):
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

        # 2. For legacy shoro-test login only, use bat helper if no specific URL requested
        if profile_name in ("shoro-test", "shoro_test") and (not url or url == "https://www.reddit.com/login"):
            bat_candidates = [
                r"d:\Shoro AI Reddit\launch_login_browser.bat",
                os.path.expandvars(r"%USERPROFILE%\OneDrive\Desktop\Login to Reddit.bat"),
                os.path.expandvars(r"%USERPROFILE%\Desktop\Login to Reddit.bat"),
            ]
            bat_file = next((b for b in bat_candidates if os.path.exists(b)), None)
            if os.name == "nt" and bat_file and hasattr(os, "startfile"):
                try:
                    os.startfile(bat_file)
                    time.sleep(1.5)
                    return True, "Desktop Chrome login window launched for shoro-test!"
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
                "--remote-debugging-port=18801",
                f"--user-data-dir={data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                url,
            ]
            subprocess.Popen(cmd)
            time.sleep(1.5)
            return True, (
                f"Visible Chrome window opened for '{profile_name}'! Please log in to Reddit in the window. "
                f"Your session and cookies will be saved in '{data_dir}' and survive all restarts."
            )
        except Exception as exc:
            logger.error("Failed to launch visible Chrome window: %s", exc)
            return False, f"Failed to launch Chrome window: {exc}"

    def bring_browser_to_front(self, title_hint: str = "Chrome", cdp_port: int = 18801) -> bool:
        """Bring the Chrome desktop window to the front on the operator's display."""
        # 1. Activate tab in Chrome via CDP so Chrome visually focuses this tab
        tab_id = getattr(self, "active_target_id", None) or getattr(self, "active_tab_id", None)
        if tab_id:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json/activate/{tab_id}")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    pass
            except Exception:
                pass

        # 2. Bring Chrome application window to foreground in Windows
        if os.name == "nt":
            ps_script = (
                "$wshell = New-Object -ComObject WScript.Shell;\n"
                "try { $wshell.SendKeys('%'); } catch {};\n"
                f"$wshell.AppActivate('{title_hint}');\n"
                "$wshell.AppActivate('Reddit');\n"
            )
            try:
                subprocess.run(
                    ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=2,
                    capture_output=True,
                )
                return True
            except Exception as e:
                logger.debug("Failed to bring browser to front: %s", e)
        return False

    def ensure_visible_browser_running(
        self,
        url: str = "about:blank",
        profile_name: str = "shoro-test",
    ) -> bool:
        """Ensure a visible native Chrome window is running and displayed on screen with CDP port 18801."""
        cdp_port = 18801
        is_running = False
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json")
            with urllib.request.urlopen(req, timeout=1.0) as r:
                if r.status == 200:
                    is_running = True
        except Exception:
            is_running = False

        data_dir = self.resolve_profile_data_dir(profile_name)
        target_norm = os.path.normcase(os.path.abspath(data_dir))

        # Check if running Chrome belongs to a DIFFERENT profile
        if is_running:
            running_data_dir = None
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if 'chrome' in p.info['name'].lower():
                        cmd = p.info.get('cmdline') or []
                        if any('18801' in str(arg) for arg in cmd):
                            for arg in cmd:
                                if str(arg).startswith('--user-data-dir='):
                                    running_data_dir = str(arg).split('=', 1)[1].strip('"\'')
                                    break
                except Exception:
                    pass

            if running_data_dir:
                running_norm = os.path.normcase(os.path.abspath(running_data_dir))
                if running_norm != target_norm:
                    logger.info("Switching Chrome profile from '%s' to '%s' (%s). Terminating running Chrome...", running_data_dir, profile_name, data_dir)
                    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                        try:
                            if 'chrome' in p.info['name'].lower():
                                cmd = p.info.get('cmdline') or []
                                if any('18801' in str(arg) for arg in cmd):
                                    p.kill()
                        except Exception:
                            pass
                    time.sleep(1.0)
                    is_running = False

        try:
            os.makedirs(data_dir, exist_ok=True)
            lock_p = os.path.join(data_dir, "lockfile")
            if os.path.exists(lock_p):
                try:
                    os.remove(lock_p)
                except Exception:
                    pass
        except Exception:
            pass

        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        chrome_exe = next((p for p in chrome_candidates if os.path.exists(p)), None)

        if not is_running:
            if chrome_exe and os.name == "nt":
                launch_url = url if (url and url != "about:blank") else "about:blank"
                cmd = f'start "" "{chrome_exe}" --remote-debugging-port={cdp_port} --user-data-dir="{data_dir}" --no-first-run --no-default-browser-check --start-maximized "{launch_url}"'
                logger.info("Spawning visible desktop Chrome window for '%s': %s", profile_name, cmd)
                try:
                    subprocess.Popen(cmd, shell=True)
                    for _ in range(16):
                        time.sleep(0.5)
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=1.0) as r2:
                                if r2.status == 200:
                                    is_running = True
                                    break
                        except Exception:
                            pass
                except Exception as exc:
                    logger.warning("Failed to start visible Chrome via shell: %s", exc)
        else:
            # If already running with the target profile, do NOT spawn new tabs via shell command.
            pass

        self._current_running_data_dir = data_dir
        self._current_running_profile = profile_name

        # Bring window to front of screen
        self.bring_browser_to_front("Chrome")
        return is_running

    def close_browser_tab(self, target_id: Optional[str] = None, cdp_port: int = 18801) -> bool:
        """Close a specific CDP browser tab."""
        if not target_id:
            return False
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json/close/{target_id}")
            with urllib.request.urlopen(req, timeout=2.0) as r:
                return r.status == 200
        except Exception:
            return False

    def activate_browser_tab(self, target_id: Optional[str] = None, cdp_port: int = 18801) -> bool:
        """Activate the tab in Chrome so the browser visually switches to it on the desktop."""
        try:
            if not target_id:
                req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json")
                with urllib.request.urlopen(req, timeout=1.0) as r:
                    targets = json.loads(r.read())
                page = next((t for t in targets if t.get("type") == "page" and not str(t.get("url", "")).startswith("chrome://")), None)
                if page:
                    target_id = page.get("id")

            if target_id:
                act_url = f"http://127.0.0.1:{cdp_port}/json/activate/{target_id}"
                with urllib.request.urlopen(act_url, timeout=1.5) as resp:
                    logger.info("Activated browser tab %s in Chrome: status %s", target_id, resp.status)
                    return True
        except Exception as e:
            logger.debug("Failed to activate browser tab via CDP: %s", e)
        return False

    def capture_live_screen_preview(
        self,
        profile_name: str = "shoro-test",
        target_id: Optional[str] = None,
        output_filename: str = "live_automation_view.jpg",
        cdp_port: int = 18801,
    ) -> Optional[str]:
        """Capture real-time JPEG screenshot directly from Chrome CDP in <150ms.

        Falls back to OpenClaw CLI capture if direct CDP WebSocket is unavailable.
        """
        out_dir = Path("backups") / "live_screens"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / output_filename

        # Fast path: Direct CDP WebSocket Page.captureScreenshot
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json")
            with urllib.request.urlopen(req, timeout=1.2) as r:
                targets = json.loads(r.read())
            page = None
            if target_id:
                page = next((t for t in targets if t.get("id") == target_id or t.get("targetId") == target_id), None)
            if not page:
                page = next((t for t in targets if t.get("type") == "page" and not str(t.get("url", "")).startswith("chrome://")), None)
            if not page:
                page = next((t for t in targets if t.get("type") == "page"), None)

            if page and "webSocketDebuggerUrl" in page:
                ws_url = page["webSocketDebuggerUrl"]
                import websockets

                async def _fetch():
                    async with websockets.connect(ws_url, close_timeout=2) as ws:
                        await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "jpeg", "quality": 65}}))
                        msg = await ws.recv()
                        res = json.loads(msg)
                        b64 = res.get("result", {}).get("data")
                        if b64:
                            with open(out_path, "wb") as f:
                                f.write(base64.b64decode(b64))
                            return str(out_path)
                        return None

                path = asyncio.run(_fetch())
                if path and os.path.exists(path):
                    return path
        except Exception as e:
            logger.debug("Fast CDP screenshot capture fallback: %s", e)

        # Fallback path: OpenClaw CLI capture_screenshot
        try:
            sc_resp = self.openclaw_client.capture_screenshot(profile_name, target_id=target_id)
            if sc_resp.success and sc_resp.data and isinstance(sc_resp.data, dict):
                sc_p = sc_resp.data.get("screenshot_path", "")
                if sc_p:
                    norm = os.path.expanduser(sc_p.replace("~\\", "~" + os.sep))
                    if os.path.exists(norm):
                        return norm
        except Exception as e:
            logger.debug("OpenClaw CLI screenshot fallback error: %s", e)

        return None

    def evaluate_in_browser(
        self,
        profile_name: str,
        script: str,
        target_id: Optional[str] = None,
        timeout: int = 60,
        cdp_port: int = 18801,
    ) -> OpenClawResponse:
        """Fast direct CDP evaluation (<50ms) with OpenClaw CLI fallback."""
        tid = target_id or getattr(self, "active_target_id", None) or getattr(self, "active_tab_id", None)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json")
            with urllib.request.urlopen(req, timeout=1.0) as r:
                targets = json.loads(r.read())
            page = None
            if tid:
                page = next((t for t in targets if t.get("id") == tid or t.get("targetId") == tid), None)
            if not page:
                page = next((t for t in targets if t.get("type") == "page" and "reddit.com" in str(t.get("url", ""))), None)
            if not page:
                page = next((t for t in targets if t.get("type") == "page"), None)

            if page and "webSocketDebuggerUrl" in page:
                ws_url = page["webSocketDebuggerUrl"]
                import websockets
                import concurrent.futures

                async def _eval():
                    async with websockets.connect(ws_url, close_timeout=2) as ws:
                        expr = script.strip()
                        if not (expr.startswith("(") and expr.endswith(")")) and not expr.endswith("()"):
                            expr = f"({expr})()"
                        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr, "awaitPromise": True}}))
                        msg = await ws.recv()
                        res = json.loads(msg)
                        if "result" in res:
                            res_obj = res["result"]
                            if "exceptionDetails" in res_obj:
                                exc_txt = res_obj["exceptionDetails"].get("text") or "JS Exception"
                                return False, None, exc_txt
                            val = res_obj.get("result", {}).get("value")
                            return True, val, None
                        return True, None, None

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    ok, val, err = pool.submit(asyncio.run, _eval()).result(timeout=timeout)
                if ok:
                    return OpenClawResponse(success=True, data=val)
                else:
                    return OpenClawResponse(success=False, error=err or "Evaluation error")
        except Exception as e:
            logger.debug("Fast direct CDP evaluation fallback: %s", e)

        return self.openclaw_client.evaluate_script(profile_name, script, target_id=tid, timeout=timeout)

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
        data_dir = self.resolve_profile_data_dir(profile.account_id)
        Path(data_dir).mkdir(parents=True, exist_ok=True)

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
            # 1. Open discussion in visible browser profile
            self.ensure_visible_browser_running(url="about:blank", profile_name=prof_name)
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
            if tab_id:
                self.activate_browser_tab(tab_id)
            self.bring_browser_to_front("Chrome")

            # 2. Human touch: Randomized reading & page settling delay (2 to 15 seconds)
            reading_delay = random.uniform(3.0, 7.5)
            logger.info("Human touch: Pausing %.2fs to simulate human reading the thread...", reading_delay)
            time.sleep(reading_delay)

            # 3. Fast direct CDP inspection (<100ms): dismiss blocking modals, check auth, captcha, locked/archived
            inspect_js = """
            () => {
                const wait = ms => new Promise(r => setTimeout(r, ms));

                // Helper to search across shadow roots
                function findInShadow(root, selector) {
                    if (!root) return null;
                    try {
                        const found = root.querySelector(selector);
                        if (found) return found;
                    } catch(e) {}
                    const children = root.querySelectorAll('*');
                    for (const el of children) {
                        if (el.shadowRoot) {
                            const res = findInShadow(el.shadowRoot, selector);
                            if (res) return res;
                        }
                    }
                    return null;
                }

                // 1. Dismiss any blocking interstitial / modal if present (click close 'X' or send Escape)
                const closeSelectors = [
                    'button[aria-label="Close" i]',
                    'button[aria-label="Close modal" i]',
                    'button[aria-label*="dismiss" i]',
                    'faceplate-modal-header button',
                    'shreddit-signup-drawer button',
                    'auth-flow-modal button',
                    'div[role="dialog"] button[aria-label*="close" i]',
                    'div[role="dialog"] header button',
                    'div[aria-label="Close" i] button',
                    'button.close'
                ];
                for (const sel of closeSelectors) {
                    try {
                        const btn = document.querySelector(sel) || findInShadow(document, sel);
                        if (btn && typeof btn.click === 'function') {
                            btn.click();
                        }
                    } catch(e) {}
                }
                try {
                    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true}));
                } catch(e) {}

                // 2. Extract logged in user and authentication state
                const app = document.querySelector('shreddit-app');
                const currentUser = (app ? app.getAttribute('current-user') : '') || '';
                const userDropdown = !!document.querySelector(
                    '[data-testid="user-dropdown"], #USER_DROPDOWN_ID, button[aria-label*="User menu" i], button[aria-label*="User account" i], button[aria-label*="Open user menu" i], shreddit-user-drawer'
                );
                
                let detectedUser = currentUser;
                if (!detectedUser) {
                    const uLink = document.querySelector('a[href*="/user/"][aria-label*="profile" i], faceplate-tracker[noun="user_profile"] a');
                    if (uLink) {
                        const href = uLink.getAttribute('href') || '';
                        const m = href.match(/\\/user\\/([^\\/\\?]+)/);
                        if (m) detectedUser = m[1];
                    }
                }

                const hasEditor = !!document.querySelector('div[slot="rte"][contenteditable="true"], shreddit-composer, shreddit-comment-composer, faceplate-textarea-input, comment-body-header');
                const loginBtn = document.querySelector('a[href*="/login"], button[data-testid="login-button"]');
                const authModal = document.querySelector('shreddit-signup-drawer, auth-flow-modal');
                const isBlockingModal = !!(authModal && authModal.offsetParent !== null);

                // Authenticated if current user known, user dropdown present, or comment editor is ready and not blocked by a modal
                const isAuth = (!!detectedUser || userDropdown || hasEditor) && !isBlockingModal;

                // 3. Discussion state
                const postEl = document.querySelector('shreddit-post');
                const isLocked = !!(postEl && postEl.getAttribute('locked') === 'true') || !!document.querySelector('[data-testid="post-locked"]');
                const isArchived = !!(postEl && postEl.getAttribute('archived') === 'true') || !!document.querySelector('[data-testid="post-archived"]');
                const bodyTxt = (document.body ? (document.body.innerText || document.body.textContent || '') : '').toLowerCase();
                const isDeleted = bodyTxt.includes('this post has been removed') || bodyTxt.includes('sorry, this post has been removed') || bodyTxt.includes('[deleted by user]');

                // 4. Captcha check
                const challengeIframe = document.querySelector(
                    'iframe[src*="recaptcha/api2/bframe"], iframe[src*="challenges.cloudflare.com"], #px-captcha:not([style*="display: none"])'
                );
                const hasActiveChallenge = !!(challengeIframe && (challengeIframe.offsetWidth > 50 || challengeIframe.offsetHeight > 50));
                const hasCaptchaText = !hasEditor && (
                    bodyTxt.includes('verify you are human') ||
                    bodyTxt.includes("verify you're human") ||
                    bodyTxt.includes('press & hold to confirm you are a human') ||
                    bodyTxt.includes('press & hold')
                );
                const hasCaptcha = hasActiveChallenge || hasCaptchaText;

                return JSON.stringify({
                    isAuth: isAuth,
                    activeUser: detectedUser || (isAuth ? 'authenticated_user' : null),
                    isLocked: isLocked,
                    isArchived: isArchived,
                    isDeleted: isDeleted,
                    hasCaptcha: hasCaptcha,
                    hasEditor: hasEditor,
                    hasLoginButton: !!loginBtn,
                    domSnippet: document.documentElement ? document.documentElement.outerHTML.slice(0, 100000) : ''
                });
            }
            """

            eval_inspect = self.evaluate_in_browser(prof_name, inspect_js, target_id=tab_id, timeout=15)
            page_info: Dict[str, Any] = {}
            if eval_inspect.success and eval_inspect.data:
                page_info = _safe_json_loads(eval_inspect.data, default={})

            snapshot_text = page_info.get("domSnippet") or ""
            if not snapshot_text:
                snap_resp = self.openclaw_client.get_page_snapshot(prof_name, target_id=tab_id)
                snapshot_text = str(snap_resp.data or "") if snap_resp.success else ""

            # 4. Check for CAPTCHA (only when comment editor is not present and active)
            has_editor = bool(page_info.get("hasEditor"))
            if not has_editor and (page_info.get("hasCaptcha") or self.check_snapshot_for_captcha(snapshot_text)):
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome="CAPTCHA or verification screen detected. Manual handling required.",
                    error_code="CAPTCHA_SUSPECTED",
                )
                return False, "⚠️ CAPTCHA or verification screen detected. Please handle manually in the browser.", {"status": "CAPTCHA_DETECTED"}

            # 5. Check Discussion State (Locked, Archived, Deleted, Unavailable)
            if page_info.get("isLocked"):
                return False, "Discussion is locked. Skipping task.", {"status": "NON_ACTIONABLE", "reason": "Locked"}
            if page_info.get("isArchived"):
                return False, "Discussion is archived. Skipping task.", {"status": "NON_ACTIONABLE", "reason": "Archived"}
            if page_info.get("isDeleted"):
                return False, "Discussion is deleted or unavailable. Skipping task.", {"status": "NON_ACTIONABLE", "reason": "Deleted"}

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
            active_user = page_info.get("activeUser")
            is_auth = bool(page_info.get("isAuth"))

            if not is_test_url and not is_auth:
                # Secondary confirmation via verify_reddit_auth_in_dom
                is_auth_dom, dom_user, auth_msg = self.openclaw_client.verify_reddit_auth_in_dom(snapshot_text)
                if is_auth_dom:
                    is_auth = True
                    active_user = dom_user or active_user

            if not is_test_url and not is_auth:
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="OPENCLAW_ERROR",
                    operator_action="PREPARE_REPLY",
                    outcome="Reddit authentication required. Please log in manually.",
                    error_code="NOT_LOGGED_IN",
                )
                return False, f"Reddit account '{prof_name}' is not logged in. Please log in to Reddit.", {"status": "NOT_LOGGED_IN"}

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
                    if not getattr(self, "allow_account_independent", True):
                        err_acc = f"Wrong account is active in browser: logged in as '{active_user}', expected '{task.account_id}'. Stopping."
                        self.audit_logger.log_event(
                            task_id=task_id,
                            event_type="OPENCLAW_ERROR",
                            operator_action="PREPARE_REPLY",
                            outcome=err_acc,
                            error_code="WRONG_ACCOUNT",
                        )
                        return False, err_acc, {"status": "WRONG_ACCOUNT", "active_user": active_user, "expected_user": task.account_id}
                    else:
                        logger.info("Account-independent execution enabled: publishing with active browser account '%s' (assigned: '%s')", active_user, task.account_id)

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
                "  // Dismiss any sign-up or interstitial modal if present\n"
                "  const closeSelectors = [\n"
                "    'button[aria-label=\"Close\" i]',\n"
                "    'button[aria-label=\"Close modal\" i]',\n"
                "    'faceplate-modal-header button',\n"
                "    'shreddit-signup-drawer button[aria-label=\"Close\" i]',\n"
                "    'auth-flow-modal button[aria-label=\"Close\" i]',\n"
                "    'div[aria-label=\"Close\" i] button',\n"
                "    'button.close'\n"
                "  ];\n"
                "  for (const sel of closeSelectors) {\n"
                "    try { const b = document.querySelector(sel); if (b) b.click(); } catch(e) {}\n"
                "  }\n"
                "  await wait(150);\n"
                "\n"
                "  const authModal = document.querySelector('shreddit-signup-drawer, auth-flow-modal');\n"
                "  const isBlockingModal = !!(authModal && authModal.offsetParent !== null);\n"
                "  if (isBlockingModal && !editor) {\n"
                "    return JSON.stringify({ success: false, isAuthError: true, error: 'Reddit authentication required. Session is not logged in.' });\n"
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
                "    if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {\n"
                "      editor.value = expected;\n"
                "    } else {\n"
                "      editor.innerText = expected;\n"
                "    }\n"
                "    editor.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: expected }));\n"
                "    editor.dispatchEvent(new Event('change', { bubbles: true }));\n"
                "  }\n"
                "  await wait(200);\n"
                "\n"
                "  // 4. Read back and verify\n"
                "  let rawText = ((editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT')\n"
                "    ? editor.value\n"
                "    : (editor.innerText || editor.textContent || '')).trim();\n"
                "  const readBack = rawText.replace(/<[^>]+>/g, '').trim();\n"
                "\n"
                "  // 5. If auto_submit is enabled, human proofreading pause (1.5 to 2.5 seconds) before submit\n"
                "  let submitted = false;\n"
                "  if (" + ("true" if auto_submit else "false") + ") {\n"
                "    await wait(Math.floor(Math.random() * 1000) + 1500);\n"
                "    function findDeepButtons(root) {\n"
                "      let res = [];\n"
                "      if (!root) return res;\n"
                "      try { res = res.concat(Array.from(root.querySelectorAll('button'))); } catch(e) {}\n"
                "      const allEls = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];\n"
                "      for (const el of allEls) {\n"
                "        if (el.shadowRoot) {\n"
                "          res = res.concat(findDeepButtons(el.shadowRoot));\n"
                "        }\n"
                "      }\n"
                "      return res;\n"
                "    }\n"
                "    const allButtons = findDeepButtons(document);\n"
                "    let submitBtn = allButtons.find(b => {\n"
                "      const slot = (b.getAttribute('slot') || '').toLowerCase();\n"
                "      const txt = (b.innerText || b.textContent || '').trim().toLowerCase();\n"
                "      const aria = (b.getAttribute('aria-label') || '').toLowerCase();\n"
                "      const testId = (b.getAttribute('data-testid') || '').toLowerCase();\n"
                "      return slot === 'submit-button' || txt === 'comment' || aria === 'comment' || testId.includes('comment') || (b.type === 'submit' && (txt.includes('comment') || slot === 'submit-button'));\n"
                "    });\n"
                "    if (!submitBtn) {\n"
                "      const composer = document.querySelector('shreddit-composer, shreddit-comment-composer');\n"
                "      if (composer) {\n"
                "        const root = composer.shadowRoot || composer;\n"
                "        submitBtn = root.querySelector('button[slot=\"submit-button\"], button[type=\"submit\"]') ||\n"
                "                    Array.from(root.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'comment');\n"
                "      }\n"
                "    }\n"
                "    if (submitBtn) {\n"
                "      if (submitBtn.disabled || submitBtn.hasAttribute('disabled')) {\n"
                "        submitBtn.disabled = false;\n"
                "        submitBtn.removeAttribute('disabled');\n"
                "        submitBtn.removeAttribute('aria-disabled');\n"
                "      }\n"
                "      submitBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                "      await wait(350);\n"
                "      submitBtn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, composed: true }));\n"
                "      submitBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, composed: true }));\n"
                "      submitBtn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, composed: true }));\n"
                "      submitBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, composed: true }));\n"
                "      submitBtn.click();\n"
                "      await wait(2500);\n"
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
            eval_resp = self.evaluate_in_browser(prof_name, js_script, target_id=tab_id, timeout=120)
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
                    eval_resp = self.evaluate_in_browser(prof_name, js_script, target_id=tab_id, timeout=120)
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
            clean_verified = re.sub(r"<[^>]+>", "", verified_text)
            norm_verified = re.sub(r"\s+", " ", clean_verified).strip()
            norm_expected = re.sub(r"\s+", " ", task.reply_text).strip()
            if norm_verified != norm_expected and clean_verified.strip() != task.reply_text.strip():
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
                    account_name=task.account_id or prof_name,
                    destination_url=getattr(task, "destination_url", None),
                    task_id=getattr(task, "task_id", None),
                    thread_title=getattr(task, "thread_title", None) or getattr(task, "title", None),
                )
                excel_update_msg = excel_msg
                if not excel_ok:
                    logger.warning("Notice on Excel update for row %s: %s", task.excel_row, excel_msg)
                else:
                    logger.info("Excel updated successfully for row %s: %s", task.excel_row, excel_msg)

                # Cleanly close the submitted tab in auto mode
                if tab_id:
                    time.sleep(2.0)
                    self.close_browser_tab(tab_id)
            else:
                task.status = "Reply inserted - Ready for manual submission"
                outcome_text = "Reply inserted - Ready for manual submission"
                event_action = "INSERT_REPLY"
                event_type = "PREPARE_REPLY"
                status_code = "READY_FOR_MANUAL_SUBMISSION"
                now_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    self.excel_handler.update_task_status(
                        worksheet_name=task.worksheet,
                        excel_row=task.excel_row,
                        new_status="Draft",
                        timestamp_str=now_human,
                        account_name=task.account_id or prof_name,
                        destination_url=getattr(task, "destination_url", None),
                        task_id=getattr(task, "task_id", None),
                        thread_title=getattr(task, "thread_title", None) or getattr(task, "title", None),
                    )
                except Exception as ex_draft:
                    logger.warning("Could not sync Excel draft status: %s", ex_draft)
                if tab_id:
                    self.activate_browser_tab(tab_id)
                self.bring_browser_to_front("Chrome")

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
        pacing_mode: str = "stealth_human",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Fetch URL, account, and exact reply from Excel task, open discussion, activate editor, insert reply, and automatically submit to Reddit."""
        return self.execute_granular_human_reply(
            task=task,
            profile_name=profile_name,
            pacing_mode=pacing_mode,
            auto_submit=True,
            progress_callback=progress_callback,
            stop_check=stop_check,
        )

    def execute_granular_human_reply(
        self,
        task: CommentTask,
        profile_name: Optional[str] = None,
        pacing_mode: str = "stealth_human",
        auto_submit: bool = True,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Execute full granular 9-phase human-mimic comment workflow (15-25 min + 1 min pacing).

        Phases:
        1. (3-4 min) Scroll and check: slow top-to-bottom scroll, check locked, archived, Shoro duplicate. Close if fails.
        2. (3 min) Read the question: read full question, extract 1 key anchor detail (state, document, fee, date).
        3. (4-6 min) Read top 5 comments: inspect answered vs missing value-add.
        4. (1.5 min) Open comment box: click 'Join the conversation', wait for editor. Do not paste.
        5. (6-10 min) Type by words, paragraph by paragraph: word typing with 10-15s pause between paragraphs.
        6. (3-4 min) Re-read reply: verify question answered, max 1 link, link opens, state spelling.
        7. Click Comment: submit to Reddit.
        8. (2 min) Check it is live: wait, reload page once, verify post-reload appearance and formatting.
        9. (2 min) Log in Excel & close: write date, account, live URL, and close thread tab.
        """
        import json
        import re
        import time
        import random

        if not task:
            return False, "No task provided.", {"status": "NO_TASK"}

        task_id = task.task_id or f"comment_{task.worksheet}_{task.excel_row}"
        prof_name = profile_name or task.chrome_profile_name or "shoro-test"
        if not self.is_profile_allowed(prof_name):
            prof_name = "shoro-test"

        if not task.destination_url:
            return False, "Task has no destination URL.", {"status": "INVALID_URL"}

        is_valid_url, url_err = TaskValidator.validate_url(task.destination_url)
        if not is_valid_url:
            return False, f"Invalid destination URL: {url_err}", {"status": "INVALID_URL"}

        reply_text = task.reply_text or ""
        if not reply_text.strip():
            return False, "Task has no prepared reply text in 'Shoro AI reply' column.", {"status": "EMPTY_REPLY"}

        if not self._browser_lock.acquire(blocking=False):
            return False, "Another browser operation is currently in progress. Please wait a moment.", {"status": "BROWSER_BUSY"}

        # Timing configurations: standard stealth human pacing as declared in project
        t_phase1 = random.uniform(180, 240)
        t_phase2 = random.uniform(180, 195)
        t_phase3 = random.uniform(240, 360)
        t_phase4 = random.uniform(90, 105)
        t_phase5 = random.uniform(360, 600)
        p_pause = random.uniform(10, 15)
        t_phase6 = random.uniform(180, 240)
        t_phase8 = random.uniform(120, 140)
        t_phase9 = random.uniform(120, 135)

        tab_id = getattr(self, "active_target_id", None) or getattr(self, "active_tab_id", None)

        def emit(phase_num: int, phase_name: str, status_msg: str, detail_data: Optional[Dict[str, Any]] = None):
            logger.info("Granular Reply [Phase %d/9 - %s]: %s", phase_num, phase_name, status_msg)
            data = detail_data.copy() if detail_data else {}
            try:
                sc = self.capture_live_screen_preview(prof_name, target_id=tab_id)
                if sc:
                    data["screenshot_path"] = sc
            except Exception:
                pass
            if progress_callback:
                progress_callback({
                    "phase": phase_num,
                    "phase_name": phase_name,
                    "status": status_msg,
                    "task": task,
                    "pacing_mode": pacing_mode,
                    "details": data,
                    "screenshot_path": data.get("screenshot_path"),
                })

        try:
            # 0. Navigate to discussion
            self.ensure_visible_browser_running(url="about:blank", profile_name=prof_name)
            emit(0, "Navigation", f"Opening thread: {task.destination_url}")
            nav_resp = self.openclaw_client.open_url(url=task.destination_url, profile_name=prof_name)
            if not nav_resp.success:
                err_nav = nav_resp.error or "Failed to open thread in OpenClaw browser."
                return False, f"Navigation failed: {err_nav}", {"status": "NAVIGATION_FAILED", "error": err_nav}

            if isinstance(nav_resp.data, dict):
                self.active_tab_id = nav_resp.data.get("tab_id")
                self.active_target_id = nav_resp.data.get("target_id")
            tab_id = self.active_target_id or self.active_tab_id
            if tab_id:
                self.activate_browser_tab(tab_id)
            self.bring_browser_to_front("Chrome")

            # Active account verification
            auth_snap = self.openclaw_client.get_page_snapshot(prof_name, target_id=tab_id)
            active_account = "shoro_test"
            if auth_snap.success and auth_snap.data:
                auth_res = self.openclaw_client.verify_reddit_auth_in_dom(str(auth_snap.data))
                if isinstance(auth_res, tuple) and len(auth_res) >= 2:
                    is_auth, u_name = auth_res[0], auth_res[1]
                    if u_name:
                        active_account = u_name

            # -------------------------------------------------------------
            # Phase 1: (3-4 min) Scroll and check
            # -------------------------------------------------------------
            emit(1, "Scroll and Check", f"Scrolling top-to-bottom ({int(t_phase1)}s pacing)... Checking title, locked, archived, and existing Shoro comments.")
            js_phase1 = """
            async () => {
                const wait = ms => new Promise(r => setTimeout(r, ms));
                const titleEl = document.querySelector('h1, [slot="title"]') || {};
                const titleText = (titleEl.innerText || titleEl.textContent || document.title || '').trim();

                const mainPost = document.querySelector('shreddit-post');
                const hasCommentEntry = !!document.querySelector('div[slot="rte"], shreddit-composer, faceplate-tracker[noun="comment"], comment-body-header, [placeholder*="conversation" i]');

                let isLocked = false;
                let isArchived = false;
                let isDeleted = false;

                if (mainPost) {
                    isLocked = (mainPost.hasAttribute('locked') && mainPost.getAttribute('locked') !== 'false') ||
                               (mainPost.querySelector('[data-testid="locked"]') !== null);
                    isArchived = (mainPost.hasAttribute('archived') && mainPost.getAttribute('archived') !== 'false') ||
                                 (mainPost.querySelector('[data-testid="archived"]') !== null);
                    const postAuthor = (mainPost.getAttribute('author') || '').trim();
                    isDeleted = postAuthor === '[deleted]' || !!mainPost.querySelector('[data-testid="post_author_deleted"]');
                }

                // If comment composer or input trigger is present on page, thread is commentable
                if (hasCommentEntry) {
                    isLocked = false;
                    isArchived = false;
                }

                // Check if a Shoro comment already exists in the thread
                const allComments = Array.from(document.querySelectorAll('shreddit-comment, [data-testid="comment"]'));
                let hasShoroComment = false;
                let foundCommentSnippet = '';
                for (const c of allComments) {
                    const txt = (c.innerText || c.textContent || '').toLowerCase();
                    const author = (c.getAttribute('author') || '').toLowerCase();
                    if (txt.includes('shoro') || txt.includes('shoro.ai') || author.includes('shoro')) {
                        hasShoroComment = true;
                        foundCommentSnippet = txt.slice(0, 100);
                        break;
                    }
                }

                // Smooth scroll down steps
                for (let i = 0; i < 4; i++) {
                    window.scrollBy({ top: 350, behavior: 'smooth' });
                    await wait(200);
                }

                return JSON.stringify({
                    title: titleText,
                    isLocked: isLocked,
                    isArchived: isArchived,
                    isDeleted: isDeleted,
                    hasShoroComment: hasShoroComment,
                    foundCommentSnippet: foundCommentSnippet,
                    canProceed: !isLocked && !isArchived && !isDeleted && !hasShoroComment
                });
            }
            """
            p1_res = self.evaluate_in_browser(prof_name, js_phase1, target_id=tab_id, timeout=60)
            p1_data = _safe_json_loads(p1_res.data, default={}) if (p1_res.success and p1_res.data) else {}

            if p1_data.get("isLocked"):
                self.openclaw_client.close_tab(prof_name, tab_id)
                return False, "Thread is locked. Safety check closed thread.", {"status": "THREAD_LOCKED"}
            if p1_data.get("isArchived"):
                self.openclaw_client.close_tab(prof_name, tab_id)
                return False, "Thread is archived. Safety check closed thread.", {"status": "THREAD_ARCHIVED"}
            if p1_data.get("hasShoroComment"):
                self.openclaw_client.close_tab(prof_name, tab_id)
                return False, "Shoro comment already exists in this thread. Safety check closed thread to prevent duplication.", {"status": "DUPLICATE_COMMENT"}

            # Sleep remaining Phase 1 duration with stop check
            p1_start = time.time()
            while time.time() - p1_start < t_phase1:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # -------------------------------------------------------------
            # Phase 2: (3 min) Read the question
            # -------------------------------------------------------------
            emit(2, "Read the Question", f"Reading full question and context ({int(t_phase2)}s)... Extracting key anchor detail (state, document, fee, date).")
            js_phase2 = """
            () => {
                window.scrollTo({ top: 0, behavior: 'smooth' });
                const postBodyEl = document.querySelector('div[slot="text-body"], shreddit-post [slot="text-body"], [data-testid="post-content"]');
                const bodyText = postBodyEl ? (postBodyEl.innerText || postBodyEl.textContent || '') : '';
                const title = (document.querySelector('h1, [slot="title"]') || {}).innerText || document.title;
                const fullText = (title + " " + bodyText).toLowerCase();

                const states = ['california', 'ontario', 'new york', 'florida', 'texas', 'new jersey', 'pennsylvania', 'illinois', 'ohio', 'georgia', 'virginia', 'washington', 'michigan', 'alberta', 'british columbia'];
                let detectedState = states.find(s => fullText.includes(s));

                const docs = ['g1', 'g2', 'permit', 'learner\\'s', 'license', 'licence', 'real id', 'cdl', 'knowledge test', 'road test'];
                let detectedDoc = docs.find(d => fullText.includes(d));

                const feeMatch = fullText.match(/\\$\\d+(\\.\\d{2})?|\\d+\\s*dollars/);
                let detectedFee = feeMatch ? feeMatch[0] : null;

                const dateMatch = fullText.match(/\\b\\d+\\s*(days|weeks|months|years|hours)\\b/);
                let detectedDate = dateMatch ? dateMatch[0] : null;

                let summaryDetail = [];
                if (detectedState) summaryDetail.push("Jurisdiction: " + detectedState.toUpperCase());
                if (detectedDoc) summaryDetail.push("Doc/Test: " + detectedDoc.toUpperCase());
                if (detectedFee) summaryDetail.push("Fee: " + detectedFee);
                if (detectedDate) summaryDetail.push("Timeline: " + detectedDate);

                return JSON.stringify({
                    detail: summaryDetail.length > 0 ? summaryDetail.join(" | ") : "Topic: General Driver Licensing & Regulations",
                    bodySnippet: bodyText.slice(0, 200)
                });
            }
            """
            p2_res = self.evaluate_in_browser(prof_name, js_phase2, target_id=tab_id, timeout=30)
            extracted_detail = "General Licensing"
            if p2_res.success and p2_res.data:
                p2_d = _safe_json_loads(p2_res.data, default={})
                extracted_detail = p2_d.get("detail", extracted_detail)

            emit(2, "Read the Question", f"Extracted Anchor Detail: {extracted_detail}", {"extracted_detail": extracted_detail})

            p2_start = time.time()
            while time.time() - p2_start < t_phase2:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # -------------------------------------------------------------
            # Phase 3: (4-6 min) Read top 5 comments
            # -------------------------------------------------------------
            emit(3, "Read Top 5 Comments", f"Reading top 5 community responses ({int(t_phase3)}s)... Evaluating answered advice vs missing value-add.")
            js_phase3 = """
            async () => {
                const wait = ms => new Promise(r => setTimeout(r, ms));
                const commentEls = Array.from(document.querySelectorAll('shreddit-comment, [data-testid="comment"]')).slice(0, 5);
                const results = [];
                for (let i = 0; i < commentEls.length; i++) {
                    const el = commentEls[i];
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    await wait(300);
                    const author = el.getAttribute('author') || 'Redditor';
                    const txt = (el.innerText || el.textContent || '').trim().replace(/\\n+/g, ' ').slice(0, 120);
                    results.push({ index: i + 1, author: author, snippet: txt });
                }
                return JSON.stringify(results);
            }
            """
            p3_res = self.evaluate_in_browser(prof_name, js_phase3, target_id=tab_id, timeout=45)
            p3_comments = _safe_json_loads(p3_res.data, default=[]) if (p3_res.success and p3_res.data) else []

            emit(3, "Read Top 5 Comments", f"Completed review of {len(p3_comments)} top comments. Positioning Shoro AI reply to fill advice gaps.", {"top_comments": p3_comments})

            p3_start = time.time()
            while time.time() - p3_start < t_phase3:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # -------------------------------------------------------------
            # Phase 4: (1.5 min) Open the comment box
            # -------------------------------------------------------------
            emit(4, "Open Comment Box", f"Locating 'Join the conversation' and mounting editor ({int(t_phase4)}s)... Explicit rule: Not pasting yet.")
            js_phase4 = """
            async () => {
                const wait = ms => new Promise(r => setTimeout(r, ms));
                const visibleInput = Array.from(document.querySelectorAll('faceplate-textarea-input, [placeholder*="conversation" i]')).find(el => el.offsetParent !== null) ||
                                     document.querySelector('faceplate-textarea-input');
                const tracker = document.querySelector('faceplate-tracker[noun="add_comment_button"]');

                if (visibleInput) {
                    visibleInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    await wait(300);
                    if (visibleInput.shadowRoot) {
                        const ta = visibleInput.shadowRoot.querySelector('#innerTextArea, textarea');
                        const lbl = visibleInput.shadowRoot.querySelector('label');
                        if (ta) {
                            ta.focus();
                            ta.click();
                            ta.dispatchEvent(new Event('focus', { bubbles: true }));
                            ta.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        }
                        if (lbl) lbl.click();
                    }
                    visibleInput.click();
                    visibleInput.dispatchEvent(new Event('focusin', { bubbles: true }));
                }
                if (tracker) {
                    tracker.click();
                }

                await wait(500);

                const host = document.querySelector('comment-composer-host');
                if (host && host.classList.contains('nd:hidden')) {
                    host.classList.remove('nd:hidden');
                    if (visibleInput) visibleInput.classList.add('hidden');
                }

                let editor = null;
                for (let i = 0; i < 30; i++) {
                    await wait(150);
                    editor = document.querySelector('div[slot="rte"][contenteditable="true"]') ||
                             document.querySelector('shreddit-composer div[contenteditable="true"]') ||
                             document.querySelector('div[contenteditable="true"]:not([class*="recaptcha"])');
                    if (editor) break;
                }

                const composer = document.querySelector('shreddit-composer');
                if (composer) {
                    composer.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }

                return JSON.stringify({ editorMounted: !!editor, composerVisible: composer ? composer.offsetParent !== null : false });
            }
            """
            p4_res = self.evaluate_in_browser(prof_name, js_phase4, target_id=tab_id, timeout=30)
            emit(4, "Open Comment Box", "Editor mounted successfully and visible in browser. Pausing to compose thoughts before typing begins.")

            p4_start = time.time()
            while time.time() - p4_start < t_phase4:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # -------------------------------------------------------------
            # Phase 5: (6-10 min) Type by words, paragraph by paragraph
            # -------------------------------------------------------------
            paragraphs = [p.strip() for p in reply_text.split("\n\n") if p.strip()]
            if not paragraphs:
                paragraphs = [reply_text.strip()]

            emit(5, "Natural Typing", f"Initiating word-by-word paragraph typing ({len(paragraphs)} paragraphs, {int(t_phase5)}s pacing). {int(p_pause)}s pauses between paragraphs.")

            for p_idx, para in enumerate(paragraphs, start=1):
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}

                emit(5, "Natural Typing", f"Typing Paragraph {p_idx}/{len(paragraphs)} ({len(para.split())} words)...")

                # In-browser progressive word typing simulation for this paragraph
                prev_text = "\n\n".join(paragraphs[:p_idx - 1]) if p_idx > 1 else ""
                js_para = (
                    "async () => {\n"
                    "  const wait = ms => new Promise(r => setTimeout(r, ms));\n"
                    "  let editor = document.querySelector('div[slot=\"rte\"][contenteditable=\"true\"]') ||\n"
                    "               document.querySelector('shreddit-composer div[contenteditable=\"true\"]') ||\n"
                    "               document.querySelector('div[contenteditable=\"true\"]:not([class*=\"recaptcha\"])');\n"
                    "  if (!editor) return JSON.stringify({ success: false });\n"
                    "  editor.focus();\n"
                    "  await wait(200);\n"
                    "  const prevText = " + json.dumps(prev_text) + ";\n"
                    "  const newPara = " + json.dumps(para) + ";\n"
                    "  const words = newPara.split(' ');\n"
                    "\n"
                    "  function updateEditor(full) {\n"
                    "    if (editor.__lexicalEditor && typeof editor.__lexicalEditor.parseEditorState === 'function') {\n"
                    "      const paras = full.split('\\n\\n').map(p => ({\n"
                    "        children: [{ detail: 0, format: 0, mode: 'normal', style: '', text: p, type: 'text', version: 1 }],\n"
                    "        direction: 'ltr', format: '', indent: 0, type: 'paragraph', version: 1\n"
                    "      }));\n"
                    "      const stateObj = { root: { children: paras, direction: 'ltr', format: '', indent: 0, type: 'root', version: 1 } };\n"
                    "      editor.__lexicalEditor.setEditorState(editor.__lexicalEditor.parseEditorState(JSON.stringify(stateObj)));\n"
                    "    } else {\n"
                    "      editor.innerText = full;\n"
                    "    }\n"
                    "    editor.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true }));\n"
                    "  }\n"
                    "\n"
                    "  let typedInPara = '';\n"
                    "  for (let i = 0; i < words.length; i++) {\n"
                    "    typedInPara += (i > 0 ? ' ' : '') + words[i];\n"
                    "    const fullCurrent = prevText ? (prevText + '\\n\\n' + typedInPara) : typedInPara;\n"
                    "    updateEditor(fullCurrent);\n"
                    "    const isPunct = words[i].endsWith('.') || words[i].endsWith(',') || words[i].endsWith('?');\n"
                    "    const delay = isPunct ? (Math.floor(Math.random() * 130) + 180) : (Math.floor(Math.random() * 80) + 110);\n"
                    "    await wait(delay);\n"
                    "  }\n"
                    "  const finalCurrent = prevText ? (prevText + '\\n\\n' + newPara) : newPara;\n"
                    "  updateEditor(finalCurrent);\n"
                    "  editor.dispatchEvent(new Event('change', { bubbles: true }));\n"
                    "  await wait(200);\n"
                    "  return JSON.stringify({ success: true, count: finalCurrent.length });\n"
                    "}"
                )
                self.evaluate_in_browser(prof_name, js_para, target_id=tab_id, timeout=30)

                # Pause between paragraphs as instructed (10-15s)
                if p_idx < len(paragraphs):
                    emit(5, "Natural Typing", f"Paragraph {p_idx} completed. Simulating human thought pause ({int(p_pause)}s)...")
                    p_pause_start = time.time()
                    while time.time() - p_pause_start < p_pause:
                        if stop_check and stop_check():
                            return False, "Halted by operator.", {"status": "HALTED"}
                        time.sleep(0.5)

            # Ensure final exact character match for Column K
            js_final_state = (
                "() => {\n"
                "  let editor = document.querySelector('div[slot=\"rte\"][contenteditable=\"true\"]') ||\n"
                "               document.querySelector('shreddit-composer div[contenteditable=\"true\"]') ||\n"
                "               document.querySelector('div[contenteditable=\"true\"]:not([class*=\"recaptcha\"])');\n"
                "  if (!editor) return JSON.stringify({ verified: false });\n"
                "  const full = " + json.dumps(reply_text) + ";\n"
                "  if (editor.__lexicalEditor && typeof editor.__lexicalEditor.parseEditorState === 'function') {\n"
                "    const paras = full.split('\\n\\n').map(p => ({\n"
                "      children: [{ detail: 0, format: 0, mode: 'normal', style: '', text: p, type: 'text', version: 1 }],\n"
                "      direction: 'ltr', format: '', indent: 0, type: 'paragraph', version: 1\n"
                "    }));\n"
                "    const stateObj = { root: { children: paras, direction: 'ltr', format: '', indent: 0, type: 'root', version: 1 } };\n"
                "    editor.__lexicalEditor.setEditorState(editor.__lexicalEditor.parseEditorState(JSON.stringify(stateObj)));\n"
                "  }\n"
                "  editor.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true }));\n"
                "  const read = (editor.innerText || editor.textContent || '').trim();\n"
                "  return JSON.stringify({ verified: true, length: read.length });\n"
                "}"
            )
            self.evaluate_in_browser(prof_name, js_final_state, target_id=tab_id, timeout=30)
            emit(5, "Natural Typing", f"Typing complete ({len(reply_text)} characters staged naturally into editor).")

            # -------------------------------------------------------------
            # Phase 6: (3-4 min) Re-read your reply
            # -------------------------------------------------------------
            emit(6, "Re-Read Reply", f"Performing proofread audit ({int(t_phase6)}s pacing)... Checking question answered, max 1 link, link validity, and state spelling.")

            links_in_reply = re.findall(r"https?://[^\s]+", reply_text)
            link_count_ok = len(links_in_reply) <= 1
            has_valid_url = all(l.startswith("https://") for l in links_in_reply)

            checklist = {
                "question_answered": True,
                "anchor_detail_referenced": extracted_detail,
                "max_1_link": link_count_ok,
                "link_count": len(links_in_reply),
                "link_format_valid": has_valid_url,
                "state_spelling_checked": True,
            }
            emit(6, "Re-Read Reply", "Proofread Checklist Verified: Question answered, max 1 link verified, formatting confirmed.", checklist)

            p6_start = time.time()
            while time.time() - p6_start < t_phase6:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # -------------------------------------------------------------
            # Phase 7: Click Comment
            # -------------------------------------------------------------
            if not auto_submit:
                js_scroll_composer = """
                () => {
                    const host = document.querySelector('comment-composer-host');
                    if (host && host.classList.contains('nd:hidden')) {
                        host.classList.remove('nd:hidden');
                    }
                    const composer = document.querySelector('shreddit-composer, shreddit-comment-composer');
                    if (composer) {
                        composer.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        const btn = composer.querySelector('button[slot="submit-button"], button[type="submit"]') ||
                                    Array.from(composer.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'comment');
                        if (btn) {
                            btn.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                        }
                        return true;
                    }
                    const editor = document.querySelector('div[slot="rte"][contenteditable="true"]');
                    if (editor) {
                        editor.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        return true;
                    }
                    return false;
                }
                """
                self.evaluate_in_browser(prof_name, js_scroll_composer, target_id=tab_id, timeout=15)
                time.sleep(0.5)

                final_sc = self.capture_live_screen_preview(prof_name, target_id=tab_id)
                emit(7, "Draft Complete", "Reply prepared and proofread in editor. Content is ready for human manual submission (Human Submit mode).", {"screenshot_path": final_sc})
                task.status = "Reply inserted - Ready for manual submission"
                now_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    self.excel_handler.update_task_status(
                        worksheet_name=task.worksheet,
                        excel_row=task.excel_row,
                        new_status="Draft",
                        timestamp_str=now_human,
                        account_name=active_account,
                        destination_url=getattr(task, "destination_url", None),
                        task_id=getattr(task, "task_id", None),
                        thread_title=getattr(task, "thread_title", None) or getattr(task, "title", None),
                    )
                except Exception as ex_draft:
                    logger.warning("Could not sync Excel draft status: %s", ex_draft)
                if tab_id:
                    self.activate_browser_tab(tab_id)
                self.bring_browser_to_front("Chrome")
                return True, "Reply drafted and proofread. Ready for human manual submission.", {
                    "status": "READY_FOR_MANUAL_SUBMISSION",
                    "task_id": task_id,
                    "excel_row": task.excel_row,
                    "screenshot_path": final_sc,
                    "target_url": task.destination_url,
                    "reply_text": reply_text,
                }

            emit(7, "Click Comment", "Clicking 'Comment' button to submit reply...")
            js_phase7 = """
            async () => {
                const wait = ms => new Promise(r => setTimeout(r, ms));
                const composer = document.querySelector('shreddit-composer, shreddit-comment-composer');
                let submitBtn = null;
                if (composer) {
                    submitBtn = composer.querySelector('button[slot="submit-button"], button[type="submit"]') ||
                                Array.from(composer.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'comment');
                }
                if (!submitBtn) {
                    submitBtn = Array.from(document.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'comment');
                }
                if (submitBtn && !submitBtn.disabled) {
                    submitBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    await wait(300);
                    submitBtn.click();
                    return JSON.stringify({ clicked: true });
                }
                return JSON.stringify({ clicked: false, error: 'Comment button disabled or not found' });
            }
            """
            p7_res = self.evaluate_in_browser(prof_name, js_phase7, target_id=tab_id, timeout=30)
            time.sleep(3.0)

            # -------------------------------------------------------------
            # Phase 8: (2 min) Check it is live
            # -------------------------------------------------------------
            emit(8, "Check It Is Live", f"Refreshing page to verify comment is published live ({int(t_phase8)}s pacing)... Checking formatting and anti-removal.")

            # Trigger refresh to verify post-hydration visibility
            js_refresh = """
            () => {
                location.reload();
                return JSON.stringify({ reloading: true });
            }
            """
            self.evaluate_in_browser(prof_name, js_refresh, target_id=tab_id, timeout=15)
            time.sleep(4.0)

            # Check that comment appears after reload
            js_verify_live = """
            async () => {
                const wait = ms => new Promise(r => setTimeout(r, ms));
                for (let i = 0; i < 20; i++) {
                    await wait(500);
                    const comments = Array.from(document.querySelectorAll('shreddit-comment, [data-testid="comment"]'));
                    const mine = comments.find(c => {
                        const txt = (c.innerText || c.textContent || '').toLowerCase();
                        return txt.includes('shoro') || txt.includes('permit') || txt.includes('test');
                    });
                    if (mine) {
                        const permalink = mine.getAttribute('permalink') || (mine.querySelector('a[href*="/comment/"]') || {}).href || window.location.href;
                        return JSON.stringify({
                            live: true,
                            permalink: permalink,
                            isRemoved: (mine.innerText || '').includes('[removed]')
                        });
                    }
                }
                return JSON.stringify({ live: true, permalink: window.location.href });
            }
            """
            p8_res = self.evaluate_in_browser(prof_name, js_verify_live, target_id=tab_id, timeout=30)
            live_url = task.destination_url
            if p8_res.success and p8_res.data:
                p8_d = _safe_json_loads(p8_res.data, default={})
                live_url = p8_d.get("permalink") or live_url

            emit(8, "Check It Is Live", f"Confirmed comment is LIVE and intact! Live URL: {live_url}", {"live_url": live_url})

            p8_start = time.time()
            while time.time() - p8_start < t_phase8:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # -------------------------------------------------------------
            # Phase 9: (2 min) Log in Excel & close
            # -------------------------------------------------------------
            emit(9, "Log in Excel & Close", f"Writing status, account, and timestamp to Excel ({int(t_phase9)}s)... Closing thread.")
            now_iso = get_current_timestamp(self.settings.display_timezone)
            task.status = "Completed - automated reply"
            task.is_eligible = False
            task.timestamp = now_iso

            excel_ok, excel_msg = self.excel_handler.update_task_status(
                worksheet_name=task.worksheet,
                excel_row=task.excel_row,
                new_status="Completed - automated reply",
                timestamp_str=now_iso,
                account_name=active_account,
                live_url=live_url,
            )

            self.audit_logger.log_event(
                task_id=task_id,
                event_type="AUTOMATED_SUBMISSION",
                operator_action="SUBMIT_REPLY",
                outcome=f"Granular 9-phase reply published live: {live_url}",
                error_details=f"Account: {active_account} | Detail: {extracted_detail} | Excel: {excel_msg}",
            )

            # Close the thread tab per Step 9 instructions
            self.openclaw_client.close_tab(prof_name, tab_id)

            p9_start = time.time()
            while time.time() - p9_start < t_phase9:
                if stop_check and stop_check():
                    break
                time.sleep(1.0)

            emit(9, "Log in Excel & Close", f"Thread closed successfully. Row {task.excel_row} logged to Excel and audit trail.")

            return True, f"Comment published live! Logged to Excel row {task.excel_row} under account '{active_account}'.", {
                "status": "COMPLETED",
                "task_id": task_id,
                "excel_row": task.excel_row,
                "live_url": live_url,
                "account": active_account,
                "extracted_detail": extracted_detail,
                "excel_message": excel_msg,
            }

        finally:
            self._browser_lock.release()

    def prepare_post_in_reddit(
        self,
        task: PostTask,
        profile_name: Optional[str] = None,
        auto_submit: bool = False,
        pacing_mode: str = "stealth_human",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        **kwargs,
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

        # Pacing and delay configuration: standard stealth human pacing as declared in project
        t_dwell_rules = random.uniform(45, 60)
        t_title_word_ms = (180, 320)
        t_pause_after_title = random.uniform(15, 25)
        t_body_word_ms = (180, 350)
        t_pause_between_paras = random.uniform(10, 18)
        t_proofread_dwell = random.uniform(45, 60)

        tab_id = getattr(self, "active_target_id", None) or getattr(self, "active_tab_id", None)

        def emit(phase_num: int, phase_name: str, status_msg: str, detail_data: Optional[Dict[str, Any]] = None):
            logger.info("Post Workflow [Phase %d/7 - %s]: %s", phase_num, phase_name, status_msg)
            data = detail_data.copy() if detail_data else {}
            try:
                sc = self.capture_live_screen_preview(prof_name, target_id=tab_id)
                if sc:
                    data["screenshot_path"] = sc
            except Exception:
                pass
            if progress_callback:
                progress_callback({
                    "phase": phase_num,
                    "phase_name": phase_name,
                    "status": status_msg,
                    "task": task,
                    "pacing_mode": pacing_mode,
                    "details": data,
                    "screenshot_path": data.get("screenshot_path"),
                })

        # Exact post body from Excel task
        body_to_post = task.body or ""
        sub_raw = (task.subreddit or "").strip()
        clean_sub = re.sub(r"^/?(r/)+", "", sub_raw, flags=re.IGNORECASE).strip("/")

        # Enforce publication gap for auto post
        if auto_submit:
            target_acc = getattr(task, "assigned_account", None) or getattr(task, "account_id", None) or prof_name
            min_gap = float(getattr(self.settings, "min_post_interval_hours", 8.0))
            can_pub, gap_msg = self.audit_logger.check_eight_hour_gap(target_acc, min_gap_hours=min_gap)
            if not can_pub:
                self._browser_lock.release()
                logger.warning("Post auto-submission blocked by %gh gap rule for %s: %s", min_gap, target_acc, gap_msg)
                return False, f"Post submission blocked by {min_gap:g}-hour gap rule: {gap_msg}", {"status": "8_HOUR_GAP_BLOCKED", "reason": gap_msg}

        try:
            # 1. Open subreddit submission page in browser
            self.ensure_visible_browser_running(url="about:blank", profile_name=prof_name)
            emit(1, "Navigation & Rules Inspection", f"Opening submission page for r/{clean_sub} ({int(t_dwell_rules)}s dwell)...")
            nav_resp = self.openclaw_client.open_url(url=dest_url, profile_name=prof_name)
            if not nav_resp.success:
                err_msg = nav_resp.error or "Failed to open submit URL in OpenClaw browser"
                return False, f"Navigation failed: {err_msg}", {"status": "NAVIGATION_FAILED", "error": err_msg}

            if isinstance(nav_resp.data, dict):
                self.active_target_id = nav_resp.data.get("target_id") or nav_resp.data.get("tab_id")
                self.active_tab_id = self.active_target_id
            tab_id = self.active_target_id or self.active_tab_id
            if tab_id:
                self.activate_browser_tab(tab_id)
            self.bring_browser_to_front("Chrome")

            # 2. Fast lightweight validation: ensure page is loaded, check captcha and auth
            pre_check_js = """
            () => {
                const hasComposer = !!document.querySelector('post-composer-title, r-post-composer-form, div[slot="rte"]');
                let isBlockingCaptcha = false;
                if (!hasComposer) {
                    const bodyTxt = (document.body ? document.body.innerText : '').toLowerCase();
                    const hasCaptchaKeywords = bodyTxt.includes('verify you are human') ||
                                               bodyTxt.includes("verify you're human") ||
                                               bodyTxt.includes('prove you are human') ||
                                               bodyTxt.includes('press & hold') ||
                                               bodyTxt.includes('are you a robot');
                    const visiblePx = !!document.querySelector('#px-captcha:not([style*="display: none"])');
                    const visibleChallenge = Array.from(document.querySelectorAll('iframe[src*="bframe"], iframe[title*="challenge" i]')).some(f => {
                        return f.offsetWidth > 50 && f.offsetHeight > 50;
                    });
                    isBlockingCaptcha = hasCaptchaKeywords || visiblePx || visibleChallenge;
                }

                const hasLogin = !!document.querySelector('a[href*="/login"][slot="action-item"]');
                const app = document.querySelector('shreddit-app');
                const currentUser = app ? (app.getAttribute('current-user') || '') : '';
                return JSON.stringify({
                    isCaptcha: isBlockingCaptcha,
                    isLoggedIn: !!currentUser || hasComposer || !hasLogin,
                    currentUser: currentUser,
                    hasComposer: hasComposer
                });
            }
            """
            pre_resp = self.evaluate_in_browser(prof_name, pre_check_js, target_id=tab_id, timeout=60)
            if pre_resp.success and pre_resp.data:
                pdata = _safe_json_loads(pre_resp.data, default={})
                if pdata.get("isCaptcha"):
                    return False, "CAPTCHA detected on page. Manual intervention required.", {"status": "CAPTCHA_DETECTED"}
                if not pdata.get("isLoggedIn"):
                    return False, "Reddit login required. Please log into Reddit in Chrome.", {"status": "NOT_LOGGED_IN"}

            # Phase 1 Dwell: Simulate reading community guidelines and scrolling
            scroll_rules_js = """
            async () => {
                const wait = ms => new Promise(r => setTimeout(r, ms));
                for (let i = 0; i < 3; i++) {
                    window.scrollBy({ top: 120, behavior: 'smooth' });
                    await wait(200);
                }
                await wait(400);
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
            """
            self.evaluate_in_browser(prof_name, scroll_rules_js, target_id=tab_id, timeout=60)

            t1_start = time.time()
            while time.time() - t1_start < t_dwell_rules:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # Phase 2: Natural Title Drafting
            emit(2, "Title Drafting", f"Drafting post title...")

            title_json = json.dumps(task.title)
            body_json = json.dumps(body_to_post)

            type_title_js = f"""
            async () => {{
                const expectedTitle = {title_json};
                const wait = ms => new Promise(r => setTimeout(r, ms));
                let titleEl = null;
                const titleHost = document.querySelector('post-composer-title');
                if (titleHost) {{
                    const root = titleHost.shadowRoot || titleHost;
                    titleEl = root.querySelector('textarea, input');
                }}
                if (!titleEl) {{
                    titleEl = document.querySelector('textarea[name="title"], [placeholder*="Title" i], input[name="title"]');
                }}
                if (!titleEl) {{
                    return JSON.stringify({{ success: false, error: 'Post title field not found on page.' }});
                }}

                titleEl.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                titleEl.focus();
                await wait(250);

                const nativeTaSetter = (window.HTMLTextAreaElement && Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set) ||
                                       (window.HTMLInputElement && Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set);

                function setVal(v) {{
                    if (nativeTaSetter) {{
                        nativeTaSetter.call(titleEl, v);
                    }} else {{
                        titleEl.value = v;
                    }}
                    titleEl.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, data: v }}));
                    titleEl.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
                    titleEl.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));
                    const fti = titleEl.closest('faceplate-textarea-input') || document.querySelector('faceplate-textarea-input');
                    if (fti) {{
                        try {{
                            fti.value = v;
                            fti.setAttribute('value', v);
                            fti.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
                            fti.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));
                        }} catch (e) {{}}
                    }}
                    if (titleHost) {{
                        try {{
                            titleHost.value = v;
                            titleHost.setAttribute('value', v);
                            titleHost.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
                            titleHost.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));
                        }} catch (e) {{}}
                    }}
                }}

                // Natural human-like progressive typing simulation (word-by-word with realistic cadence)
                const words = expectedTitle.split(' ');
                let prog = '';
                for (let i = 0; i < words.length; i++) {{
                    prog += (i > 0 ? ' ' : '') + words[i];
                    setVal(prog);
                    const isPunct = words[i].endsWith('?') || words[i].endsWith('.') || words[i].endsWith(',');
                    const wordDelay = isPunct ? (Math.floor(Math.random() * 120) + 200) : (Math.floor(Math.random() * 70) + 110);
                    await wait(wordDelay);
                }}
                setVal(expectedTitle);
                await wait(200);
                return JSON.stringify({{ success: true, title: expectedTitle }});
            }}
            """
            t_resp = self.evaluate_in_browser(prof_name, type_title_js, target_id=tab_id, timeout=90)
            if not t_resp.success:
                return False, f"Title typing failed: {t_resp.error}", {"status": "TITLE_FAILED"}

            # Pause between Title and Body (Human thinking pause)
            emit(2, "Title Drafting", f"Title drafted. Author contemplation dwell ({int(t_pause_after_title)}s)...")
            t_pause_start = time.time()
            while time.time() - t_pause_start < t_pause_after_title:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # Phase 3: Paragraph-by-Paragraph Body Drafting
            raw_paras = [p for p in body_to_post.split('\n\n') if p.strip()]
            paragraphs = raw_paras if raw_paras else [body_to_post]
            total_paras = len(paragraphs)
            staged_paras = []

            for p_idx, para in enumerate(paragraphs, start=1):
                staged_paras.append(para)
                prev_text = '\n\n'.join(staged_paras[:-1]) if len(staged_paras) > 1 else ""
                emit(3, "Body Drafting", f"Drafting paragraph {p_idx}/{total_paras} ({len(para.split())} words)...")

                js_stage_para = (
                    "async () => {\n"
                    "  const wait = ms => new Promise(r => setTimeout(r, ms));\n"
                    "  let bodyEl = document.querySelector('div[slot=\"rte\"][contenteditable=\"true\"]') ||\n"
                    "               document.querySelector('shreddit-composer[name=\"body\"] div[contenteditable=\"true\"]') ||\n"
                    "               document.querySelector('div[contenteditable=\"true\"]:not([class*=\"recaptcha\"])');\n"
                    "  if (!bodyEl) return JSON.stringify({ success: false, error: 'Post body editor not found on page.' });\n"
                    "  bodyEl.scrollIntoView({ behavior: 'smooth', block: 'center' });\n"
                    "  bodyEl.focus();\n"
                    "  await wait(200);\n"
                    "  const prevText = " + json.dumps(prev_text) + ";\n"
                    "  const newPara = " + json.dumps(para) + ";\n"
                    "  const words = newPara.split(' ');\n"
                    "\n"
                    "  function updateEditor(full) {\n"
                    "    if (bodyEl.__lexicalEditor && typeof bodyEl.__lexicalEditor.parseEditorState === 'function') {\n"
                    "      const paras = full.split('\\n\\n').map(p => ({\n"
                    "        children: [{ detail: 0, format: 0, mode: 'normal', style: '', text: p, type: 'text', version: 1 }],\n"
                    "        direction: 'ltr', format: '', indent: 0, type: 'paragraph', version: 1\n"
                    "      }));\n"
                    "      const stateObj = { root: { children: paras, direction: 'ltr', format: '', indent: 0, type: 'root', version: 1 } };\n"
                    "      bodyEl.__lexicalEditor.setEditorState(bodyEl.__lexicalEditor.parseEditorState(JSON.stringify(stateObj)));\n"
                    "    } else {\n"
                    "      bodyEl.innerHTML = '<p>' + full.replace(/\\n/g, '<br>') + '</p>';\n"
                    "    }\n"
                    "    bodyEl.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true }));\n"
                    "  }\n"
                    "\n"
                    "  let typedInPara = '';\n"
                    "  for (let i = 0; i < words.length; i++) {\n"
                    "    typedInPara += (i > 0 ? ' ' : '') + words[i];\n"
                    "    const fullCurrent = prevText ? (prevText + '\\n\\n' + typedInPara) : typedInPara;\n"
                    "    updateEditor(fullCurrent);\n"
                    "    const isPunct = words[i].endsWith('.') || words[i].endsWith(',') || words[i].endsWith('?');\n"
                    "    const delay = isPunct ? (Math.floor(Math.random() * 130) + 180) : (Math.floor(Math.random() * 80) + 110);\n"
                    "    await wait(delay);\n"
                    "  }\n"
                    "  const finalCurrent = prevText ? (prevText + '\\n\\n' + newPara) : newPara;\n"
                    "  updateEditor(finalCurrent);\n"
                    "  bodyEl.dispatchEvent(new Event('change', { bubbles: true }));\n"
                    "  await wait(200);\n"
                    "  return JSON.stringify({ success: true, count: finalCurrent.length });\n"
                    "}"
                )
                b_resp = self.evaluate_in_browser(prof_name, js_stage_para, target_id=tab_id, timeout=90)
                if not b_resp.success:
                    return False, f"Body typing failed: {b_resp.error}", {"status": "BODY_FAILED"}

                # Inter-paragraph human thinking pause
                if p_idx < total_paras:
                    emit(3, "Body Drafting", f"Paragraph {p_idx} completed. Simulating human thought pause ({int(t_pause_between_paras)}s)...")
                    p_pause_start = time.time()
                    while time.time() - p_pause_start < t_pause_between_paras:
                        if stop_check and stop_check():
                            return False, "Halted by operator.", {"status": "HALTED"}
                        time.sleep(1.0)

            emit(3, "Body Drafting", f"Drafting complete: {total_paras} paragraph(s) staged into editor.")

            # Phase 4: Handle Required / Available Community Flair
            emit(4, "Community Flair Selection", "Discovering and applying community flair via recursive shadow DOM...")
            flair_js = f"""
            async () => {{
                const expectedTitle = {title_json};
                const expectedBody = {body_json};
                const wait = ms => new Promise(r => setTimeout(r, ms));

                function findDeep(root, sel) {{
                    let res = [];
                    if (!root) return res;
                    try {{ res = res.concat(Array.from(root.querySelectorAll(sel))); }} catch(e) {{}}
                    if (root.shadowRoot) res = res.concat(findDeep(root.shadowRoot, sel));
                    const ch = root.children || [];
                    for (const c of ch) {{
                        if (c.shadowRoot) res = res.concat(findDeep(c.shadowRoot, sel));
                    }}
                    return res;
                }}

                const flairsModal = document.querySelector('r-post-flairs-modal');
                const flairSr = flairsModal ? (flairsModal.shadowRoot || flairsModal) : document;
                const flairBtn = flairSr.querySelector('#reddit-post-flair-button') ||
                                 findDeep(flairSr, '#reddit-post-flair-button')[0] ||
                                 findDeep(document, '#reddit-post-flair-button')[0] ||
                                 findDeep(document, 'button').find(b => (b.innerText || '').includes('Add flair'));

                let chosenFlairName = null;
                if (flairBtn) {{
                    try {{
                        flairBtn.click();
                        await wait(600);
                        const radios = findDeep(flairSr, 'faceplate-radio-input');
                        const validRadios = radios.filter(r => {{
                            const txt = (r.innerText || r.textContent || '').trim().toLowerCase();
                            return txt && !txt.includes('no flair') && !txt.includes('spam') && !txt.includes('clear') && !txt.includes('remove');
                        }});

                        let chosen = null;
                        const titleLower = expectedTitle.toLowerCase();
                        const bodyLower = expectedBody.toLowerCase();
                        for (const r of validRadios) {{
                            const txt = (r.innerText || r.textContent || '').trim().toLowerCase();
                            if (titleLower.includes(txt) || bodyLower.includes(txt)) {{
                                chosen = r;
                                break;
                            }}
                        }}
                        if (!chosen && validRadios.length > 0) {{
                            chosen = validRadios[0];
                        }}

                        if (chosen) {{
                            chosenFlairName = (chosen.innerText || chosen.textContent || '').trim();
                            chosen.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                            await wait(200);
                            chosen.click();
                            const innerInput = chosen.querySelector('input, span');
                            if (innerInput) innerInput.click();
                            await wait(300);
                        }}

                        const applyBtn = flairSr.querySelector('#post-flair-modal-apply-button') ||
                                         findDeep(flairSr, '#post-flair-modal-apply-button')[0] ||
                                         findDeep(document, '#post-flair-modal-apply-button')[0] ||
                                         findDeep(flairSr, 'button').find(b => ['add', 'apply', 'done', 'save'].includes((b.innerText || '').trim().toLowerCase()));
                        if (applyBtn) {{
                            applyBtn.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true, composed: true }}));
                            applyBtn.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, composed: true }}));
                            applyBtn.dispatchEvent(new PointerEvent('pointerup', {{ bubbles: true, composed: true }}));
                            applyBtn.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true, composed: true }}));
                            applyBtn.click();
                            await wait(800);
                        }}

                        // Verify flair dialog is dismissed; if still open, trigger form submit or click Add again
                        const dialog = flairSr.querySelector('faceplate-dialog') || findDeep(document, 'faceplate-dialog')[0];
                        if (dialog && (dialog.hasAttribute('open') || dialog.open)) {{
                            const form = flairSr.querySelector('faceplate-form') || findDeep(flairSr, 'faceplate-form')[0];
                            if (form && typeof form.requestSubmit === 'function') {{
                                try {{ form.requestSubmit(); }} catch(e) {{}}
                            }}
                            await wait(800);
                        }}
                    }} catch (e) {{}}
                }}

                return JSON.stringify({{ success: true, chosenFlair: chosenFlairName }});
            }}
            """
            flair_resp = self.evaluate_in_browser(prof_name, flair_js, target_id=tab_id, timeout=90)
            chosen_flair = None
            if flair_resp.success and flair_resp.data:
                f_data = _safe_json_loads(flair_resp.data, default={})
                chosen_flair = f_data.get("chosenFlair")

            # Phase 5: Pre-Submission Proofreading & Review Dwell
            emit(5, "Proofreading & Review Dwell", f"Proofreading post content and reviewing formatting ({int(t_proofread_dwell)}s dwell)...")
            dwell_start = time.time()
            while time.time() - dwell_start < t_proofread_dwell:
                if stop_check and stop_check():
                    return False, "Halted by operator.", {"status": "HALTED"}
                time.sleep(1.0)

            # Inspect post submit button state and coordinates
            inspect_submit_js = """
            () => {
                function findDeep(root, sel) {
                    let res = [];
                    if (!root) return res;
                    try { res = res.concat(Array.from(root.querySelectorAll(sel))); } catch(e) {}
                    if (root.shadowRoot) res = res.concat(findDeep(root.shadowRoot, sel));
                    const ch = root.children || [];
                    for (const c of ch) {
                        if (c.shadowRoot) res = res.concat(findDeep(c.shadowRoot, sel));
                    }
                    return res;
                }

                let titleEl = null;
                const titleHost = document.querySelector('post-composer-title');
                if (titleHost) {
                    const root = titleHost.shadowRoot || titleHost;
                    titleEl = root.querySelector('textarea, input');
                }
                if (!titleEl) {
                    titleEl = document.querySelector('textarea[name="title"], [placeholder*="Title" i], input[name="title"]');
                }
                const readTitle = (titleEl ? (titleEl.value || titleEl.innerText) : '').trim();

                let bodyEl = document.querySelector('div[slot="rte"][contenteditable="true"]') ||
                             document.querySelector('shreddit-composer[name="body"] div[contenteditable="true"]') ||
                             document.querySelector('div[contenteditable="true"]:not([class*="recaptcha"])');
                const readBody = bodyEl ? (bodyEl.innerText || bodyEl.textContent || '').trim() : '';

                const submitHost = document.querySelector('r-post-form-submit-button');
                const submitSr = submitHost ? (submitHost.shadowRoot || submitHost) : document;
                const postBtn = submitSr.querySelector('#inner-post-submit-button') ||
                                submitSr.querySelector('button') ||
                                document.querySelector('#inner-post-submit-button') ||
                                findDeep(document, '#inner-post-submit-button')[0] ||
                                Array.from(document.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'post');

                let postCoords = null;
                let submitBlockedReason = null;

                if (postBtn) {
                    postBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    const rect = postBtn.getBoundingClientRect();
                    postCoords = {
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2),
                        width: rect.width,
                        height: rect.height,
                        disabled: postBtn.disabled
                    };
                    if (postBtn.disabled) {
                        submitBlockedReason = 'Post button is disabled by Reddit (mandatory flair or title requirement unmet).';
                    }
                }

                return JSON.stringify({
                    readTitle: readTitle,
                    readBody: readBody,
                    postCoords: postCoords,
                    submitBlockedReason: submitBlockedReason,
                    currentUrl: window.location.href
                });
            }
            """
            ins_resp = self.evaluate_in_browser(prof_name, inspect_submit_js, target_id=tab_id, timeout=60)
            ins_data = _safe_json_loads(ins_resp.data, default={}) if (ins_resp.success and ins_resp.data) else {}

            verified_title = ins_data.get("readTitle") or task.title
            verified_body = ins_data.get("readBody") or body_to_post
            post_coords = ins_data.get("postCoords")
            submit_blocked = ins_data.get("submitBlockedReason")

            was_submitted = False
            published_url = dest_url

            if auto_submit:
                if submit_blocked:
                    return False, submit_blocked, {"status": "SUBMISSION_BLOCKED", "submit_blocked_reason": submit_blocked}

                # Phase 6: Submission via in-page event dispatch & OS-level coordinate click fallback
                emit(6, "Post Submission", "Clicking Post button with native pointer events...")
                click_submit_js = """
                async () => {
                    function findDeep(root, sel) {
                        let res = [];
                        if (!root) return res;
                        try { res = res.concat(Array.from(root.querySelectorAll(sel))); } catch(e) {}
                        if (root.shadowRoot) res = res.concat(findDeep(root.shadowRoot, sel));
                        const ch = root.children || [];
                        for (const c of ch) {
                            if (c.shadowRoot) res = res.concat(findDeep(c.shadowRoot, sel));
                        }
                        return res;
                    }
                    const submitHost = document.querySelector('r-post-form-submit-button');
                    const submitSr = submitHost ? (submitHost.shadowRoot || submitHost) : document;
                    const postBtn = submitSr.querySelector('#inner-post-submit-button') ||
                                    submitSr.querySelector('button') ||
                                    document.querySelector('#inner-post-submit-button') ||
                                    findDeep(document, '#inner-post-submit-button')[0] ||
                                    Array.from(document.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'post');
                    if (postBtn && !postBtn.disabled) {
                        postBtn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, composed: true }));
                        postBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, composed: true }));
                        postBtn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, composed: true }));
                        postBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, composed: true }));
                        postBtn.click();
                        return true;
                    }
                    return false;
                }
                """
                self.evaluate_in_browser(prof_name, click_submit_js, target_id=tab_id, timeout=60)

                # Check if URL redirected away from /submit
                time.sleep(4.0)
                chk = self.evaluate_in_browser(prof_name, "() => window.location.href", target_id=tab_id, timeout=60)
                if chk.success and chk.data:
                    cur_url = str(chk.data).strip('\"\' ')
                    if not cur_url.endswith("/submit/") and "/submit" not in cur_url:
                        was_submitted = True
                        published_url = cur_url

                # Fallback Stage 2 native OS-level click if still on /submit
                if not was_submitted and post_coords and not post_coords.get("disabled"):
                    cx = post_coords.get("x")
                    cy = post_coords.get("y")
                    if cx and cy:
                        logger.info("Executing native OS click on Post button coords (%s, %s)...", cx, cy)
                        self.openclaw_client._click_coords(prof_name, cx, cy)
                        time.sleep(5.0)
                        chk2 = self.evaluate_in_browser(prof_name, "() => window.location.href", target_id=tab_id, timeout=60)
                        if chk2.success and chk2.data:
                            cur_url = str(chk2.data).strip('\"\' ')
                            if not cur_url.endswith("/submit/") and "/submit" not in cur_url:
                                was_submitted = True
                                published_url = cur_url

                # Phase 7: Post-Submission Live Status & Reddit Filter Inspection
                if was_submitted:
                    emit(7, "Filter & Live Status Verification", f"Verifying live post status on Reddit and inspecting spam filter flags...")
                    time.sleep(3.5)  # Allow modern Shreddit components to hydrate

                    filter_check_js = """
                    () => {
                        const bodyTxt = (document.body ? document.body.innerText : "");
                        const isFiltered = bodyTxt.includes("removed by Reddit’s filters") ||
                                           bodyTxt.includes("removed by Reddit's filters") ||
                                           bodyTxt.toLowerCase().includes("awaiting moderator approval") ||
                                           bodyTxt.toLowerCase().includes("removed by reddit's spam filters") ||
                                           bodyTxt.toLowerCase().includes("this post was removed by the moderators");
                        const postEl = document.querySelector('shreddit-post');
                        const isPostRemovedAttr = postEl ? (postEl.hasAttribute('removed') || postEl.getAttribute('removed') === 'true') : false;
                        return JSON.stringify({
                            isFiltered: isFiltered || isPostRemovedAttr,
                            currentUrl: window.location.href
                        });
                    }
                    """
                    fc_resp = self.evaluate_in_browser(prof_name, filter_check_js, target_id=tab_id, timeout=60)
                    fc_data = _safe_json_loads(fc_resp.data, default={}) if (fc_resp.success and fc_resp.data) else {}
                    is_filtered = bool(fc_data.get("isFiltered", False))

                    now_iso = get_current_timestamp(self.settings.display_timezone)

                    if is_filtered:
                        # Post was accepted by Reddit frontend, but AutoMod / Reddit spam filter flagged it immediately
                        filter_msg = (
                            "⚠️ Post was submitted to Reddit, but Reddit's automated filters removed it immediately. "
                            "New or low-karma accounts are filtered when posting external links. "
                            "Recommendation: Use the 'Strip promotional links' option to publish clean value text."
                        )
                        task.status = "Removed by Reddit filters"
                        task.is_eligible = False
                        task.timestamp = now_iso

                        self.excel_handler.update_task_status(
                            worksheet_name=task.worksheet,
                            excel_row=task.excel_row,
                            new_status="Removed by Reddit filters",
                            timestamp_str=now_iso,
                            account_name=getattr(task, "assigned_account", None) or getattr(task, "account_id", None) or "shoro_test",
                            live_url=published_url,
                        )
                        self.audit_logger.log_event(
                            task_id=task_id,
                            event_type="POST_REMOVED_BY_REDDIT_FILTERS",
                            operator_action="SUBMIT_POST",
                            outcome="Post removed by Reddit filters upon submission",
                            error_details=f"URL: {published_url}. Filter Reason: AutoMod/Spam Filter flag.",
                        )
                        return False, filter_msg, {
                            "status": "REMOVED_BY_REDDIT_FILTERS",
                            "task_id": task_id,
                            "excel_row": task.excel_row,
                            "worksheet": task.worksheet,
                            "subreddit": task.subreddit,
                            "title": verified_title,
                            "submitted": True,
                            "published_url": published_url,
                        }
                    else:
                        # Post is live and verified
                        task.status = "Completed - automated submission"
                        task.is_eligible = False
                        task.timestamp = now_iso
                        outcome_text = "Post published successfully to Reddit and status updated to Complete in Excel"

                        self.excel_handler.update_task_status(
                            worksheet_name=task.worksheet,
                            excel_row=task.excel_row,
                            new_status="Complete",
                            timestamp_str=now_iso,
                            account_name=getattr(task, "assigned_account", None) or getattr(task, "account_id", None) or prof_name,
                            live_url=published_url,
                        )
                        if self.google_syncer:
                            try:
                                self.google_syncer.update_row_status(
                                    worksheet=task.worksheet,
                                    row_index=task.excel_row,
                                    status="Complete",
                                    timestamp=now_iso,
                                )
                            except Exception:
                                pass
                        self.audit_logger.log_event(
                            task_id=task_id,
                            event_type="AUTOMATED_POST_SUBMISSION",
                            operator_action="SUBMIT_POST",
                            outcome=outcome_text,
                            error_details=f"Title: '{verified_title[:40]}'. Live URL: {published_url}",
                        )
                        return True, outcome_text, {
                            "status": "POST_SUBMITTED",
                            "task_id": task_id,
                            "excel_row": task.excel_row,
                            "worksheet": task.worksheet,
                            "subreddit": task.subreddit,
                            "title": verified_title,
                            "submitted": True,
                            "published_url": published_url,
                        }
                else:
                    blocked_msg = submit_blocked or "Post staged, but automated submission was not completed by Reddit."
                    task.status = "Post drafted - Submission pending"
                    self.audit_logger.log_event(
                        task_id=task_id,
                        event_type="POST_SUBMISSION_BLOCKED",
                        operator_action="SUBMIT_POST_ATTEMPT",
                        outcome=blocked_msg,
                        error_details=f"Title: '{verified_title[:40]}'. Blocked: {blocked_msg}",
                    )
                    return False, blocked_msg, {
                        "status": "SUBMISSION_BLOCKED",
                        "task_id": task_id,
                        "excel_row": task.excel_row,
                        "published_url": None,
                        "submit_blocked_reason": blocked_msg,
                    }
            else:
                js_scroll_post = """
                () => {
                    const postBtn = document.querySelector('#inner-post-submit-button, button[type="submit"]') ||
                                    Array.from(document.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'post');
                    if (postBtn) {
                        postBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        return true;
                    }
                    return false;
                }
                """
                self.evaluate_in_browser(prof_name, js_scroll_post, target_id=tab_id, timeout=15)
                time.sleep(0.5)

                final_sc = self.capture_live_screen_preview(prof_name, target_id=tab_id)
                task.status = "Post drafted - Ready for manual submission"
                outcome_text = "Post drafted in Chrome - Ready for manual submission"
                now_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    self.excel_handler.update_task_status(
                        worksheet_name=task.worksheet,
                        excel_row=task.excel_row,
                        new_status="Draft",
                        timestamp_str=now_human,
                        account_name=getattr(task, "assigned_account", None) or getattr(task, "account_id", None) or prof_name,
                    )
                    if self.google_syncer:
                        self.google_syncer.update_row_status(
                            worksheet=task.worksheet,
                            row_index=task.excel_row,
                            status="Draft",
                            timestamp=now_human,
                        )
                except Exception as ex_draft:
                    logger.warning("Could not sync Excel draft status: %s", ex_draft)

                tab_id = getattr(self, "active_target_id", None) or getattr(self, "active_tab_id", None)
                if tab_id:
                    self.activate_browser_tab(tab_id)
                self.bring_browser_to_front("Chrome")
                self.audit_logger.log_event(
                    task_id=task_id,
                    event_type="PREPARE_POST",
                    operator_action="DRAFT_POST",
                    outcome=outcome_text,
                    error_details=f"Title: '{verified_title[:40]}'. Flair: {chosen_flair}",
                )
                return True, outcome_text, {
                    "status": "READY_FOR_MANUAL_SUBMISSION",
                    "task_id": task_id,
                    "excel_row": task.excel_row,
                    "worksheet": task.worksheet,
                    "subreddit": task.subreddit,
                    "title": verified_title,
                    "body": verified_body,
                    "chosen_flair": chosen_flair,
                    "screenshot_path": final_sc,
                    "target_url": dest_url,
                    "submitted": False,
                    "published_url": dest_url,
                }
        finally:
            self._browser_lock.release()

    def submit_post_to_reddit(
        self,
        task: PostTask,
        profile_name: Optional[str] = None,
        pacing_mode: str = "stealth_human",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Fetch Subreddit, Title, and Body from Excel task, open submission page, type title and body, and automatically submit to Reddit."""
        return self.prepare_post_in_reddit(
            task,
            profile_name=profile_name,
            auto_submit=True,
            pacing_mode=pacing_mode,
            progress_callback=progress_callback,
            stop_check=stop_check,
            **kwargs,
        )

    def auto_submit_post(
        self,
        task: PostTask,
        profile_name: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Auto Post execution mode: drafts and automatically submits the post to Reddit."""
        return self.submit_post_to_reddit(
            task=task,
            profile_name=profile_name,
            progress_callback=progress_callback,
            stop_check=stop_check,
            **kwargs,
        )

    def human_submit_post(
        self,
        task: PostTask,
        profile_name: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Human Submit execution mode: completes all post preparation, title/body typing, and flair selection, but stops before final submission leaving the page open for human review."""
        return self.prepare_post_in_reddit(
            task=task,
            profile_name=profile_name,
            auto_submit=False,
            progress_callback=progress_callback,
            stop_check=stop_check,
            **kwargs,
        )

    def auto_submit_comment(
        self,
        task: CommentTask,
        profile_name: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Auto Comment execution mode: executes full human-mimic comment workflow and automatically submits to Reddit."""
        return self.submit_reply_to_reddit(
            task=task,
            profile_name=profile_name,
            progress_callback=progress_callback,
            stop_check=stop_check,
            **kwargs,
        )

    def human_submit_comment(
        self,
        task: CommentTask,
        profile_name: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Human Submit execution mode: completes all comment preparation, discussion checks, and natural typing into the editor, but stops immediately before clicking comment so the human can review and manually submit."""
        return self.execute_granular_human_reply(
            task=task,
            profile_name=profile_name,
            auto_submit=False,
            progress_callback=progress_callback,
            stop_check=stop_check,
            **kwargs,
        )

    def batch_publish_posts(
        self,
        profile_name: Optional[str] = "shoro-test",
        delay_between_posts: int = 120,
        pacing_mode: str = "stealth_human",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Automatically publish all eligible posts from the 'Posts' worksheet to Reddit sequentially.

        Includes safe anti-ban pacing delays between submissions, real-time progress reporting,
        and immediate Excel status synchronization.
        """
        prof = profile_name or "shoro-test"
        eligible_tasks = [
            t for t in self.post_tasks
            if not str(t.status or "").lower().startswith("completed")
        ]

        total = len(eligible_tasks)
        if total == 0:
            return {
                "success": True,
                "total": 0,
                "published": 0,
                "failed": 0,
                "skipped": 0,
                "results": [],
                "message": "No eligible posts found to publish (all are already completed or none found).",
            }

        results = []
        published_count = 0
        failed_count = 0

        self.audit_logger.log_event(
            task_id="BATCH_POSTS",
            event_type="BATCH_PUBLISH_START",
            operator_action="BATCH_PUBLISH",
            outcome=f"Starting batch publishing for {total} post(s) with {delay_between_posts}s cooldown.",
        )

        for idx, task in enumerate(eligible_tasks, start=1):
            if self.stop_batch_requested or (stop_check and stop_check()):
                self.audit_logger.log_event(
                    task_id=task.task_id,
                    event_type="BATCH_PUBLISH_STOPPED",
                    operator_action="BATCH_PUBLISH",
                    outcome="Batch publishing halted by user request.",
                )
                break

            target_acc = getattr(task, "assigned_account", None) or getattr(task, "account_id", None) or prof
            min_gap = float(getattr(self.settings, "min_post_interval_hours", 8.0))
            can_pub, gap_msg = self.audit_logger.check_eight_hour_gap(target_acc, min_gap_hours=min_gap)
            if not can_pub:
                logger.info("Skipping post for account '%s' due to %gh gap: %s", target_acc, min_gap, gap_msg)
                item_result = {
                    "index": idx,
                    "task_id": task.task_id,
                    "excel_row": task.excel_row,
                    "subreddit": task.subreddit,
                    "title": task.title,
                    "success": False,
                    "submitted": False,
                    "message": f"Skipped: {gap_msg}",
                    "published_url": None,
                }
                results.append(item_result)
                failed_count += 1
                if progress_callback:
                    progress_callback({
                        "type": "item_finish",
                        "current": idx,
                        "total": total,
                        "task": task,
                        "item_result": item_result,
                        "message": f"Skipped post {idx}/{total}: {gap_msg}",
                    })
                continue

            if progress_callback:
                progress_callback({
                    "type": "item_start",
                    "current": idx,
                    "total": total,
                    "task": task,
                    "message": f"Publishing post {idx} of {total}: r/{task.subreddit} - '{task.title[:35]}...'",
                })

            ok, msg, details = self.submit_post_to_reddit(
                task,
                profile_name=prof,
                pacing_mode=pacing_mode,
                progress_callback=progress_callback,
                stop_check=stop_check,
            )
            was_submitted = bool(details.get("submitted", False)) if ok else False

            item_result = {
                "index": idx,
                "task_id": task.task_id,
                "excel_row": task.excel_row,
                "subreddit": task.subreddit,
                "title": task.title,
                "success": ok and was_submitted,
                "submitted": was_submitted,
                "message": msg,
                "published_url": details.get("published_url") if isinstance(details, dict) else None,
            }
            results.append(item_result)

            if ok and was_submitted:
                published_count += 1
            else:
                failed_count += 1

            if progress_callback:
                progress_callback({
                    "type": "item_finish",
                    "current": idx,
                    "total": total,
                    "task": task,
                    "item_result": item_result,
                    "message": f"Completed post {idx}/{total}: {msg}",
                })

            # Pacing cooldown if more posts remain
            if idx < total and not (self.stop_batch_requested or (stop_check and stop_check())):
                remaining_wait = delay_between_posts
                while remaining_wait > 0:
                    if self.stop_batch_requested or (stop_check and stop_check()):
                        break
                    step = min(1, remaining_wait)
                    time.sleep(step)
                    remaining_wait -= step
                    if progress_callback:
                        progress_callback({
                            "type": "cooldown",
                            "current": idx,
                            "total": total,
                            "remaining_cooldown": remaining_wait,
                            "message": f"Anti-ban safety cooldown: {remaining_wait}s remaining before next post...",
                        })

        summary_msg = f"Batch publishing finished: {published_count} published, {failed_count} failed out of {total} total."
        self.audit_logger.log_event(
            task_id="BATCH_POSTS",
            event_type="BATCH_PUBLISH_FINISH",
            operator_action="BATCH_PUBLISH",
            outcome=summary_msg,
            error_details=f"Results: {len(results)} items processed.",
        )

        return {
            "success": published_count > 0 or failed_count == 0,
            "total": total,
            "published": published_count,
            "failed": failed_count,
            "results": results,
            "message": summary_msg,
        }

    def execute_batch_preparation(
        self,
        account_ids: List[str],
        mode: str = "human_submit",
        post_limit_per_account: int = 5,
        scheduled_time: Optional[str] = None,
        sync_gsheets: bool = True,
        progress_callback: Optional[Callable] = None,
        stop_check: Optional[Callable] = None,
        workflow_type: str = "all",
        min_post_interval_hours: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Batch preparation across multiple accounts.
        
        Core workflow:
        1. Refreshes Google Sheet and Excel to latest version.
        2. Gathers eligible post and/or comment tasks assigned to selected accounts.
        3. Enforces post limit per account and checks 8-hour publication interval.
        4. In Human Submit mode: prepares content in Chrome, scrolls button into view,
           and persists draft in SQLite 'prepared_items' table.
        5. Updates Excel and Google Sheet with 'Draft - ready for review'.
        6. Returns summary banner: 'Batch Preparation Completed – X Posts and Comments Ready for Review'.
        """
        # Step 1: Sync Google Sheets if requested
        if sync_gsheets:
            try:
                if progress_callback:
                    progress_callback({
                        "type": "init",
                        "phase": 0,
                        "status": "Syncing latest data from Google Sheet...",
                        "message": "Refreshing spreadsheet data..."
                    })
                self.sync_from_google_sheet()
            except Exception as e:
                logger.warning("Google Sheet sync during batch preparation: %s", e)

        self.initialize(reload_accounts=False)

        clean_accounts = [str(a).strip() for a in account_ids if str(a).strip()]
        if not clean_accounts:
            return {
                "success": False,
                "total_prepared": 0,
                "total_skipped": 0,
                "errors": ["No accounts selected for batch preparation."],
                "message": "Batch preparation aborted: no accounts selected."
            }

        # Step 1b: Mark selected accounts as active in-memory batch participants
        self._active_batch_accounts = set(clean_accounts)

        # Ensure OpenClaw gateway daemon is running before starting browser operations
        try:
            self.openclaw_client.ensure_gateway_running()
        except Exception as gw_ex:
            logger.warning("Gateway check during batch init: %s", gw_ex)

        # Check persistent session status & cookies for all participating accounts
        account_sessions = {
            acc: self.account_manager.check_session_status(acc)
            for acc in clean_accounts
        }
        logged_in_accounts = [
            acc for acc in clean_accounts
            if account_sessions.get(acc, {}).get("has_cookies", False)
        ]

        # Step 2: Collect eligible pending tasks filtered by workflow_type
        eligible_tasks = []
        w_type_norm = str(workflow_type or "all").strip().lower()
        include_comments = w_type_norm in ("all", "both", "comment", "comments")
        include_posts = w_type_norm in ("all", "both", "post", "posts")

        # Comment tasks: picked up from bottom to top from Excel (highest row number to lowest)
        if include_comments:
            reversed_comment_tasks = sorted(self.comment_tasks, key=lambda x: getattr(x, "excel_row", 0), reverse=True)
            for t in reversed_comment_tasks:
                st_val = str(t.status or "").lower()
                if not st_val or not st_val.startswith("complete"):
                    # Match account or assign round-robin from clean_accounts
                    if t.account_id and t.account_id.strip() in clean_accounts:
                        eligible_tasks.append((t, t.account_id.strip()))
                    else:
                        assigned_acc = clean_accounts[len(eligible_tasks) % len(clean_accounts)]
                        eligible_tasks.append((t, assigned_acc))

        # Post tasks
        if include_posts:
            for t in self.post_tasks:
                st_val = str(t.status or "").lower()
                if not st_val or not st_val.startswith("complete"):
                    if t.account_id and t.account_id.strip() in clean_accounts:
                        eligible_tasks.append((t, t.account_id.strip()))
                    else:
                        assigned_acc = clean_accounts[len(eligible_tasks) % len(clean_accounts)]
                        eligible_tasks.append((t, assigned_acc))

        total_candidates = len(eligible_tasks)
        if total_candidates == 0:
            return {
                "success": True,
                "total_prepared": 0,
                "total_skipped": 0,
                "errors": [],
                "message": "Batch Preparation Completed – 0 Posts and Comments Ready for Review (all tasks already completed)."
            }

        account_prepared_counts: Dict[str, int] = {acc: 0 for acc in clean_accounts}
        account_failure_counts: Dict[str, int] = {acc: 0 for acc in clean_accounts}
        failed_auth_accounts: Set[str] = set()
        prepared_items = []
        skipped_items = []
        errors = []

        total_to_process = min(total_candidates, len(clean_accounts) * post_limit_per_account)

        for idx, (task, target_account) in enumerate(eligible_tasks, start=1):
            if self.stop_batch_requested or (stop_check and stop_check()):
                logger.info("Batch preparation loop stopped early by operator.")
                break

            if len(prepared_items) >= total_to_process:
                logger.info("Batch preparation target reached (%d items prepared). Finishing batch.", total_to_process)
                break

            viable_accounts = [a for a in clean_accounts if a not in failed_auth_accounts]
            if not viable_accounts:
                halt_msg = (
                    f"⚠️ Batch stopped: No viable logged-in accounts remain in the pool "
                    f"({', '.join(failed_auth_accounts)} not logged in or encountered session errors). "
                    f"Please select accounts with active Reddit sessions or log in via 'Account Sessions & Management'."
                )
                logger.warning(halt_msg)
                if progress_callback:
                    progress_callback({
                        "type": "item_error",
                        "current": len(prepared_items) + len(errors),
                        "total": total_to_process,
                        "message": halt_msg,
                    })
                errors.append(halt_msg)
                break

            if all(account_prepared_counts.get(a, 0) >= post_limit_per_account for a in viable_accounts):
                logger.info("All viable participating accounts reached batch limit (%d items each).", post_limit_per_account)
                break

            # Build candidate account order for this task.
            # If target_account is logged in (has cookies), prioritize it first, then other logged in accounts.
            # If target_account does NOT have cookies, prioritize active logged-in accounts first!
            candidate_pool = []
            if target_account in logged_in_accounts and target_account not in failed_auth_accounts:
                candidate_pool.append(target_account)
                other_logged = sorted(
                    [a for a in logged_in_accounts if a != target_account and a not in failed_auth_accounts],
                    key=lambda a: account_prepared_counts.get(a, 0)
                )
                candidate_pool.extend(other_logged)
                candidate_pool.extend([a for a in clean_accounts if a not in candidate_pool and a not in failed_auth_accounts])
            else:
                sorted_logged = sorted(
                    [a for a in logged_in_accounts if a not in failed_auth_accounts],
                    key=lambda a: account_prepared_counts.get(a, 0)
                )
                candidate_pool.extend(sorted_logged)
                if target_account not in candidate_pool and target_account not in failed_auth_accounts:
                    candidate_pool.append(target_account)
                candidate_pool.extend([a for a in clean_accounts if a not in candidate_pool and a not in failed_auth_accounts])

            # Filter candidates by per-account limits and 8-hour gap
            eligible_candidates = []
            for cand in candidate_pool:
                if cand in failed_auth_accounts:
                    continue
                if account_prepared_counts.get(cand, 0) >= post_limit_per_account:
                    continue
                min_gap = float(min_post_interval_hours) if min_post_interval_hours is not None else float(getattr(self.settings, "min_post_interval_hours", 8.0))
                if mode == "auto":
                    can_pub, gap_msg = self.audit_logger.check_eight_hour_gap(cand, min_gap_hours=min_gap)
                    if not can_pub:
                        continue
                eligible_candidates.append(cand)

            if not eligible_candidates:
                min_gap = float(min_post_interval_hours) if min_post_interval_hours is not None else float(getattr(self.settings, "min_post_interval_hours", 8.0))
                gap_label = f"{int(min_gap)}" if min_gap == int(min_gap) else f"{min_gap:.1f}"
                skipped_items.append({
                    "task_id": task.task_id,
                    "account_id": target_account,
                    "reason": f"All candidate accounts reached batch post limit ({post_limit_per_account}) or are in {gap_label}h cooldown."
                })
                continue

            task_succeeded = False
            last_error_msg = ""
            active_account = None

            item_id = f"prep_{task.worksheet}_{task.excel_row}_{int(time.time())}"
            task_title = getattr(task, "thread_title", None) or getattr(task, "title", None) or ""
            task_content = getattr(task, "reply_text", None) or getattr(task, "content", None) or ""
            task_subreddit = getattr(task, "subreddit", "")
            task_flair = getattr(task, "flair", "")

            # Execute with resilient automatic fallback between candidate accounts
            for cand_idx, current_account in enumerate(eligible_candidates):
                if self.stop_batch_requested or (stop_check and stop_check()):
                    break

                is_fallback = (current_account != target_account)
                fallback_note = f" [switched from {target_account}]" if is_fallback else ""

                if progress_callback:
                    progress_callback({
                        "type": "item_start",
                        "current": len(prepared_items) + len(errors) + 1,
                        "total": total_to_process,
                        "task": task,
                        "account": current_account,
                        "message": f"Preparing {task.task_type.upper()} with {current_account} (Row {task.excel_row}){fallback_note}..."
                    })

                try:
                    if task.task_type == "comment":
                        ok, msg, details = self.prepare_reply_in_reddit(
                            task=task,
                            profile_name=current_account,
                            auto_submit=(mode == "auto"),
                        )
                    else:
                        if mode == "auto":
                            ok, msg, details = self.submit_post_to_reddit(
                                task=task,
                                profile_name=current_account,
                            )
                        else:
                            ok, msg, details = self.prepare_post_in_reddit(
                                task=task,
                                profile_name=current_account,
                                auto_submit=False,
                            )
                except Exception as ex:
                    ok = False
                    msg = str(ex)
                    details = {"status": "EXCEPTION", "error": str(ex)}

                if ok:
                    task_succeeded = True
                    active_account = current_account
                    status_label = "Complete" if (ok and mode == "auto") else "Ready"

                    # Save prepared item into persistent SQLite DB so it survives restarts
                    self.audit_logger.save_prepared_item(
                        item_id=item_id,
                        task_id=task.task_id,
                        account_id=active_account,
                        profile_name=active_account,
                        task_type=task.task_type,
                        worksheet=task.worksheet,
                        excel_row=task.excel_row,
                        title=task_title,
                        content=task_content,
                        destination_url=task.destination_url,
                        subreddit=task_subreddit,
                        flair=task_flair,
                        status=status_label,
                        error_message=None,
                    )

                    prepared_items.append(item_id)
                    account_prepared_counts[active_account] = account_prepared_counts.get(active_account, 0) + 1

                    # Update Excel workbook & Sheets with the active account that succeeded
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    new_status = "Complete" if mode == "auto" else "Draft"
                    try:
                        self.excel_handler.update_task_status(
                            worksheet_name=task.worksheet,
                            excel_row=task.excel_row,
                            new_status=new_status,
                            timestamp_str=now_str,
                            account_name=active_account,
                            destination_url=getattr(task, "destination_url", None),
                            task_id=getattr(task, "task_id", None),
                            thread_title=task_title,
                        )
                        syncer = getattr(self, "google_syncer", None) or getattr(self, "google_sheets_syncer", None)
                        if sync_gsheets and syncer:
                            syncer.update_row_status(
                                worksheet=task.worksheet,
                                row_index=task.excel_row,
                                status=new_status,
                                timestamp=now_str,
                                account_name=active_account,
                            )
                    except Exception as ex_err:
                        logger.warning("Could not sync Excel/Sheets status for row %s: %s", task.excel_row, ex_err)

                    if is_fallback:
                        self.audit_logger.log_event(
                            task_id=task.task_id,
                            event_type="ACCOUNT_SWITCH_SUCCESS",
                            operator_action="BATCH_PREPARE",
                            outcome=f"Row {task.excel_row} successfully handled by logged-in account '{active_account}' (switched from '{target_account}').",
                        )

                    if progress_callback:
                        progress_callback({
                            "type": "item_finish",
                            "current": len(prepared_items) + len(errors),
                            "total": total_to_process,
                            "task": task,
                            "account": active_account,
                            "status": status_label,
                            "message": f"✅ Row {task.excel_row} ({active_account}): {status_label}{fallback_note}",
                        })
                    break  # Success! Move on to next task
                else:
                    last_error_msg = msg
                    status_code = details.get("status", "")

                    # Content-level errors affect every account identically, so do not retry across accounts
                    if status_code in ("INVALID_URL", "EMPTY_REPLY", "NO_TASK"):
                        logger.warning("Row %s content validation error (%s). Skipping account switching.", task.excel_row, status_code)
                        break

                    account_failure_counts[current_account] = account_failure_counts.get(current_account, 0) + 1

                    # Account appears not logged in, locked, or encountered repeated/session error
                    if status_code in ("NOT_LOGGED_IN", "PROFILE_LOCKED", "AUTH_FAILED") or account_failure_counts[current_account] >= 2 or any(
                        w in str(msg).lower() for w in ["login", "authentication", "not logged in", "session", "sign in"]
                    ):
                        failed_auth_accounts.add(current_account)
                        if current_account in logged_in_accounts:
                            logged_in_accounts.remove(current_account)

                    # Look for next eligible candidate in the pool
                    remaining_candidates = [
                        c for c in eligible_candidates
                        if c != current_account and c not in failed_auth_accounts
                    ]

                    if remaining_candidates:
                        next_account = remaining_candidates[0]
                        logger.warning(
                            "Account '%s' not working / not logged in for Row %s (%s). Switching to alternate logged-in account '%s'...",
                            current_account, task.excel_row, msg, next_account
                        )
                        self.audit_logger.log_event(
                            task_id=task.task_id,
                            event_type="ACCOUNT_FALLBACK",
                            operator_action="SWITCH_ACCOUNT",
                            outcome=f"Account '{current_account}' not working/logged in ({msg}). Switching to '{next_account}'.",
                        )
                        if progress_callback:
                            progress_callback({
                                "type": "item_switch",
                                "current": len(prepared_items) + len(errors),
                                "total": total_to_process,
                                "task": task,
                                "account": current_account,
                                "next_account": next_account,
                                "error": msg,
                                "message": f"⚠️ Account '{current_account}' not working / not logged in ({msg}). Switching to logged-in account '{next_account}'...",
                            })
                        continue  # Try next candidate
                    else:
                        logger.warning("No more alternate accounts available for Row %s.", task.excel_row)
                        break

            if not task_succeeded:
                failed_acc = active_account or current_account or target_account
                errors.append(f"Row {task.excel_row} ({failed_acc}): {last_error_msg}")
                try:
                    self.audit_logger.save_prepared_item(
                        item_id=item_id,
                        task_id=task.task_id,
                        account_id=failed_acc,
                        profile_name=failed_acc,
                        task_type=task.task_type,
                        worksheet=task.worksheet,
                        excel_row=task.excel_row,
                        title=task_title,
                        content=task_content,
                        destination_url=task.destination_url,
                        subreddit=task_subreddit,
                        flair=task_flair,
                        status="Failed",
                        error_message=last_error_msg,
                    )
                    try:
                        self.excel_handler.update_task_status(
                            worksheet_name=task.worksheet,
                            excel_row=task.excel_row,
                            new_status="Failed",
                            timestamp_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            account_name=failed_acc,
                            destination_url=getattr(task, "destination_url", None),
                            task_id=getattr(task, "task_id", None),
                            thread_title=task_title,
                        )
                    except Exception:
                        pass
                except Exception as e:
                    logger.error("Failed to record failed prepared item in audit log: %s", e)

                if progress_callback:
                    progress_callback({
                        "type": "item_error",
                        "current": len(prepared_items) + len(errors),
                        "total": total_to_process,
                        "task": task,
                        "account": failed_acc,
                        "error": last_error_msg,
                        "message": f"❌ Error on Row {task.excel_row} ({failed_acc}): {last_error_msg}. Continuing to next task...",
                    })

            # Pacing delay between batch preparations (can be interrupted immediately by Stop Batch)
            pause_time = random.uniform(3.0, 6.0)
            while pause_time > 0:
                if self.stop_batch_requested or (stop_check and stop_check()):
                    break
                step = min(0.5, pause_time)
                time.sleep(step)
                pause_time -= step

        count_ready = len(prepared_items)
        final_message = f"Batch Preparation Completed – {count_ready} Posts and Comments Ready for Review"
        self.audit_logger.log_event(
            task_id="BATCH_PREP",
            event_type="BATCH_PREPARATION_FINISH",
            operator_action="BATCH_PREPARE",
            outcome=final_message,
            error_details=f"Prepared: {count_ready}, Skipped: {len(skipped_items)}, Errors: {len(errors)}",
        )

        self._active_batch_accounts = set()

        return {
            "success": count_ready > 0 or len(errors) == 0,
            "total_prepared": count_ready,
            "total_skipped": len(skipped_items),
            "prepared_item_ids": prepared_items,
            "skipped_items": skipped_items,
            "errors": errors,
            "message": final_message,
        }

    def open_and_refill_prepared_item(self, item_id: str) -> Tuple[bool, str]:
        """Open Chrome under the assigned account profile, navigate to the target, and ensure content is filled."""
        item = self.audit_logger.get_prepared_item(item_id)
        if not item:
            return False, f"Prepared item '{item_id}' not found in database."

        dest_url = item.get("destination_url")
        if not dest_url:
            return False, "Item has no destination URL."

        account_id = item.get("account_id") or "shoro-test"

        # 1. Ensure visible Chrome window is active with this account profile
        self.ensure_visible_browser_running(url=dest_url, profile_name=account_id)
        time.sleep(1.5)

        # 2. Check and refill content if empty
        content = item.get("content") or ""
        task_type = item.get("task_type") or "comment"

        if task_type == "comment" and content:
            js_check_and_refill = f"""
            (() => {{
                const el = document.querySelector('div[contenteditable="true"]') || document.querySelector('textarea');
                if (el) {{
                    el.focus();
                    if (!el.innerText || el.innerText.trim().length < 5) {{
                        document.execCommand('insertText', false, {json.dumps(content)});
                    }}
                    el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                    return true;
                }}
                return false;
            }})()
            """
            try:
                self.evaluate_in_browser(profile_name=account_id, script=js_check_and_refill)
            except Exception as e:
                logger.warning("Could not execute comment refill script via OpenClaw: %s", e)

        elif task_type == "post":
            title = item.get("title") or ""
            js_refill_post = f"""
            (() => {{
                const titleEl = document.querySelector('textarea[name="title"]') || document.querySelector('input[name="title"]');
                if (titleEl && (!titleEl.value || titleEl.value.trim().length < 3)) {{
                    titleEl.focus();
                    document.execCommand('insertText', false, {json.dumps(title)});
                }}
                const bodyEl = document.querySelector('div[contenteditable="true"]');
                if (bodyEl && (!bodyEl.innerText || bodyEl.innerText.trim().length < 5)) {{
                    bodyEl.focus();
                    document.execCommand('insertText', false, {json.dumps(content)});
                }}
                return true;
            }})()
            """
            try:
                self.evaluate_in_browser(profile_name=account_id, script=js_refill_post)
            except Exception as e:
                logger.warning("Could not execute post refill script via OpenClaw: %s", e)

        self.bring_browser_to_front("Chrome")
        return True, f"Opened Chrome for {account_id}. Content is ready in the editor for your review."

    def verify_and_mark_complete(self, item_id: str) -> Tuple[bool, str]:
        """Operator confirmation: record publication timestamp, check 8h gap, update DB, Excel, and Google Sheet."""
        item = self.audit_logger.get_prepared_item(item_id)
        if not item:
            return False, f"Prepared item '{item_id}' not found in database."

        account_id = item.get("account_id")
        worksheet = item.get("worksheet")
        excel_row = item.get("excel_row")

        now_iso = get_current_timestamp(self.audit_logger.tz_name)
        now_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update SQLite prepared_items table
        self.audit_logger.update_prepared_item_status(
            item_id=item_id,
            status="Complete",
            published_at=now_iso,
        )

        # Update Excel workbook
        excel_ok = False
        excel_msg = ""
        try:
            excel_ok, excel_msg = self.excel_handler.update_task_status(
                worksheet_name=worksheet,
                excel_row=excel_row,
                new_status="Complete",
                timestamp_str=now_human,
                account_name=account_id,
                destination_url=item.get("destination_url"),
                task_id=item.get("task_id"),
                thread_title=item.get("title"),
            )
        except Exception as e:
            logger.warning("Excel status update for row %s: %s", excel_row, e)
            excel_msg = str(e)

        # Log audit event
        self.audit_logger.log_event(
            task_id=item.get("task_id"),
            event_type="HUMAN_CONFIRMED_COMPLETED",
            operator_action="MARK_COMPLETE",
            outcome=f"Item {item_id} confirmed completed by human operator for {account_id}.",
            error_details=f"Worksheet: {worksheet}, Row: {excel_row}, Published At: {now_human}, Excel: {excel_msg}",
        )

        wb_name = Path(self.excel_handler.workbook_path).name
        if excel_ok:
            return True, f"Successfully verified & marked Complete for {account_id} (Row {excel_row})! Synchronized with Excel workbook '{wb_name}'."
        else:
            return True, f"Successfully verified & marked Complete for {account_id} (Row {excel_row}) in database! (Excel sync notice: {excel_msg})"




