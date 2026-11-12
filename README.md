# Shoro AI Reddit Engagement Operations Tool

A local Python application designed for human-in-the-loop Reddit engagement operations based on the Shoro AI Excel workbook (`Reddit Threads.xlsx`).

---

## 🛡️ Compliance & Safety Architecture

- **Human-in-the-Loop Review Only:** The application strictly disallows automated submission or publishing. It opens destination URLs in isolated Chrome profiles for human operator review and manual confirmation.
- **Source of Truth:** Excel remains the sole source of truth. Approved reply texts (`Shoro AI reply`) are preserved verbatim.
- **Fail-Closed on Missing Assignments:** Tasks with blank `Account posted` cells are skipped. Account assignments are never guessed.
- **Anti-Pollution / Account Isolation:** Every Reddit account maps to its own distinct Chrome user-data-directory in `config/accounts.json`. No session sharing or account rotation evasion.
- **No Credentials Stored:** No passwords, cookies, or secrets are ever accepted or stored.
- **Write-Safe Excel Handling:**
  - Automated timestamped backups are created in `backups/` before any write.
  - Column N is not modified unless explicitly confirmed (inspection showed Column N does not exist in the source sheets).
  - No new columns or sheet structure modifications are made.
- **Test Mode Default:** Test Mode is enabled by default to prevent accidental live workbook updates.

---

## 📁 Project Structure

```text
Shoro AI Reddit/
├── app.py                     # Streamlit Operator Dashboard
├── main.py                    # Diagnostic CLI runner
├── config.py                  # Dataclass application settings
├── requirements.txt           # Dependency specifications
├── README.md                  # Documentation and runbook
│
├── data/
│   └── Reddit Threads.xlsx   # Source workbook
│
├── config/
│   ├── accounts.json         # Account-to-Chrome-profile mapping
│   └── settings.json         # Runtime settings (Test Mode, daily caps)
│
├── modules/
│   ├── __init__.py
│   ├── schema_inspector.py   # Workbook structure and header inspection
│   ├── validator.py          # Field, URL, and status validators
│   ├── task_builder.py       # Comment and Post data models & fingerprints
│   ├── comment_manager.py    # Extracts Driving DMV comment tasks
│   ├── post_manager.py       # Extracts Posts draft tasks (safe fail-closed)
│   ├── account_manager.py    # Validates profiles and directory isolation
│   ├── browser_manager.py    # Playwright & Chrome review navigation
│   ├── duplicate_checker.py  # SHA-256 fingerprint duplicate prevention
│   ├── audit_logger.py       # SQLite execution events & history
│   ├── state_manager.py      # Session lifecycle, cap counting & resume
│   ├── excel_handler.py      # Non-destructive workbook updates & backups
│   └── workflow_controller.py# Pipeline orchestrator
│
├── database/
│   └── execution_state.db    # SQLite audit and fingerprint store
├── logs/
│   └── application.log       # Application logs
├── backups/                  # Timestamped workbook backups
└── tests/                    # 34 Unit and integration tests
    ├── test_schema_inspector.py
    ├── test_validator.py
    ├── test_task_builder.py
    ├── test_account_manager.py
    ├── test_browser_manager.py
    ├── test_audit_logger.py
    ├── test_duplicate_checker.py
    ├── test_state_manager.py
    ├── test_excel_handler.py
    └── test_workflow_controller.py
```

---

## 🚀 Quickstart & Usage

### 1. Requirements & Installation

Python 3.11+ is required.

```bash
pip install -r requirements.txt
```

### 2. Inspect Workbook Schema (CLI)

Inspect sheets, headers, populated row counts, and column availability:

```bash
python main.py inspect
```

### 3. View Task Queue & Configured Accounts (CLI)

```bash
python main.py queue
python main.py accounts
```

### 4. Run Test Mode Dry Run (CLI)

```bash
python main.py test-run
```

### 5. Launch the Streamlit Operator Dashboard

```bash
streamlit run app.py
```
Or via CLI:
```bash
python main.py dashboard
```

---

## 🖥️ Dashboard Overview

- **Header Banner:** Displays whether `TEST MODE` is active.
- **Summary Metrics:** Total rows, eligible tasks, skipped rows (blank accounts, invalid URLs, non-actionable status), and active queue count.
- **Review Panel:**
  - Displays the active task details, exact approved reply text, destination link, and assigned account.
  - "🌐 Open for Human Review" opens Google Chrome in the assigned profile.
  - Operator Checklist to verify user identity and community rules.
  - Human Action Outcome dropdown (`Completed - verified`, `Not submitted`, `Locked or archived`, `Needs manual verification`, etc.) and "Record Outcome & Advance".
- **Task Queue:** Filterable table of all loaded tasks with skip reasons and statuses.
- **Schema & Summary:** Detailed breakdown of actual workbook headers, column counts, and safety notices.
- **Audit Trail:** Live table of all SQLite execution events and outcomes.

---

## 📋 PRD Findings & Unresolved Workbook Assumptions

1. **`Posts` Worksheet Schema:**
   - The `Posts` sheet contains 7 columns (`Date drafted`, `Subreddit`, `Region`, `Post title`, `Post body`, `Shoro link`, `Status`).
   - The `Account posted` column is **absent** from `Posts`. Per PRD Section 6.2 and FR-03, account assignments cannot be guessed. The application safely reports a schema warning and skips all post tasks until the workbook is updated with an `Account posted` column.
2. **Column N Timestamps:**
   - `Driving DMV` has 13 columns (Col A to M).
   - `Posts` has 7 columns (Col A to G).
   - Column N does not exist in either worksheet. Per PRD Section 6.4, 6.5, and Rule 8/9, the application never invents columns or writes to unverified columns. Outcomes are safely recorded in the existing `Status` column (Col M in `Driving DMV`), and full timestamps are stored in SQLite `database/execution_state.db`.

---

## 🧪 Running Tests

To run the complete suite of 34 unit and integration tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

