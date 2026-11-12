import os
import sys
from modules.workflow_controller import WorkflowController
from config import AppSettings

settings = AppSettings.load()
controller = WorkflowController(settings)
controller.initialize()

tasks = controller.comment_tasks
print(f"Loaded {len(tasks)} tasks.")

target_task = next((t for t in tasks if t.excel_row == 380), None)
print("Testing task:", target_task.task_id, target_task.excel_row, target_task.destination_url)
print("Reply text:", target_task.reply_text[:60])

ok, msg, details = controller.prepare_reply_in_reddit(target_task, profile_name="shoro-test")
print("RESULT OK:", ok)
print("RESULT MSG:", msg)
print("RESULT DETAILS:", details)
