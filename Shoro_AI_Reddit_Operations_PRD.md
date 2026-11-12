# Product Requirements Document (PRD)

## Shoro AI Reddit Engagement Operations Tool

**Version:** 1.0\
**Date:** 1 November 2026\
**Target implementation:** Antigravity\
**Application:** Local Python application\
**Source workbook:** `Reddit Threads.xlsx`

------------------------------------------------------------------------

## 1. Executive summary

Build a Python application that processes Reddit engagement tasks from
the Shoro AI Excel workbook, maps each task to its assigned Chrome
profile, opens the relevant Reddit page for human review, and maintains
an auditable record of outcomes.

The application supports two workflows:

1.  **Existing discussion comments:** Read eligible rows from
    `Driving DMV` and use the exact text in `Shoro AI reply`.
2.  **New Reddit posts:** Read eligible rows from `Posts` and prepare
    the subreddit, title, body, and assigned account for review.

Excel remains the source of truth. The application must not invent
content, rewrite approved replies, select an account when the assignment
is blank, or silently change workbook structure.

The first release includes Excel processing, validation, account/profile
mapping, browser navigation for human review, duplicate protection,
audit logging, Test Mode, Start/Stop/Pause/Resume, and safe recovery.

**Compliance gate:** The company has not confirmed explicit Reddit
authorization for automated commercial publishing. Reddit rules apply to
automated browser interactions as well as API use. Therefore, automated
submission/publishing must remain disabled in this release. The
application may open the mapped browser profile and destination for
human review. It must not bypass CAPTCHA, login challenges, rate limits,
account restrictions, moderation decisions, or other platform
safeguards. Any future publishing capability requires documented
authorization and an approved method.

------------------------------------------------------------------------

## 2. Problem statement

The team maintains Reddit research, suggested replies, account
information, and drafted posts in Excel. Manually filtering tasks,
finding the right account, opening discussions, avoiding repeated work,
and recording outcomes creates unnecessary effort and risk.

The application should: - Read and validate the existing workbook. -
Prepare tasks from both in-scope worksheets. - Use only the account
assignment recorded in Excel. - Map account IDs to separate Chrome
profiles. - Preserve approved content exactly. - Skip invalid,
incomplete, unassigned, or already completed tasks. - Open relevant
pages for human review. - Record human-confirmed outcomes and
timestamps. - Recover safely after a restart.

------------------------------------------------------------------------

## 3. Goals and success criteria

### Goals

-   Process `Driving DMV` and `Posts`.
-   Inspect actual worksheet headers at runtime.
-   Never guess missing columns or account assignments.
-   Preserve original Excel row numbers.
-   Use separate Chrome profiles mapped to account IDs.
-   Support Test Mode by default.
-   Provide Start, Pause, Resume, and Stop controls.
-   Prevent duplicate local processing.
-   Maintain SQLite audit and recovery data.
-   Update only confirmed existing Excel fields.

### Success criteria

1.  Workbook loads and schema is reported accurately.
2.  Eligible and skipped rows are counted correctly.
3.  Blank account assignments are skipped.
4.  Exact prepared content is preserved.
5.  Correct mapped profile and destination are opened for review.
6.  Test Mode performs no Reddit submissions.
7.  A task is not marked successful just because a page opened or a
    button was clicked.
8.  Timestamp and status updates occur only after field mapping is
    verified.
9.  Restart does not silently repeat completed or uncertain tasks.

------------------------------------------------------------------------

## 4. Scope

### In scope

-   Excel reading and validation.
-   Existing discussion comment task preparation.
-   New post task preparation.
-   Account-to-Chrome-profile mapping.
-   Browser navigation for human review.
-   Duplicate task detection.
-   Daily internal activity counter.
-   Configurable pause between review tasks.
-   Test Mode.
-   Human-confirmed outcome tracking.
-   Excel status/timestamp updates after schema validation.
-   SQLite audit trail.
-   Logging and error handling.
-   Local Streamlit dashboard.
-   Stop, pause, and resume.

### Out of scope for the first release

-   Reddit API integration.
-   Scraping Reddit at scale.
-   CAPTCHA solving or bypass.
-   Automated login or credential storage.
-   Bypassing login verification, anti-bot controls, rate limits,
    restrictions, or moderation.
-   Account rotation to evade enforcement.
-   Automated submission/publishing.
-   AI-generated or rewritten comments.
-   Guessing account assignments.
-   Adding new workbook columns.
-   Direct messages or chat automation.
-   Hosted multi-user deployment.

------------------------------------------------------------------------

## 5. Users and roles

### Operator

Can select the workbook, validate it, run Test Mode, review the task
queue, open a task in its assigned browser profile, perform any
authorized action manually, confirm the outcome, and view audit history.

### Administrator / project owner

Can configure account-to-profile mappings, workbook path, processing
settings, and inspect audit history. Any future publishing integration
requires documented authorization and separate review.

A single local operator is sufficient for the first release. Full
authentication and role-based access control are not required.

------------------------------------------------------------------------

## 6. Workbook specification

The supplied workbook is `Reddit Threads.xlsx`. Inspect its actual
sheets, headers, row counts, and cell values before implementation. Do
not rely solely on assumptions in this document.

### 6.1 `Driving DMV`

Purpose: existing Reddit discussions and prepared replies.

Previously inspected headers: - Last checked - State / Province - Thread
title - Thread link - Engagement - Why it matters / What changed -
Suggested reply - Content idea - My status - Revised suggested reply -
Shoro AI reply - Account posted - Status

Rules: - Final prepared comment text is `Shoro AI reply`. - Preserve
this text exactly. Do not rewrite, summarize, correct, or regenerate
it. - Use `Account posted` as the assigned account, as clarified by the
project lead. - If `Account posted` is blank, skip the row. - Use the
existing `Thread link`; do not create another URL column. - Preserve
existing status values such as `Locked post`, `Post deleted`,
`Deleted by user`, and `Archived post`.

### 6.2 `Posts`

Purpose: new Reddit post drafts.

Previously inspected headers: - Date drafted - Subreddit - Region - Post
title - Post body - Shoro link - Status

The lead says account assignment for new posts must also come from
`Account posted`, and blank assignments must be skipped. Earlier
inspection did not show `Account posted` in the visible `Posts` headers.

Therefore: - Inspect the actual headers. - If `Account posted` exists,
use it. - If it does not exist, do not guess or substitute another
column. Report a schema error and skip new-post tasks until the workbook
is clarified or corrected. - Preserve title and body exactly. - Do not
add a new URL column.

### 6.3 Other worksheets

`NCERT Reddit` and `Threads` were previously empty. Do not process them
in the first release. They may be listed as ignored worksheets.

### 6.4 Workbook write safety

-   Never rename, reorder, delete, or insert columns.
-   Never overwrite non-empty cells without validating their purpose.
-   Preserve formatting where possible.
-   Create a timestamped backup before the first workbook write in a
    run.
-   Use `openpyxl` for controlled updates.
-   Do not save if schema validation fails.
-   If Excel has locked the workbook, show an actionable message.
-   Prefer writing to a temporary file and atomically replacing the
    original where practical.
-   Keep workbook path configurable.

### 6.5 Status and timestamp fields

The lead requires using the existing `Status` column only. Do not add a
new status column.

The lead specified Column N for timestamps. Before writing: - Inspect
Column N's header and existing values separately in each relevant
worksheet. - Confirm that it is the intended timestamp field and safe to
update. - If missing, occupied by unrelated data, or ambiguous, do not
overwrite it. Report the issue and request confirmation. - Do not assume
Column N has the same meaning in both worksheets.

Do not create a new URL column. Use existing workbook URLs/references.

Suggested status values for new outcomes: - `To review` -
`Ready for review` - `Opened for review` -
`Completed - human confirmed` - `Skipped - blank account` -
`Skipped - invalid URL` - `Skipped - missing content` -
`Skipped - duplicate` - `Blocked - login required` -
`Blocked - CAPTCHA or verification` - `Blocked - restricted` -
`Failed - browser error` - `Failed - workbook validation` -
`Needs manual verification`

Do not replace existing meaningful statuses with generic statuses.
Confirm exact status-column mapping before writing.

------------------------------------------------------------------------

## 7. Functional requirements

### FR-01: Workbook loading

-   Allow workbook selection; default to `data/Reddit Threads.xlsx`.
-   Validate file existence and extension.
-   Validate required worksheet names.
-   Inspect actual headers dynamically.
-   Display row counts, missing columns, and schema warnings.
-   Do not silently process unexpected worksheets.

### FR-02: Existing discussion task preparation

For each `Driving DMV` row: - Require `Thread link`, `Shoro AI reply`,
and `Account posted`. - Skip if account is blank. - Skip if URL is blank
or invalid. - Skip if reply is blank. - Preserve exact reply text. -
Preserve original Excel row number. - Respect existing status. - Do not
automatically reprocess rows already marked completed. - Treat locked,
deleted, or archived discussions as non-actionable.

### FR-03: New post task preparation

For each `Posts` row: - Require `Subreddit`, `Post title`, `Post body`,
and `Account posted`. - If `Account posted` is absent from the sheet,
report a schema error and skip all new-post tasks. - Skip blank account
assignments. - Skip missing required content. - Preserve exact title and
body. - Preserve original Excel row number. - Respect existing status. -
Do not invent missing values.

### FR-04: Account-to-Chrome-profile mapping

Maintain a local `config/accounts.json`.

Example:

``` json
{
  "accounts": [
    {
      "account_id": "account_identifier_1",
      "chrome_profile_name": "Profile 2",
      "user_data_directory": "C:/RedditProfiles/account_1",
      "enabled": true
    }
  ]
}
```

Requirements: - Account ID comes from the workbook. - Each account maps
to a distinct Chrome profile/user-data directory. - Do not store
passwords, cookies, session tokens, or recovery codes. - Do not automate
login. - Missing mapping means stop that task and report
`Account profile not configured`. - Operator must verify the visible
Reddit account before any human action. - If identity cannot be
verified, pause. - Never switch accounts to evade restrictions or
enforcement.

### FR-05: Browser navigation and human review

Use Playwright for permitted browser navigation/review support.

The browser module may: - Open the configured profile using a supported
persistent profile directory, subject to Chrome/Playwright
compatibility. - Navigate to the existing thread URL. - Open a known
subreddit destination for review. - Display task details in the
application. - Pause for human review. - Allow the operator to record
the outcome after an authorized manual action.

The first release must not: - Automatically fill and submit comments or
posts. - Automatically click publish/submit controls. - Bypass CAPTCHA
or human verification. - Retry login repeatedly. - Circumvent locked
threads, account restrictions, rate limits, or moderation. - Use hidden
endpoints or undocumented Reddit interfaces.

If the UI changes or page identity is ambiguous, stop and request manual
review.

### FR-06: Test Mode

Test Mode is enabled by default.

It must: - Read and validate the workbook. - Resolve account/profile
mappings. - Check local duplicate history. - Display exact task content
and destination. - Optionally open a page for review only when
explicitly requested. - Never submit anything to Reddit. - Never mark a
task published/completed. - Record test activity in SQLite. - Display a
clear `TEST MODE - NO REDDIT SUBMISSIONS` indicator.

The first release must not contain a working automated submission action
hidden behind the Test Mode toggle.

### FR-07: Duplicate prevention

Generate a stable fingerprint.

Comment fingerprint inputs: - Worksheet name - Excel row number - Thread
URL - Account ID - Exact reply text

Post fingerprint inputs: - Worksheet name - Excel row number -
Subreddit - Account ID - Exact title - Exact body

Requirements: - Store fingerprints in SQLite. - Check before preparing a
task. - Do not reprocess tasks already confirmed complete. - Describe a
match as previously processed by this application, not as proof of a
Reddit-side duplicate. - Provide a manual reconciliation path. - Do not
silently delete audit history.

### FR-08: Daily activity counter

The lead requested an internal target of approximately 4-5 comments per
account per day.

Requirements: - Configurable internal daily counter. - Default
configured cap: 5 human-confirmed completed comment tasks per account
per local calendar day. - Count only human-confirmed completed comments,
not page opens or test tasks. - Display count before opening a new
review task. - Use the workbook's assigned account exactly; do not
distribute tasks to other accounts. - Pause when the configured internal
cap is reached. - This internal cap does not define Reddit's permitted
limits. Follow any stricter platform or subreddit restrictions. - Do not
use account switching, randomization, or delays to evade platform
enforcement.

### FR-09: Pacing

The lead requested a pause between tasks.

Requirements: - Configurable pause between review tasks. - Default: 30
seconds between opening review tasks. - Pause is for operator workflow
pacing, not evasion. - Operator can pause or stop during the wait. - If
the platform displays a restriction or retry-after instruction, stop and
follow it. - Do not randomize delays to disguise automation.

### FR-10: Start, Pause, Resume, Stop

-   Start processes eligible tasks in stable order.
-   Pause prevents the next task from opening.
-   Resume continues from saved state.
-   Stop safely ends the current review cycle.
-   Do not interrupt an active workbook write.
-   Persist state after each meaningful transition.
-   On restart, restore unfinished tasks.
-   Never automatically repeat completed tasks.
-   Uncertain outcomes become `Needs manual verification`; do not
    automatically retry.

### FR-11: Audit database

Use SQLite at `database/execution_state.db`.

Suggested tables:

**tasks** - task_id - task_fingerprint - workbook_name -
worksheet_name - excel_row_number - task_type - account_id -
destination_url - task_status - created_at - updated_at

**execution_events** - event_id - task_id - event_type -
event_timestamp - operator_action - outcome - error_code - error_details

**account_profiles** - account_id - chrome_profile_name -
user_data_directory - enabled - created_at - updated_at

**run_sessions** - run_id - started_at - ended_at - run_mode -
run_status - total_tasks - completed_tasks - skipped_tasks -
failed_tasks

Requirements: - Store timestamps in ISO 8601 with timezone. - Display
Asia/Kolkata by default. - Do not store credentials or browser session
data. - Keep audit events append-only in normal operation. - Log only
necessary task metadata.

### FR-12: Human-confirmed outcomes

After a human action, ask the operator to choose: - Completed -
verified - Not submitted - Page unavailable - Locked or archived - Login
required - CAPTCHA or verification required - Account restriction -
Submission outcome uncertain - Other error

For `Completed - verified`: - Require operator confirmation that the
published item is visible. - Use existing workbook URL/reference as
instructed. - Do not invent or construct a new Reddit URL. - Record
timestamp in the confirmed existing timestamp field. - Update existing
Status cell only after its mapping is validated. - Save an audit event
in SQLite.

Opening a URL or clicking a control is never sufficient evidence of
successful publication.

### FR-13: Error handling

Handle: - Workbook missing. - Worksheet missing. - Required column
missing. - Blank account assignment. - Invalid URL. - Missing content. -
Duplicate local task. - Profile mapping missing. - Chrome profile
unavailable/in use. - Browser launch failure. - Login required. -
CAPTCHA/verification challenge. - Deleted, locked, or archived
discussion. - Account restriction. - Platform rate limit. - Workbook
locked by Excel. - SQLite unavailable. - Unexpected shutdown.

For platform restrictions or verification: - Pause the workflow. -
Record the condition. - Require manual resolution. - Do not bypass or
retry automatically.

### FR-14: Logging

Use Python's standard `logging` package.

Log: - Application start/stop. - Workbook load and validation summary. -
Task preparation and skip reason. - Profile mapping result. - Browser
navigation outcome. - Human-confirmed outcome. - Workbook update
result. - Errors and recovery events.

Never log passwords, cookies, tokens, or other secrets.

### FR-15: Dashboard

Use Streamlit for the first release.

Dashboard sections:

**Header** - Shoro AI Reddit Operations - Workbook path - Current mode

**Summary** - Total comment rows - Eligible comment tasks - Total post
rows - Eligible post tasks - Skipped tasks - Completed tasks - Needs
manual verification

**Controls** - Select workbook - Validate workbook - Start - Pause -
Resume - Stop - Test Mode indicator

**Task queue** - Task type - Worksheet - Excel row - Account ID -
Destination/subreddit - Status - Skip/error reason

**Review panel** - Account ID - Chrome profile name - Destination -
Thread/post title - Exact prepared comment or body - Validation
results - Open for human review

**Audit panel** - Recent events - Timestamp - Task ID - Account ID -
Outcome - Error details

Keep the interface simple. Avoid unnecessary charts and complex
settings.

------------------------------------------------------------------------

## 8. Technical architecture

### Technology stack

-   Python 3.11+
-   Streamlit: local dashboard
-   pandas: workbook reading and task preparation
-   openpyxl: controlled Excel updates
-   SQLite: task state and audit history
-   Playwright: permitted browser navigation/review
-   pathlib: file paths
-   logging: application logs
-   hashlib: task fingerprints
-   zoneinfo: timezone support

### Project structure

``` text
shoro-reddit-operations/
│
├── app.py
├── main.py
├── config.py
├── requirements.txt
├── README.md
│
├── data/
│   └── Reddit Threads.xlsx
│
├── config/
│   ├── accounts.json
│   └── settings.json
│
├── modules/
│   ├── __init__.py
│   ├── excel_handler.py
│   ├── schema_inspector.py
│   ├── task_builder.py
│   ├── validator.py
│   ├── account_manager.py
│   ├── browser_manager.py
│   ├── duplicate_checker.py
│   ├── state_manager.py
│   ├── audit_logger.py
│   ├── comment_manager.py
│   ├── post_manager.py
│   └── workflow_controller.py
│
├── database/
│   └── execution_state.db
├── logs/
│   └── application.log
├── backups/
└── tests/
    ├── test_excel_handler.py
    ├── test_schema_inspector.py
    ├── test_task_builder.py
    ├── test_validator.py
    ├── test_duplicate_checker.py
    └── test_state_manager.py
```

### Module responsibilities

-   `schema_inspector.py`: inspect sheets, headers, row counts, and
    timestamp-column availability.
-   `excel_handler.py`: read workbook and safely update validated
    existing cells.
-   `task_builder.py`: create normalized comment/post task objects.
-   `validator.py`: validate fields, URLs, assignments, and statuses.
-   `account_manager.py`: map account IDs to Chrome profiles.
-   `browser_manager.py`: open the mapped profile and destination for
    human review.
-   `duplicate_checker.py`: fingerprint tasks and query local history.
-   `state_manager.py`: persist run state and resume.
-   `audit_logger.py`: structured SQLite events and application logs.
-   `comment_manager.py`: prepare and display existing-discussion tasks.
-   `post_manager.py`: prepare and display new-post tasks.
-   `workflow_controller.py`: coordinate validation, task queue, pacing,
    pause/stop, and recovery.
-   `app.py`: Streamlit dashboard.

------------------------------------------------------------------------

## 9. Data models

### Comment task

``` python
{
    "task_type": "comment",
    "worksheet": "Driving DMV",
    "excel_row": 2,
    "account_id": "account_identifier",
    "chrome_profile_name": "Profile 2",
    "thread_title": "Example thread title",
    "destination_url": "https://www.reddit.com/...",
    "reply_text": "Exact text from Shoro AI reply",
    "status": "Ready for review",
    "timestamp": None
}
```

### Post task

``` python
{
    "task_type": "post",
    "worksheet": "Posts",
    "excel_row": 2,
    "account_id": "account_identifier",
    "chrome_profile_name": "Profile 2",
    "subreddit": "ExampleSubreddit",
    "title": "Exact title from workbook",
    "body": "Exact body from workbook",
    "status": "Ready for review",
    "timestamp": None
}
```

These are normalized internal objects. Retain the original worksheet and
row number for safe Excel updates.

------------------------------------------------------------------------

## 10. End-to-end workflows

### 10.1 Startup

1.  Load configuration.
2.  Open SQLite database.
3.  Load selected workbook.
4.  Inspect workbook schema.
5.  Validate sheets and columns.
6.  Load account-profile mappings.
7.  Restore unfinished run state.
8.  Display validation summary.
9.  Keep Test Mode enabled by default.

### 10.2 Existing discussion

1.  Read `Driving DMV`.
2.  Check existing status.
3.  Read `Account posted`; skip if blank.
4.  Validate `Thread link`.
5.  Read exact `Shoro AI reply`; skip if blank.
6.  Check local duplicate history.
7.  Resolve mapped Chrome profile.
8.  Display task in review queue.
9.  Open destination for human review when requested.
10. Operator verifies account and page.
11. Operator performs any authorized action manually.
12. Operator confirms outcome.
13. Save audit event and timestamp.
14. Update existing Status and confirmed timestamp fields safely.
15. Apply configured pause before the next review task.

### 10.3 New post

1.  Read `Posts`.
2.  Inspect actual headers.
3.  Confirm `Account posted` exists; otherwise report schema error and
    skip.
4.  Check status and required fields.
5.  Skip blank account assignment.
6.  Check local duplicate history.
7.  Resolve mapped Chrome profile.
8.  Display subreddit, title, body, and account.
9.  Open destination for human review when requested.
10. Operator verifies account and content.
11. Operator performs any authorized action manually.
12. Operator confirms outcome.
13. Save audit event and timestamp.
14. Update existing Status and confirmed timestamp fields safely.
15. Apply configured pause before the next review task.

### 10.4 Stop and resume

-   Save state after every transition.
-   Stop prevents the next task from opening.
-   Do not interrupt an active workbook write.
-   On restart, show unfinished tasks.
-   Uncertain tasks require manual verification.
-   Never assume an interrupted task failed.
-   Never automatically repeat an uncertain task.

------------------------------------------------------------------------

## 11. Configuration

Example `config/settings.json`:

``` json
{
  "workbook_path": "data/Reddit Threads.xlsx",
  "comment_sheet": "Driving DMV",
  "post_sheet": "Posts",
  "test_mode": true,
  "daily_comment_cap": 5,
  "review_task_pause_seconds": 30,
  "display_timezone": "Asia/Kolkata",
  "require_human_outcome_confirmation": true,
  "automated_submission_enabled": false
}
```

The daily cap and pause are internal workflow controls, not permission
to exceed Reddit limits. Do not implement randomized delays as an
evasion technique.

------------------------------------------------------------------------

## 12. Security and privacy

-   Never store Reddit passwords in configuration.
-   Never extract or export browser cookies.
-   Never log session cookies, passwords, recovery codes, or tokens.
-   Keep account-profile mappings local.
-   Restrict access to workbook, database, and logs.
-   Do not upload workbook contents to external services.
-   Do not send task content to third-party AI services.
-   Provide a backup process for workbook and local database.

------------------------------------------------------------------------

## 13. Testing strategy

### Unit tests

Test: - Missing workbook. - Missing worksheet. - Missing required
column. - Blank account assignment. - Invalid URL. - Blank reply. -
Blank post title/body. - Existing completed status. -
Locked/deleted/archived status. - Missing profile mapping. - Duplicate
fingerprint. - Timestamp field unavailable. - Workbook write failure. -
Resume after simulated interruption.

### Integration tests

Use a workbook copy and temporary SQLite database.

Verify: - Both sheets load. - Original row numbers are retained. - Exact
reply text is preserved. - Blank account assignments are skipped. -
Existing statuses are not overwritten incorrectly. - Test Mode makes no
Reddit submissions. - Audit events are saved. - Resume does not repeat
completed tasks. - Workbook formatting and unrelated cells remain
unchanged.

### Acceptance tests

Project owner confirms: - Correct account mapping. - Correct Chrome
profile opening. - Correct task content. - Correct timestamp field. -
Correct status values. - Correct skip reasons. - Correct Stop/Resume
behavior. - Correct audit records.

Do not conduct production publishing tests until required platform
authorization and an approved method are confirmed.

------------------------------------------------------------------------

## 14. Non-functional requirements

-   **Reliability:** crashes must not corrupt the workbook or lose
    committed audit events.
-   **Safety:** fail closed when a column, account, task, or browser
    state is ambiguous.
-   **Traceability:** every task maps to a worksheet and original row.
-   **Usability:** a non-developer operator can use the dashboard.
-   **Maintainability:** separate responsibilities into Python modules.
-   **Performance:** avoid unnecessary repeated workbook reads/writes.
-   **Portability:** support Windows first; avoid hardcoded
    user-specific paths.
-   **Recoverability:** persist state after each meaningful transition.

------------------------------------------------------------------------

## 15. Development milestones

### Milestone 1 - Workbook inspection

-   Inspect the supplied workbook.
-   Report sheet names, actual headers, row counts, and Column N
    contents.
-   Identify missing or ambiguous fields.
-   Do not modify the workbook.

**Deliverable:** schema inspection report and tests.

### Milestone 2 - Task preparation

-   Implement comment and post task builders.
-   Preserve exact content and row numbers.
-   Skip invalid and blank-account rows.
-   Respect existing statuses.

**Deliverable:** validated task queue.

### Milestone 3 - Account mapping

-   Implement `accounts.json`.
-   Validate unique profile paths.
-   Resolve account IDs from Excel.
-   Do not store credentials or automate login.

**Deliverable:** account mapping screen and tests.

### Milestone 4 - Browser review navigation

-   Open the correct configured profile.
-   Navigate to the destination for human review.
-   Require manual account verification.
-   Do not submit automatically.

**Deliverable:** browser navigation module.

### Milestone 5 - Audit and recovery

-   Implement SQLite tables.
-   Implement task fingerprints.
-   Implement human outcome confirmation.
-   Implement safe Excel status/timestamp updates.
-   Implement Stop and Resume.

**Deliverable:** durable audit and recovery workflow.

### Milestone 6 - Dashboard and final testing

-   Implement Streamlit controls.
-   Add queue, summary, errors, and audit views.
-   Test with a copy of the supplied workbook.
-   Document setup and usage.

**Deliverable:** local application ready for operator testing.

------------------------------------------------------------------------

## 16. Instructions for Antigravity

Use the supplied `Reddit Threads.xlsx` as the source workbook. Inspect
the actual file before coding.

1.  Inspect the repository and workbook before changing anything.
2.  Produce a short implementation plan before coding.
3.  Implement one milestone at a time.
4.  Run tests after every milestone.
5.  Use clear Python modules and type hints where appropriate.
6.  Do not hardcode absolute paths.
7.  Do not invent worksheet columns.
8.  Do not add columns to the workbook.
9.  Do not overwrite Column N until its header and purpose are verified.
10. Do not assume `Posts` contains `Account posted`; inspect and stop
    safely if missing.
11. Do not change `Shoro AI reply`.
12. Do not store Reddit credentials or cookies.
13. Do not bypass CAPTCHA, login verification, rate limits,
    restrictions, or moderation.
14. Do not implement automated Reddit submissions in this release.
15. Keep Test Mode enabled by default.
16. Limit browser actions to opening the mapped profile and destination
    for human review.
17. Require human confirmation for outcomes.
18. Maintain audit records in SQLite.
19. Use existing Excel Status fields only after confirming the correct
    column.
20. Do not deploy the application or publish anything to Reddit during
    development.

At the end of each milestone, report: - Files created or changed. - What
was implemented. - Tests executed and results. - Unresolved workbook
assumptions. - Next milestone.

------------------------------------------------------------------------

## 17. Open questions requiring confirmation

1.  Does `Posts` actually contain `Account posted`? If not, the project
    owner must identify the intended assignment field.
2.  What is the exact header and purpose of Column N in each worksheet?
3.  Is the existing `Status` column in `Posts` intended for post outcome
    tracking?
4.  Is the existing `Status` column in `Driving DMV` intended for
    comment outcome tracking?
5.  Which account IDs map to which Chrome profile directories?
6.  What is the approved human verification process for a completed
    action?
7.  Has the organization obtained the required Reddit authorization for
    any future automated publishing? Until confirmed, automated
    submission remains disabled.

------------------------------------------------------------------------

## 18. Definition of done

The first release is complete when: - Workbook loads and is inspected
safely. - Schema issues are reported rather than guessed. - Comment and
post tasks are prepared accurately. - Blank account assignments are
skipped. - Account IDs resolve to configured Chrome profiles. - Browser
navigation opens the relevant page for human review. - Test Mode makes
no submissions. - Operator can Start, Pause, Resume, and Stop. -
Duplicate local processing is prevented. - Human-confirmed outcomes are
recorded. - Timestamp is written only to a verified existing field. -
Existing Status fields are updated safely. - SQLite audit and recovery
work. - Unit and integration tests pass. - README setup and operating
instructions are complete.

**Product principle:** Excel is the source of truth; account assignment
is never guessed; approved text is never changed; uncertain outcomes are
never treated as success; and the application fails safely rather than
bypassing platform safeguards.
