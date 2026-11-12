from modules.workflow_controller import WorkflowController
from config import AppSettings
import logging

logging.basicConfig(level=logging.INFO)
ctrl = WorkflowController(AppSettings.load())
ctrl.initialize()
task = next((t for t in ctrl.comment_tasks if t.excel_row == 380), None)

ok, msg, details = ctrl.prepare_reply_in_reddit(task, "shoro-test")
print("OK:", ok)
print("MSG:", msg)
print("DETAILS:", details)

