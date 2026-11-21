"""Safe Excel workbook handler with backup, atomic saves, and schema validation."""

from datetime import datetime
from pathlib import Path
import gc
import logging
import os
import shutil
import time
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import openpyxl

logger = logging.getLogger(__name__)


class ExcelHandler:
    """Safely updates existing status cells in the source workbook."""

    def __init__(
        self,
        workbook_path: Path | str = "data/Reddit Threads.xlsx",
        backup_dir: Path | str = "backups",
        tz_name: str = "Asia/Kolkata",
    ):
        p = Path(workbook_path)
        if not p.is_absolute():
            p = Path(os.getcwd()) / p
        self.workbook_path = p
        self.backup_dir = Path(backup_dir)
        self.tz_name = tz_name
        self._backup_created_for_session = False

    def get_sheet_names(self) -> List[str]:
        """Return list of sheet names in the workbook, or empty list if unreadable."""
        if not self.workbook_path.exists():
            return []
        try:
            wb = openpyxl.load_workbook(self.workbook_path, read_only=True)
            names = list(wb.sheetnames)
            wb.close()
            return names
        except Exception as e:
            logger.warning("Could not read sheet names from %s: %s", self.workbook_path, e)
            return []

    def set_workbook_path(self, new_path: Path | str) -> Tuple[bool, str]:
        """Update and validate the active workbook path."""
        p = Path(new_path)
        if not p.is_absolute():
            p = Path(os.getcwd()) / p
        if not p.exists():
            return False, f"File does not exist: {p}"
        if p.suffix.lower() != ".xlsx":
            return False, f"File must be an Excel workbook (.xlsx): {p}"
        self.workbook_path = p
        return True, str(p)

    def get_recent_updated_rows(self, worksheet_name: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Return recently updated or completed rows for UI inspection."""
        if not self.workbook_path.exists():
            return []
        try:
            wb = openpyxl.load_workbook(self.workbook_path, read_only=True, data_only=True)
            target_sheet = None
            if worksheet_name and worksheet_name in wb.sheetnames:
                target_sheet = worksheet_name
            else:
                w_lower = str(worksheet_name or "").strip().lower()
                for s in wb.sheetnames:
                    if s.strip().lower() == w_lower:
                        target_sheet = s
                        break
            if not target_sheet:
                if len(wb.sheetnames) == 1 or not worksheet_name:
                    target_sheet = wb.sheetnames[0]
                else:
                    wb.close()
                    return []

            ws = wb[target_sheet]
            max_r = ws.max_row or 0
            if max_r < 2:
                wb.close()
                return []

            max_c = ws.max_column or 15
            headers = [ws.cell(row=1, column=c).value for c in range(1, max_c + 1)]
            col_idx: Dict[str, int] = {}
            for i, h in enumerate(headers, start=1):
                if not h:
                    continue
                h_c = str(h).strip().lower()
                if h_c in ("status", "my status"):
                    col_idx["status"] = i
                elif "account" in h_c:
                    col_idx["account"] = i
                elif any(k in h_c for k in ("time", "date", "last checked")):
                    col_idx["timestamp"] = i
                elif "title" in h_c:
                    col_idx["title"] = i

            rows: List[Dict[str, Any]] = []
            for r in range(max_r, 1, -1):
                st_val = ws.cell(row=r, column=col_idx.get("status", 13)).value
                if st_val and str(st_val).strip() not in ("", "None", "To review"):
                    rows.append({
                        "row": r,
                        "status": str(st_val),
                        "account": str(ws.cell(row=r, column=col_idx.get("account", 12)).value or ""),
                        "timestamp": str(ws.cell(row=r, column=col_idx.get("timestamp", 1)).value or ""),
                        "title": str(ws.cell(row=r, column=col_idx.get("title", 3)).value or "")[:35],
                    })
                    if len(rows) >= limit:
                        break
            wb.close()
            return rows
        except Exception as e:
            logger.warning("Error reading recent rows from %s: %s", self.workbook_path, e)
            return []

    def create_timestamped_backup(self) -> Tuple[bool, str]:
        """Create a timestamped backup before any write operation.
        
        Strict safety:
        - Creates a new incremental backup before each write.
        - Appends timestamp and counter to guarantee uniqueness (never overwrites earlier backup).
        - Verifies the backup exists, is non-empty, and matches source file size.
        - Aborts and returns (False, err) if backup creation fails.
        """
        if not self.workbook_path.exists():
            return False, f"Source workbook {self.workbook_path} does not exist"

        try:
            source_size = self.workbook_path.stat().st_size
        except Exception as exc:
            return False, f"Cannot inspect source workbook size: {exc}"

        self.backup_dir.mkdir(parents=True, exist_ok=True)

        now_dt = datetime.now(ZoneInfo(self.tz_name))
        now_str = now_dt.strftime("%Y%m%d_%H%M%S_%f")
        backup_name = f"{self.workbook_path.stem}_backup_{now_str}{self.workbook_path.suffix}"
        backup_path = self.backup_dir / backup_name
        counter = 1
        while backup_path.exists():
            backup_name = f"{self.workbook_path.stem}_backup_{now_str}_{counter}{self.workbook_path.suffix}"
            backup_path = self.backup_dir / backup_name
            counter += 1

        try:
            shutil.copy2(self.workbook_path, backup_path)

            if not backup_path.exists():
                return False, f"Backup file {backup_path} was not created."
            backup_size = backup_path.stat().st_size
            if backup_size == 0 or backup_size != source_size:
                if backup_path.exists():
                    try:
                        backup_path.unlink()
                    except Exception:
                        pass
                return False, f"Backup file {backup_path} size mismatch (source: {source_size}, backup: {backup_size})."

            self._backup_created_for_session = True
            logger.info("Created incremental workbook backup at %s (size: %d bytes)", backup_path, backup_size)
            return True, str(backup_path)
        except Exception as exc:
            err = f"Failed to create workbook backup: {exc}"
            logger.error(err)
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except Exception:
                    pass
            return False, err

    def update_task_status(
        self,
        worksheet_name: str,
        excel_row: Optional[int] = None,
        new_status: Optional[str] = None,
        timestamp_str: Optional[str] = None,
        account_name: Optional[str] = None,
        live_url: Optional[str] = None,
        destination_url: Optional[str] = None,
        task_id: Optional[str] = None,
        thread_title: Optional[str] = None,
        row_number: Optional[int] = None,
        status: Optional[str] = None,
        timestamp: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str]:
        """Update task row status, account, timestamps, and live url in the local workbook.
        
        Matches tasks securely using:
        1. Destination URL / Thread link match (if provided)
        2. Task ID match (if provided)
        3. Thread title match (if provided)
        4. Row mapping fallback (excel_row / row_number)
        
        Updates existing columns wherever they exist without adding duplicate columns or rows:
        - Status
        - My Status
        - Account Posted
        - Last Checked
        - Timestamp (or Time Stamp)
        - Date
        - Live URL
        """
        actual_row = excel_row if excel_row is not None else (row_number if row_number is not None else kwargs.get("excel_row"))
        actual_status = new_status if new_status is not None else (status if status is not None else kwargs.get("new_status", "Draft"))
        actual_timestamp = timestamp_str if timestamp_str is not None else (timestamp if timestamp is not None else kwargs.get("timestamp_str"))
        actual_account = account_name if account_name is not None else (kwargs.get("account_name") or kwargs.get("account_id"))
        actual_live_url = live_url if live_url is not None else kwargs.get("live_url")
        actual_dest_url = destination_url or kwargs.get("destination_url") or actual_live_url
        actual_task_id = task_id or kwargs.get("task_id")
        actual_title = thread_title or kwargs.get("thread_title") or kwargs.get("title")

        from config import is_force_test_mode_enabled
        if is_force_test_mode_enabled():
            return False, "Excel update aborted: FORCE_TEST_MODE environment safeguard is active."

        if not self.workbook_path.exists():
            return False, f"Excel workbook not found at '{self.workbook_path}'. Please check file path in settings."

        # 1. Create incremental backup BEFORE every write
        backup_success, backup_info = self.create_timestamped_backup()
        if not backup_success:
            return False, f"Aborting write: Backup creation failed ({backup_info})"

        # 2. Check if file is locked with brief retry
        locked = True
        for attempt in range(2):
            try:
                with open(self.workbook_path, "a+"):
                    pass
                locked = False
                break
            except (PermissionError, IOError):
                if attempt == 0:
                    time.sleep(0.4)
        if locked:
            return False, (
                f"Workbook '{self.workbook_path.name}' is currently open in Microsoft Excel. "
                "Please save and close the file in Excel to allow updates."
            )

        # 3. Load workbook (preserves formulas, styles, and other sheets)
        try:
            wb = openpyxl.load_workbook(self.workbook_path)
        except Exception as exc:
            return False, f"Failed to open workbook for update: {exc}"

        # Resolve sheet name (exact or case-insensitive)
        target_sheet_name = None
        if worksheet_name in wb.sheetnames:
            target_sheet_name = worksheet_name
        else:
            w_lower = str(worksheet_name or "").strip().lower()
            for s in wb.sheetnames:
                if s.strip().lower() == w_lower:
                    target_sheet_name = s
                    break

        if not target_sheet_name:
            if not str(worksheet_name or "").strip() and len(wb.sheetnames) == 1:
                target_sheet_name = wb.sheetnames[0]
                logger.info("Single sheet workbook detected. Using '%s'", target_sheet_name)
            else:
                wb.close()
                return False, f"Worksheet '{worksheet_name}' not found in workbook"

        sheet = wb[target_sheet_name]
        max_col = sheet.max_column or 0
        max_row = sheet.max_row or 0

        if max_row < 2:
            wb.close()
            return False, f"Sheet '{target_sheet_name}' has no data rows (max_row={max_row})."

        # 4. Map columns dynamically from header row
        col_map: Dict[str, int] = {}
        url_col_candidates: List[int] = []
        id_col_candidates: List[int] = []
        title_col_candidates: List[int] = []

        for c in range(1, max_col + 1):
            h_val = sheet.cell(row=1, column=c).value
            if h_val:
                h_clean = str(h_val).strip().lower()
                if h_clean == "status":
                    col_map["status"] = c
                elif h_clean in ("my status", "mystatus"):
                    col_map["my_status"] = c
                elif "account posted" in h_clean or h_clean == "account":
                    col_map["account"] = c
                elif "last checked" in h_clean:
                    col_map["last_checked"] = c
                elif any(k in h_clean for k in ("time stamp", "timestamp", "date posted")):
                    col_map["timestamp"] = c
                elif h_clean == "date":
                    col_map["date"] = c
                elif any(k in h_clean for k in ("live url", "published url", "permalink")):
                    col_map["live_url"] = c

                if any(k in h_clean for k in ("thread link", "thread url", "destination url", "url", "link", "permalink")):
                    url_col_candidates.append(c)
                if any(k in h_clean for k in ("task id", "task_id", "id")):
                    id_col_candidates.append(c)
                if any(k in h_clean for k in ("thread title", "title", "post title")):
                    title_col_candidates.append(c)

        if "status" not in col_map and "my_status" in col_map:
            col_map["status"] = col_map["my_status"]

        if "status" not in col_map:
            wb.close()
            return False, f"Could not find 'Status' column in worksheet '{target_sheet_name}'. Write aborted."

        # 5. Smart Target Row Matching
        matched_row: Optional[int] = None

        # Priority 1: Match by Destination URL / Thread Link
        if actual_dest_url:
            clean_dest = str(actual_dest_url).strip().rstrip("/").lower()
            search_cols = url_col_candidates or list(range(1, max_col + 1))
            for c_idx in search_cols:
                for r in range(2, max_row + 1):
                    cell_val = sheet.cell(row=r, column=c_idx).value
                    if cell_val:
                        clean_cell = str(cell_val).strip().rstrip("/").lower()
                        if clean_cell == clean_dest or (clean_dest and clean_dest in clean_cell) or (clean_cell and clean_cell in clean_dest):
                            matched_row = r
                            logger.info("Matched task to row %d via URL match in col %d", matched_row, c_idx)
                            break
                if matched_row:
                    break

        # Priority 2: Match by Task ID
        if not matched_row and actual_task_id:
            clean_id = str(actual_task_id).strip().lower()
            for c_idx in id_col_candidates:
                for r in range(2, max_row + 1):
                    cell_val = sheet.cell(row=r, column=c_idx).value
                    if cell_val and str(cell_val).strip().lower() == clean_id:
                        matched_row = r
                        logger.info("Matched task to row %d via Task ID in col %d", matched_row, c_idx)
                        break
                if matched_row:
                    break

        # Priority 3: Match by Thread Title
        if not matched_row and actual_title:
            clean_title = str(actual_title).strip().lower()
            if len(clean_title) > 5:
                for c_idx in title_col_candidates:
                    for r in range(2, max_row + 1):
                        cell_val = sheet.cell(row=r, column=c_idx).value
                        if cell_val and str(cell_val).strip().lower() == clean_title:
                            matched_row = r
                            logger.info("Matched task to row %d via Title in col %d", matched_row, c_idx)
                            break
                    if matched_row:
                        break

        # Priority 4: Fallback to explicit row number if within bounds
        if not matched_row and actual_row and 2 <= actual_row <= max_row:
            matched_row = actual_row
            logger.info("Using explicit row number %d for task update", matched_row)

        if not matched_row and actual_row is not None and (actual_row < 2 or actual_row > max_row):
            wb.close()
            return False, f"Invalid row number {actual_row} for sheet '{target_sheet_name}' (max: {max_row})"

        if not matched_row:
            wb.close()
            return False, f"Could not determine target row in sheet '{target_sheet_name}' (row: {actual_row}, url: {actual_dest_url})"

        # 6. Safely update target cells in matched_row
        if actual_status:
            if "status" in col_map:
                sheet.cell(row=matched_row, column=col_map["status"], value=actual_status)
            if "my_status" in col_map:
                sheet.cell(row=matched_row, column=col_map["my_status"], value=actual_status)

        if actual_account and "account" in col_map:
            sheet.cell(row=matched_row, column=col_map["account"], value=actual_account)

        if actual_timestamp:
            if "last_checked" in col_map:
                sheet.cell(row=matched_row, column=col_map["last_checked"], value=actual_timestamp)
            if "timestamp" in col_map:
                sheet.cell(row=matched_row, column=col_map["timestamp"], value=actual_timestamp)
            if "date" in col_map:
                date_val = str(actual_timestamp).split()[0] if " " in str(actual_timestamp) else str(actual_timestamp)
                sheet.cell(row=matched_row, column=col_map["date"], value=date_val)

        if actual_live_url and "live_url" in col_map:
            sheet.cell(row=matched_row, column=col_map["live_url"], value=actual_live_url)

        # 7. Atomic Save via Temporary File in the same directory
        temp_filename = f"{self.workbook_path.stem}_atomic_tmp_{uuid.uuid4().hex[:8]}{self.workbook_path.suffix}"
        temp_path = self.workbook_path.parent / temp_filename

        try:
            wb.save(temp_path)
            wb.close()
            del wb
            gc.collect()

            if not temp_path.exists():
                raise IOError(f"Temporary workbook {temp_path} was not created.")

            temp_size = temp_path.stat().st_size
            if temp_size == 0:
                raise IOError(f"Temporary workbook {temp_path} is empty (0 bytes).")

            if not zipfile.is_zipfile(temp_path):
                raise IOError(f"Temporary workbook {temp_path} is corrupted (not a valid zip/xlsx archive).")

            # Atomically replace target workbook
            os.replace(temp_path, self.workbook_path)
            logger.info("Atomically updated workbook %s via temporary file %s", self.workbook_path, temp_path.name)
            return True, f"Successfully updated row {matched_row} Status to '{actual_status}' in '{target_sheet_name}'"

        except Exception as exc:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return False, f"Failed to save workbook updates atomically: {exc}"
