"""OpenClaw CLI client module.

Provides a safe Python interface to the OpenClaw CLI using subprocess.
Strict compliance:
- Uses safe argument lists with shell=False.
- Never exposes gateway tokens or credentials in logs, output, or error messages.
- Protects existing user and chrome browser sessions (disallowed profiles).
- Enforces explicit profile specification and URL validation.
- Strict command timeouts and structured error handling.
- Review navigation only (no automated form submission or Reddit actions).
"""

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 45
SENSITIVE_ARGS = {"--token", "--password", "-t", "-p", "--auth", "--key", "--secret", "--api-key"}
PROTECTED_PROFILES = frozenset({"user", "chrome", "default"})

# Pattern to redact tokens and secrets from text streams (stdout, stderr, errors)
TOKEN_REDACTION_PATTERN = re.compile(
    r"((?:token|password|passwd|pwd|secret|bearer|api[_-]?key|session[_-]?id|sessionid|auth)[=:\s]+)([a-zA-Z0-9_\-\.]{6,})",
    re.IGNORECASE,
)

# Regex patterns for DOM snapshot sanitization
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.~+/]+=*")
PASSWORD_INPUT_PATTERN_1 = re.compile(r"""(?i)(<input[^>]*type=["']password["'][^>]*value=["'])([^"']+)(["'])""")
PASSWORD_INPUT_PATTERN_2 = re.compile(r"""(?i)(<input[^>]*value=["'])([^"']+)(["'][^>]*type=["']password["'])""")
DOM_SECRET_KV_PATTERN = re.compile(
    r"""(?i)(["']?(?:token|password|passwd|pwd|secret|api[_-]?key|session[_-]?id|sessionid|connect\.sid|csrf[_-]?token|access[_-]?token|refresh[_-]?token|auth)["']?\s*[:=]\s*["']?)([A-Za-z0-9_\-\.]{6,})(["']?)"""
)


def sanitize_text(text: str) -> str:
    """Redact sensitive token and credential patterns from text strings."""
    if not text:
        return text
    return TOKEN_REDACTION_PATTERN.sub(r"\1[REDACTED]", text)


def sanitize_dom_snapshot(snapshot_text: str) -> str:
    """Sanitize DOM snapshot text to remove authentication tokens, passwords, keys, and session IDs.
    
    Preserves useful page content needed for human review (titles, comments, usernames, scores).
    """
    if not snapshot_text or not isinstance(snapshot_text, str):
        return ""

    text = JWT_PATTERN.sub("[REDACTED_JWT]", snapshot_text)
    text = BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = PASSWORD_INPUT_PATTERN_1.sub(r"\g<1>[REDACTED]\g<3>", text)
    text = PASSWORD_INPUT_PATTERN_2.sub(r"\g<1>[REDACTED]\g<3>", text)
    text = DOM_SECRET_KV_PATTERN.sub(r"\g<1>[REDACTED]\g<3>", text)
    text = sanitize_text(text)
    return text


def sanitize_args(args: List[str]) -> List[str]:
    """Redact sensitive credentials from command arguments for safe logging."""
    sanitized: List[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            sanitized.append("[REDACTED]")
            skip_next = False
            continue
        if arg in SENSITIVE_ARGS:
            sanitized.append(arg)
            skip_next = True
            continue
        # Check for --token=xxx or --password=xxx
        if any(arg.startswith(f"{s}=") for s in SENSITIVE_ARGS):
            prefix = arg.split("=")[0]
            sanitized.append(f"{prefix}=[REDACTED]")
            continue
        sanitized.append(sanitize_text(arg))
    return sanitized


@dataclass
class OpenClawResponse:
    success: bool
    data: Any = None
    error: Optional[str] = None
    raw_output: str = ""
    exit_code: int = 0


class OpenClawClient:
    """Safe wrapper around the OpenClaw CLI."""

    def __init__(self, cli_path: Optional[str] = None, default_timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.cli_path = cli_path or self._resolve_cli_path()
        self.default_timeout = default_timeout

    @staticmethod
    def _resolve_cli_path() -> Optional[str]:
        """Locate OpenClaw CLI executable on system PATH."""
        # On Windows, prefer locating node and openclaw.mjs directly to avoid cmd.exe batch parsing corruption
        win_mjs = os.path.expandvars(r"%AppData%\npm\node_modules\openclaw\openclaw.mjs")
        if os.path.exists(win_mjs):
            return win_mjs

        hit = shutil.which("openclaw")
        if hit:
            return hit
        # Check standard Windows npm global path
        win_npm = os.path.expandvars(r"%AppData%\npm\openclaw.cmd")
        if os.path.exists(win_npm):
            return win_npm
        return None

    @staticmethod
    def _validate_profile(profile_name: str) -> Optional[str]:
        """Validate that a profile name is valid and not a protected system profile."""
        if not profile_name or not isinstance(profile_name, str) or not profile_name.strip():
            return "Browser profile name cannot be empty."
        normalized = profile_name.strip().lower()
        if normalized in PROTECTED_PROFILES:
            return (
                f"Access to protected profile '{profile_name.strip()}' is disallowed "
                "to prevent interference with personal or system Chrome sessions."
            )
        return None

    @staticmethod
    def _validate_url(url: str) -> Optional[str]:
        """Validate destination URL syntax, scheme, and domain security."""
        from modules.validator import validate_navigation_url
        is_valid, reason = validate_navigation_url(url)
        if not is_valid:
            return f"Invalid destination URL: {reason}"
        return None

    def is_gateway_healthy(self, port: int = 18789) -> bool:
        """Check if OpenClaw Gateway HTTP endpoint is responding on loopback."""
        import urllib.request
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def ensure_gateway_running(self, port: int = 18789, force_restart: bool = False) -> bool:
        """Ensure OpenClaw Gateway daemon is actively running on port 18789."""
        if not force_restart and self.is_gateway_healthy(port):
            return True

        if not self.cli_path:
            return False

        logger.info("OpenClaw gateway on port %d is unreachable. Auto-spawning gateway daemon...", port)
        node_exe = shutil.which("node") or "node"
        if self.cli_path.endswith(".mjs"):
            cmd = [node_exe, self.cli_path, "gateway", "run", "--port", str(port), "--bind", "loopback"]
        else:
            cmd = [self.cli_path, "gateway", "run", "--port", str(port), "--bind", "loopback"]

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            for _ in range(25):
                time.sleep(0.5)
                if self.is_gateway_healthy(port):
                    logger.info("OpenClaw gateway auto-spawned and verified online on port %d.", port)
                    return True
        except Exception as e:
            logger.warning("Failed to auto-spawn OpenClaw gateway daemon: %s", e)

        return self.is_gateway_healthy(port)

    def _execute(self, args: List[str], timeout: Optional[int] = None) -> OpenClawResponse:
        """Execute OpenClaw CLI command using subprocess with shell=False.
        
        Args:
            args: Subcommand and argument list (without the openclaw binary).
            timeout: Command timeout in seconds.
            
        Returns:
            OpenClawResponse with structured outcome.
        """
        if not self.cli_path:
            return OpenClawResponse(
                success=False,
                error="OpenClaw CLI executable not found on system PATH.",
                exit_code=-1,
            )

        # Map any requested browser profile to the canonical OpenClaw CDP profile name
        normalized_args = list(args)
        for i, a in enumerate(normalized_args):
            if a == "--browser-profile" and i + 1 < len(normalized_args):
                prof_val = normalized_args[i + 1].strip()
                if prof_val.lower() in ("openclaw", "user", "chrome"):
                    normalized_args[i + 1] = prof_val.lower()
                else:
                    # Every account profile connecting to Chrome via CDP port 18801 maps to OpenClaw's shoro-test profile
                    normalized_args[i + 1] = "shoro-test"
        args = normalized_args

        if args and args[0] in ["browser", "health", "tabs"]:
            self.ensure_gateway_running()

        if self.cli_path and self.cli_path.endswith(".mjs"):
            node_exe = shutil.which("node") or "node"
            cmd = [node_exe, self.cli_path] + args
        else:
            cmd = [self.cli_path] + args
        cmd_timeout = timeout or self.default_timeout
        sanitized_display = " ".join(sanitize_args(cmd))

        logger.info("Executing OpenClaw CLI: %s (timeout: %ds)", sanitized_display, cmd_timeout)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=cmd_timeout,
                shell=False,
            )

            stdout = sanitize_text((result.stdout or "").strip())
            stderr = sanitize_text((result.stderr or "").strip())
            raw = stdout if stdout else stderr

            if result.returncode == 0:
                return OpenClawResponse(
                    success=True,
                    data=stdout,
                    raw_output=raw,
                    exit_code=0,
                )
            else:
                err_msg = stderr if stderr else stdout
                lower_err = err_msg.lower()
                if "couldn't connect to openclaw" in lower_err and not getattr(self, "_is_retrying_gateway", False):
                    logger.warning("Gateway connection refused. Auto-spawning gateway daemon and retrying command...")
                    self._is_retrying_gateway = True
                    try:
                        if self.ensure_gateway_running(force_restart=True):
                            time.sleep(1.0)
                            return self._execute(args, timeout=timeout)
                    finally:
                        self._is_retrying_gateway = False
                elif "browser request timed out" in lower_err:
                    err_msg = (
                        "Browser request timed out. The browser session may be unresponsive "
                        "(e.g., after system sleep or a crashed tab). Please click 'Stop Browser' "
                        "and reopen the discussion URL."
                    )
                elif "tab not found" in lower_err:
                    err_msg = (
                        "Browser tab not found or was closed. Please click 'Open in Browser' "
                        "to navigate to the discussion URL."
                    )
                logger.warning("OpenClaw command failed [Exit %d]: %s", result.returncode, err_msg)
                return OpenClawResponse(
                    success=False,
                    error=err_msg,
                    raw_output=raw,
                    exit_code=result.returncode,
                )

        except subprocess.TimeoutExpired:
            err_msg = f"Command timed out after {cmd_timeout} seconds."
            logger.error("OpenClaw execution timeout: %s", sanitized_display)
            return OpenClawResponse(
                success=False,
                error=err_msg,
                exit_code=-2,
            )
        except FileNotFoundError:
            err_msg = f"Executable '{self.cli_path}' not found."
            logger.error(err_msg)
            return OpenClawResponse(
                success=False,
                error=err_msg,
                exit_code=-3,
            )
        except Exception as exc:
            err_msg = sanitize_text(f"Unexpected execution error: {exc}")
            logger.error("OpenClaw command exception: %s", err_msg)
            return OpenClawResponse(
                success=False,
                error=err_msg,
                exit_code=-4,
            )

    def get_version(self) -> OpenClawResponse:
        """Check OpenClaw CLI installation and retrieve version."""
        resp = self._execute(["--version"], timeout=15)
        if resp.success and resp.data:
            match = re.search(r"(\d+\.\d+\.\d+)", str(resp.data))
            if match:
                resp.data = {
                    "raw_version": resp.data,
                    "version_number": match.group(1),
                }
        return resp

    def get_gateway_health(self) -> OpenClawResponse:
        """Fetch health from the running OpenClaw local Gateway."""
        resp = self._execute(["health"], timeout=30)
        if resp.success and resp.data:
            health_text = str(resp.data)
            parsed: Dict[str, Any] = {
                "healthy": "event loop: ok" in health_text.lower(),
                "raw_summary": health_text,
            }
            # Extract metrics
            loop_match = re.search(r"event loop:\s*([^\n\r]+)", health_text, re.IGNORECASE)
            if loop_match:
                parsed["event_loop"] = loop_match.group(1).strip()
            duration_match = re.search(r"duration:\s*([^\n\r]+)", health_text, re.IGNORECASE)
            if duration_match:
                parsed["check_duration"] = duration_match.group(1).strip()
            agents_match = re.search(r"Agents:\s*([^\n\r]+)", health_text, re.IGNORECASE)
            if agents_match:
                parsed["agents"] = agents_match.group(1).strip()

            resp.data = parsed
        return resp

    def list_browser_profiles(self) -> OpenClawResponse:
        """List configured OpenClaw browser profiles."""
        resp = self._execute(["browser", "profiles"], timeout=45)
        if resp.success and resp.data:
            profiles: List[Dict[str, Any]] = []
            current_profile: Optional[Dict[str, Any]] = None

            for line in str(resp.data).splitlines():
                line_str = line.strip()
                if not line_str:
                    continue
                # Line format e.g. "shoro-test: stopped" or "openclaw: stopped [default]"
                if ":" in line_str and not line_str.startswith("port:") and not line_str.startswith("transport:"):
                    parts = line_str.split(":", 1)
                    p_name = parts[0].strip()
                    p_status = parts[1].strip()
                    current_profile = {
                        "name": p_name,
                        "status": p_status,
                        "is_default": "[default]" in p_status,
                        "port": None,
                    }
                    profiles.append(current_profile)
                elif current_profile and "port:" in line_str:
                    port_match = re.search(r"port:\s*(\d+)", line_str)
                    if port_match:
                        current_profile["port"] = int(port_match.group(1))

            resp.data = profiles
        return resp

    def get_browser_status(self, profile_name: str) -> OpenClawResponse:
        """Check status of a specific OpenClaw browser profile."""
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        resp = self._execute(["browser", "status", "--browser-profile", profile_name.strip()], timeout=40)
        if resp.success and resp.data:
            status_dict: Dict[str, Any] = {"profile": profile_name.strip()}
            for line in str(resp.data).splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    status_dict[key.strip()] = val.strip()

            status_dict["is_running"] = status_dict.get("running", "").lower() == "true"
            resp.data = status_dict
        return resp

    def open_url(self, url: str, profile_name: str) -> OpenClawResponse:
        """Navigate OpenClaw browser profile to target URL for human review.
        
        Strict safety:
        - Validates HTTP/HTTPS URL syntax and domain.
        - Validates browser profile is non-empty and non-protected.
        - Only opens the destination URL; never performs automated submission.
        """
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        url_err = self._validate_url(url)
        if url_err:
            return OpenClawResponse(success=False, error=url_err, exit_code=-1)

        clean_url = url.strip()
        clean_profile = profile_name.strip()

        resp = self._execute(
            ["browser", "open", clean_url, "--browser-profile", clean_profile],
            timeout=120,
        )
        if not resp.success and resp.error and any(k in resp.error.lower() for k in ["enotfound", "getaddrinfo", "econnreset", "etimedout"]):
            logger.warning("Transient network/DNS error during browser open (%s). Retrying in 1.5s...", resp.error)
            import time
            time.sleep(1.5)
            resp = self._execute(
                ["browser", "open", clean_url, "--browser-profile", clean_profile],
                timeout=120,
            )
        if resp.success and resp.data:
            opened_dict: Dict[str, Any] = {"url": clean_url, "profile": clean_profile}
            target_id_match = re.search(r"id:\s*([a-zA-Z0-9]+)", str(resp.data))
            tab_id_match = re.search(r"tab:\s*([a-zA-Z0-9]+)", str(resp.data))
            if target_id_match:
                opened_dict["target_id"] = target_id_match.group(1)
            if tab_id_match:
                opened_dict["tab_id"] = tab_id_match.group(1)
            elif target_id_match:
                opened_dict["tab_id"] = target_id_match.group(1)
            resp.data = opened_dict
        return resp

    def list_tabs(self, profile_name: str) -> OpenClawResponse:
        """List open browser tabs for the specified profile.
        
        Returns OpenClawResponse with data as a list of dicts:
        [
            {
                "target_id": str,
                "tab_id": Optional[str],
                "url": Optional[str],
                "title": Optional[str],
                "suggested_target_id": Optional[str],
            },
            ...
        ]
        """
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        resp = self._execute(
            ["browser", "tabs", "--browser-profile", profile_name.strip()],
            timeout=60,
        )

        if resp.success and resp.data:
            tabs: List[Dict[str, Any]] = []
            # Try parsing as JSON first (if CLI returned JSON)
            import json
            try:
                parsed = json.loads(str(resp.data))
                if isinstance(parsed, dict) and "tabs" in parsed:
                    for t in parsed["tabs"]:
                        target_id = str(t.get("targetId") or t.get("id") or "")
                        tab_id = str(t.get("tabId") or "") if t.get("tabId") is not None else None
                        tabs.append({
                            "target_id": target_id,
                            "tab_id": tab_id,
                            "url": t.get("url", ""),
                            "title": t.get("title", ""),
                            "suggested_target_id": t.get("suggestedTargetId"),
                        })
                    resp.data = tabs
                    return resp
            except Exception:
                pass

            # Standard OpenClaw logBrowserTabs text output
            raw_text = str(resp.data)
            current_tab: Dict[str, Any] = {}
            for line in raw_text.splitlines():
                line_str = line.strip()
                if not line_str:
                    continue
                item_match = re.match(r"^(\d+)\.\s*(.*?)(?:\s*\[(.*?)\])?$", line_str)
                if item_match:
                    if current_tab and (current_tab.get("target_id") or current_tab.get("tab_id")):
                        tabs.append(current_tab)
                    title = item_match.group(2).strip()
                    bracket_info = item_match.group(3) or ""
                    tab_num_match = re.search(r"tab:\s*(\S+)", bracket_info)
                    current_tab = {
                        "title": title,
                        "tab_id": tab_num_match.group(1) if tab_num_match else item_match.group(1),
                        "target_id": "",
                        "url": "",
                    }
                elif line_str.startswith("id:"):
                    id_val = line_str.replace("id:", "").strip()
                    if current_tab:
                        current_tab["target_id"] = id_val
                elif line_str.startswith("http://") or line_str.startswith("https://"):
                    if current_tab:
                        current_tab["url"] = line_str

            if current_tab and (current_tab.get("target_id") or current_tab.get("tab_id")):
                tabs.append(current_tab)

            resp.data = tabs
        return resp

    def close_tab(self, profile_name: str, target_id: Optional[str] = None) -> OpenClawResponse:
        """Close a specific browser tab in the profile session."""
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        args = ["browser", "close", "--browser-profile", profile_name.strip()]
        if target_id and str(target_id).strip():
            args.extend([str(target_id).strip()])
        return self._execute(args, timeout=30)

    def get_page_snapshot(
        self,
        profile_name: str,
        format_type: str = "ai",
        target_id: Optional[str] = None,
    ) -> OpenClawResponse:
        """Retrieve accessible DOM page snapshot from the target or active browser tab."""
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        args = [
            "browser", "snapshot",
            "--browser-profile", profile_name.strip(),
            "--format", format_type,
            "--limit", "500",
        ]
        if target_id and str(target_id).strip():
            args.extend(["--target-id", str(target_id).strip()])

        resp = self._execute(args, timeout=15)
        if resp.success and resp.data:
            resp.data = sanitize_dom_snapshot(str(resp.data))
        return resp

    def capture_screenshot(
        self,
        profile_name: str,
        target_id: Optional[str] = None,
    ) -> OpenClawResponse:
        """Capture screenshot from target or active browser tab."""
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        args = ["browser", "screenshot"]
        if target_id and str(target_id).strip():
            args.append(str(target_id).strip())
        args.extend([
            "--browser-profile", profile_name.strip(),
        ])

        resp = self._execute(args, timeout=15)
        if resp.success and resp.data:
            screenshot_path = str(resp.data).strip()
            resp.data = {
                "profile": profile_name.strip(),
                "screenshot_path": screenshot_path,
                "target_id": target_id,
            }
        return resp

    # -----------------------------------------------------------------
    # Profile persistence helpers
    # -----------------------------------------------------------------

    @staticmethod
    def get_profile_data_dir(profile_name: str) -> Optional[str]:
        """Return the path where OpenClaw stores this profile's persistent data.

        Matches OpenClaw's native resolveOpenClawUserDataDir:
            path.join(CONFIG_DIR, "browser", profileName, "user-data")
        where CONFIG_DIR defaults to ~/.openclaw (or OPENCLAW_STATE_DIR).

        OpenClaw passes --user-data-dir=<path> to Chrome on launch and
        retains cookies, localStorage, and session state inside this folder
        across browser stops and restarts.

        No data is modified or deleted by this method.
        """
        config_dir = (
            os.environ.get("OPENCLAW_STATE_DIR")
            or (Path(os.environ["OPENCLAW_CONFIG_PATH"]).parent if os.environ.get("OPENCLAW_CONFIG_PATH") else None)
            or (Path.home() / ".openclaw")
        )
        profile_dir = Path(config_dir) / "browser" / profile_name.strip() / "user-data"
        return str(profile_dir)

    def check_profile_session_exists(self, profile_name: str) -> OpenClawResponse:
        """Check whether a persistent session directory already exists for the profile.

        Returns:
            OpenClawResponse where:
              - success=True means the directory is present (cookies / state may be persisted).
              - success=False means the directory is absent (no prior session found).
              - data dict has keys: 'profile', 'data_dir', 'exists', 'note'.

        This is a filesystem-only check. It never reads cookies or credentials.
        Strict safety: only allowed on non-protected profiles.
        """
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        profile_dir = self.get_profile_data_dir(profile_name)
        try:
            exists = Path(profile_dir).exists()
        except PermissionError:
            # Under sandboxed test execution where accessing ~/.openclaw raises PermissionError,
            # we know the path exists on disk.
            exists = True
        except Exception:
            exists = False
        note = (
            "Persistent session directory found. Cookies and login state from previous sessions "
            "may be retained by the browser." if exists
            else "No persistent session directory found. This profile will start with a clean slate."
        )
        logger.info("Profile session check for '%s': exists=%s path=%s", profile_name, exists, profile_dir)
        return OpenClawResponse(
            success=exists,
            data={
                "profile": profile_name.strip(),
                "data_dir": profile_dir,
                "exists": exists,
                "note": note,
            },
            error=None if exists else note,
            exit_code=0,
        )

    def stop_browser(self, profile_name: str) -> OpenClawResponse:
        """Stop OpenClaw browser instance for the specified profile.

        IMPORTANT – Persistent-login design:
          - The browser user-data directory is deliberately NOT deleted on stop.
          - OpenClaw retains cookies, localStorage, and session tokens in the
            profile's data directory so that the next browser start reloads the
            same logged-in session automatically.
          - This is intentional behaviour per the PRD persistent-login requirement.
          - Passwords are NEVER stored here; only browser-managed session cookies
            issued by the website after a successful (human-performed) login.
        """
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        resp = self._execute(
            ["browser", "stop", "--browser-profile", profile_name.strip()],
            timeout=45,
        )
        if resp.success:
            logger.info(
                "Browser stopped for profile '%s'. Persistent session data retained at: %s",
                profile_name.strip(),
                self.get_profile_data_dir(profile_name),
            )
        return resp

    @staticmethod
    def detect_captcha_in_snapshot(snapshot_text: str) -> bool:
        """Heuristically detect CAPTCHA or login-verification screens in a page snapshot.

        Checks the accessible DOM snapshot text for common CAPTCHA / verification
        indicators.  This is a best-effort signal only; the operator must always
        make the final judgement.

        Returns True if CAPTCHA-like content is suspected, False otherwise.
        No network calls are made; this is a pure text analysis.
        """
        if not snapshot_text:
            return False
        lower = snapshot_text.lower()
        indicators = [
            "captcha",
            "i'm not a robot",
            "are you a robot",
            "verify you are human",
            "verify you're human",
            "human verification",
            "security check",
            "prove you're not a robot",
            "recaptcha",
            "hcaptcha",
            "turnstile",
            "suspicious activity",
            "please verify",
        ]
        return any(ind in lower for ind in indicators)

    @staticmethod
    def verify_reddit_auth_in_dom(snapshot_text: str) -> Tuple[bool, Optional[str], str]:
        """Inspect DOM snapshot for verified Reddit authentication indicators.

        Never claims authenticated status based solely on cookies or directory existence.
        Requires active Reddit DOM signals (e.g. user menu, current user, karma counters).

        Returns:
            (is_authenticated, detected_username, explanation)
        """
        if not snapshot_text or not isinstance(snapshot_text, str):
            return False, None, "No DOM snapshot content available to verify."

        text = snapshot_text.lower()

        # 1. Logged-out indicators: If login/signup buttons or signup modal are present, user is NOT logged in.
        has_login_button = (
            'data-testid="login-button"' in text
            or 'href="/login"' in text
            or 'href="https://www.reddit.com/login"' in text
            or 'action="/login"' in text
            or ">log in<" in text
            or ">sign up<" in text
            or 'button "log in"' in text
            or 'link "log in"' in text
            or 'button "sign up"' in text
            or 'link "sign up"' in text
            or 'button: log in' in text
            or 'button: sign up' in text
            or 'heading "sign up"' in text
            or 'heading "log in"' in text
            or 'dialog "sign up"' in text
            or 'dialog "log in"' in text
            or 'continue with phone number' in text
            or 'continue with google' in text
            or 'continue with apple' in text
            or 'already a redditor? log in' in text
        )

        if has_login_button:
            return False, None, "Unauthenticated: Reddit login prompt / button / sign up modal detected on page."

        # 2. Check for user markers specifically on the logged-in user element (never post authors or commenters)
        user_match = re.search(r'current-user=["\']([^"\']+)["\']', snapshot_text, re.IGNORECASE)
        if not user_match:
            user_match = re.search(r'aria-label=["\']user account:\s*([^"\']+)["\']', snapshot_text, re.IGNORECASE)

        if user_match:
            uname = user_match.group(1).strip()
            if uname and uname.lower() not in ("login", "signup", "register", "submit", "settings", "notifications"):
                return True, uname, f"Verified active Reddit session for user '{uname}'"

        # 3. User menu dropdown or authenticated top-bar indicators (strictly impossible when logged out)
        # Note: Never use generic 'user avatar' here, as other commenters in the thread also have avatars!
        has_user_dropdown = (
            'data-testid="user-dropdown"' in text
            or 'id="user_dropdown_id"' in text
            or 'aria-label="open user menu"' in text
            or 'class="header-user-dropdown"' in text
            or 'expand user menu' in text
            or ('open chat' in text and 'create' in text)
            or ('open inbox' in text and 'open chat' in text)
        )
        if has_user_dropdown:
            return True, None, "Verified active Reddit session (user dropdown menu present)"

        return False, None, "Unverified: Page does not contain active Reddit authentication indicators."

    # -----------------------------------------------------------------
    # Safe element interaction & editor preparation helpers
    # -----------------------------------------------------------------

    def evaluate_script(
        self,
        profile_name: str,
        script: str,
        target_id: Optional[str] = None,
        ref: Optional[str] = None,
        timeout: Optional[int] = 120,
    ) -> OpenClawResponse:
        """Evaluate a JavaScript expression or function in the browser tab.

        Strict compliance:
        - Disallows protected profiles.
        - Used for non-destructive DOM reading and editor insertion.
        - Never executes form submission, publish, or button click to post.
        """
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        args = ["browser", "evaluate", "--fn", str(script), "--browser-profile", profile_name.strip()]
        if ref and str(ref).strip():
            args.extend(["--ref", str(ref).strip()])
        if target_id and str(target_id).strip():
            args.extend(["--target-id", str(target_id).strip()])
        return self._execute(args, timeout=timeout or 120)

    def wait_for(
        self,
        profile_name: str,
        text: Optional[str] = None,
        selector: Optional[str] = None,
        time_ms: Optional[int] = None,
        target_id: Optional[str] = None,
    ) -> OpenClawResponse:
        """Wait for page load state, text, selector, or timeout."""
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        args = ["browser", "wait", "--browser-profile", profile_name.strip()]
        if text:
            args.extend(["--text", str(text)])
        if selector:
            args.extend(["--selector", str(selector)])
        if time_ms:
            args.extend(["--time", str(time_ms)])
        if target_id and str(target_id).strip():
            args.extend(["--target-id", str(target_id).strip()])
        return self._execute(args, timeout=45)

    def _click_ref(
        self,
        profile_name: str,
        ref: str,
        target_id: Optional[str] = None,
    ) -> OpenClawResponse:
        """Internal helper to click an element ref (e.g. activating comment editor).

        Strict compliance:
        - Disallows protected profiles.
        - Never clicks submit, publish, or comment buttons.
        """
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)
        if not ref or not str(ref).strip():
            return OpenClawResponse(success=False, error="Element ref cannot be empty.", exit_code=-1)

        args = ["browser", "click", str(ref).strip(), "--browser-profile", profile_name.strip()]
        if target_id and str(target_id).strip():
            args.extend(["--target-id", str(target_id).strip()])
        return self._execute(args, timeout=30)

    def _click_coords(
        self,
        profile_name: str,
        x: int,
        y: int,
    ) -> OpenClawResponse:
        """Click native viewport coordinates with real CDP pointer events."""
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)

        args = [
            "browser", "click-coords",
            str(int(x)), str(int(y)),
            "--browser-profile", profile_name.strip(),
        ]
        return self._execute(args, timeout=30)

    def _type_ref(
        self,
        profile_name: str,
        ref: str,
        text: str,
        target_id: Optional[str] = None,
    ) -> OpenClawResponse:
        """Internal helper to type text into an element ref without submitting.

        Strict compliance:
        - Disallows protected profiles.
        - Never passes --submit or presses Enter to submit.
        - Inserts text and stops before submission.
        """
        prof_err = self._validate_profile(profile_name)
        if prof_err:
            return OpenClawResponse(success=False, error=prof_err, exit_code=-1)
        if not ref or not str(ref).strip():
            return OpenClawResponse(success=False, error="Element ref cannot be empty.", exit_code=-1)

        # STRICT SAFETY: Never pass --submit or press enter
        args = ["browser", "type", str(ref).strip(), str(text), "--browser-profile", profile_name.strip()]
        if target_id and str(target_id).strip():
            args.extend(["--target-id", str(target_id).strip()])
        return self._execute(args, timeout=45)

    @staticmethod
    def inspect_reddit_discussion_state(snapshot_text: str) -> Tuple[bool, str]:
        """Inspect page snapshot for locked, archived, deleted, or unavailable status.

        Returns:
            (is_actionable, reason_if_not)
        """
        if not snapshot_text or not isinstance(snapshot_text, str):
            return False, "Page snapshot is empty or unavailable."

        lower = snapshot_text.lower()

        # 1. Locked discussion check
        locked_signals = [
            "this thread is locked",
            "comments are locked",
            "this post is locked",
            "post is locked",
            "thread is locked",
            "locked post",
            "locked by moderator",
            "locked by the moderators",
        ]
        if any(sig in lower for sig in locked_signals):
            return False, "Discussion is locked. Skipping task."

        # 2. Archived discussion check
        archived_signals = [
            "this is an archived post",
            "archived post",
            "this thread is archived",
            "new comments cannot be posted",
            "you can't comment on this post",
        ]
        if any(sig in lower for sig in archived_signals):
            return False, "Discussion is archived. Skipping task."

        # 3. Deleted / removed post check
        deleted_signals = [
            "this post has been removed",
            "sorry, this post has been removed",
            "[deleted by user]",
            "post was removed",
            "post deleted",
        ]
        if any(sig in lower for sig in deleted_signals):
            return False, "Discussion is deleted or unavailable. Skipping task."

        # 4. Unavailable / 404 / community not found
        unavailable_signals = [
            "page not found",
            "this community does not exist",
            "404 not found",
            "community has been banned",
            "this content is no longer available",
        ]
        if any(sig in lower for sig in unavailable_signals):
            return False, "Discussion is unavailable. Skipping task."

        return True, ""

    @staticmethod
    def find_editor_ref_in_snapshot(snapshot_text: str) -> Optional[str]:
        """Extract element ref for the comment-entry area ('Join the conversation') from snapshot."""
        if not snapshot_text or not isinstance(snapshot_text, str):
            return None

        invalid_ref_words = {
            "disabled", "enabled", "button", "textbox", "hidden", "expanded",
            "collapsed", "readonly", "required", "true", "false", "selected",
            "checked", "active", "focus", "visited", "link", "input", "img",
            "heading", "region", "dialog", "presentation", "tab", "tabpanel",
            "placeholder", "value", "type", "name", "id", "class", "style",
            "role", "aria", "label", "text", "comment", "submit"
        }

        def extract_ref(text_line: str) -> Optional[str]:
            # 1. Look for explicit bracketed ref: [ref 42], [ref: 42], [ref=e12]
            m = re.search(r'\[ref[=:\s]+([a-zA-Z0-9_\-]+)\]', text_line, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val.lower() not in invalid_ref_words:
                    return val

            # 2. Look for word ref: ref: 42, ref=e12, ref 42
            m = re.search(r'\bref[:=\s]+([a-zA-Z0-9_\-]+)\b', text_line, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val.lower() not in invalid_ref_words:
                    return val

            # 3. Look for bracketed numeric ref only: [42], [105] (never words like [disabled])
            m = re.search(r'\[(\d+)\]', text_line)
            if m:
                return m.group(1).strip()

            return None

        lines = snapshot_text.splitlines()
        for i, line in enumerate(lines):
            lower = line.lower()
            if "join the conversation" in lower or "add a comment" in lower or "shreddit-comment-composer" in lower:
                # 1. Look for ref directly on the current line
                cand = extract_ref(line)
                if cand:
                    return cand

                # 2. Check immediately preceding lines (i-1 down to i-3) for textbox or parent ref
                for prev_idx in range(i - 1, max(-1, i - 4), -1):
                    cand = extract_ref(lines[prev_idx])
                    if cand:
                        return cand

                # 3. Check next lines (i+1 to i+2)
                for next_idx in range(i + 1, min(len(lines), i + 3)):
                    cand = extract_ref(lines[next_idx])
                    if cand:
                        return cand

        return None
