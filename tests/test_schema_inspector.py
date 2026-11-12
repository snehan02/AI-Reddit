"""Unit tests for SchemaInspector."""

import unittest
from pathlib import Path
import openpyxl
from modules.schema_inspector import SchemaInspector, SheetSchema, WorkbookSchemaReport


class TestSchemaInspector(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("backups/test_scratch")
        self.test_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_missing_workbook(self):
        inspector = SchemaInspector("non_existent_file.xlsx")
        report = inspector.inspect()
        self.assertFalse(report.file_exists)
        self.assertFalse(report.is_valid_file)
        self.assertTrue(any("does not exist" in err for err in report.errors))

    def test_invalid_extension(self):
        bad_file = self.test_dir / "sample.csv"
        bad_file.write_text("a,b,c")
        inspector = SchemaInspector(bad_file)
        report = inspector.inspect()
        self.assertTrue(report.file_exists)
        self.assertFalse(report.is_valid_file)
        self.assertTrue(any("Unsupported file format" in err for err in report.errors))

    def test_inspect_actual_workbook(self):
        actual_path = Path("data/Reddit Threads.xlsx")
        if not actual_path.exists():
            self.skipTest("Source workbook not found in data/ directory")

        inspector = SchemaInspector(actual_path)
        report = inspector.inspect()

        self.assertTrue(report.file_exists)
        self.assertTrue(report.is_valid_file)
        self.assertIn("Driving DMV", report.sheet_names)
        self.assertIn("Posts", report.sheet_names)
        self.assertIn("NCERT Reddit", report.ignored_sheets)
        self.assertIn("Threads", report.ignored_sheets)

        # Driving DMV should be valid for comments
        self.assertTrue(report.is_valid_for_comments)
        dmv = report.sheets["Driving DMV"]
        self.assertIn("Thread link", dmv.header_indices)
        self.assertIn("Shoro AI reply", dmv.header_indices)
        self.assertIn("Account posted", dmv.header_indices)
        self.assertIn("Status", dmv.header_indices)
        # Column N should NOT exist
        self.assertFalse(dmv.has_column_n)

        # Posts should fail schema validation because Account posted is missing
        self.assertFalse(report.is_valid_for_posts)
        posts = report.sheets["Posts"]
        self.assertIn("Account posted", posts.missing_required_columns)
        self.assertFalse(posts.has_column_n)

    def test_synthetic_workbook_with_column_n(self):
        synth_path = self.test_dir / "synthetic_test.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Driving DMV"
        # 14 columns
        headers = [
            "Last checked", "State / Province", "Thread title", "Thread link",
            "Engagement", "Why it matters / What changed", "Suggested reply",
            "Content idea", "My status", "Revised suggested reply",
            "Shoro AI reply", "Account posted", "Status", "Timestamp"
        ]
        ws.append(headers)
        ws.append(["val"] * 14)
        wb.save(synth_path)
        wb.close()

        inspector = SchemaInspector(synth_path)
        report = inspector.inspect()
        dmv = report.sheets["Driving DMV"]
        self.assertTrue(dmv.has_column_n)
        self.assertEqual(dmv.column_n_header, "Timestamp")
        self.assertEqual(dmv.column_n_samples[0], "val")


if __name__ == "__main__":
    unittest.main()

