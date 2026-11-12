"""Unit and safety tests for ExcelHandler."""

import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid
import openpyxl

from modules.excel_handler import ExcelHandler


def get_file_sha256(path: Path | str) -> str:
    """Return SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestExcelHandler(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch") / f"excel_test_{uuid.uuid4().hex[:8]}"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.test_dir / "backups"
        self.test_wb_path = self.test_dir / "test_threads.xlsx"

        # Create a sample workbook matching production layout
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Driving DMV"
        headers = [
            "Last checked", "State / Province", "Thread title", "Thread link",
            "Engagement", "Why it matters / What changed", "Suggested reply",
            "Content idea", "My status", "Revised suggested reply",
            "Shoro AI reply", "Account posted", "Status"
        ]
        ws.append(headers)
        ws.append([
            "2026-11-01", "VA", "Title 1", "https://reddit.com/r/driving/1",
            "high", "why", "sug", "idea", "To review", "rev",
            "Shoro reply text", "acc@gmail.com", "To review"
        ])
        ws.append([
            "2026-11-01", "MD", "Title 2", "https://reddit.com/r/driving/2",
            "low", "why2", "sug2", "idea2", "To review", "rev2",
            "Shoro reply text 2", "acc2@gmail.com", "To review"
        ])
        wb.save(self.test_wb_path)
        wb.close()

        self.handler = ExcelHandler(self.test_wb_path, backup_dir=self.backup_dir)

    def tearDown(self):
        # Clean up temporary test directory
        try:
            if self.test_dir.exists():
                shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def test_successful_atomic_write(self):
        """Verify successful atomic update modifies target cell and cleans up temp files."""
        orig_sha = get_file_sha256(self.test_wb_path)

        success, msg = self.handler.update_task_status(
            worksheet_name="Driving DMV",
            excel_row=2,
            new_status="Completed - human confirmed",
            timestamp_str="2026-11-05T20:00:00+05:30",
        )
        self.assertTrue(success, msg)

        # Check that backup was created
        backups = list(self.backup_dir.glob("test_threads_backup_*.xlsx"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].stat().st_size > 0)

        # Check no temporary atomic files were left behind
        temp_files = list(self.test_dir.glob("*_atomic_tmp_*.xlsx"))
        self.assertEqual(len(temp_files), 0, f"Found leftover temp files: {temp_files}")

        # Check target workbook was modified
        new_sha = get_file_sha256(self.test_wb_path)
        self.assertNotEqual(orig_sha, new_sha)

        wb = openpyxl.load_workbook(self.test_wb_path)
        ws = wb["Driving DMV"]
        self.assertEqual(ws.cell(row=2, column=13).value, "Completed - human confirmed")
        # Row 3 must remain untouched
        self.assertEqual(ws.cell(row=3, column=13).value, "To review")
        wb.close()

    def test_temporary_save_failure(self):
        """Verify that failure during temporary file save preserves original workbook."""
        orig_sha = get_file_sha256(self.test_wb_path)

        with patch("openpyxl.Workbook.save", side_effect=IOError("Simulated disk error during temp save")):
            success, msg = self.handler.update_task_status(
                worksheet_name="Driving DMV",
                excel_row=2,
                new_status="Completed - human confirmed",
            )
            self.assertFalse(success)
            self.assertIn("Failed to save workbook updates atomically", msg)

        # Original workbook must be 100% preserved
        self.assertEqual(get_file_sha256(self.test_wb_path), orig_sha)

        # No temp files left
        temp_files = list(self.test_dir.glob("*_atomic_tmp_*.xlsx"))
        self.assertEqual(len(temp_files), 0)

    def test_atomic_replacement_failure(self):
        """Verify that failure during os.replace preserves original workbook and cleans temp."""
        orig_sha = get_file_sha256(self.test_wb_path)

        with patch("os.replace", side_effect=OSError("Simulated permission error on atomic swap")):
            success, msg = self.handler.update_task_status(
                worksheet_name="Driving DMV",
                excel_row=2,
                new_status="Completed - human confirmed",
            )
            self.assertFalse(success)
            self.assertIn("Simulated permission error", msg)

        # Original workbook must be 100% preserved
        self.assertEqual(get_file_sha256(self.test_wb_path), orig_sha)

        # Temporary file should have been cleaned up
        temp_files = list(self.test_dir.glob("*_atomic_tmp_*.xlsx"))
        self.assertEqual(len(temp_files), 0)

    def test_backup_creation_failure_aborts_write(self):
        """Verify that if backup creation fails, write is immediately aborted."""
        orig_sha = get_file_sha256(self.test_wb_path)

        with patch.object(self.handler, "create_timestamped_backup", return_value=(False, "Simulated backup disk full")):
            success, msg = self.handler.update_task_status(
                worksheet_name="Driving DMV",
                excel_row=2,
                new_status="Completed - human confirmed",
            )
            self.assertFalse(success)
            self.assertIn("Aborting write: Backup creation failed", msg)

        # Original workbook must be untouched
        self.assertEqual(get_file_sha256(self.test_wb_path), orig_sha)

    def test_separate_backups_for_multiple_writes(self):
        """Verify incremental backups create distinct, non-overwriting files for every write."""
        # Write 1
        success1, _ = self.handler.update_task_status("Driving DMV", 2, "Status A")
        self.assertTrue(success1)
        backups1 = sorted(list(self.backup_dir.glob("test_threads_backup_*.xlsx")))
        self.assertEqual(len(backups1), 1)

        # Write 2
        success2, _ = self.handler.update_task_status("Driving DMV", 3, "Status B")
        self.assertTrue(success2)
        backups2 = sorted(list(self.backup_dir.glob("test_threads_backup_*.xlsx")))
        self.assertEqual(len(backups2), 2)

        # Verify both backups are separate files and exist
        self.assertNotEqual(backups1[0].name, backups2[1].name)
        self.assertTrue(backups2[0].exists() and backups2[0].stat().st_size > 0)
        self.assertTrue(backups2[1].exists() and backups2[1].stat().st_size > 0)

    def test_original_workbook_preservation_after_corrupted_temp(self):
        """Verify that if a temporary file is corrupted/invalid zip, original workbook is preserved."""
        orig_sha = get_file_sha256(self.test_wb_path)

        # Mock zipfile.is_zipfile to return False (corrupted file simulation)
        with patch("zipfile.is_zipfile", return_value=False):
            success, msg = self.handler.update_task_status("Driving DMV", 2, "Corrupted attempt")
            self.assertFalse(success)
            self.assertIn("not a valid zip/xlsx archive", msg)

        # Original workbook must remain identical
        self.assertEqual(get_file_sha256(self.test_wb_path), orig_sha)

    def test_structure_formatting_and_unrelated_cells_preserved(self):
        """Verify that non-target sheets, formulas, and cells are intact after update."""
        # Add a secondary sheet with formula and data
        wb = openpyxl.load_workbook(self.test_wb_path)
        ws2 = wb.create_sheet(title="Stats")
        ws2["A1"] = 10
        ws2["B1"] = 20
        ws2["C1"] = "=A1+B1"
        wb.save(self.test_wb_path)
        wb.close()

        # Update row 2 in Driving DMV
        success, msg = self.handler.update_task_status("Driving DMV", 2, "Verified - human")
        self.assertTrue(success)

        # Reload and verify all structures
        wb_check = openpyxl.load_workbook(self.test_wb_path)
        self.assertIn("Driving DMV", wb_check.sheetnames)
        self.assertIn("Stats", wb_check.sheetnames)

        # Verify formula in Stats sheet
        ws_stats = wb_check["Stats"]
        self.assertEqual(ws_stats["C1"].value, "=A1+B1")
        self.assertEqual(ws_stats["A1"].value, 10)

        # Verify unrelated cells in Driving DMV
        ws_dmv = wb_check["Driving DMV"]
        self.assertEqual(ws_dmv.cell(row=2, column=13).value, "Verified - human")
        self.assertEqual(ws_dmv.cell(row=2, column=3).value, "Title 1")
        self.assertEqual(ws_dmv.cell(row=2, column=11).value, "Shoro reply text")
        self.assertEqual(ws_dmv.cell(row=3, column=3).value, "Title 2")
        wb_check.close()

    def test_force_test_mode_write_protection(self):
        """Verify that FORCE_TEST_MODE prevents any write or backup creation."""
        orig_sha = get_file_sha256(self.test_wb_path)
        os.environ["FORCE_TEST_MODE"] = "true"
        try:
            success, msg = self.handler.update_task_status("Driving DMV", 2, "Test Status")
            self.assertFalse(success)
            self.assertIn("FORCE_TEST_MODE environment safeguard is active", msg)

            # Original workbook must be untouched
            self.assertEqual(get_file_sha256(self.test_wb_path), orig_sha)

            # No backup should have been created
            backups = list(self.backup_dir.glob("*.xlsx"))
            self.assertEqual(len(backups), 0)
        finally:
            os.environ.pop("FORCE_TEST_MODE", None)

    def test_update_invalid_sheet(self):
        success, msg = self.handler.update_task_status("NonExistent", 2, "Test")
        self.assertFalse(success)
        self.assertIn("not found", msg)

    def test_invalid_row_numbers(self):
        for invalid_row in [1, 0, -1, 9999]:
            success, msg = self.handler.update_task_status("Driving DMV", invalid_row, "Test Status")
            self.assertFalse(success)
            self.assertIn("Invalid row number", msg)

    def test_missing_status_column(self):
        wb = openpyxl.load_workbook(self.test_wb_path)
        ws = wb.create_sheet(title="NoStatusSheet")
        ws.append(["Col1", "Col2", "Col3"])
        ws.append(["val1", "val2", "val3"])
        wb.save(self.test_wb_path)
        wb.close()

        success, msg = self.handler.update_task_status("NoStatusSheet", 2, "Completed - verified")
        self.assertFalse(success)
        self.assertIn("Could not find 'Status' column", msg)

    def test_nonexistent_workbook(self):
        bad_handler = ExcelHandler(self.test_dir / "does_not_exist.xlsx", backup_dir=self.backup_dir)
        success, msg = bad_handler.update_task_status("Driving DMV", 2, "Test")
        self.assertFalse(success)
        self.assertIn("not found", msg.lower())


if __name__ == "__main__":
    unittest.main()
