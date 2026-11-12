"""Streamlit Dashboard for Shoro AI Reddit Engagement Operations.

Strict compliance:
- Review navigation only via OpenClaw isolated browser ('shoro-test').
- Automated clicking, typing, form filling, commenting, and posting are strictly disabled.
- Excel is the source of truth; text is preserved exactly.
- Test Mode enabled by default.
- Gateway bound to localhost (127.0.0.1:18789).
"""

from datetime import datetime
import os
from pathlib import Path
import time
import pandas as pd
import streamlit as st

from config import AppSettings
from modules.account_manager import AccountManager
from modules.logging_config import setup_application_logging
from modules.openclaw_client import sanitize_dom_snapshot
from modules.state_manager import RunStatus
from modules.workflow_controller import WorkflowController

# Configure rotating logs with redaction
setup_application_logging()

# Page configuration
st.set_page_config(
    page_title="Shoro AI Reddit Operations",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def get_controller() -> WorkflowController:
    if "controller" not in st.session_state:
        settings = AppSettings.load()
        ctrl = WorkflowController(settings)
        ctrl.initialize()
        st.session_state.controller = ctrl
    return st.session_state.controller


controller = get_controller()
settings = controller.settings

# ----------------- SIDEBAR CONTROLS -----------------
with st.sidebar:
    st.title("🛡️ Shoro AI Reddit Ops")
    st.caption("Human-in-the-Loop Reddit Review Console")

    st.markdown("---")
    st.subheader("⚙️ Settings & Mode")

    force_mode = controller.settings.is_force_test_mode_active()
    if force_mode:
        st.toggle(
            "Test Mode",
            value=True,
            disabled=True,
            help="Test Mode is locked ON by environment safeguard FORCE_TEST_MODE.",
        )
        st.caption("🔒 **Locked by environment:** `FORCE_TEST_MODE=true`")
    else:
        test_mode_val = st.toggle(
            "Test Mode",
            value=settings.test_mode,
            help="When enabled, no live completion is written to Excel.",
        )
        if not test_mode_val and settings.test_mode:
            st.warning("⚠️ **Warning: Disabling Test Mode allows live updates to the Excel workbook.**")
            confirm_prod = st.checkbox(
                "I explicitly confirm switching to Production Mode (writes to Excel enabled)",
                key="confirm_disable_test_mode",
            )
            if confirm_prod:
                if st.button("Apply: Switch to Production Mode", type="primary", key="btn_apply_prod"):
                    settings.test_mode = False
                    controller.browser_manager.test_mode = False
                    controller.state_manager.test_mode = False
                    settings.save()
                    st.success("Production Mode activated.")
                    st.rerun()
        elif test_mode_val and not settings.test_mode:
            settings.test_mode = True
            controller.browser_manager.test_mode = True
            controller.state_manager.test_mode = True
            settings.save()
            st.info("Switched to Test Mode.")
            st.rerun()

    daily_cap = st.number_input("Daily Cap (per account)", min_value=1, max_value=50, value=settings.daily_comment_cap)
    if daily_cap != settings.daily_comment_cap:
        settings.daily_comment_cap = daily_cap
        controller.state_manager.daily_cap = daily_cap
        settings.save()

    pause_sec = st.number_input("Pause Between Tasks (s)", min_value=0, max_value=300, value=settings.review_task_pause_seconds)
    if pause_sec != settings.review_task_pause_seconds:
        settings.review_task_pause_seconds = pause_sec
        settings.save()

    st.markdown("---")
    st.subheader("🎮 Session Controls")
    status = controller.state_manager.run_status
    status_colors = {
        RunStatus.IDLE: "gray",
        RunStatus.RUNNING: "green",
        RunStatus.PAUSED: "orange",
        RunStatus.STOPPED: "red",
        RunStatus.COMPLETED: "blue",
    }
    st.markdown(f"**Session Status:** :{status_colors.get(status, 'gray')}[**{status.value}**]")

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("▶️ Start", width='stretch', disabled=status == RunStatus.RUNNING):
            controller.start_run()
            st.rerun()
        if st.button("⏸️ Pause", width='stretch', disabled=status != RunStatus.RUNNING):
            controller.pause_run()
            st.rerun()

    with col_btn2:
        if st.button("⏯️ Resume", width='stretch', disabled=status != RunStatus.PAUSED):
            controller.resume_run()
            st.rerun()
        if st.button("⏹️ Stop", width='stretch', disabled=status in (RunStatus.IDLE, RunStatus.STOPPED)):
            controller.stop_run()
            st.rerun()

    # ----------------- OPENCLAW GATEWAY STATUS IN SIDEBAR -----------------
    st.markdown("---")
    st.markdown("🦞 OpenClaw Gateway")
    st.subheader("🦞 OpenClaw Gateway")

    # Fetch status on demand or initial load
    if "openclaw_overview" not in st.session_state:
        st.session_state.openclaw_overview = controller.get_openclaw_overview()

    oc_info = st.session_state.openclaw_overview
    is_healthy = oc_info.get("is_healthy", False)
    v_data = oc_info.get("version")
    version_label = v_data.get("version_number") if isinstance(v_data, dict) else (v_data or "Unknown")

    if is_healthy:
        st.markdown(f"**Gateway:** :green[🟢 Online] (`127.0.0.1:18789`)")
        st.caption(f"OpenClaw v{version_label}")
        health_data = oc_info.get("health", {})
        if isinstance(health_data, dict) and health_data.get("event_loop"):
            st.caption(f"Loop: `{health_data.get('event_loop')}`")
    else:
        st.markdown(f"**Gateway:** :red[🔴 Offline]")
        if oc_info.get("health_error"):
            st.caption(f"Error: {oc_info.get('health_error')}")

    shoro_running = oc_info.get("shoro_running", False)
    st.markdown(f"**Profile (`shoro-test`):** {'🟢 Running' if shoro_running else '⚪ Stopped'}")

    if st.button("🔄 Refresh OpenClaw Status", width='stretch'):
        st.session_state.openclaw_overview = controller.get_openclaw_overview()
        st.rerun()

    st.markdown("---")
    st.subheader("👥 Account Profiles")
    accounts = controller.account_manager.list_accounts()
    st.caption(f"{len(accounts)} configured accounts (all disabled/unverified)")
    for acc in accounts[:5]:
        status_dot = "🟢" if acc.enabled else "🔴"
        st.write(f"- {status_dot} `{acc.account_id}` ({acc.chrome_profile_name})")
    if len(accounts) > 5:
        st.caption(f"...and {len(accounts) - 5} more in `config/accounts.json`")

    st.markdown("---")
    if st.button("🔄 Reload Workbook"):
        controller.initialize()
        st.toast("Workbook reloaded!")
        st.rerun()

# ----------------- HEADER BANNER -----------------
effective_test_mode = settings.test_mode or controller.settings.is_force_test_mode_active()
if effective_test_mode:
    st.warning("⚠️ **TEST MODE ACTIVE - NO REDDIT SUBMISSIONS WILL BE MADE** | Outcomes recorded for review only.")
else:
    st.error("🚨 **PRODUCTION MODE** | Outcomes confirmed by operator will be written to Excel.")

# ----------------- METRIC CARDS -----------------
stats = controller.stats
m_col1, m_col2, m_col3, m_col4, m_col5, m_col6 = st.columns(6)
m_col1.metric("Driving DMV Rows", stats.get("total_comments", 0))
m_col2.metric("Eligible Comments", stats.get("eligible_comments", 0))
m_col3.metric("Skipped Comments", stats.get("skipped_comments", 0))
m_col4.metric("Post Rows", stats.get("total_posts", 0))
m_col5.metric("Eligible Posts", stats.get("eligible_posts", 0), help="Posts require 'Account posted' column")
m_col6.metric("Active Queue", stats.get("total_queue", 0))

st.markdown("---")

# ----------------- TABS -----------------
tab_review, tab_queue, tab_schema, tab_audit, tab_openclaw = st.tabs([
    "🎯 Review Panel",
    "📋 Task Queue",
    "🔍 Schema & Summary",
    "📜 Audit Trail",
    "🦞 OpenClaw Console",
])

# ----------------- TAB 1: REVIEW PANEL -----------------
with tab_review:
    st.subheader("Operator Review Console")

    automation_mode = st.radio(
        "Choose Mode:",
        ["💬 Reddit Comment / Reply Automation (Driving DMV)", "📢 Reddit New Post / Thread Automation (Posts)"],
        horizontal=True,
        help="Switch between automating comments on existing discussion threads or publishing new posts.",
    )

    is_comment_mode = "Comment" in automation_mode

    if is_comment_mode:
        selectable_tasks = [t for t in controller.comment_tasks if t.destination_url]
        if not selectable_tasks:
            st.warning("No comment tasks found with valid URLs in Driving DMV worksheet.")
            selected_task = None
        else:
            # Default to row 380 if present, else first
            default_idx = next((i for i, t in enumerate(selectable_tasks) if t.excel_row == 380), 0)
            task_labels = [f"Row {t.excel_row}: {t.thread_title[:65]}... ({t.account_id or 'BLANK'})" for t in selectable_tasks]
            chosen_idx = st.selectbox(
                "Select Discussion Task to Review / Comment:",
                range(len(selectable_tasks)),
                index=default_idx,
                format_func=lambda i: task_labels[i],
                key="comment_task_selector",
            )
            selected_task = selectable_tasks[chosen_idx]
    else:
        selectable_tasks = controller.post_tasks
        if not selectable_tasks:
            st.warning("No post tasks found in Posts worksheet.")
            selected_task = None
        else:
            task_labels = [f"Row {t.excel_row}: {t.subreddit} - {t.title[:65]}" for t in selectable_tasks]
            chosen_idx = st.selectbox(
                "Select New Post / Thread to Review / Publish:",
                range(len(selectable_tasks)),
                index=0,
                format_func=lambda i: task_labels[i],
                key="post_task_selector",
            )
            selected_task = selectable_tasks[chosen_idx]

    if not selected_task:
        st.warning("No tasks available in workbook.")
    else:
        st.markdown(f"### Inspecting Task: `{selected_task.task_id}`")
        r_col1, r_col2 = st.columns([1.6, 1.4])

        with r_col1:
            st.markdown(f"**Worksheet:** `{selected_task.worksheet}` | **Row:** `{selected_task.excel_row}` | **Type:** `{selected_task.task_type.upper()}`")
            st.markdown(f"**Assigned Account:** `{selected_task.account_id or 'BLANK'}` | **Status:** `{selected_task.status}`")

            # Check daily cap
            if selected_task.account_id:
                allowed, cap_count, cap_msg = controller.state_manager.check_daily_cap(selected_task.account_id)
                st.caption(f"📊 Activity today: {cap_msg}")

            if selected_task.task_type == "comment":
                st.markdown(f"**Thread Title:** {selected_task.thread_title}")
                st.markdown(f"**Destination:** [{selected_task.destination_url}]({selected_task.destination_url})")
                st.markdown("#### Exact Prepared Reply (`Shoro AI reply` - Column K)")
                st.info(selected_task.reply_text)
                if selected_task.suggested_reply or selected_task.revised_suggested_reply or selected_task.why_it_matters:
                    with st.expander("📝 Review Workbook Draft Progression (Columns F, G, J)", expanded=False):
                        if selected_task.why_it_matters:
                            st.caption(f"**Why it matters / What changed (Col F):**\n\n{selected_task.why_it_matters}")
                        if selected_task.suggested_reply:
                            st.caption(f"**Original Suggested reply (Col G):**\n\n{selected_task.suggested_reply}")
                        if selected_task.revised_suggested_reply:
                            st.caption(f"**Revised suggested reply (Col J):**\n\n{selected_task.revised_suggested_reply}")
            else:
                st.markdown(f"**Subreddit:** `{selected_task.subreddit}`")
                st.markdown(f"**Post Title:** {selected_task.title}")
                st.markdown("#### Exact Post Body")
                st.info(selected_task.body)

        with r_col2:
            st.markdown("#### 1. OpenClaw Browser Review")
            st.caption("Operations restricted exclusively to the isolated `shoro-test` profile.")
            
            # Show live profile status badge
            is_shoro_up = oc_info.get("shoro_running", False)
            if is_shoro_up:
                st.success("🟢 Browser running on `shoro-test` (CDP Port: 18801)")
            else:
                st.info("⚪ Browser stopped on `shoro-test`")

            # Destination choice: safe test vs task url
            dest_label = f"Task Submit URL: {selected_task.destination_url}" if selected_task.task_type == "post" else f"Task Discussion URL: {selected_task.destination_url}"
            url_choice = st.radio(
                "Navigation Destination:",
                [
                    "Safe Test URL: https://example.com (Recommended for connectivity test)",
                    dest_label,
                ],
                help="Select destination URL for browser navigation.",
            )
            task_prof, _ = controller.account_manager.resolve_account(selected_task.account_id)
            target_nav_url = "https://example.com" if "Safe Test" in url_choice else selected_task.destination_url
            active_nav_profile = "shoro-test" if "Safe Test" in url_choice else (task_prof.chrome_profile_name if task_prof else "shoro-test")

            if selected_task.account_id and not task_prof:
                st.info(
                    f"🌐 **Account-Independent Session:** Task has assigned account `{selected_task.account_id}`. "
                    f"Running through active browser profile (`shoro-test`). Any active Reddit login will be used."
                )

            col_nav_a, col_nav_b = st.columns(2)
            with col_nav_a:
                if st.button("🌐 Open in Browser", width='stretch', type="primary"):
                    with st.spinner(f"Navigating {active_nav_profile} to {target_nav_url}..."):
                        success, msg, data = controller.open_in_openclaw(
                            url=target_nav_url,
                            profile_name=active_nav_profile,
                            task=selected_task,
                        )
                        if success:
                            st.success(msg)
                            if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                                st.session_state.openclaw_overview["shoro_running"] = True
                            st.rerun()
                        else:
                            st.error(msg)

            with col_nav_b:
                if st.button("🛑 Stop Browser", width='stretch'):
                    with st.spinner("Stopping browser..."):
                        success, msg = controller.stop_openclaw_browser(active_nav_profile, task=selected_task)
                        # Clear outdated tab and media references on stop
                        st.session_state.review_screenshot = None
                        st.session_state.review_snapshot = None
                        if success:
                            st.info(msg)
                            if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                                st.session_state.openclaw_overview["shoro_running"] = False
                            st.rerun()
                        else:
                            st.error(msg)

            if st.button("🔑 Open Chrome Window to Log into Reddit (e.g. Shoro-AI-02)", width='stretch'):
                with st.spinner("Launching visible Chrome window on your desktop..."):
                    ok, msg = controller.launch_visible_browser_for_login(active_nav_profile, "https://www.reddit.com/login")
                    if ok:
                        st.success(f"🖥️ {msg}")
                        st.info("A Google Chrome window has opened directly on your screen. Log in to Reddit there, and your session will be saved for the bot!")
                    else:
                        st.error(msg)

            # Inspection Controls: Screenshot & Snapshot
            col_insp_a, col_insp_b = st.columns(2)
            with col_insp_a:
                if st.button("📸 Capture Screenshot", width='stretch'):
                    with st.spinner("Capturing screenshot..."):
                        success, msg, s_path = controller.capture_openclaw_screenshot(active_nav_profile, task=selected_task)
                        if success and s_path:
                            st.session_state.review_screenshot = s_path
                            st.success("Screenshot captured!")
                        else:
                            if "stopped" in msg.lower() or "not running" in msg.lower():
                                st.warning(f"⚠️ {msg}")
                            else:
                                st.error(msg)

            with col_insp_b:
                if st.button("📄 Page DOM Snapshot", width='stretch'):
                    with st.spinner("Retrieving snapshot..."):
                        success, msg, snap_content = controller.get_openclaw_snapshot(active_nav_profile, task=selected_task)
                        if success and snap_content:
                            st.session_state.review_snapshot = sanitize_dom_snapshot(snap_content)
                            if "CAPTCHA" in msg or "⚠️" in msg:
                                st.warning(msg)
                            else:
                                st.success("Snapshot retrieved!")
                        else:
                            if "stopped" in msg.lower() or "not running" in msg.lower():
                                st.warning(f"⚠️ {msg}")
                            else:
                                st.error(msg)

            # Render captured media if present
            if st.session_state.get("review_screenshot"):
                resolved_s_path = os.path.expanduser(st.session_state.review_screenshot)
                if os.path.exists(resolved_s_path):
                    st.image(resolved_s_path, caption=f"OpenClaw Browser View ({os.path.basename(resolved_s_path)})", width='stretch')

            if st.session_state.get("review_snapshot"):
                snap_text = st.session_state.review_snapshot
                # Show CAPTCHA warning banner prominently
                if controller.check_snapshot_for_captcha(snap_text):
                    st.warning(
                        "⚠️ **CAPTCHA or verification screen detected.** "
                        "Please open the browser and complete the verification manually. "
                        "Do not attempt to bypass it."
                    )
                with st.expander("📄 Accessible DOM Snapshot Preview", expanded=False):
                    st.code(snap_text[:2500] + ("\n... [truncated]" if len(snap_text) > 2500 else ""))

            st.markdown("---")
            if selected_task.task_type == "post":
                st.markdown("#### 📢 Reddit New Thread / Post Automation")
                st.caption("Reads Subreddit, Post Title, and Post Body from Excel, navigates to the submit page, types the headline and body, and submits the post.")

                col_prep, col_submit = st.columns(2)
                with col_prep:
                    if st.button("📝 Prepare Post (Draft Only)", width='stretch'):
                        with st.spinner("Navigating to submit page, typing title and post body..."):
                            prep_ok, prep_msg, prep_details = controller.prepare_post_in_reddit(
                                selected_task, profile_name=active_nav_profile, auto_submit=False
                            )
                            st.session_state.prep_result = {
                                "success": prep_ok,
                                "message": prep_msg,
                                "details": prep_details,
                                "task_id": selected_task.task_id,
                            }
                            if prep_ok:
                                st.success(f"✅ {prep_msg}")
                                st.info("✋ **Ready for manual review:** The Title and Post Body have been typed into Reddit. Review in Chrome and click 'Post' when ready.")
                                if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                                    st.session_state.openclaw_overview["shoro_running"] = True
                            else:
                                if "login" in prep_msg.lower() or "authentication" in prep_msg.lower():
                                    st.error(f"🔒 **{prep_msg}**")
                                    st.info("To create posts on Reddit, the browser session must be logged in. Open Reddit's login page in Chrome.")
                                else:
                                    st.error(f"❌ {prep_msg}")

                with col_submit:
                    if st.button("🚀 Automatically Submit Post", width='stretch', type="primary"):
                        with st.spinner("Navigating to submit page, typing title & body, and publishing post..."):
                            sub_ok, sub_msg, sub_details = controller.submit_post_to_reddit(
                                selected_task, profile_name=active_nav_profile
                            )
                            st.session_state.prep_result = {
                                "success": sub_ok,
                                "message": sub_msg,
                                "details": sub_details,
                                "task_id": selected_task.task_id,
                            }
                            if sub_ok:
                                st.success(f"🎉 **{sub_msg}**")
                                pub_url = sub_details.get("published_url")
                                if pub_url and "reddit.com" in pub_url:
                                    st.markdown(f"🔗 **Published Post URL:** [{pub_url}]({pub_url})")
                                if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                                    st.session_state.openclaw_overview["shoro_running"] = True
                            else:
                                if "login" in sub_msg.lower() or "authentication" in sub_msg.lower():
                                    st.error(f"🔒 **{sub_msg}**")
                                    st.info("To create posts on Reddit, the browser session must be logged in.")
                                else:
                                    st.error(f"❌ {sub_msg}")

                # Display post preparation summary
                prep_info = st.session_state.get("prep_result")
                if prep_info and prep_info.get("task_id") == selected_task.task_id:
                    st.markdown("##### 📋 Post Automation Summary")
                    st.markdown(f"**Worksheet & Row:** `{selected_task.worksheet}` (Row {selected_task.excel_row})")
                    st.markdown(f"**Subreddit:** `{selected_task.subreddit}`")
                    st.markdown(f"**Post Title:** {selected_task.title}")
                    status_val = prep_info.get("details", {}).get("status", "READY_FOR_MANUAL_SUBMISSION" if prep_info.get("success") else "FAILED")
                    st.markdown(f"**Post Status:** `{status_val}`")
                    if prep_info.get("success"):
                        st.success(f"**Result:** `{prep_info.get('message')}`")
                    else:
                        st.error(f"**Result:** `{prep_info.get('message')}`")

            else:
                st.markdown("#### 💬 Reddit Reply Automation")
                st.caption("Reads the thread URL, assigned account, and exact Column K reply from Excel, locates 'Join the conversation', inserts the text, and submits.")

                col_prep, col_submit = st.columns(2)
                with col_prep:
                    if st.button("📝 Prepare Reply (Draft Only)", width='stretch'):
                        with st.spinner("Locating 'Join the conversation', activating editor, and inserting reply..."):
                            prep_ok, prep_msg, prep_details = controller.prepare_reply_in_reddit(
                                selected_task, profile_name=active_nav_profile, auto_submit=False
                            )
                            st.session_state.prep_result = {
                                "success": prep_ok,
                                "message": prep_msg,
                                "details": prep_details,
                                "task_id": selected_task.task_id,
                            }
                            if prep_ok:
                                st.success(f"✅ {prep_msg}")
                                st.info("✋ **Ready for manual submission:** The exact reply from Column K has been inserted into the Reddit editor. Review the text in Chrome and click 'Comment' manually when ready.")
                                if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                                    st.session_state.openclaw_overview["shoro_running"] = True
                            else:
                                if "login" in prep_msg.lower() or "authentication" in prep_msg.lower():
                                    st.error(f"🔒 **{prep_msg}**")
                                    st.info("To prepare comments on Reddit, the browser session must be logged in. Open Reddit's login page in Chrome.")
                                else:
                                    st.error(f"❌ {prep_msg}")

                with col_submit:
                    if st.button("🚀 Automatically Submit Reply", width='stretch', type="primary"):
                        with st.spinner("Navigating, locating 'Join the conversation', inserting Column K reply, and submitting..."):
                            sub_ok, sub_msg, sub_details = controller.submit_reply_to_reddit(
                                selected_task, profile_name=active_nav_profile
                            )
                            st.session_state.prep_result = {
                                "success": sub_ok,
                                "message": sub_msg,
                                "details": sub_details,
                                "task_id": selected_task.task_id,
                            }
                            if sub_ok:
                                st.success(f"🎉 **{sub_msg}**")
                                st.info("The comment has been published directly to the Reddit thread! You can check your Chrome window to view it.")
                                if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                                    st.session_state.openclaw_overview["shoro_running"] = True
                            else:
                                if "login" in sub_msg.lower() or "authentication" in sub_msg.lower():
                                    st.error(f"🔒 **{sub_msg}**")
                                    st.info("To submit comments on Reddit, the browser session must be logged in.")
                                else:
                                    st.error(f"❌ {sub_msg}")

                # Quick helper to open login page if not logged in
                prep_info = st.session_state.get("prep_result")
                if prep_info and prep_info.get("details", {}).get("status") == "NOT_LOGGED_IN":
                    if st.button("🔑 Open Reddit Login Page in Chrome Window", width='stretch'):
                        with st.spinner("Opening https://www.reddit.com/login in browser..."):
                            controller.open_in_openclaw("https://www.reddit.com/login", active_nav_profile, task=selected_task)
                            st.info("Opened Reddit login screen. Please complete login in the Chrome window, then click 'Prepare Reply in Reddit' again.")

                # Display preparation summary
                prep_info = st.session_state.get("prep_result")
                if prep_info and prep_info.get("task_id") == selected_task.task_id:
                    st.markdown("##### 📋 Reply Preparation Summary")
                    st.markdown(f"**Worksheet & Row:** `{selected_task.worksheet}` (Row {selected_task.excel_row})")
                    st.markdown(f"**Assigned Account:** `{selected_task.account_id or 'BLANK'}`")
                    st.markdown(f"**Reddit Discussion URL:** [{selected_task.destination_url}]({selected_task.destination_url})")
                    status_val = prep_info.get("details", {}).get("status", "READY_FOR_MANUAL_SUBMISSION" if prep_info.get("success") else "FAILED")
                    st.markdown(f"**Editor Status:** `{status_val}`")
                    
                    if prep_info.get("success"):
                        st.success(f"**Result:** `Ready for manual submission`")
                    else:
                        st.error(f"**Result:** `{prep_info.get('message')}`")

                    with st.expander("📄 Exact Reply (Column K)", expanded=False):
                        st.code(selected_task.reply_text)

            # Manual Status Override Fallback (collapsed by default, for troubleshooting only)
            with st.expander("⚙️ Manual Status Override (Fallback)", expanded=False):
                st.caption("Use only if manual intervention or exception handling is required.")
                outcome = st.selectbox(
                    "Override Task Outcome",
                    [
                        "Completed - verified",
                        "Not submitted",
                        "Page unavailable",
                        "Locked or archived",
                        "Login required",
                        "CAPTCHA or verification required",
                        "Account restriction",
                        "Submission outcome uncertain",
                        "Other error",
                    ],
                    key="override_outcome",
                )
                notes = st.text_input("Override Notes (Optional)", key="override_notes")
                if st.button("💾 Apply Status Override & Advance", width='stretch'):
                    success, msg = controller.record_outcome(selected_task, outcome, notes)
                    if success:
                        st.success(msg)
                        time.sleep(1)
                        controller.current_task = None
                        st.rerun()
                    else:
                        st.error(msg)


# ----------------- TAB 2: TASK QUEUE -----------------
with tab_queue:
    st.subheader("Loaded Tasks")
    queue_filter = st.radio("Filter tasks:", ["All", "Eligible", "Skipped", "Comments Only", "Posts Only"], horizontal=True)

    all_tasks = controller.comment_tasks + controller.post_tasks
    if queue_filter == "Eligible":
        filtered = [t for t in all_tasks if t.is_eligible]
    elif queue_filter == "Skipped":
        filtered = [t for t in all_tasks if not t.is_eligible]
    elif queue_filter == "Comments Only":
        filtered = controller.comment_tasks
    elif queue_filter == "Posts Only":
        filtered = controller.post_tasks
    else:
        filtered = all_tasks

    data = []
    for t in filtered:
        data.append({
            "Task ID": t.task_id,
            "Type": t.task_type,
            "Sheet": t.worksheet,
            "Row": t.excel_row,
            "Account": t.account_id or "BLANK",
            "Profile": t.chrome_profile_name or "-",
            "Status": t.status,
            "Eligible": "✅ Yes" if t.is_eligible else "❌ No",
            "Reason / Note": t.skip_reason or "",
        })

    st.dataframe(pd.DataFrame(data), width='stretch', hide_index=True)

# ----------------- TAB 3: SCHEMA & SUMMARY -----------------
with tab_schema:
    st.subheader("Workbook Inspection Report")
    report = controller.schema_report
    if report:
        st.write(f"**Workbook Path:** `{report.file_path}`")
        st.write(f"**Worksheets Found:** {', '.join(report.sheet_names)}")
        st.write(f"**Ignored Sheets:** {', '.join(report.ignored_sheets)}")

        if report.errors:
            st.error("### Schema Errors")
            for err in report.errors:
                st.write(f"- {err}")

        if report.warnings:
            st.warning("### Schema Warnings")
            for warn in report.warnings:
                st.write(f"- {warn}")

        st.markdown("---")
        st.markdown("#### Worksheet Details")
        for sname, sschema in report.sheets.items():
            with st.expander(f"Sheet: {sname} ({sschema.populated_row_count} populated rows, {sschema.max_column} columns)"):
                st.write(f"- **Max Row:** {sschema.max_row}")
                st.write(f"- **Max Column:** {sschema.max_column}")
                st.write(f"- **Column N Present:** {'Yes' if sschema.has_column_n else 'No (Safety enforced: no write to Column N)'}")
                st.write(f"- **Headers:** {sschema.headers}")
                if sschema.missing_required_columns:
                    st.error(f"Missing required columns: {sschema.missing_required_columns}")

# ----------------- TAB 4: AUDIT TRAIL -----------------
with tab_audit:
    st.subheader("SQLite Execution Events (`database/execution_state.db`)")
    col_aud1, col_aud2 = st.columns([1, 4])
    with col_aud1:
        if st.button("🔄 Refresh Audit Log", width='stretch'):
            st.rerun()
    events = controller.audit_logger.get_recent_events(limit=100)
    if events:
        st.dataframe(pd.DataFrame(events), width='stretch', hide_index=True)
    else:
        st.info("No audit events recorded yet.")

# ----------------- TAB 5: OPENCLAW CONSOLE -----------------
with tab_openclaw:
    st.subheader("🦞 OpenClaw Infrastructure & Profile Console")
    st.caption("Central management and safety monitoring for the OpenClaw Gateway and isolated browser profiles.")

    # Gateway Health Details
    h_col1, h_col2 = st.columns([1, 1])
    with h_col1:
        st.markdown("#### Gateway Status")
        if is_healthy:
            st.success(f"🟢 **Gateway Online & Healthy** (Bound to `127.0.0.1:18789`)")
            h_data = oc_info.get("health", {})
            if isinstance(h_data, dict):
                st.write(f"- **OpenClaw Version:** `{version_label}`")
                st.write(f"- **Event Loop Health:** `{h_data.get('event_loop', 'ok')}`")
                st.write(f"- **Check Duration:** `{h_data.get('check_duration', 'N/A')}`")
                st.write(f"- **Agents:** `{h_data.get('agents', 'main (default)')}`")
        else:
            st.error("🔴 **Gateway Offline or Unreachable**")
            if oc_info.get("health_error"):
                st.code(oc_info.get("health_error"))

    with h_col2:
        st.markdown("#### Configured Browser Profiles")
        profiles_list = oc_info.get("profiles", [])
        if profiles_list:
            profile_rows = []
            for p in profiles_list:
                p_name = p.get("name", "")
                is_operable = p_name == "shoro-test"
                is_protected = p_name in ("user", "chrome")
                policy_badge = "✅ Allowed (Isolated)" if is_operable else ("🔒 Disallowed (Protected)" if is_protected else "🔒 Locked (Default)")
                profile_rows.append({
                    "Profile Name": p_name,
                    "Live Status": p.get("status", "unknown"),
                    "CDP Port": str(p.get("port")) if p.get("port") else "-",
                    "Default": "Yes" if p.get("is_default") else "No",
                    "Security Policy": policy_badge,
                })
            st.dataframe(pd.DataFrame(profile_rows), width='stretch', hide_index=True)
        else:
            st.info("No profiles detected or Gateway offline.")

    # ---- Persistent session status panel ----
    st.markdown("---")
    st.markdown("#### 💾 Persistent Session Status (`shoro-test`)")
    session_status = controller.get_profile_session_status("shoro-test")
    if session_status.get("exists"):
        st.success(
            f"🟢 **Session data directory found.** Cookies and login state from previous sessions "
            f"are retained on disk.\n\n"
            f"📂 `{session_status.get('data_dir', 'N/A')}`"
        )
        st.caption(
            "**Note:** A persistent directory means the *browser* remembers past sessions, "
            "but it does NOT guarantee an active Reddit login. "
            "The operator must verify login state manually by opening the browser."
        )
    else:
        st.info(
            f"⚪ **No session directory found.** The `shoro-test` profile will start with a clean slate.\n\n"
            f"📂 Expected path: `{session_status.get('data_dir', 'N/A')}`\n\n"
            f"{session_status.get('note', '')}"
        )
    st.caption(
        "🔒 **Security:** Passwords are never stored by this application. "
        "Session cookies (if any) are managed exclusively by OpenClaw's browser process "
        "inside the isolated profile directory."
    )

    st.markdown("---")
    st.markdown("#### Interactive Review Sandbox (`shoro-test` Profile)")
    st.caption("Test URL navigation and visual inspection in the isolated browser without interacting with Reddit.")

    sb_col1, sb_col2 = st.columns([2, 1])
    with sb_col1:
        test_url_input = st.text_input("Destination URL:", value="https://example.com")
    with sb_col2:
        st.write("**Target Profile:** `shoro-test`")
        st.caption("Protected profiles `user` and `chrome` are permanently locked.")

    b_col1, b_col2, b_col3, b_col4 = st.columns(4)
    with b_col1:
        if st.button("🌐 Navigate", key="sb_nav", width='stretch', type="primary"):
            with st.spinner(f"Navigating to {test_url_input}..."):
                success, msg, data = controller.open_in_openclaw(test_url_input, "shoro-test")
                if success:
                    st.success(msg)
                    if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                        st.session_state.openclaw_overview["shoro_running"] = True
                    st.rerun()
                else:
                    st.error(msg)

    with b_col2:
        if st.button("📸 Screenshot", key="sb_scr", width='stretch'):
            with st.spinner("Capturing screenshot..."):
                success, msg, s_path = controller.capture_openclaw_screenshot("shoro-test")
                if success and s_path:
                    st.session_state.console_screenshot = s_path
                    st.success("Screenshot saved!")
                else:
                    if "stopped" in msg.lower() or "not running" in msg.lower():
                        st.warning(f"⚠️ {msg}")
                    else:
                        st.error(msg)

    with b_col3:
        if st.button("📄 DOM Snapshot", key="sb_snap", width='stretch'):
            with st.spinner("Retrieving DOM snapshot..."):
                success, msg, snap_content = controller.get_openclaw_snapshot("shoro-test")
                if success and snap_content:
                    st.session_state.console_snapshot = sanitize_dom_snapshot(snap_content)
                    st.success("DOM snapshot retrieved!")
                else:
                    if "stopped" in msg.lower() or "not running" in msg.lower():
                        st.warning(f"⚠️ {msg}")
                    else:
                        st.error(msg)

    with b_col4:
        if st.button("🛑 Stop Browser", key="sb_stop", width='stretch'):
            with st.spinner("Stopping browser..."):
                success, msg = controller.stop_openclaw_browser("shoro-test")
                # Clear outdated tab and media references on stop
                st.session_state.console_screenshot = None
                st.session_state.console_snapshot = None
                if success:
                    st.info(msg)
                    if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                        st.session_state.openclaw_overview["shoro_running"] = False
                    st.rerun()
                else:
                    st.error(msg)

    # Render console outputs
    if st.session_state.get("console_screenshot"):
        resolved_cs_path = os.path.expanduser(st.session_state.console_screenshot)
        if os.path.exists(resolved_cs_path):
            st.image(resolved_cs_path, caption=f"Sandbox Screenshot ({os.path.basename(resolved_cs_path)})", width='stretch')

    if st.session_state.get("console_snapshot"):
        with st.expander("📄 Accessible DOM Tree Output", expanded=True):
            st.code(st.session_state.console_snapshot[:3500] + ("\n... [truncated]" if len(st.session_state.console_snapshot) > 3500 else ""))
