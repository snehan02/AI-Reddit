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
        excel_row: int,
        new_status: str,
        timestamp_str: Optional[str] = None,
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
        from config import is_force_test_mode_enabled
        if is_force_test_mode_enabled():
            return False, "Excel update aborted: FORCE_TEST_MODE environment safeguard is active."

        if not self.workbook_path.exists():
            return False, f"Workbook {self.workbook_path} not found"

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

        if excel_row < 2 or excel_row > max_row:
            wb.close()
            return False, f"Invalid row number {excel_row} for sheet '{worksheet_name}' (max: {max_row})"

        # Find Status column dynamically
        status_col_idx: Optional[int] = None
        for c in range(1, max_col + 1):
            header_val = sheet.cell(row=1, column=c).value
            if header_val and str(header_val).strip().lower() == "status":
                status_col_idx = c
                break

        if not status_col_idx:
            wb.close()
            return False, f"Could not find 'Status' column in worksheet '{worksheet_name}'. Write aborted."

        # Safely update the Status cell
        sheet.cell(row=excel_row, column=status_col_idx, value=new_status)
        logger.info(
            "Updated row %d in sheet '%s' (col %d 'Status') -> '%s'",
            excel_row, worksheet_name, status_col_idx, new_status
        )

        # Handle Column N timestamp if requested
        if timestamp_str:
            if max_col >= 14:
                col_n_header = sheet.cell(row=1, column=14).value
                if col_n_header and "timestamp" in str(col_n_header).lower():
                    sheet.cell(row=excel_row, column=14, value=timestamp_str)
                    logger.info("Updated Column N timestamp on row %d: %s", excel_row, timestamp_str)
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
