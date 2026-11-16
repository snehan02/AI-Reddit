"""Unit tests for AccountManager."""

import unittest
from pathlib import Path
import json
import os
from modules.account_manager import (
    AccountManager,
    AccountProfile,
    PROTECTED_PROFILE_NAMES,
    is_personal_chrome_directory,
    normalize_directory_path,
)


class TestAccountManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.test_config = self.test_dir / "test_accounts.json"

    def test_load_default_accounts(self):
        manager = AccountManager("config/accounts.json")
        self.assertGreaterEqual(len(manager.accounts), 20)
        # All accounts must remain disabled until explicit approval is confirmed
        enabled_accs = [a for a in manager.accounts.values() if a.enabled]
        self.assertEqual(len(enabled_accs), 0)

        # Verify schoolwaale01 is configured but disabled
        self.assertIn("schoolwaale01@gmail.com", manager.accounts)
        self.assertFalse(manager.accounts["schoolwaale01@gmail.com"].enabled)
        profile, err = manager.resolve_account("schoolwaale01@gmail.com")
        self.assertIsNone(profile)
        self.assertIn("disabled", err)

        # All accounts disabled by default
        disabled_accs = [a for a in manager.accounts.values() if not a.enabled]
        self.assertEqual(len(disabled_accs), len(manager.accounts))
        self.assertIn("schoolwaale02@gmail.com", manager.accounts)
        self.assertFalse(manager.accounts["schoolwaale02@gmail.com"].enabled)
        prof_dis, err_dis = manager.resolve_account("schoolwaale02@gmail.com")
        self.assertIsNone(prof_dis)
        self.assertIn("disabled", err_dis)

    def test_resolve_unmapped_account(self):
        manager = AccountManager("config/accounts.json")
        profile, err = manager.resolve_account("unknown_user@gmail.com")
        self.assertIsNone(profile)
        self.assertIn("Account profile not configured", err)

    def test_resolve_blank_account(self):
        manager = AccountManager("config/accounts.json")
        profile, err = manager.resolve_account("")
        self.assertIsNone(profile)
        self.assertIn("blank account", err)

    def test_profile_directory_collision(self):
        data = {
            "accounts": [
                {
                    "account_id": "acc1",
                    "chrome_profile_name": "Profile 1",
                    "user_data_directory": "chrome_profiles/shared",
                    "enabled": True,
                },
                {
                    "account_id": "acc2",
                    "chrome_profile_name": "Profile 2",
                    "user_data_directory": "chrome_profiles/shared",
                    "enabled": True,
                },
            ]
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        manager = AccountManager(self.test_config)
        success, errors = manager.load()
        self.assertFalse(success)
        self.assertTrue(any("Profile directory collision" in e for e in errors))

    def test_duplicate_directory_both_disabled(self):
        """Directory collisions must be rejected even when both accounts are disabled."""
        data = {
            "accounts": [
                {
                    "account_id": "disabled_acc1@gmail.com",
                    "chrome_profile_name": "Profile Dis 1",
                    "user_data_directory": "chrome_profiles/shared_dis",
                    "enabled": False,
                },
                {
                    "account_id": "disabled_acc2@gmail.com",
                    "chrome_profile_name": "Profile Dis 2",
                    "user_data_directory": "chrome_profiles/shared_dis",
                    "enabled": False,
                },
            ]
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        manager = AccountManager(self.test_config)
        success, errors = manager.load()
        self.assertFalse(success)
        self.assertTrue(any("Profile directory collision" in e for e in errors))

    def test_duplicate_directory_path_normalization(self):
        """Equivalent directory paths with slashes, dots, and case must be detected as collisions."""
        data = {
            "accounts": [
                {
                    "account_id": "acc_norm1@gmail.com",
                    "chrome_profile_name": "Profile Norm 1",
                    "user_data_directory": "chrome_profiles/norm_target",
                    "enabled": False,
                },
                {
                    "account_id": "acc_norm2@gmail.com",
                    "chrome_profile_name": "Profile Norm 2",
                    "user_data_directory": "./chrome_profiles/norm_target/",
                    "enabled": False,
                },
            ]
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        manager = AccountManager(self.test_config)
        success, errors = manager.load()
        self.assertFalse(success)
        self.assertTrue(any("Profile directory collision" in e for e in errors))

    def test_duplicate_chrome_profile_name_both_disabled(self):
        """Profile name collisions must be rejected across all accounts, including disabled."""
        data = {
            "accounts": [
                {
                    "account_id": "acc_name1@gmail.com",
                    "chrome_profile_name": "Profile Duplicate Name",
                    "user_data_directory": "chrome_profiles/dir1",
                    "enabled": False,
                },
                {
                    "account_id": "acc_name2@gmail.com",
                    "chrome_profile_name": "profile duplicate name",  # case-insensitive collision
                    "user_data_directory": "chrome_profiles/dir2",
                    "enabled": False,
                },
            ]
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        manager = AccountManager(self.test_config)
        success, errors = manager.load()
        self.assertFalse(success)
        self.assertTrue(any("Chrome profile name collision" in e for e in errors))

    def test_account_id_collision_rejected_in_load(self):
        """Duplicate account IDs must be rejected in load() even if other fields differ."""
        data = {
            "accounts": [
                {
                    "account_id": "duplicate_id@shoro.ai",
                    "chrome_profile_name": "Profile A",
                    "user_data_directory": "chrome_profiles/dir_a",
                    "enabled": False,
                },
                {
                    "account_id": "DUPLICATE_ID@shoro.ai",  # Case-insensitive collision
                    "chrome_profile_name": "Profile B",
                    "user_data_directory": "chrome_profiles/dir_b",
                    "enabled": False,
                },
            ]
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        manager = AccountManager(self.test_config)
        success, errors = manager.load()
        self.assertFalse(success)
        self.assertTrue(any("Account ID collision" in e for e in errors))

    def test_protected_profile_names_rejected_in_load(self):
        """Protected profile names (user, chrome, default) must be rejected in load() for enabled and disabled."""
        for name in ["user", "chrome", "default"]:
            data = {
                "accounts": [
                    {
                        "account_id": f"acc_{name}@shoro.ai",
                        "chrome_profile_name": name,
                        "user_data_directory": f"chrome_profiles/{name}_dir",
                        "enabled": False,
                    }
                ]
            }
            with open(self.test_config, "w", encoding="utf-8") as f:
                json.dump(data, f)

            manager = AccountManager(self.test_config)
            success, errors = manager.load()
            self.assertFalse(success)
            self.assertTrue(
                any("Protected profile name collision" in e for e in errors),
                f"Expected rejection for protected profile name '{name}'",
            )

    def test_case_insensitive_protected_profile_names(self):
        """Case variants and whitespace in protected profile names must be rejected."""
        variants = ["USER", "User", "CHROME", "Chrome", "DEFAULT", "Default", " user ", " DEFAULT "]
        for v in variants:
            data = {
                "accounts": [
                    {
                        "account_id": "acc_var@shoro.ai",
                        "chrome_profile_name": v,
                        "user_data_directory": "chrome_profiles/var_dir",
                        "enabled": True,
                    }
                ]
            }
            with open(self.test_config, "w", encoding="utf-8") as f:
                json.dump(data, f)

            manager = AccountManager(self.test_config)
            success, errors = manager.load()
            self.assertFalse(success)
            self.assertTrue(
                any("Protected profile name collision" in e for e in errors),
                f"Expected rejection for protected profile variant '{v}'",
            )

    def test_protected_profile_names_rejected_in_add_or_update(self):
        """Dynamic add_or_update must raise ValueError for protected profile names."""
        manager = AccountManager(self.test_config)
        for name in ["user", "chrome", "default", "USER", "CHROME", "DEFAULT", " default "]:
            with self.assertRaises(ValueError) as ctx:
                manager.add_or_update("acc@shoro.ai", name, "chrome_profiles/safe_dir", enabled=False)
            self.assertIn("Protected profile name collision", str(ctx.exception))

    def test_personal_chrome_directory_rejected_in_load(self):
        """Pointing directly to personal Chrome User Data directory must be rejected."""
        test_paths = [
            "C:\\Users\\sneha\\AppData\\Local\\Google\\Chrome\\User Data",
            "C:/Users/CustomUser/AppData/Local/Google/Chrome/User Data",
            "c:/users/test/appdata/local/google/chrome/user data/",
        ]
        for p in test_paths:
            data = {
                "accounts": [
                    {
                        "account_id": "personal_acc@shoro.ai",
                        "chrome_profile_name": "Profile Personal",
                        "user_data_directory": p,
                        "enabled": False,
                    }
                ]
            }
            with open(self.test_config, "w", encoding="utf-8") as f:
                json.dump(data, f)

            manager = AccountManager(self.test_config)
            success, errors = manager.load()
            self.assertFalse(success)
            self.assertTrue(
                any("personal Chrome User Data directory" in e for e in errors),
                f"Expected rejection for personal chrome directory '{p}'",
            )

    def test_nested_personal_chrome_directory_rejected_in_load(self):
        """Pointing inside personal Chrome User Data directory must be rejected."""
        test_paths = [
            "C:\\Users\\sneha\\AppData\\Local\\Google\\Chrome\\User Data\\Default",
            "C:\\Users\\Test\\AppData\\Local\\Google\\Chrome\\User Data\\Profile 1",
            "C:/Users/Test/AppData/Local/Google/Chrome/User Data/Nested/Subdir",
        ]
        for p in test_paths:
            data = {
                "accounts": [
                    {
                        "account_id": "nested_acc@shoro.ai",
                        "chrome_profile_name": "Profile Nested",
                        "user_data_directory": p,
                        "enabled": False,
                    }
                ]
            }
            with open(self.test_config, "w", encoding="utf-8") as f:
                json.dump(data, f)

            manager = AccountManager(self.test_config)
            success, errors = manager.load()
            self.assertFalse(success)
            self.assertTrue(
                any("personal Chrome User Data directory" in e for e in errors),
                f"Expected rejection for nested personal chrome directory '{p}'",
            )

    def test_personal_and_nested_chrome_directory_rejected_in_add_or_update(self):
        """Dynamic add_or_update must raise ValueError for personal or nested Chrome directories."""
        manager = AccountManager(self.test_config)
        invalid_dirs = [
            "C:\\Users\\sneha\\AppData\\Local\\Google\\Chrome\\User Data",
            "C:\\Users\\Test\\AppData\\Local\\Google\\Chrome\\User Data\\Default",
            "C:/Users/Test/AppData/Local/Google/Chrome/User Data/Profile 2",
        ]
        for bad_dir in invalid_dirs:
            with self.assertRaises(ValueError) as ctx:
                manager.add_or_update("acc@shoro.ai", "Profile Safe Name", bad_dir, enabled=False)
            self.assertIn("personal Chrome User Data directory", str(ctx.exception))

    def test_valid_isolated_openclaw_directory_acceptance(self):
        """Legitimate isolated OpenClaw profile directories must be accepted without error."""
        valid_entries = [
            ("acc1@shoro.ai", "Profile Acc 1", "chrome_profiles/isolated_user1"),
            ("acc2@shoro.ai", "Profile Acc 2", "./chrome_profiles/isolated_user2/"),
            ("acc3@shoro.ai", "Profile Acc 3", "C:\\OpenClaw\\profiles\\user3"),
            ("acc4@shoro.ai", "Profile Acc 4", "backups/test_scratch/isolated_acc4"),
        ]
        data = {
            "accounts": [
                {
                    "account_id": acc_id,
                    "chrome_profile_name": prof_name,
                    "user_data_directory": udir,
                    "enabled": False,
                }
                for acc_id, prof_name, udir in valid_entries
            ]
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        manager = AccountManager(self.test_config)
        success, errors = manager.load()
        self.assertTrue(success, f"Expected clean load, but got errors: {errors}")
        self.assertEqual(len(manager.accounts), 4)

        # Dynamic add of another valid isolated directory
        manager.add_or_update("acc5@shoro.ai", "Profile Acc 5", "chrome_profiles/isolated_user5", enabled=False)
        self.assertIn("acc5@shoro.ai", manager.accounts)

    def test_add_or_update_collision_prevention(self):
        """add_or_update must raise ValueError when attempting to assign duplicate dir or name."""
        manager = AccountManager(self.test_config)
        manager.add_or_update("user1@shoro.ai", "Profile Alpha", "chrome_profiles/alpha", enabled=False)

        # Duplicate directory
        with self.assertRaises(ValueError) as ctx_dir:
            manager.add_or_update("user2@shoro.ai", "Profile Beta", "./chrome_profiles/alpha/", enabled=False)
        self.assertIn("Profile directory collision", str(ctx_dir.exception))

        # Duplicate profile name
        with self.assertRaises(ValueError) as ctx_name:
            manager.add_or_update("user3@shoro.ai", "profile alpha", "chrome_profiles/gamma", enabled=False)
        self.assertIn("Chrome profile name collision", str(ctx_name.exception))

        # Empty fields
        with self.assertRaises(ValueError):
            manager.add_or_update("", "Profile X", "chrome_profiles/x")
        with self.assertRaises(ValueError):
            manager.add_or_update("user4@shoro.ai", "", "chrome_profiles/x")
        with self.assertRaises(ValueError):
            manager.add_or_update("user4@shoro.ai", "Profile X", "")

        # Updating same account is allowed
        manager.add_or_update("user1@shoro.ai", "Profile Alpha Updated", "chrome_profiles/alpha_updated", enabled=False)
        self.assertIn("user1@shoro.ai", manager.accounts)
        self.assertEqual(manager.accounts["user1@shoro.ai"].chrome_profile_name, "Profile Alpha Updated")

        # Updating same account with different casing is allowed
        manager.add_or_update("USER1@SHORO.AI", "Profile Alpha Final", "chrome_profiles/alpha_final", enabled=False)
        self.assertIn("USER1@SHORO.AI", manager.accounts)
        self.assertEqual(manager.accounts["USER1@SHORO.AI"].chrome_profile_name, "Profile Alpha Final")

    def test_security_rejection_of_credentials(self):
        data = {
            "accounts": [
                {
                    "account_id": "acc1",
                    "chrome_profile_name": "Profile 1",
                    "user_data_directory": "chrome_profiles/p1",
                    "enabled": True,
                    "password": "secret_password",
                }
            ]
        }
        with open(self.test_config, "w", encoding="utf-8") as f:
            json.dump(data, f)

        manager = AccountManager(self.test_config)
        success, errors = manager.load()
        self.assertFalse(success)
        self.assertTrue(any("Disallowed security-sensitive key" in e for e in errors))

    def test_add_and_update(self):
        manager = AccountManager(self.test_config)
        manager.add_or_update("new_user@shoro.ai", "Profile New", "chrome_profiles/new_user")
        profile, err = manager.resolve_account("new_user@shoro.ai")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.chrome_profile_name, "Profile New")


if __name__ == "__main__":
    unittest.main()
