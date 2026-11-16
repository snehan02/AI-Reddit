# Shoro AI Reddit Multi-Account Automation & Operations Console

A centralized multi-account automation and human-in-the-loop engagement engine for Reddit operations. The system automates Reddit discussion comments and subreddit post publishing driven by Excel and Google Sheets, supporting 20–25+ persistent accounts, dual execution modes, real-time browser visibility, human-mimicking cadences, and comprehensive audit logging.

---

## 🌟 Key Features & Architecture

### 1. Dual Execution Modes (Posts & Comments)
Both workflows support independent operational modes with clean, context-aware controls:
- **Auto Mode (`Auto Comment` / `Auto Post`):** Complete hands-free automation across all phases—navigates to the thread or submit page, checks community rules, types naturally, applies flairs, proofreads, clicks submit, verifies the published post/comment live on Reddit, and updates the spreadsheet.
- **Human Submit Mode:** End-to-end preparation for operator review. Navigates in the assigned account profile, types the exact approved title and body, selects flair, proofreads, brings the visible Chrome window to the front, and **stops immediately before the final submit button**. The operator reviews the live draft and manually clicks Post or Comment.

### 2. Account Manager & Persistent Chrome Profiles (20–25+ Accounts)
- **Centralized Account Management:** Easily add, configure, enable, or inspect 20–25+ Reddit accounts in `config/accounts.json`.
- **Dedicated Persistent Profiles:** Every account maintains an independent Chrome profile in `chrome_profiles/<account_dir>` preserving cookies, active Reddit sessions, and local storage across system reboots and app restarts.
- **One-Time Manual Login:** Launch an isolated Chrome window directly from the dashboard to complete manual Reddit login and OTP verification once.
- **Zero Credential Storage:** Passwords, credentials, and authentication secrets are never accepted or stored in code, configuration, or spreadsheets.
- **Anti-Pollution & Collision Guards:** System-level validation prevents profile directory collisions and protects system Chrome directories (`Default`, `User Data`, `Profile 1`).

### 3. Human-Mimicking Interaction Cadence
To ensure strict platform safety and mimic genuine human behavior:
- **Keystroke Delays:** Natural word-by-word typing with randomized 110–190 ms intervals.
- **Paragraph Dwells:** 10–18 second pauses between paragraphs simulating thoughtful writing.
- **Proofreading Delays:** 45–60 second review dwell periods after typing before taking any final action.
- **Anti-Ban Account Gaps:** Enforces an 8-hour cooldown between consecutive actions on any single account.
- **No Accelerated Modes:** Accelerated Demo Mode has been completely removed; standard human timing is preserved across all execution paths.

### 4. Fully Visible Browser Automation
- **No Headless Browsers:** Automations run in visible desktop Chrome windows so operators can monitor actions in real time.
- **Real-Time Visual Monitoring:** Live screenshot capture and DOM snapshot inspectors in the dashboard monitor each phase.
- **Quick Window Focus:** Dedicated "Bring Chrome to Front" button immediately foregrounds the active automation window.

### 5. Google Sheets & Excel Synchronization
- **Live Bidirectional Sync:** Synchronizes with Google Sheets ID `1_AcKE7VErFV2sLIFqLWeGcBQy8AKmPBEmeSPkHjUkl4` and the local master workbook `data/Reddit Threads.xlsx`.
- **Accurate Status Tracking:** Prepared but unpublished records are marked as **Draft**; only verified live publications are marked as **Complete** with their exact publication timestamp.
- **Write-Safe Handling:** Automated timestamped backups are generated in `backups/` before any file write.
- **Strict Column Preservation:** Column M records comment statuses in `Driving DMV`, and Column G records post statuses in `Posts`.

### 6. Unified Review & Batch Operations Panel
- **Merged Review & Batch Controls:** Single unified dashboard panel combining individual task inspection, account selection, batch preparation, and batch execution.
- **Stop Batch Control:** Prominent `🛑 Stop Batch` button stops scheduling new tasks immediately, safely pauses pending work, and preserves all already completed records without corruption.
- **Account-Level Error Resilience:** If an individual account experiences an error (e.g. rate limit or browser glitch), the error is logged and marked as `Failed`, while the batch safely continues processing the remaining eligible accounts.
- **Posts Per Account & 8-Hour Gap:** Configurable posts per account limit in the sidebar alongside an enforced 8-hour gap between successfully published posts from the same account.

### 7. Real-Time Task Queue
- **Live State Tracking:** Dynamic queue displaying Draft, Ready, Complete, and Failed items with instant status updates.
- **Persistent State:** Queue states survive dashboard reloads and application restarts via SQLite persistence.

---

## 📁 Project Structure

```text
Shoro AI Reddit/
├── app.py                     # Streamlit Operator Dashboard & UI
├── main.py                    # Diagnostic CLI runner & batch utility
├── config.py                  # Dataclass application settings
├── requirements.txt           # Python dependency specifications
├── README.md                  # System runbook & documentation
├── Shoro_AI_Reddit_Operations_PRD.md # Product Requirements Document
├── launch_chrome_window.bat   # Windows batch helper for isolated Chrome
├── launch_login_browser.bat   # Launcher for account login sessions
│
├── data/
│   ├── Reddit Threads.xlsx   # Local master Excel workbook
│   └── backups/              # Automated workbook backups
│
├── config/
│   ├── accounts.json         # 20–25+ account profile mappings & statuses
│   └── settings.json         # Runtime settings (Test Mode, caps, intervals)
│
├── database/
│   ├── execution_state.db    # SQLite audit store, task logs, and fingerprints
│   └── session_state.json    # Operator UI state persistence
│
├── modules/
│   ├── __init__.py
│   ├── account_manager.py    # Multi-account isolation, validation & login
│   ├── audit_logger.py       # SQLite execution history & event logging
│   ├── browser_manager.py    # Playwright & Chrome review navigation
│   ├── comment_manager.py    # Extracts Driving DMV discussion comment tasks
│   ├── duplicate_checker.py  # SHA-256 fingerprint duplicate prevention
│   ├── excel_handler.py      # Non-destructive Excel updates & backups
│   ├── google_sheets_syncer.py # Two-way Google Sheets synchronizer
│   ├── logging_config.py     # Centralized rotating file logger
│   ├── openclaw_client.py    # CDP browser automation, typing & screen captures
│   ├── post_manager.py       # Extracts subreddit post draft tasks
│   ├── schema_inspector.py   # Workbook header and column validation
│   ├── state_manager.py      # Session lifecycle & rate limiting
│   ├── task_builder.py       # Comment & Post data models & fingerprints
│   ├── validator.py          # Field, URL, and status validators
│   └── workflow_controller.py# High-level pipeline orchestrator
│
├── logs/
│   └── application.log       # Application runtime logs
├── backups/                  # Timestamped workbook backups & snapshots
└── tests/                    # Comprehensive test suite (178 tests, 17 files)
    ├── test_account_manager.py
    ├── test_audit_logger.py
    ├── test_batch_and_account_manager.py
    ├── test_browser_manager.py
    ├── test_dashboard_openclaw_integration.py
    ├── test_duplicate_checker.py
    ├── test_excel_handler.py
    ├── test_google_sheets_syncer.py
    ├── test_logging_config.py
    ├── test_openclaw_client.py
    ├── test_reddit_session_integration.py
    ├── test_schema_inspector.py
    ├── test_state_manager.py
    ├── test_streamlit_app.py
    ├── test_task_builder.py
    ├── test_validator.py
    └── test_workflow_controller.py
```

---

## 🚀 Quickstart & Setup

### 1. Prerequisites & Installation

- **OS:** Windows 10/11
- **Python:** 3.11+
- **Browser:** Google Chrome

Install required dependencies:
```bash
pip install -r requirements.txt
```

### 2. Launch the Operator Dashboard

Start the Streamlit console:
```bash
python -m streamlit run app.py --server.port=8501
```
Open **[http://localhost:8501](http://localhost:8501)** in your browser.

---

## 🖥️ Dashboard Runbook

### Operation Workflows
Select your workflow using the top toggle:
- **💬 Reddit Discussion Comments (Driving DMV):** Loads comment reply tasks. Displays Comment execution actions (**Auto Comment** and **Human Submit**).
- **📢 Reddit New Posts & Threads (Posts):** Loads subreddit post drafting tasks. Displays Post execution actions (**Auto Post** and **Human Submit**).
- **🔄 Both Posts & Comments (Combined Batch):** Processes both eligible DMV discussion comments and top-level posts across all selected accounts in a single combined batch.

### Quick Account Selection
Above the account selector, one-click action buttons streamline batch targeting:
- **`🟢 Select Logged In (<count>)`:** Automatically detects and selects only accounts with active saved Chrome login sessions.
- **`👥 Select All`:** Selects all registered accounts.
- **`❌ Clear`:** Deselects all accounts.

### Task Intelligence & Review
- **Task Header:** Displays worksheet, row number, task type, and qualification status.
- **Approved Reply / Post Content:** Renders the exact text from the spreadsheet (`Shoro AI reply` or `Post body`).
- **Browser Controls:**
  - **Open Thread in Chrome:** Launches Chrome in the assigned profile and navigates to the target thread.
  - **Show Chrome:** Foreground the active Chrome window to the front of the screen.
  - **Stop Browser:** Closes the Chrome instance safely while preserving session cookies.

### Execution Options
- **Auto Submit:** Runs full end-to-end automation with natural typing, proofreading, submitting, and live URL verification.
- **Human Submit:** Stages title, body, and flairs directly into Reddit's editor, brings Chrome forward, and waits for the operator to review and click Submit.

### Dashboard Tabs

1. **🎯 Review & Batch Operations Tab:**
   - **Task Inspection & Review:** Select discussion thread or post draft, preview approved content, check live Reddit status, and use single-task execution controls (**Auto Comment/Post** or **Human Submit**).
   - **Batch Operations:** Configure operation workflow, select target accounts, set submission mode, launch batch preparation/publishing, and interrupt safely anytime with **Stop Batch**.
   - **Prepared Items Review:** Inspect all staged Drafts, view live snapshots, proofread, and verify final manual submissions with one click.

2. **👥 Account Manager Tab:**
   - Select an account or add a new account ID (supporting 20–25+ accounts).
   - Click **Login Account** to launch an isolated Chrome profile window.
   - Manually sign in to Reddit once (including 2FA / OTP if enabled).
   - Close the window; the session cookies and credentials remain permanently preserved in `chrome_profiles/<account>`.

3. **📋 Real-Time Task Queue Tab:**
   - Live queue tracking items by state: **Draft**, **Ready**, **Complete**, and **Failed**.
   - Displays task ID, account ID, worksheet, row, and error messages for any failed items.

4. **🦞 OpenClaw Console Tab:**
   - Real-time diagnostic health monitoring, active browser port inspection, and OpenClaw daemon connection status.

---

## 🧪 Testing & Verification

Run the entire automated test suite (175 tests across 17 test modules):

```bash
python -m unittest discover tests
```

To run a specific test suite:
```bash
python -m unittest tests/test_account_manager.py
python -m unittest tests/test_workflow_controller.py
python -m unittest tests/test_google_sheets_syncer.py
```

---

## 🛡️ Safety & Platform Guidelines

1. **Account Isolation:** Each account strictly uses its own persistent data directory. Cross-account cookie sharing is blocked.
2. **8-Hour Gap Rule:** Any account that has posted or commented must observe an 8-hour pause before its next automated action.
3. **No Unsafe Acceleration:** All typing and submission cadences mimic genuine human operators with randomized pauses.
4. **Data Protection:** Master spreadsheets are backed up automatically to `backups/` prior to any modification.
