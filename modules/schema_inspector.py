"""Schema Inspector for Reddit workbook validation.

Inspects sheets, headers, row counts, and timestamp column availability
without modifying the source workbook.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging
import openpyxl

logger = logging.getLogger(__name__)

# Expected critical columns according to PRD
DRIVING_DMV_REQUIRED_COLS = [
    "Thread link",
    "Shoro AI reply",
    "Account posted",
    "Status",
]

POSTS_REQUIRED_COLS = [
    "Subreddit",
    "Post title",
    "Post body",
    "Account posted",
    "Status",
]


@dataclass
class SheetSchema:
    name: str
    max_row: int
    max_column: int
    headers: List[Optional[str]]
    header_indices: Dict[str, int] = field(default_factory=dict)
    missing_required_columns: List[str] = field(default_factory=list)
    has_column_n: bool = False
    column_n_header: Optional[str] = None
    column_n_samples: List[Any] = field(default_factory=list)
    populated_row_count: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class WorkbookSchemaReport:
    file_path: str
    file_exists: bool
    is_valid_file: bool
    sheet_names: List[str] = field(default_factory=list)
    sheets: Dict[str, SheetSchema] = field(default_factory=dict)
    ignored_sheets: List[str] = field(default_factory=list)
    unexpected_sheets: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    is_valid_for_comments: bool = False
    is_valid_for_posts: bool = False

    def summary(self) -> str:
        lines = [
            f"Workbook: {self.file_path}",
            f"File exists: {self.file_exists}",
            f"Sheets found: {', '.join(self.sheet_names)}",
            f"Comment tasks eligible to run: {self.is_valid_for_comments}",
            f"Post tasks eligible to run: {self.is_valid_for_posts}",
        ]
        if self.errors:
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"  - {err}")
        if self.warnings:
            lines.append("Warnings:")
            for warn in self.warnings:
                lines.append(f"  - {warn}")
        return "\n".join(lines)


class SchemaInspector:
    """Inspects Excel workbooks against PRD specifications."""

    KNOWN_SHEETS = {"Driving DMV", "Posts", "NCERT Reddit", "Threads"}
    IGNORED_SHEETS = {"NCERT Reddit", "Threads"}

    def __init__(self, workbook_path: Path | str = "data/Reddit Threads.xlsx"):
        self.workbook_path = Path(workbook_path)

    def inspect(self) -> WorkbookSchemaReport:
        path = self.workbook_path
        if not path.exists():
            return WorkbookSchemaReport(
                file_path=str(path),
                file_exists=False,
                is_valid_file=False,
                errors=[f"Workbook file does not exist: {path}"],
            )

        if not path.suffix.lower() in [".xlsx", ".xlsm"]:
            return WorkbookSchemaReport(
                file_path=str(path),
                file_exists=True,
                is_valid_file=False,
                errors=[f"Unsupported file format: {path.suffix}. Expected .xlsx"],
            )

        try:
            wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
        except Exception as exc:
            return WorkbookSchemaReport(
                file_path=str(path),
                file_exists=True,
                is_valid_file=False,
                errors=[f"Failed to open workbook: {exc}"],
            )

        report = WorkbookSchemaReport(
            file_path=str(path),
            file_exists=True,
            is_valid_file=True,
            sheet_names=list(wb.sheetnames),
        )

        # Check required sheets
        if "Driving DMV" not in wb.sheetnames:
            report.errors.append("Required worksheet 'Driving DMV' is missing from workbook.")
        if "Posts" not in wb.sheetnames:
            report.warnings.append("Worksheet 'Posts' is missing from workbook.")

        # Classify sheets
        for sname in wb.sheetnames:
            if sname in self.IGNORED_SHEETS:
                report.ignored_sheets.append(sname)
            elif sname not in self.KNOWN_SHEETS:
                report.unexpected_sheets.append(sname)
                report.warnings.append(f"Unexpected worksheet found: '{sname}'. Will be ignored.")

        # Inspect Driving DMV
        if "Driving DMV" in wb.sheetnames:
            dmv_schema = self._inspect_sheet(wb["Driving DMV"], "Driving DMV", DRIVING_DMV_REQUIRED_COLS)
            report.sheets["Driving DMV"] = dmv_schema
            if not dmv_schema.missing_required_columns:
                report.is_valid_for_comments = True
            else:
                report.is_valid_for_comments = False
                report.errors.append(
                    f"'Driving DMV' is missing required columns: {dmv_schema.missing_required_columns}"
                )
            if not dmv_schema.has_column_n:
                report.warnings.append(
                    "'Driving DMV' has no Column N (13 columns present). Column N will not be created; timestamps will be stored in SQLite."
                )

        # Inspect Posts
        if "Posts" in wb.sheetnames:
            posts_schema = self._inspect_sheet(wb["Posts"], "Posts", POSTS_REQUIRED_COLS)
            report.sheets["Posts"] = posts_schema
            if not posts_schema.missing_required_columns:
                report.is_valid_for_posts = True
            else:
                # Missing Account posted is a known critical schema issue for Posts
                report.is_valid_for_posts = False
                report.errors.append(
                    f"'Posts' worksheet schema check failed: missing required columns: {posts_schema.missing_required_columns}. "
                    "Per PRD FR-03, account assignments cannot be guessed; all post tasks will be safely skipped until resolved."
                )
            if not posts_schema.has_column_n:
                report.warnings.append(
                    "'Posts' has no Column N (7 columns present). Column N will not be created; timestamps will be stored in SQLite."
                )

        wb.close()
        return report

    def _inspect_sheet(
        self, sheet: openpyxl.worksheet.worksheet.Worksheet, name: str, required_cols: List[str]
    ) -> SheetSchema:
        max_row = sheet.max_row or 0
        max_col = sheet.max_column or 0

        # Read row 1 headers
        headers: List[Optional[str]] = []
        header_indices: Dict[str, int] = {}

        if max_row >= 1 and max_col >= 1:
            for col_idx in range(1, max_col + 1):
                val = sheet.cell(row=1, column=col_idx).value
                header_str = str(val).strip() if val is not None else None
                headers.append(header_str)
                if header_str:
                    header_indices[header_str] = col_idx

        # Check required columns
        missing: List[str] = []
        for req in required_cols:
            if req not in header_indices:
                missing.append(req)

        # Inspect Column N (1-based index 14)
        has_col_n = max_col >= 14
        col_n_header = None
        col_n_samples: List[Any] = []
        if has_col_n:
            val = sheet.cell(row=1, column=14).value
            col_n_header = str(val).strip() if val is not None else None
            for r in range(2, min(7, max_row + 1)):
                col_n_samples.append(sheet.cell(row=r, column=14).value)

        # Count populated rows (rows with at least one non-empty cell)
        populated = 0
        for r in range(2, max_row + 1):
            row_has_data = any(
                sheet.cell(row=r, column=c).value is not None
                for c in range(1, max_col + 1)
            )
            if row_has_data:
                populated += 1

        schema = SheetSchema(
            name=name,
            max_row=max_row,
            max_column=max_col,
            headers=headers,
            header_indices=header_indices,
            missing_required_columns=missing,
            has_column_n=has_col_n,
            column_n_header=col_n_header,
            column_n_samples=col_n_samples,
            populated_row_count=populated,
        )

        if missing:
            schema.errors.append(f"Missing required columns in '{name}': {', '.join(missing)}")
        if not has_col_n:
            schema.warnings.append(f"Column N does not exist in '{name}' (total columns: {max_col}).")

        return schema

