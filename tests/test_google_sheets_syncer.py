"""Unit tests for Google Sheets synchronization and status updates."""

import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import zipfile
import openpyxl

from config import AppSettings
from modules.google_sheets_syncer import GoogleSheetsSyncer, DEFAULT_SPREADSHEET_ID
from modules.excel_handler import ExcelHandler
from modules.workflow_controller import WorkflowController


def _create_mock_xlsx_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Posts"
    ws1.append(["Date drafted", "Subreddit", "Region", "Post title", "Post body", "Shoro link", "Status"])
    ws1.append(["2026-11-14", "DMV", "CA", "Title 1", "Body 1", "https://shoro.ai", "To review"])

    ws2 = wb.create_sheet(title="Driving DMV")
    ws2.append(["Last checked", "State", "Title", "Link", "Engage", "Why", "Reply", "Idea", "My status", "Rev", "Shoro AI reply", "Account", "Status"])
    ws2.append(["2026-11-14", "CA", "Title 2", "https://reddit.com", "High", "Reason", "Sug", "Idea", "Stat", "Rev", "Reply", "acc1", "To review"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestGoogleSheetsSyncer(unittest.TestCase):

    def setUp(self):
        self.test_target = "data/test_sync_target.xlsx"
        self.backup_dir = "data/backups"
        self.syncer = GoogleSheetsSyncer(
            spreadsheet_id=DEFAULT_SPREADSHEET_ID,
            webhook_url="https://script.google.com/macros/s/TEST_EXEC/exec",
            backup_dir=self.backup_dir,
        )

    def tearDown(self):
        if os.path.exists(self.test_target):
            try:
                os.remove(self.test_target)
            except Exception:
                pass

    @patch("urllib.request.urlopen")
    def test_pull_latest_from_google_sheet_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = _create_mock_xlsx_bytes()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        ok, msg = self.syncer.pull_latest_from_google_sheet(target_path=self.test_target)
        self.assertTrue(ok)
        self.assertIn("Successfully synced local workbook", msg)
        self.assertTrue(os.path.exists(self.test_target))

        wb = openpyxl.load_workbook(self.test_target)
        self.assertIn("Posts", wb.sheetnames)
        self.assertIn("Driving DMV", wb.sheetnames)
        wb.close()

    @patch("urllib.request.urlopen")
    def test_update_status_in_google_sheet_webhook_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"result": "success", "row": 2, "status": "Completed - automated submission"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        ok, msg = self.syncer.update_status_in_google_sheet(
            worksheet_name="Posts",
            excel_row=2,
            new_status="Completed - automated submission",
            live_url="https://reddit.com/r/DMV/comments/abc1234/",
            timestamp_str="2026-11-14 17:00:00",
        )
        self.assertTrue(ok)
        self.assertIn("Updated Google Sheet 'Posts' row 2 status", msg)

        # Verify POST payload sent
        call_args, call_kwargs = mock_urlopen.call_args
        req = call_args[0]
        self.assertEqual(req.method, "POST")
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["sheet"], "Posts")
        self.assertEqual(payload["row"], 2)
        self.assertEqual(payload["status"], "Completed - automated submission")
        self.assertEqual(payload["live_url"], "https://reddit.com/r/DMV/comments/abc1234/")

    @patch("urllib.request.urlopen")
    def test_excel_handler_invokes_google_syncer(self, mock_urlopen):
        # Create dummy target
        mock_bytes = _create_mock_xlsx_bytes()
        with open(self.test_target, "wb") as f:
            f.write(mock_bytes)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"result": "success"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        handler = ExcelHandler(workbook_path=self.test_target, backup_dir=self.backup_dir)
        with patch.dict(os.environ, {"FORCE_TEST_MODE": "false", "GOOGLE_SHEETS_WEBHOOK_URL": "https://script.google.com/macros/s/TEST/exec"}):
            ok, msg = handler.update_task_status(
                worksheet_name="Posts",
                excel_row=2,
                new_status="Done",
                live_url="https://reddit.com/test",
            )
            self.assertTrue(ok)
            # Verify openpyxl modified the local file
            wb = openpyxl.load_workbook(self.test_target)
            self.assertEqual(wb["Posts"].cell(row=2, column=7).value, "Done")
            wb.close()


if __name__ == "__main__":
    unittest.main()

