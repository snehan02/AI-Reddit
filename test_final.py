from modules.workflow_controller import WorkflowController
from config import AppSettings
import re

ctrl = WorkflowController(AppSettings.load())
ctrl.initialize()
task = next((t for t in ctrl.comment_tasks if t.excel_row == 380), None)

print("Running prepare_reply_in_reddit with unpacked eval_data...")
ok, msg, details = ctrl.prepare_reply_in_reddit(task, "shoro-test")
print("RESULT OK:", ok)
print("RESULT MSG:", msg)
print("RESULT DETAILS:", details)

