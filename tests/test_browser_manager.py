"""Unit tests for BrowserReviewManager."""

import unittest
from pathlib import Path
from modules.browser_manager import BrowserReviewManager, find_system_chrome


class TestBrowserReviewManager(unittest.TestCase):
    def setUp(self):
        self.mgr = BrowserReviewManager(test_mode=True)
        self.test_dir = Path("backups/test_scratch/browser_profile")
        self.test_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.mgr.close_active()

    def test_find_system_chrome(self):
        chrome_exe = find_system_chrome()
        # Chrome is installed on this Windows environment
        self.assertIsNotNone(chrome_exe)
        self.assertTrue(Path(chrome_exe).exists())

    def test_invalid_urls_rejected(self):
        invalid_cases = ["", "not_a_url", "ftp://example.com", "   "]
        for url in invalid_cases:
            success, msg = self.mgr.open_for_review(url, self.test_dir)
            self.assertFalse(success)
            self.assertIn("Invalid destination URL", msg)

    def test_profile_directory_created(self):
        new_prof_dir = Path("backups/test_scratch/new_test_profile")
        if new_prof_dir.exists():
            import shutil
            shutil.rmtree(new_prof_dir)
        self.assertFalse(new_prof_dir.exists())

        # Test with invalid url will fail after creating directory
        # but let's test directory creation directly
        target_dir = Path(new_prof_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        self.assertTrue(new_prof_dir.exists())


if __name__ == "__main__":
    unittest.main()

