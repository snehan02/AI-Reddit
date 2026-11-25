"""Browser Manager for opening assigned Chrome profiles and destinations for human review.

Strict compliance enforcement:
- Navigation only (opens page for operator to review).
- Automated submission or form filling is STRICTLY disabled.
- No CAPTCHA/login bypass.
- Each account uses an isolated user-data-directory.
"""

from pathlib import Path
import logging
import os
import shutil
import subprocess
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def find_system_chrome() -> Optional[str]:
    """Find local Google Chrome executable on Windows."""
    # 1. Check standard PATH
    path_hit = shutil.which("chrome") or shutil.which("google-chrome")
    if path_hit and os.path.exists(path_hit):
        return path_hit

    # 2. Check Windows Registry App Paths (most reliable across all Windows machines & drives)
    if os.name == "nt":
        try:
            import winreg
            reg_keys = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            ]
            for root, subkey in reg_keys:
                try:
                    with winreg.OpenKey(root, subkey) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        if val and os.path.exists(val):
                            return val
                except Exception:
                    pass
        except Exception:
            pass

    # 3. Standard Windows install locations
    common_locations = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for loc in common_locations:
        if os.path.exists(loc):
            return loc
    return None


class BrowserReviewManager:
    """Manages browser sessions for human review."""

    def __init__(self, test_mode: bool = True):
        self.test_mode = test_mode
        self._active_process: Optional[subprocess.Popen] = None
        self._active_playwright = None
        self._active_browser_context = None

    def open_for_review(
        self,
        url: str,
        user_data_dir: str | Path,
        profile_name: Optional[str] = None,
        prefer_playwright: bool = False,
    ) -> Tuple[bool, str]:
        """Open the target Reddit URL in the designated Chrome profile for human review.
        
        Args:
            url: Valid web URL to navigate to.
            user_data_dir: Isolated profile directory path for this account.
            profile_name: Friendly profile label.
            prefer_playwright: Whether to attempt Playwright before system Chrome launcher.
            
        Returns:
            (success, message)
        """
        if not url or not url.strip().startswith("http"):
            return False, f"Invalid destination URL: '{url}'"

        target_dir = Path(user_data_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Opening destination for review [Profile: %s, DataDir: %s, URL: %s, TestMode: %s]",
            profile_name or target_dir.name,
            target_dir,
            url,
            self.test_mode,
        )

        # In Test Mode, remind that submission is disabled
        if self.test_mode:
            logger.info("TEST MODE: Destination opened for review only. Automated submission is disabled.")

        # Method 1: Playwright persistent context (if preferred and available)
        if prefer_playwright:
            try:
                from playwright.sync_api import sync_playwright
                self._active_playwright = sync_playwright().start()
                self._active_browser_context = self._active_playwright.chromium.launch_persistent_context(
                    user_data_dir=str(target_dir),
                    headless=False,
                    channel="chrome",
                    args=["--no-first-run", "--no-default-browser-check"],
                )
                page = self._active_browser_context.new_page()
                page.goto(url, timeout=30000)
                return True, f"Opened {url} in Chrome profile via Playwright for human review."
            except Exception as exc:
                logger.warning("Playwright persistent launch failed (%s). Falling back to system Chrome.", exc)
                self.close_active()

        # Method 2: System Chrome executable launch
        chrome_exe = find_system_chrome()
        if not chrome_exe:
            return False, "Google Chrome executable was not found on this system."

        cmd = [
            chrome_exe,
            f"--user-data-dir={target_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]

        try:
            self._active_process = subprocess.Popen(cmd)
            return True, f"Opened {url} in Chrome profile '{profile_name or target_dir.name}' for human review."
        except Exception as exc:
            err = f"Failed to launch Chrome browser: {exc}"
            logger.error(err)
            return False, err

    def close_active(self) -> None:
        """Safely close any active browser process or Playwright context."""
        if self._active_browser_context:
            try:
                self._active_browser_context.close()
            except Exception:
                pass
            self._active_browser_context = None

        if self._active_playwright:
            try:
                self._active_playwright.stop()
            except Exception:
                pass
            self._active_playwright = None

        if self._active_process:
            try:
                self._active_process.terminate()
            except Exception:
                pass
            self._active_process = None

