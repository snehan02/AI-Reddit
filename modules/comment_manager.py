"""Comment task manager for 'Driving DMV' worksheet.

Extracts, validates, and prepares comment tasks while strictly preserving
the exact approved reply text and original Excel row numbers.
"""

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging
import openpyxl

from modules.account_manager import AccountManager
from modules.duplicate_checker import DuplicateChecker
from modules.task_builder import CommentTask
from modules.validator import TaskValidator

logger = logging.getLogger(__name__)


class CommentManager:
    """Extracts, validates, and prepares comment tasks from Driving DMV."""

    def __init__(
        self,
        workbook_path: Path | str,
        sheet_name: str = "Driving DMV",
        account_manager: Optional[AccountManager] = None,
        duplicate_checker: Optional[DuplicateChecker] = None,
    ):
        self.workbook_path = Path(workbook_path)
        self.sheet_name = sheet_name
        self.account_manager = account_manager
        self.duplicate_checker = duplicate_checker

    def load_tasks(self, workbook: Optional[openpyxl.Workbook] = None) -> Tuple[List[CommentTask], Dict[str, Any]]:
        """Load and validate all rows from the worksheet.
        
        Returns:
            (tasks, summary_stats)
        """
        if not self.workbook_path.exists():
            logger.error("Workbook %s does not exist", self.workbook_path)
            return [], {
                "total": 0,
                "eligible": 0,
                "skipped": 0,
                "skipped_by_reason": {},
            }

        should_close = False
        if workbook is not None:
            wb = workbook
        else:
            wb = openpyxl.load_workbook(self.workbook_path, data_only=True)
            should_close = True
        if self.sheet_name not in wb.sheetnames:
            logger.error("Worksheet %s not found in workbook", self.sheet_name)
            if should_close:
                wb.close()
            return [], {
                "total": 0,
                "eligible": 0,
                "skipped": 0,
                "skipped_by_reason": {},
            }

        sheet = wb[self.sheet_name]
        max_row = sheet.max_row or 0
        max_col = sheet.max_column or 0

        # Map headers
        header_map: Dict[str, int] = {}
        for c in range(1, max_col + 1):
            val = sheet.cell(row=1, column=c).value
            if val is not None:
                header_map[str(val).strip()] = c

        col_url = header_map.get("Thread link")
        col_reply = header_map.get("Shoro AI reply")
        col_suggested = header_map.get("Suggested reply")
        col_revised = header_map.get("Revised suggested reply")
        col_why = header_map.get("Why it matters / What changed")
        col_account = header_map.get("Account posted")
        col_status = header_map.get("Status")
        col_title = header_map.get("Thread title")

        tasks: List[CommentTask] = []
        skipped_by_reason: Counter[str] = Counter()
        total_count = 0
        eligible_count = 0

        for row_idx in range(2, max_row + 1):
            # Check if row has any populated cells
            row_has_data = any(
                sheet.cell(row=row_idx, column=c).value is not None
                for c in range(1, max_col + 1)
            )
            if not row_has_data:
                continue

            total_count += 1

            raw_url = sheet.cell(row=row_idx, column=col_url).value if col_url else None
            raw_reply = sheet.cell(row=row_idx, column=col_reply).value if col_reply else None
            raw_suggested = sheet.cell(row=row_idx, column=col_suggested).value if col_suggested else None
            raw_revised = sheet.cell(row=row_idx, column=col_revised).value if col_revised else None
            raw_why = sheet.cell(row=row_idx, column=col_why).value if col_why else None
            raw_account = sheet.cell(row=row_idx, column=col_account).value if col_account else None
            raw_status = sheet.cell(row=row_idx, column=col_status).value if col_status else None
            raw_title = sheet.cell(row=row_idx, column=col_title).value if col_title else None

            thread_title = str(raw_title).strip() if raw_title is not None else ""
            destination_url = str(raw_url).strip() if raw_url is not None else ""
            # PRESERVE Shoro AI reply EXACTLY as written (Column K / col_reply)
            reply_text = str(raw_reply) if raw_reply is not None else ""
            suggested_reply = str(raw_suggested) if raw_suggested is not None else None
            revised_suggested_reply = str(raw_revised) if raw_revised is not None else None
            why_it_matters = str(raw_why) if raw_why is not None else None
            account_id = str(raw_account).strip() if raw_account is not None else None
            existing_status = str(raw_status).strip() if raw_status is not None else None

            is_eligible = True
            skip_reason = None
            chrome_profile_name = None

            # 1. Existing status check (non-actionable / archived / deleted / locked / completed)
            actionable, status_reason = TaskValidator.check_existing_status(existing_status)
            if not actionable:
                is_eligible = False
                skip_reason = status_reason

            # 2. Account check (blank or invalid assignment)
            if is_eligible:
                valid_acc, acc_reason = TaskValidator.validate_account(account_id)
                if not valid_acc:
                    is_eligible = False
                    skip_reason = acc_reason

            # 3. Account profile mapping & verification check (if account_manager provided)
            if is_eligible and self.account_manager and account_id:
                profile, prof_err = self.account_manager.resolve_account(account_id)
                if not profile:
                    is_eligible = False
                    skip_reason = f"Skipped - unverified/disabled account: {prof_err}"
                else:
                    chrome_profile_name = profile.chrome_profile_name

            # 4. Destination URL validation
            if is_eligible:
                valid_url, url_reason = TaskValidator.validate_url(destination_url)
                if not valid_url:
                    is_eligible = False
                    skip_reason = url_reason

            # 5. Exact reply text presence check
            if is_eligible:
                valid_content, content_reason = TaskValidator.validate_reply_text(reply_text)
                if not valid_content:
                    is_eligible = False
                    skip_reason = content_reason

            # 6. Duplicate check (if duplicate_checker provided)
            if is_eligible and self.duplicate_checker and account_id:
                temp_task = CommentTask(
                    worksheet=self.sheet_name,
                    excel_row=row_idx,
                    account_id=account_id,
                    thread_title=thread_title,
                    destination_url=destination_url,
                    reply_text=reply_text,
                )
                is_dup, dup_reason = self.duplicate_checker.check_task(temp_task)
                if is_dup:
                    is_eligible = False
                    skip_reason = dup_reason

            if is_eligible:
                eligible_count += 1
                task_status = "Ready for review"
            else:
                task_status = skip_reason or "Skipped"
                # Group skip reasons by category
                if "blank account" in str(skip_reason):
                    category = "Blank account assignment"
                elif "unverified/disabled account" in str(skip_reason) or "disabled" in str(skip_reason) or "not configured" in str(skip_reason):
                    category = "Unverified or disabled account profile"
                elif "non-actionable" in str(skip_reason):
                    category = f"Non-actionable status ({existing_status})"
                elif "invalid URL" in str(skip_reason):
                    category = "Invalid or non-web URL"
                elif "missing content" in str(skip_reason):
                    category = "Missing reply content"
                elif "duplicate" in str(skip_reason):
                    category = "Duplicate task previously completed"
                else:
                    category = str(skip_reason)
                skipped_by_reason[category] += 1

            task = CommentTask(
                worksheet=self.sheet_name,
                excel_row=row_idx,
                account_id=account_id if (account_id and account_id.lower() != "none") else None,
                chrome_profile_name=chrome_profile_name,
                thread_title=thread_title,
                destination_url=destination_url,
                reply_text=reply_text,
                suggested_reply=suggested_reply,
                revised_suggested_reply=revised_suggested_reply,
                why_it_matters=why_it_matters,
                status=task_status,
                is_eligible=is_eligible,
                skip_reason=skip_reason,
            )
            tasks.append(task)

        if should_close:
            wb.close()
        stats = {
            "total": total_count,
            "eligible": eligible_count,
            "skipped": total_count - eligible_count,
            "skipped_by_reason": dict(skipped_by_reason),
        }
        return tasks, stats
