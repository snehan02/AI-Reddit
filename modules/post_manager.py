"""Post task manager for 'Posts' worksheet."""

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging
import openpyxl

from modules.account_manager import AccountManager
from modules.duplicate_checker import DuplicateChecker
from modules.task_builder import PostTask
from modules.validator import TaskValidator

logger = logging.getLogger(__name__)


class PostManager:
    """Extracts, validates, and prepares post tasks from Posts worksheet."""

    def __init__(
        self,
        workbook_path: Path | str,
        sheet_name: str = "Posts",
        account_manager: Optional[AccountManager] = None,
        duplicate_checker: Optional[DuplicateChecker] = None,
        allow_account_independent: bool = False,
    ):
        self.workbook_path = Path(workbook_path)
        self.sheet_name = sheet_name
        self.account_manager = account_manager
        self.duplicate_checker = duplicate_checker
        self.allow_account_independent = allow_account_independent

    def load_tasks(self, workbook: Optional[openpyxl.Workbook] = None) -> Tuple[List[PostTask], Dict[str, Any]]:
        """Load and validate all rows from the Posts worksheet.
        
        If 'Account posted' column is absent, marks all tasks as skipped
        due to schema failure and reports schema_error.
        """
        if not self.workbook_path.exists():
            logger.error("Workbook %s does not exist", self.workbook_path)
            return [], {
                "total": 0,
                "eligible": 0,
                "skipped": 0,
                "schema_error": "File not found",
                "skipped_by_reason": {},
            }

        should_close = False
        if workbook is not None:
            wb = workbook
        else:
            wb = openpyxl.load_workbook(self.workbook_path, data_only=True)
            should_close = True
        if self.sheet_name not in wb.sheetnames:
            logger.warning("Worksheet %s not found in workbook", self.sheet_name)
            if should_close:
                wb.close()
            return [], {
                "total": 0,
                "eligible": 0,
                "skipped": 0,
                "schema_error": f"Sheet '{self.sheet_name}' missing",
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

        col_subreddit = header_map.get("Subreddit")
        col_title = header_map.get("Post title")
        col_body = header_map.get("Post body")
        col_link = header_map.get("Shoro link")
        col_account = header_map.get("Account posted")
        col_status = header_map.get("Status")

        has_account_column = col_account is not None
        schema_error = None

        if not has_account_column and not self.allow_account_independent:
            schema_error = f"Worksheet '{self.sheet_name}' is missing required column 'Account posted'"
            logger.warning(
                "%s. All post tasks in this sheet will be marked as skipped.",
                schema_error,
            )

        tasks: List[PostTask] = []
        skipped_by_reason: Counter[str] = Counter()
        total_count = 0
        eligible_count = 0

        for row_idx in range(2, max_row + 1):
            row_has_data = any(
                sheet.cell(row=row_idx, column=c).value is not None
                for c in range(1, max_col + 1)
            )
            if not row_has_data:
                continue

            total_count += 1

            raw_sub = sheet.cell(row=row_idx, column=col_subreddit).value if col_subreddit else None
            raw_title = sheet.cell(row=row_idx, column=col_title).value if col_title else None
            raw_body = sheet.cell(row=row_idx, column=col_body).value if col_body else None
            raw_link = sheet.cell(row=row_idx, column=col_link).value if col_link else None
            raw_account = sheet.cell(row=row_idx, column=col_account).value if col_account else None
            raw_status = sheet.cell(row=row_idx, column=col_status).value if col_status else None

            subreddit = str(raw_sub).strip() if raw_sub is not None else ""
            title = str(raw_title).strip() if raw_title is not None else ""
            # PRESERVE post body EXACTLY (no stripping of approved body content)
            body = str(raw_body) if raw_body is not None else ""
            shoro_link = str(raw_link).strip() if raw_link is not None else None
            account_id = str(raw_account).strip() if raw_account is not None else None
            existing_status = str(raw_status).strip() if raw_status is not None else None

            is_eligible = True
            skip_reason = None
            chrome_profile_name = "shoro-test"

            # 1. Schema check
            if not has_account_column and not self.allow_account_independent:
                is_eligible = False
                skip_reason = "Missing required column 'Account posted'"
            elif has_account_column:
                # 2. Account assignment check
                valid_acc, acc_reason = TaskValidator.validate_account(account_id)
                if not valid_acc:
                    is_eligible = False
                    skip_reason = acc_reason
                elif self.account_manager and account_id:
                    profile, prof_err = self.account_manager.resolve_account(account_id)
                    if not profile:
                        is_eligible = False
                        skip_reason = f"Skipped - unverified/disabled account: {prof_err}"
                    else:
                        chrome_profile_name = profile.chrome_profile_name
            elif self.allow_account_independent:
                chrome_profile_name = "shoro-test"

            # 3. Existing status check
            if is_eligible:
                actionable, reason = TaskValidator.check_existing_status(existing_status)
                if not actionable:
                    is_eligible = False
                    skip_reason = reason

            # 4. Field validation
            if is_eligible:
                valid_fields, reason = TaskValidator.validate_post_fields(subreddit, title, body)
                if not valid_fields:
                    is_eligible = False
                    skip_reason = reason

            if is_eligible and self.duplicate_checker and account_id:
                temp_task = PostTask(
                    worksheet=self.sheet_name,
                    excel_row=row_idx,
                    account_id=account_id,
                    subreddit=subreddit,
                    title=title,
                    body=body,
                )
                is_dup, dup_reason = self.duplicate_checker.check_task(temp_task)
                if is_dup:
                    is_eligible = False
                    skip_reason = dup_reason

            if is_eligible:
                eligible_count += 1
                status = "Ready for review"
            else:
                status = skip_reason or "Skipped"
                if "schema error" in str(skip_reason):
                    category = "Missing 'Account posted' column in sheet"
                elif "blank account" in str(skip_reason):
                    category = "Blank account assignment"
                elif "unverified/disabled account" in str(skip_reason) or "disabled" in str(skip_reason):
                    category = "Unverified or disabled account profile"
                elif "non-actionable" in str(skip_reason):
                    category = f"Non-actionable status ({existing_status})"
                elif "missing content" in str(skip_reason):
                    category = "Missing required post content"
                elif "duplicate" in str(skip_reason):
                    category = "Duplicate task previously completed"
                else:
                    category = str(skip_reason)
                skipped_by_reason[category] += 1

            task = PostTask(
                worksheet=self.sheet_name,
                excel_row=row_idx,
                account_id=account_id if (account_id and account_id.lower() != "none") else None,
                chrome_profile_name=chrome_profile_name,
                subreddit=subreddit,
                title=title,
                body=body,
                shoro_link=shoro_link,
                status=status,
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
            "schema_error": schema_error,
            "skipped_by_reason": dict(skipped_by_reason),
        }
        return tasks, stats
