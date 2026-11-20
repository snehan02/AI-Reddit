"""Safe Excel workbook handler with backup, atomic saves, and schema validation."""

from datetime import datetime
from pathlib import Path
import gc
import logging
import os
import shutil
import uuid
import zipfile
from typing import Dict, Optional, Tuple
from typing import Optional, Tuple
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
        self.workbook_path = Path(workbook_path)
        self.backup_dir = Path(backup_dir)
        self.tz_name = tz_name
        self._backup_created_for_session = False

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

        # High-resolution timestamp and collision-resistant counter
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

            # Verify backup exists, is non-empty, and matches source size
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
        row_number: Optional[int] = None,
        status: Optional[str] = None,
        timestamp: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, str]:
        """Update the existing Status column for a specified row.
        
        Strict safety:
        - Blocks writes if FORCE_TEST_MODE is active.
        - Creates a verified incremental backup before every write.
        - Validates file locking before attempting read/write.
        - Validates Status column index dynamically; never inserts new columns.
        - Only writes timestamp to Column N if Column N actually exists and is verified.
        - Saves to a temporary file in the same directory first (never directly over production file).
        - Verifies temporary file integrity (exists, non-empty, valid zip/xlsx archive).
        - Uses os.replace() to atomically replace the target workbook.
        - Cleans up temporary files safely upon any error.
        - Preserves all existing worksheets, formatting, formulas, and unrelated cells.
        """
        # Resolve flexible alias arguments
        actual_row = excel_row if excel_row is not None else (row_number if row_number is not None else kwargs.get("excel_row"))
        actual_status = new_status if new_status is not None else (status if status is not None else kwargs.get("new_status", "Draft"))
        actual_timestamp = timestamp_str if timestamp_str is not None else (timestamp if timestamp is not None else kwargs.get("timestamp_str"))
        actual_account = account_name if account_name is not None else kwargs.get("account_id")

        from config import is_force_test_mode_enabled
        if is_force_test_mode_enabled():
            return False, "Excel update aborted: FORCE_TEST_MODE environment safeguard is active."

        if not self.workbook_path.exists():
            return False, f"Workbook {self.workbook_path} not found"

        if actual_row is None:
            return False, "Row number not provided for update."

        # 1. Create incremental backup BEFORE every write
        backup_success, backup_info = self.create_timestamped_backup()
        if not backup_success:
            return False, f"Aborting write: Backup creation failed ({backup_info})"

        # 2. Check if original file is locked by Excel
        try:
            with open(self.workbook_path, "a+"):
                pass
        except PermissionError:
            return False, (
                f"Workbook '{self.workbook_path}' is currently open and locked by Excel. "
                "Please save and close the file in Excel, then try again."
            )

        # 3. Load workbook (without data_only=True to preserve formulas and formatting)
        try:
            wb = openpyxl.load_workbook(self.workbook_path)
        except Exception as exc:
            return False, f"Failed to open workbook for update: {exc}"

        if worksheet_name not in wb.sheetnames:
            wb.close()
            return False, f"Worksheet '{worksheet_name}' not found in workbook"

        sheet = wb[worksheet_name]
        max_col = sheet.max_column or 0
        max_row = sheet.max_row or 0

        if actual_row < 2 or actual_row > max_row:
            wb.close()
            return False, f"Invalid row number {actual_row} for sheet '{worksheet_name}' (max: {max_row})"

        # Find Status column dynamically (and My status if present)
        status_col_idx: Optional[int] = None
        my_status_col_idx: Optional[int] = None
        for c in range(1, max_col + 1):
            header_val = sheet.cell(row=1, column=c).value
            if header_val:
                h_clean = str(header_val).strip().lower()
                if h_clean == "status":
                    status_col_idx = c
                elif h_clean in ("my status", "mystatus"):
                    my_status_col_idx = c

        if not status_col_idx and my_status_col_idx:
            status_col_idx = my_status_col_idx

        if not status_col_idx:
            wb.close()
            return False, f"Could not find 'Status' column in worksheet '{worksheet_name}'. Write aborted."

        # Safely update the Status cell
        sheet.cell(row=actual_row, column=status_col_idx, value=actual_status)
        logger.info(
            "Updated row %d in sheet '%s' (col %d 'Status') -> '%s'",
            actual_row, worksheet_name, status_col_idx, actual_status
        )

        # Also update My status if present
        if my_status_col_idx and my_status_col_idx != status_col_idx:
            sheet.cell(row=actual_row, column=my_status_col_idx, value=actual_status)
            logger.info(
                "Updated row %d in sheet '%s' (col %d 'My status') -> '%s'",
                actual_row, worksheet_name, my_status_col_idx, actual_status
            )

        # Update Account posted column if provided and column exists
        if actual_account:
            for c in range(1, max_col + 1):
                h_val = str(sheet.cell(row=1, column=c).value or "").strip().lower()
                if "account posted" in h_val or h_val == "account":
                    sheet.cell(row=actual_row, column=c, value=actual_account)
                    logger.info("Updated row %d (col %d '%s') -> '%s'", actual_row, c, h_val, actual_account)
                    break

        # Update Last checked / Date column if timestamp provided
        if actual_timestamp:
            for c in range(1, max_col + 1):
                h_val = str(sheet.cell(row=1, column=c).value or "").strip().lower()
                if "last checked" in h_val:
                    sheet.cell(row=actual_row, column=c, value=actual_timestamp)
                    logger.info("Updated row %d Last checked (col %d) -> '%s'", actual_row, c, actual_timestamp)
                    break

            for c in range(1, max_col + 1):
                h_val = str(sheet.cell(row=1, column=c).value or "").strip().lower()
                if h_val == "date":
                    date_val = actual_timestamp.split()[0] if " " in actual_timestamp else actual_timestamp
                    sheet.cell(row=actual_row, column=c, value=date_val)
                    logger.info("Updated row %d Date (col %d) -> '%s'", actual_row, c, date_val)
                    break

        # Handle Column N timestamp if requested
        if actual_timestamp:
            if max_col >= 14:
                col_n_header = sheet.cell(row=1, column=14).value
                col_n_str = str(col_n_header or "").replace(" ", "").replace("_", "").lower()
                if col_n_header and "timestamp" in col_n_str:
                    sheet.cell(row=actual_row, column=14, value=actual_timestamp)
                    logger.info("Updated Column N timestamp on row %d: %s", actual_row, actual_timestamp)
                else:
                    logger.warning(
                        "Column N header is '%s' (not verified timestamp). Skipping Column N write per PRD 6.5.",
                        col_n_header
                    )
            else:
                logger.info(
                    "Column N does not exist in '%s' (max_column=%d). Skipping Column N write to prevent adding columns.",
                    worksheet_name, max_col
                )

        # 4. Atomic Save via Temporary File in the same directory
        temp_filename = f"{self.workbook_path.stem}_atomic_tmp_{uuid.uuid4().hex[:8]}{self.workbook_path.suffix}"
        temp_path = self.workbook_path.parent / temp_filename

        try:
            # Save modifications to temporary file first (NEVER directly over target workbook)
            wb.save(temp_path)
            wb.close()
            del wb
            gc.collect()

            # Verify the temporary file was written completely and validly
            if not temp_path.exists():
                raise IOError(f"Temporary workbook {temp_path} was not created.")

            temp_size = temp_path.stat().st_size
            if temp_size == 0:
                raise IOError(f"Temporary workbook {temp_path} is empty (0 bytes).")

            if not zipfile.is_zipfile(temp_path):
                raise IOError(f"Temporary workbook {temp_path} is corrupted (not a valid zip/xlsx archive).")

            # Atomically replace the target workbook with the verified temporary workbook
            os.replace(temp_path, self.workbook_path)
            logger.info("Atomically updated workbook %s via temporary file %s", self.workbook_path, temp_path.name)

            # Trigger Google Sheet update if webhook/syncer is configured
            try:
                from modules.google_sheets_syncer import GoogleSheetsSyncer
                syncer = GoogleSheetsSyncer()
                syncer.update_status_in_google_sheet(
                    worksheet_name=worksheet_name,
                    excel_row=excel_row,
                    new_status=new_status,
                    live_url=live_url,
                    timestamp_str=timestamp_str,
                )
            except Exception as g_err:
                logger.debug("Google Sheet auto-sync notice: %s", g_err)

            return True, f"Successfully updated row {excel_row} Status to '{new_status}' in '{worksheet_name}'"

        except Exception as exc:
            try:
                wb.close()
            except Exception:
                pass
            gc.collect()

            # Safe cleanup of temporary file if it exists
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception as clean_exc:
                    logger.warning("Failed to remove temporary file %s: %s", temp_path, clean_exc)

            err = f"Failed to save workbook updates atomically: {exc}"
            logger.error(err)
            return False, err
