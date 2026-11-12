"""Account-to-Chrome-profile mapping manager."""

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DISALLOWED_FIELDS = {"password", "secret", "cookie", "token", "session", "credentials"}
PROTECTED_PROFILE_NAMES = frozenset({"user", "chrome", "default"})


def normalize_directory_path(path_str: str) -> str:
    """Normalize filesystem directory path for collision checking."""
    if not path_str or not str(path_str).strip():
        return ""
    p = str(path_str).strip()
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def get_system_chrome_user_data_dirs() -> List[str]:
    """Retrieve platform-standard personal Chrome User Data directories."""
    dirs: List[str] = []
    # Windows
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        dirs.append(os.path.join(local_app_data, "Google", "Chrome", "User Data"))
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        dirs.append(os.path.join(user_profile, "AppData", "Local", "Google", "Chrome", "User Data"))
    # macOS
    home = os.path.expanduser("~")
    dirs.append(os.path.join(home, "Library", "Application Support", "Google", "Chrome"))
    # Linux
    dirs.append(os.path.join(home, ".config", "google-chrome"))
    return dirs


def is_personal_chrome_directory(path_str: str, custom_system_dirs: Optional[List[str]] = None) -> bool:
    """Check if a path points to or is inside the personal/primary Chrome User Data directory.
    
    Handles Windows case-insensitive paths and normalizes before comparison.
    Never rejects legitimate isolated OpenClaw profile directories.
    """
    if not path_str or not str(path_str).strip():
        return False

    norm_target = normalize_directory_path(path_str)
    if not norm_target:
        return False

    # 1. Compare against known primary system directories
    system_dirs = get_system_chrome_user_data_dirs()
    if custom_system_dirs:
        system_dirs.extend(custom_system_dirs)

    for sys_dir in system_dirs:
        norm_sys = normalize_directory_path(sys_dir)
        if not norm_sys:
            continue
        if norm_target == norm_sys:
            return True
        try:
            if os.path.commonpath([norm_target, norm_sys]) == norm_sys:
                return True
        except (ValueError, Exception):
            pass

    # 2. Structural pattern check for cross-platform/synthetic/custom test paths
    parts = [p.lower() for p in Path(norm_target).parts]
    for i in range(len(parts) - 2):
        if parts[i] == "google" and parts[i + 1] == "chrome" and parts[i + 2] == "user data":
            return True
    for i in range(len(parts) - 2):
        if parts[i] == "application support" and parts[i + 1] == "google" and parts[i + 2] == "chrome":
            return True
    for i in range(len(parts) - 1):
        if parts[i] == ".config" and parts[i + 1] == "google-chrome":
            return True

    return False


@dataclass
class AccountProfile:
    account_id: str
    chrome_profile_name: str
    user_data_directory: str
    enabled: bool = True

    def validate_security(self) -> None:
        """Ensure no credential or secret data is present."""
        for key in asdict(self).keys():
            if key.lower() in DISALLOWED_FIELDS:
                raise ValueError(f"Security violation: Sensitive field '{key}' cannot be stored.")


class AccountManager:
    """Manages local account-to-Chrome-profile mappings in config/accounts.json."""

    def __init__(self, config_path: Path | str = "config/accounts.json"):
        self.config_path = Path(config_path)
        self.accounts: Dict[str, AccountProfile] = {}
        self.load()

    def load(self) -> Tuple[bool, List[str]]:
        """Load and validate account mappings.
        
        Returns:
            (success, errors)
        """
        self.accounts.clear()
        errors: List[str] = []

        if not self.config_path.exists():
            logger.warning("Accounts config %s does not exist. Initializing empty.", self.config_path)
            return True, []

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            err = f"Failed to parse accounts config: {exc}"
            logger.error(err)
            return False, [err]

        account_list = data.get("accounts", [])
        seen_dirs: Dict[str, str] = {}
        seen_profile_names: Dict[str, str] = {}
        seen_account_ids: Dict[str, str] = {}

        for item in account_list:
            # Check for illegal secret fields
            for k in item.keys():
                if k.lower() in DISALLOWED_FIELDS:
                    err = f"Disallowed security-sensitive key '{k}' in account config"
                    errors.append(err)
                    logger.error(err)

            acc_id = item.get("account_id")
            prof_name = item.get("chrome_profile_name")
            data_dir = item.get("user_data_directory")
            enabled = item.get("enabled", True)

            if not acc_id or not prof_name or not data_dir:
                errors.append(f"Incomplete account entry: {item}")
                continue

            clean_acc_id = str(acc_id).strip()
            clean_prof_name = str(prof_name).strip()
            clean_data_dir = str(data_dir).strip()

            # Ensure unique account ID across ALL accounts (case-insensitive)
            norm_acc_id = clean_acc_id.lower()
            if norm_acc_id in seen_account_ids:
                err = (
                    f"Account ID collision: '{clean_acc_id}' is defined multiple times "
                    f"('{seen_account_ids[norm_acc_id]}' and '{clean_acc_id}'). Account IDs must be unique."
                )
                errors.append(err)
                logger.error(err)
            else:
                seen_account_ids[norm_acc_id] = clean_acc_id

            # Reject protected OpenClaw profile names across ALL accounts
            norm_prof = clean_prof_name.lower()
            if norm_prof in PROTECTED_PROFILE_NAMES:
                err = (
                    f"Protected profile name collision: '{clean_prof_name}' cannot be assigned "
                    f"to account '{clean_acc_id}'. Protected profiles ({', '.join(sorted(PROTECTED_PROFILE_NAMES))}) "
                    "are reserved to prevent interference with personal or system Chrome sessions."
                )
                errors.append(err)
                logger.error(err)

            # Ensure unique chrome profile name across ALL accounts (including disabled accounts)
            if norm_prof in seen_profile_names:
                err = (
                    f"Chrome profile name collision: '{prof_name}' is assigned to multiple accounts "
                    f"('{seen_profile_names[norm_prof]}' and '{clean_acc_id}'). Profile names must be distinct."
                )
                errors.append(err)
                logger.error(err)
            else:
                seen_profile_names[norm_prof] = clean_acc_id

            # Reject paths pointing to or inside personal Chrome User Data directory
            if is_personal_chrome_directory(clean_data_dir):
                err = (
                    f"Security violation: Directory '{clean_data_dir}' assigned to account '{clean_acc_id}' "
                    "points to or is inside the personal Chrome User Data directory. "
                    "Isolated OpenClaw profiles must be used."
                )
                errors.append(err)
                logger.error(err)

            # Ensure unique profile directory across ALL accounts (including disabled accounts)
            norm_dir = normalize_directory_path(clean_data_dir)
            if norm_dir in seen_dirs:
                err = (
                    f"Profile directory collision: '{data_dir}' is assigned to multiple accounts "
                    f"('{seen_dirs[norm_dir]}' and '{clean_acc_id}'). Profiles must be distinct."
                )
                errors.append(err)
                logger.error(err)
            else:
                seen_dirs[norm_dir] = clean_acc_id

            profile = AccountProfile(
                account_id=clean_acc_id,
                chrome_profile_name=clean_prof_name,
                user_data_directory=clean_data_dir,
                enabled=bool(enabled),
            )
            profile.validate_security()
            self.accounts[profile.account_id] = profile

        return len(errors) == 0, errors

    def save(self) -> None:
        """Persist account profiles to config file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "accounts": [asdict(p) for p in self.accounts.values()]
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved %d account profiles to %s", len(self.accounts), self.config_path)

    def resolve_account(self, account_id: Optional[str]) -> Tuple[Optional[AccountProfile], str]:
        """Resolve account mapping for a given account ID.
        
        Returns:
            (AccountProfile, error_reason)
        """
        if not account_id or not str(account_id).strip():
            return None, "Skipped - blank account"

        clean_id = str(account_id).strip()
        profile = self.accounts.get(clean_id)
        if not profile:
            return None, f"Account profile not configured: '{clean_id}'"
        if not profile.enabled:
            return None, f"Account profile disabled: '{clean_id}'"

        return profile, ""

    def add_or_update(
        self, account_id: str, chrome_profile_name: str, user_data_directory: str, enabled: bool = True
    ) -> None:
        """Add or update an account profile with collision and security validation."""
        if not account_id or not str(account_id).strip():
            raise ValueError("Account ID cannot be empty.")
        if not chrome_profile_name or not str(chrome_profile_name).strip():
            raise ValueError("Chrome profile name cannot be empty.")
        if not user_data_directory or not str(user_data_directory).strip():
            raise ValueError("User data directory cannot be empty.")

        clean_acc_id = str(account_id).strip()
        clean_prof_name = str(chrome_profile_name).strip()
        clean_data_dir = str(user_data_directory).strip()

        # Reject protected profile names
        norm_prof = clean_prof_name.lower()
        if norm_prof in PROTECTED_PROFILE_NAMES:
            raise ValueError(
                f"Protected profile name collision: '{clean_prof_name}' cannot be used. "
                f"Protected profiles ({', '.join(sorted(PROTECTED_PROFILE_NAMES))}) are reserved."
            )

        # Reject personal Chrome User Data directories
        if is_personal_chrome_directory(clean_data_dir):
            raise ValueError(
                f"Security violation: Directory '{clean_data_dir}' points to or is inside the personal "
                "Chrome User Data directory. Isolated OpenClaw profiles must be used."
            )

        norm_dir = normalize_directory_path(clean_data_dir)

        # Check collisions against existing other accounts
        existing_key = None
        for other_id, other_prof in self.accounts.items():
            if other_id.lower() == clean_acc_id.lower():
                existing_key = other_id
                continue
            other_norm_dir = normalize_directory_path(other_prof.user_data_directory)
            if norm_dir == other_norm_dir:
                raise ValueError(
                    f"Profile directory collision: '{clean_data_dir}' is already assigned to '{other_id}'."
                )
            if norm_prof == other_prof.chrome_profile_name.strip().lower():
                raise ValueError(
                    f"Chrome profile name collision: '{clean_prof_name}' is already assigned to '{other_id}'."
                )

        profile = AccountProfile(
            account_id=clean_acc_id,
            chrome_profile_name=clean_prof_name,
            user_data_directory=clean_data_dir,
            enabled=enabled,
        )
        profile.validate_security()

        # If updating with different casing, clean the old key
        if existing_key and existing_key != clean_acc_id:
            del self.accounts[existing_key]

        self.accounts[profile.account_id] = profile
        self.save()

    def list_accounts(self) -> List[AccountProfile]:
        """Return all mapped account profiles."""
        return list(self.accounts.values())
