"""Command-Line Interface for Shoro AI Reddit Operations."""

import argparse
import logging
import sys

from config import AppSettings
from modules.logging_config import setup_application_logging
from modules.workflow_controller import WorkflowController

setup_application_logging("logs/application.log")
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
logger = logging.getLogger("main")


def cmd_inspect(controller: WorkflowController) -> None:
    report = controller.initialize()
    print("\n" + "=" * 60)
    print("WORKBOOK INSPECTION SUMMARY")
    print("=" * 60)
    print(report.summary())
    print("\nSheets inspected:")
    for name, schema in report.sheets.items():
        print(f"  - {name}: {schema.populated_row_count} rows, {schema.max_column} columns")
        print(f"    Missing required columns: {schema.missing_required_columns or 'None'}")
        print(f"    Column N present: {schema.has_column_n}")
    print("=" * 60 + "\n")


def cmd_queue(controller: WorkflowController) -> None:
    controller.initialize()
    print("\n" + "=" * 60)
    print("TASK QUEUE SUMMARY")
    print("=" * 60)
    print(f"Total Comment Tasks: {len(controller.comment_tasks)}")
    print(f"  - Eligible: {sum(1 for t in controller.comment_tasks if t.is_eligible)}")
    print(f"  - Skipped:  {sum(1 for t in controller.comment_tasks if not t.is_eligible)}")
    print(f"Total Post Tasks: {len(controller.post_tasks)}")
    print(f"  - Eligible: {sum(1 for t in controller.post_tasks if t.is_eligible)}")
    print(f"  - Skipped:  {sum(1 for t in controller.post_tasks if not t.is_eligible)}")
    print(f"\nActive Actionable Queue: {len(controller.task_queue)}")

    if controller.task_queue:
        print("\nTop 5 Eligible Tasks:")
        for idx, task in enumerate(controller.task_queue[:5], 1):
            print(f" {idx}. [{task.task_type.upper()}] Row {task.excel_row} | Acc: {task.account_id} | Profile: {task.chrome_profile_name}")
            print(f"    URL: {task.destination_url[:80]}...")
    print("=" * 60 + "\n")


def cmd_accounts(controller: WorkflowController) -> None:
    controller.account_manager.load()
    accounts = controller.account_manager.list_accounts()
    print("\n" + "=" * 60)
    print(f"CONFIGURED ACCOUNTS ({len(accounts)})")
    print("=" * 60)
    for acc in accounts:
        status_str = "ENABLED" if acc.enabled else "DISABLED"
        print(f"- {acc.account_id}")
        print(f"    Profile: {acc.chrome_profile_name}")
        print(f"    DataDir: {acc.user_data_directory}")
        print(f"    Status:  {status_str}")
    print("=" * 60 + "\n")


def cmd_test_run(controller: WorkflowController) -> None:
    controller.initialize()
    print("\n" + "=" * 60)
    print("EXECUTING TEST MODE DRY RUN")
    print("=" * 60)
    run_id = controller.start_run()
    print(f"Started session: {run_id}")

    task = controller.get_next_task()
    if task:
        print(f"\nNext Task Selected: {task.task_id} (Row {task.excel_row})")
        print(f"Assigned Account: {task.account_id}")
        print(f"Destination: {task.destination_url}")
        print(f"Preserved Text (first 100 chars): {task.reply_text[:100]}...")

        # Record test outcome
        success, msg = controller.record_outcome(
            task,
            outcome="Completed - verified",
            notes="Automated CLI dry-run verification test",
        )
        print(f"Recorded outcome: {msg}")

    controller.stop_run()
    print("Test run complete. All events recorded in SQLite audit trail.")
    print("=" * 60 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Shoro AI Reddit Operations CLI")
    parser.add_argument(
        "command",
        choices=["inspect", "queue", "accounts", "test-run", "dashboard"],
        default="inspect",
        nargs="?",
        help="Command to execute",
    )
    args = parser.parse_args()

    settings = AppSettings.load()
    controller = WorkflowController(settings)

    if args.command == "inspect":
        cmd_inspect(controller)
    elif args.command == "queue":
        cmd_queue(controller)
    elif args.command == "accounts":
        cmd_accounts(controller)
    elif args.command == "test-run":
        cmd_test_run(controller)
    elif args.command == "dashboard":
        import subprocess
        subprocess.run(["streamlit", "run", "app.py"])


if __name__ == "__main__":
    main()



