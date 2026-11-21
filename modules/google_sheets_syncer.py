"""Google Sheets Synchronization Module.

Handles two-way synchronization between local Excel workbooks and Google Sheets:
1. Export/Pull: Pulls latest rows directly from Google Sheet (public export URL).
2. Push/Update: Updates row Status (e.g. 'Completed - automated submission' or 'Done')
   via Google Apps Script Webhook or Google Service Account.
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple
import urllib.error
import urllib.request
import zipfile

logger = logging.getLogger(__name__)

DEFAULT_SPREADSHEET_ID = "1_AcKE7VErFV2sLIFqLWeGcBQy8AKmPBEmeSPkHjUkl4"
DEFAULT_SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{DEFAULT_SPREADSHEET_ID}/edit?gid=0#gid=0"
DEFAULT_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{DEFAULT_SPREADSHEET_ID}/export?format=xlsx"

APPS_SCRIPT_TEMPLATE = """// Google Apps Script for Shoro AI Reddit Automation Sync
// Paste this in: Extensions -> Apps Script -> replace Code.gs -> Deploy -> New Deployment -> Web app
// Set: 'Execute as: Me', 'Who has access: Anyone'

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(data.sheet || "Driving DMV");
    if (!sheet) {
      sheet = ss.getSheetByName("Posts") || ss.getSheets()[0];
    }
    if (!sheet) {
      return ContentService.createTextOutput(JSON.stringify({result: "error", message: "Sheet not found: " + data.sheet}))
        .setMimeType(ContentService.MimeType.JSON);
    }
    var row = data.row;
    var lastCol = sheet.getLastColumn();
    var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    
    for (var i = 0; i < headers.length; i++) {
      var h = String(headers[i]).toLowerCase().trim();
      var colIdx = i + 1;
      
      // 1. Status and My status
      if (h === "status" || h === "my status" || h === "mystatus") {
        if (data.status && row > 1) {
          sheet.getRange(row, colIdx).setValue(data.status);
        }
      }
      
      // 2. Account posted
      if (h.includes("account posted") || h === "account") {
        if (data.account && row > 1) {
          sheet.getRange(row, colIdx).setValue(data.account);
        }
      }
      
      // 3. Last checked & Time Stamp
      if (h.includes("last checked") || h.includes("time stamp") || h.includes("timestamp")) {
        if (data.timestamp && row > 1) {
          sheet.getRange(row, colIdx).setValue(data.timestamp);
        }
      }
      
      // 4. Date
      if (h === "date") {
        if (data.timestamp && row > 1) {
          var dateVal = String(data.timestamp).split(" ")[0];
          sheet.getRange(row, colIdx).setValue(dateVal);
        }
      }
      
      // 5. Live URL
      if (h.includes("live url") || h.includes("permalink") || h.includes("published url")) {
        if (data.live_url && row > 1) {
          sheet.getRange(row, colIdx).setValue(data.live_url);
        }
      }
    }

    return ContentService.createTextOutput(JSON.stringify({
      result: "success",
      row: row,
      sheet: sheet.getName(),
      status: data.status,
      account: data.account
    })).setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({result: "error", error: err.toString()}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  return ContentService.createTextOutput(JSON.stringify({status: "online", sheet_id: SpreadsheetApp.getActiveSpreadsheet().getId()}))
    .setMimeType(ContentService.MimeType.JSON);
}
"""


class GoogleSheetsSyncer:
    """Manages real-time status updates and export syncing with Google Sheets."""

    def __init__(
        self,
        spreadsheet_id: str = DEFAULT_SPREADSHEET_ID,
        webhook_url: Optional[str] = None,
        backup_dir: str = "data/backups",
    ):
        self.spreadsheet_id = spreadsheet_id
        self.webhook_url = webhook_url or os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "")
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def pull_latest_from_google_sheet(
        self,
        target_path: str = "data/Reddit Threads.xlsx",
        timeout: int = 25,
    ) -> Tuple[bool, str]:
        """Download latest content from Google Sheet and replace local file with verified backup."""
        target_file = Path(target_path)
        export_url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/export?format=xlsx"
        logger.info("Pulling latest data from Google Sheet: %s", export_url)

        try:
            req = urllib.request.Request(
                export_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ShoroAI/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()

            if not data or len(data) < 1000:
                return False, f"Downloaded content too small ({len(data)} bytes). Google Sheet may be private."

            temp_path = target_file.parent / f"gsheet_pull_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            with open(temp_path, "wb") as f:
                f.write(data)

            # Validate that downloaded file is a valid zip/xlsx archive
            if not zipfile.is_zipfile(temp_path):
                temp_path.unlink(missing_ok=True)
                return False, "Downloaded file is not a valid Excel (.xlsx) archive."

            # Verify presence of expected sheets
            import openpyxl
            wb = openpyxl.load_workbook(temp_path, read_only=True)
            names = wb.sheetnames
            wb.close()

            if not any(s in names for s in ["Posts", "Driving DMV"]):
                temp_path.unlink(missing_ok=True)
                return False, f"Downloaded workbook missing expected sheets. Found: {names}"

            # Backup target file if it already exists
            if target_file.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_dest = self.backup_dir / f"{target_file.stem}_pre_gsheet_pull_{ts}{target_file.suffix}"
                shutil.copy2(target_file, backup_dest)
                logger.info("Created backup before Google Sheet pull: %s", backup_dest)

            # Atomically replace target file
            os.replace(temp_path, target_file)
            logger.info("Successfully updated local %s with Google Sheet data (%d bytes)", target_file, len(data))
            return True, f"Successfully synced local workbook from Google Sheet ({len(data):,} bytes, sheets: {', '.join(names)})"

        except Exception as e:
            logger.error("Failed to pull from Google Sheet: %s", e)
            return False, f"Google Sheet pull failed: {e}"

    def update_status_in_google_sheet(
        self,
        worksheet_name: str,
        excel_row: int,
        new_status: str,
        live_url: Optional[str] = None,
        timestamp_str: Optional[str] = None,
        account_name: Optional[str] = None,
        timeout: int = 15,
    ) -> Tuple[bool, str]:
        """Send row status update to Google Sheet via deployed Apps Script webhook."""
        wh_url = self.webhook_url.strip() if self.webhook_url else ""
        if not wh_url:
            msg = "Google Sheets Webhook URL not configured. Updates saved locally to Excel."
            logger.info(msg)
            return False, msg

        payload = {
            "sheet": worksheet_name,
            "row": excel_row,
            "status": new_status,
            "account": account_name or "",
            "live_url": live_url,
            "timestamp": timestamp_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                wh_url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "ShoroAI-GoogleSheetsSync/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                resp_text = response.read().decode("utf-8")

            res_json = {}
            try:
                res_json = json.loads(resp_text)
            except Exception:
                pass

            if response.status == 200 and res_json.get("result") == "success":
                msg = f"Updated Google Sheet '{worksheet_name}' row {excel_row} status to '{new_status}'"
                logger.info(msg)
                return True, msg
            else:
                msg = f"Google Sheet Webhook returned: {resp_text[:200]}"
                logger.warning(msg)
                return False, msg

        except urllib.error.HTTPError as he:
            err = f"Google Sheet Webhook HTTP error {he.code}: {he.reason}"
            logger.warning(err)
            return False, err
        except Exception as exc:
            err = f"Failed to sync status to Google Sheet: {exc}"
            logger.warning(err)
            return False, err

    def update_row_status(
        self,
        sheet_name: Optional[str] = None,
        row_number: Optional[int] = None,
        status: Optional[str] = None,
        live_url: Optional[str] = None,
        timestamp_str: Optional[str] = None,
        account_name: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str]:
        """Convenience alias for update_status_in_google_sheet with flexible keyword arguments."""
        resolved_sheet = sheet_name or kwargs.get("worksheet") or kwargs.get("sheet") or "Driving DMV"
        resolved_row = row_number or kwargs.get("row_index") or kwargs.get("row") or kwargs.get("excel_row") or 1
        resolved_status = status or kwargs.get("new_status") or "Done"
        resolved_ts = timestamp_str or kwargs.get("timestamp") or kwargs.get("timestamp_str")
        resolved_url = live_url or kwargs.get("live_url") or kwargs.get("url")
        resolved_acc = account_name or kwargs.get("account_name") or kwargs.get("account")

        return self.update_status_in_google_sheet(
            worksheet_name=str(resolved_sheet),
            excel_row=int(resolved_row),
            new_status=str(resolved_status),
            live_url=resolved_url,
            timestamp_str=resolved_ts,
            account_name=resolved_acc,
        )

