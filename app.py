"""Streamlit Dashboard for Shoro AI Reddit Engagement Operations.

Executive-Ready, Clean, Human-in-the-Loop Reddit Automation Console.
Strict compliance:
- Review navigation and automated typing via OpenClaw isolated browser ('shoro-test').
- Natural human-touch interaction: randomized scrolling, human typing speed, Lexical AST state handling.
- Account-independent architecture: whichever Reddit account is logged into the Chrome profile can publish seamlessly.
- Excel is the source of truth; text is preserved exactly.
- Test Mode enabled by default for staging safety.
- Gateway bound to localhost (127.0.0.1:18789).
"""

import inspect
import os
from pathlib import Path
import re
import time
import pandas as pd
import streamlit as st

from config import AppSettings
from modules.logging_config import setup_application_logging
from modules.openclaw_client import sanitize_dom_snapshot
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


def get_controller(force_reload: bool = False) -> WorkflowController:
    """Instantiate and return a cached WorkflowController instance for fast loads."""
    if "controller" not in st.session_state or force_reload:
        settings = AppSettings.load()
        ctrl = WorkflowController(settings)
        ctrl.initialize()
        ctrl.allow_account_independent = True
        st.session_state.controller = ctrl
    return st.session_state.controller


controller = get_controller()
settings = controller.settings

# ----------------- SIDEBAR CONTROLS -----------------
with st.sidebar:
    st.title("🛡️ Shoro AI Operations")
    st.caption("Autonomous Reddit Review & Engagement Console")

    col_hdr_ref, _ = st.columns([1.2, 0.8])
    with col_hdr_ref:
        if st.button("🔄 Refresh Data", help="Reload latest data from Excel, database, and accounts.", use_container_width=True):
            controller.initialize(reload_accounts=True)
            # Invalidate cached UI sheets, recent rows, and account sessions
            for k in list(st.session_state.keys()):
                if k.startswith("sheets_") or k.startswith("recent_rows_") or k in ("openclaw_overview", "account_sessions_cache"):
                    del st.session_state[k]
            st.toast("✅ System data refreshed!")
            st.rerun()

    st.markdown("---")

    st.subheader("⚙️ Operational Settings")

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
            help="When enabled, actions stage text into Reddit for review without live final submission or Excel writes.",
        )
        if not test_mode_val and settings.test_mode:
            st.warning("⚠️ **Switching to Live Production Mode enables publishing to Reddit and writing status to Excel.**")
            confirm_prod = st.checkbox(
                "I confirm switching to Live Production Mode",
                key="confirm_disable_test_mode",
            )
            if confirm_prod:
                if st.button("Apply: Activate Production Mode", type="primary", key="btn_apply_prod"):
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

    controller.allow_account_independent = True

    posts_per_acc = st.number_input(
        "Posts Per Account",
        min_value=1,
        max_value=20,
        value=getattr(settings, "posts_per_account", 5),
        help="Configurable limit for maximum posts/actions prepared or published per account in a batch run.",
    )
    if posts_per_acc != getattr(settings, "posts_per_account", 5):
        settings.posts_per_account = posts_per_acc
        settings.daily_comment_cap = posts_per_acc
        controller.state_manager.daily_cap = posts_per_acc
        settings.save()

    st.markdown("---")
    st.markdown("### 📊 Local Excel / Spreadsheet Sync")
    st.caption("💡 **Why:** Automatic live workbook output. | **Use:** Specify your local Excel file (`.xlsx`) and select your worksheet. Downloaded Google Sheets are fully supported directly with zero APIs or Apps Script.")

    current_wb_path = settings.workbook_path or "data/Reddit Threads.xlsx"
    new_wb_path = st.text_input(
        "Excel File Path (.xlsx):",
        value=current_wb_path,
        help="Full path or relative path to your local or downloaded Excel workbook (e.g. data/Reddit Threads.xlsx or D:\\Shoro AI Reddit\\Driving_DMV.xlsx).",
    )

    wb_p = Path(new_wb_path.strip()) if new_wb_path and new_wb_path.strip() else Path("data/Reddit Threads.xlsx")
    if not wb_p.is_absolute():
        wb_p = Path(os.getcwd()) / wb_p

    # Auto-save file path change immediately if valid workbook exists
    if new_wb_path.strip() and new_wb_path.strip() != settings.workbook_path:
        if wb_p.exists() and wb_p.suffix.lower() == ".xlsx":
            settings.workbook_path = new_wb_path.strip()
            settings.save()
            controller.excel_handler.set_workbook_path(settings.workbook_path)
            controller.initialize(reload_accounts=False)

    # Cached sheet inspection to prevent slow openpyxl loads on every click
    wb_mtime = wb_p.stat().st_mtime if wb_p.exists() else 0
    wb_cache_key = f"sheets_{wb_p}_{wb_mtime}"
    if wb_cache_key not in st.session_state:
        available_sheets = []
        if wb_p.exists() and wb_p.suffix.lower() == ".xlsx":
            try:
                import openpyxl
                wb_inspect = openpyxl.load_workbook(wb_p, read_only=True)
                available_sheets = list(wb_inspect.sheetnames)
                wb_inspect.close()
            except Exception as wb_err:
                st.error(f"⚠️ Error reading workbook: {wb_err}")
        st.session_state[wb_cache_key] = available_sheets
    available_sheets = st.session_state[wb_cache_key]

    if not wb_p.exists() and new_wb_path.strip():
        st.warning(f"⚠️ File does not exist yet at: `{wb_p}`. You can enter an existing file path or click 'Download Google Sheet' below.")

    # Worksheet selectors
    if available_sheets:
        comment_def_idx = available_sheets.index(settings.comment_sheet) if settings.comment_sheet in available_sheets else 0
        selected_comment_sheet = st.selectbox(
            "Discussion / Comment Sheet:",
            options=available_sheets,
            index=comment_def_idx,
            help="Select the worksheet containing discussion threads to comment on.",
        )
        post_def_idx = available_sheets.index(settings.post_sheet) if settings.post_sheet in available_sheets else (1 if len(available_sheets) > 1 else 0)
        selected_post_sheet = st.selectbox(
            "Posts Sheet (Optional):",
            options=available_sheets,
            index=post_def_idx,
            help="Select the worksheet containing new posts/threads to publish.",
        )
        # Auto-save sheet change immediately
        if selected_comment_sheet != settings.comment_sheet or selected_post_sheet != settings.post_sheet:
            settings.comment_sheet = selected_comment_sheet
            settings.post_sheet = selected_post_sheet
            settings.save()
            controller.initialize(reload_accounts=False)
    else:
        selected_comment_sheet = st.text_input("Comment Sheet Name:", value=settings.comment_sheet or "Driving DMV")
        selected_post_sheet = st.text_input("Post Sheet Name:", value=settings.post_sheet or "Posts")
        if selected_comment_sheet != settings.comment_sheet or selected_post_sheet != settings.post_sheet:
            settings.comment_sheet = selected_comment_sheet
            settings.post_sheet = selected_post_sheet
            settings.save()
            controller.initialize(reload_accounts=False)

    # Save button / Confirmation
    path_changed = (new_wb_path.strip() != settings.workbook_path)
    sheet_changed = (selected_comment_sheet != settings.comment_sheet or selected_post_sheet != settings.post_sheet)
    if path_changed or sheet_changed:
        if st.button("💾 Save Excel Configuration", type="primary", use_container_width=True):
            settings.workbook_path = new_wb_path.strip()
            settings.comment_sheet = selected_comment_sheet
            settings.post_sheet = selected_post_sheet
            settings.save()
            controller.excel_handler.set_workbook_path(settings.workbook_path)
            controller.initialize(reload_accounts=False)
            st.toast("✅ Excel configuration saved and reloaded!")
            st.rerun()

    # Live Workbook Status Card & Verification
    if wb_p.exists():
        import datetime
        mtime = datetime.datetime.fromtimestamp(wb_p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        st.success(f"🟢 **Active Workbook:** `{wb_p.name}`\n\n• **Path:** `{wb_p}`\n• **Last Modified on Disk:** `{mtime}`")

        # Cached live verification preview of rows in the workbook
        recent_cache_key = f"recent_rows_{wb_p}_{wb_mtime}_{settings.comment_sheet}"
        if recent_cache_key not in st.session_state:
            st.session_state[recent_cache_key] = controller.excel_handler.get_recent_updated_rows(settings.comment_sheet, limit=6)
        recent_rows = st.session_state[recent_cache_key]

        if recent_rows:
            st.markdown("📋 **Latest Updated Rows in Excel:**")
            for r_item in recent_rows:
                st.caption(f"• **Row {r_item['row']}**: `{r_item['status']}` ({r_item['account']}) | {r_item['timestamp']}")

        st.info("💡 **Important Notice:** If this Excel file is currently open in Microsoft Excel, close and reopen it to see the updates, as Excel does not automatically reload spreadsheets updated by external software.")
    else:
        st.error(f"🔴 **Workbook Not Found:** `{wb_p}`\nPlease enter a valid file path or click 'Download Google Sheet' below.")

    # Download from Google Sheet button (public .xlsx export - NO credentials/Apps Script required)
    gsheet_url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}/edit?gid=0#gid=0"
    col_dl1, col_dl2 = st.columns([1, 1])
    with col_dl1:
        st.link_button("🌐 Open Google Sheet", gsheet_url, use_container_width=True)
    with col_dl2:
        if st.button("📥 Download Google Sheet", use_container_width=True, help="Downloads the online Google Sheet directly as an .xlsx file into your configured file path."):
            with st.spinner("Downloading Google Sheet to local Excel..."):
                ok, msg = controller.pull_latest_from_google_sheet(target_path=settings.workbook_path)
                if ok:
                    controller.initialize(reload_accounts=False)
                    st.toast("✅ Downloaded latest Google Sheet to local Excel!")
                    st.rerun()
                else:
                    st.error(f"❌ {msg}")

    st.markdown("---")
    st.markdown("### 🦞 OpenClaw Gateway")

    # Fetch status on demand or initial load
    if "openclaw_overview" not in st.session_state:
        st.session_state.openclaw_overview = controller.get_openclaw_overview()

    oc_info = st.session_state.openclaw_overview
    is_healthy = oc_info.get("is_healthy", False)
    v_data = oc_info.get("version")
    version_label = v_data.get("version_number") if isinstance(v_data, dict) else (v_data or "Unknown")

    if is_healthy:
        st.markdown(f"**Gateway:** :green[🟢 Online] (`127.0.0.1:18789`)")
        st.caption(f"OpenClaw Engine v{version_label}")
    else:
        st.markdown(f"**Gateway:** :red[🔴 Offline]")
        if oc_info.get("health_error"):
            st.caption(f"Error: {oc_info.get('health_error')}")

    shoro_running = oc_info.get("shoro_running", False)
    st.markdown(f"**Profile (`shoro-test`):** {'🟢 Running' if shoro_running else '⚪ Idle / Ready'}")

    active_nav_profile = "shoro-test"

    if st.button("🔑 Launch Desktop Chrome (Log In)", width='stretch'):
        with st.spinner("Opening Google Chrome on desktop..."):
            ok, msg = controller.launch_visible_browser_for_login(active_nav_profile, "https://www.reddit.com/login")
            if ok:
                st.success(f"🖥️ {msg}")
                st.info("Log in to Reddit in the Chrome window. Your session cookies will be saved automatically!")
            else:
                st.error(msg)

    c_b1, c_b2 = st.columns(2)
    with c_b1:
        if st.button("🔄 Refresh", width='stretch'):
            st.session_state.openclaw_overview = controller.get_openclaw_overview()
            st.rerun()
    with c_b2:
        if st.button("🔄 Reload Excel", width='stretch'):
            controller.initialize()
            st.toast("Workbook reloaded successfully!")
            st.rerun()


# ----------------- HEADER BANNER & METRICS -----------------
effective_test_mode = settings.test_mode or controller.settings.is_force_test_mode_active()
if effective_test_mode:
    st.info("🛡️ **TEST MODE ACTIVE** | Previews, typing simulations, and DOM interactions run safely without permanent changes.")
else:
    st.warning("🚨 **PRODUCTION MODE ACTIVE** | Live Reddit submissions are enabled; outcomes will be written to Excel.")

stats = controller.stats
m1, m2, m3, m4 = st.columns(4)
m1.metric("💬 Discussion Replies", stats.get("total_comments", 0), help="Total comment opportunities loaded from Driving DMV worksheet")
m2.metric("📢 Community Posts", stats.get("total_posts", 0), help="Total post tasks loaded from Posts worksheet")
m3.metric("🎯 Active Ready Queue", stats.get("total_queue", 0), help="Tasks currently eligible and ready for automation")
m4.metric("🛡️ Human-Touch Anti-Ban", "Active", help="Randomized typing delays, scrolling, and Lexical dispatch active")

st.markdown("---")

# ----------------- STREAMLINED TABS (Fully Aligned with PRD & Tests) -----------------
tab_ops, tab_accounts, tab_queue, tab_openclaw = st.tabs([
    "🎯 Review & Batch Operations",
    "👥 Account Manager",
    "📋 Real-Time Task Queue",
    "🦞 OpenClaw Console",
])

# ====================================================================
# TAB 1: UNIFIED OPERATIONS CONSOLE (REVIEW & BATCH OPERATIONS)
# ====================================================================
with tab_ops:
    st.subheader("🎯 Operator Review & Multi-Account Batch Operations")
    st.caption("💡 **Why:** Unified command console merging single-thread review with multi-account batch preparation and publishing. | **Use:** Configure accounts and batch settings below, or review and submit individual posts/comments.")

    accounts_list = controller.account_manager.list_accounts()
    all_account_ids = [p.account_id for p in accounts_list]

    # Pre-calculate accounts with active persistent sessions (cached in session state for fast render)
    if "account_sessions_cache" not in st.session_state:
        st.session_state["account_sessions_cache"] = {
            p.account_id: controller.account_manager.check_session_status(p.account_id)
            for p in accounts_list
        }
    acc_sessions = st.session_state["account_sessions_cache"]

    logged_in_ids = [
        aid for aid, st_info in acc_sessions.items()
        if st_info.get("has_cookies") or st_info.get("session_status") == "Logged In / Active"
    ]

    # Manage session state selection
    if "multiselect_participating" not in st.session_state:
        st.session_state["multiselect_participating"] = list(logged_in_ids) if logged_in_ids else list(all_account_ids)
    else:
        st.session_state["multiselect_participating"] = [
            aid for aid in st.session_state["multiselect_participating"] if aid in all_account_ids
        ]
        recent_id = st.session_state.get("last_added_account_id")
        if recent_id and recent_id in all_account_ids and recent_id not in st.session_state["multiselect_participating"]:
            st.session_state["multiselect_participating"].append(recent_id)

    col_top_acc, col_top_wf = st.columns([1.5, 1.5])
    with col_top_acc:
        st.markdown("##### 👥 Active / Participating Accounts")
        col_ab1, col_ab2, col_ab3 = st.columns([1.2, 1, 1])
        with col_ab1:
            if st.button(f"🟢 Select Logged In ({len(logged_in_ids)})", use_container_width=True, help="Select only accounts with active saved Chrome login sessions"):
                st.session_state["multiselect_participating"] = list(logged_in_ids)
                st.rerun()
        with col_ab2:
            if st.button("👥 Select All", use_container_width=True, help="Select all registered accounts"):
                st.session_state["multiselect_participating"] = list(all_account_ids)
                st.rerun()
        with col_ab3:
            if st.button("❌ Clear", use_container_width=True, help="Deselect all accounts"):
                st.session_state["multiselect_participating"] = []
                st.rerun()

        chosen_accounts = st.multiselect(
            "Select Participating Accounts:",
            options=all_account_ids,
            key="multiselect_participating",
            help="Accounts that will participate in this batch operation and content distribution.",
        )
        st.caption("💡 **Why:** Multi-account distribution. | **Use:** Select which of the 20–25 accounts participate in this run.")

    with col_top_wf:
        st.markdown("##### ⚙️ Operation Workflow")
        automation_mode = st.radio(
            "Select Operation Workflow:",
            [
                "💬 Reddit Discussion Comments (Driving DMV)",
                "📢 Reddit New Posts & Threads (Posts)",
                "🔄 Both Posts & Comments (Combined Batch)",
            ],
            horizontal=True,
            help="Toggle between commenting on relevant threads, publishing new top-level posts, or executing both in one combined batch.",
        )
        st.caption("💡 **Why:** Switches between thread commenting, original post authoring, or combined processing. | **Use:** Select 'Comments', 'Posts', or 'Both' for a unified multi-account batch.")

    # ---------------- BATCH OPERATIONS CONTROLS ----------------
    with st.expander("⚡ Multi-Account Batch Operations & Controls", expanded=True):
        st.caption("💡 **Why:** Prepares and stages drafts or publishes automatically across all participating accounts. | **Use:** Choose execution mode, set limits, and click 'Start Batch' or 'Stop Batch'.")

        col_bm1, col_bm2 = st.columns([1.5, 1.5])
        with col_bm1:
            batch_mode = st.radio(
                "Batch Execution Mode:",
                [
                    "👤 Human Submit Mode (Prepare in Chrome & Save as Draft)",
                    "🚀 Auto Submit Mode (Publish & Verify Complete)",
                ],
                index=0,
                horizontal=False,
                help="Human Submit: Types title/body/flair into Chrome and saves status as Draft for manual review. Auto Submit: Fully publishes and verifies live, setting status to Complete."
            )
            mode_val = "human_submit" if "Human" in batch_mode else "auto"
            st.caption("💡 **Why:** Choose between human approval vs direct publishing. | **Use:** 'Human Submit' leaves post/comment in editor ready to click; 'Auto' posts directly.")

        with col_bm2:
            col_blim, col_binterval, col_bsync = st.columns([1.2, 1.6, 1.0])
            with col_blim:
                post_limit = st.number_input(
                    "Posts Per Account:",
                    min_value=1,
                    max_value=20,
                    value=getattr(settings, "posts_per_account", 5),
                    help="Maximum items each account will draft or submit in this batch.",
                )
            with col_binterval:
                batch_interval_val = st.number_input(
                    "Account Cooldown Gap (Hours):",
                    min_value=0.5,
                    max_value=72.0,
                    value=float(getattr(settings, "min_post_interval_hours", 8.0)),
                    step=0.5,
                    help="Anti-ban publication cooldown: minimum time (in hours) between consecutive actions from the same Reddit account.",
                )
                if batch_interval_val != getattr(settings, "min_post_interval_hours", 8.0):
                    settings.min_post_interval_hours = float(batch_interval_val)
                    settings.save()
            with col_bsync:
                st.write("")
                sync_gsheets_first = st.checkbox("Sync Sheets First", value=True)

        st.markdown("---")
        col_b_act1, col_b_act2 = st.columns([2, 1])
        with col_b_act1:
            start_batch_clicked = st.button("🚀 Start Batch Operation", type="primary", use_container_width=True)
        with col_b_act2:
            stop_batch_clicked = st.button("🛑 Stop Batch", type="secondary", use_container_width=True)
            if stop_batch_clicked:
                controller.request_stop_batch()
                st.toast("🛑 Stop Batch requested! Halting task scheduling safely.")
                st.warning("🛑 Stop Batch requested: Running tasks will halt safely and pending items are preserved.")

        if start_batch_clicked:
            if not chosen_accounts:
                st.error("Please select at least one participating account.")
            else:
                controller.reset_stop_batch()
                st.info(f"Starting batch run in {batch_mode} across {len(chosen_accounts)} accounts...")
                p_bar = st.progress(0.0)
                status_box = st.status(f"Executing Batch ({batch_mode})...", expanded=True)

                def batch_cb(evt):
                    ev_type = evt.get("type")
                    c = evt.get("current", 0)
                    tot = evt.get("total", 1)
                    if tot > 0:
                        p_bar.progress(min(1.0, c / max(1, tot)))
                    if ev_type == "item_error":
                        status_box.write(f"⚠️ {evt.get('message', '')}")
                    elif ev_type == "item_finish":
                        status_box.write(f"✅ {evt.get('message', '')}")
                    else:
                        status_box.write(f"📌 {evt.get('message', '')}")

                wf_type = "all"
                if "Comments" in automation_mode:
                    wf_type = "comment"
                elif "Posts" in automation_mode:
                    wf_type = "post"

                controller.settings.min_post_interval_hours = float(batch_interval_val)
                controller.settings.save()

                batch_args = {
                    "account_ids": chosen_accounts,
                    "mode": mode_val,
                    "post_limit_per_account": int(post_limit),
                    "sync_gsheets": sync_gsheets_first,
                    "progress_callback": batch_cb,
                    "workflow_type": wf_type,
                }
                try:
                    sig = inspect.signature(controller.execute_batch_preparation)
                    if "min_post_interval_hours" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                        batch_args["min_post_interval_hours"] = float(batch_interval_val)
                except Exception:
                    pass

                try:
                    res = controller.execute_batch_preparation(**batch_args)
                except TypeError as te:
                    if "min_post_interval_hours" in str(te):
                        batch_args.pop("min_post_interval_hours", None)
                        res = controller.execute_batch_preparation(**batch_args)
                    else:
                        raise te
                if res.get("success") and res.get("total_prepared", 0) > 0:
                    status_box.update(label="✅ " + res.get("message", ""), state="complete", expanded=True)
                    st.success(f"🎉 **{res.get('message')}**")
                elif res.get("total_prepared", 0) == 0:
                    status_box.update(label="⚠️ Completed – 0 items prepared", state="error", expanded=True)
                    st.warning(f"⚠️ **{res.get('message')}**")
                    if res.get("errors"):
                        for err in res.get("errors"):
                            st.error(f"❌ {err}")
                else:
                    status_box.update(label="⚠️ Completed with warnings", state="error", expanded=True)
                    st.warning(res.get("message"))
                st.session_state["last_batch_result"] = res

    st.markdown("---")
    st.markdown("### 📋 Prepared Content & Review Queue (Drafts & Submissions)")
    st.caption("💡 **Why:** Central queue storing all prepared drafts across accounts. | **Use:** Click 'Open in Chrome' to inspect & submit, then 'Verify & Mark Complete' to sync records.")

    col_srch, col_mode = st.columns([2, 1])
    with col_srch:
        f_search = st.text_input("🔍 Search Prepared Content:", placeholder="Search title, reply content, or account ID...")
    with col_mode:
        f_filter = st.radio("Display View:", ["All Records", "Draft", "Ready for Review", "Complete", "Failed"], horizontal=True)

    status_query = None if f_filter == "All Records" else f_filter

    # Fetch prepared items from SQLite
    items = controller.audit_logger.list_prepared_items(status=status_query)

    if f_search and f_search.strip():
        q = f_search.strip().lower()
        items = [
            it for it in items
            if q in str(it.get("title", "")).lower()
            or q in str(it.get("content", "")).lower()
            or q in str(it.get("destination_url", "")).lower()
            or q in str(it.get("account_id", "")).lower()
        ]

    # Quick Metrics
    total_ready = sum(1 for it in items if it.get("status") in ("Ready for Review", "Ready"))
    total_draft = sum(1 for it in items if it.get("status") == "Draft")
    total_done = sum(1 for it in items if it.get("status") == "Complete")
    total_failed = sum(1 for it in items if it.get("status") == "Failed")
    m_r1, m_r2, m_r3, m_r4 = st.columns(4)
    m_r1.metric("📝 Drafts", total_draft)
    m_r2.metric("📌 Ready for Review", total_ready)
    m_r3.metric("✅ Complete", total_done)
    m_r4.metric("🔴 Failed", total_failed)

    st.markdown("---")

    if not items:
        st.info("No prepared items found matching your filters. You can prepare drafts via **Start Batch Operation** above.")
    else:
        # Caching account gap checks per render to eliminate hundreds of redundant queries
        min_gap = float(getattr(settings, "min_post_interval_hours", 8.0))
        gap_cache = {}

        # Add pagination to keep browser rendering lightweight and snappy
        page_size = 20
        total_items = len(items)
        total_pages = max(1, (total_items + page_size - 1) // page_size)

        col_pg1, col_pg2, col_pg3 = st.columns([2, 1, 1])
        with col_pg1:
            st.caption(f"Showing **{min(total_items, page_size)}** items per page (Total: **{total_items}**)")
        with col_pg2:
            current_page = st.number_input(
                "Page:",
                min_value=1,
                max_value=total_pages,
                value=1,
                step=1,
                key="tab1_prep_page",
            )
        with col_pg3:
            st.caption(f"of **{total_pages}** pages")

        start_idx = (int(current_page) - 1) * page_size
        page_items = items[start_idx : start_idx + page_size]

        for it in page_items:
            it_id = it.get("item_id")
            it_type = (it.get("task_type") or "task").upper()
            it_acc = it.get("account_id") or "Unknown"
            it_row = it.get("excel_row")
            it_status = it.get("status") or "Ready"
            it_title = it.get("title") or "No Title"
            it_content = it.get("content") or ""
            it_url = it.get("destination_url") or ""
            it_prep_time = it.get("prepared_at") or ""
            it_pub_time = it.get("published_at") or ""
            it_err = it.get("error_message") or ""

            status_icon = "🟢" if it_status == "Complete" else ("🟡" if it_status in ("Ready for Review", "Ready") else ("📝" if it_status == "Draft" else "🔴"))

            with st.expander(f"{status_icon} [{it_type}] Row {it_row} | {it_acc} | {it_title[:60]}... ({it_status})", expanded=(it_status in ("Ready for Review", "Ready", "Draft"))):
                col_d1, col_d2 = st.columns([3, 1])
                with col_d1:
                    st.write(f"**Target URL:** [{it_url}]({it_url})" if it_url else "**No URL**")
                    if it.get("subreddit"):
                        st.write(f"**Subreddit:** r/{it.get('subreddit')} | **Flair:** {it.get('flair') or 'None'}")
                    st.write(f"**Prepared At:** `{it_prep_time}`" + (f" | **Published At:** `{it_pub_time}`" if it_pub_time else ""))
                    if it_err:
                        st.error(f"⚠️ **Error Details:** {it_err}")
                    st.markdown("**Staged Content (Column K exact):**")
                    st.text_area("Content Preview", value=it_content, height=120, key=f"txt_{it_id}", disabled=True)

                with col_d2:
                    st.write("**Operator Review Actions:**")
                    if st.button("👁️ Open in Chrome", key=f"btn_open_{it_id}", use_container_width=True, help="Opens visible Chrome under this account's profile, navigates to target, refills content if needed, and scrolls submit button into view."):
                        with st.spinner(f"Opening Chrome for {it_acc}..."):
                            ok, msg = controller.open_and_refill_prepared_item(it_id)
                            if ok:
                                st.success(msg)
                            else:
                                st.error(msg)
                    st.caption("💡 **Why:** Opens Chrome. | **Use:** Inspect thread and manually click submit.")

                    if it_status != "Complete":
                        # Check publication interval gap with local render cache
                        if it_acc not in gap_cache:
                            gap_cache[it_acc] = controller.audit_logger.check_eight_hour_gap(it_acc, min_gap_hours=min_gap)
                        can_pub, gap_msg = gap_cache[it_acc]
                        if not can_pub:
                            st.caption(f"⚠️ {gap_msg}")

                        gap_str = f"{int(min_gap)}" if min_gap == int(min_gap) else f"{min_gap:.1f}"
                        if st.button("✅ Verify & Mark Complete", key=f"btn_comp_{it_id}", type="primary", use_container_width=True, help=f"Confirms human submission, updates DB, records publication timestamp for {gap_str}h gap rule, and syncs status to Excel and Google Sheet."):
                            with st.spinner("Confirming submission and syncing..."):
                                ok, msg = controller.verify_and_mark_complete(it_id)
                                if ok:
                                    st.success(msg)
                                    time.sleep(1)
                                    st.rerun()
                                else:
                                    st.error(msg)
                        st.caption("💡 **Why:** Confirms submission. | **Use:** Updates Excel, Google Sheets, & SQLite.")
                    else:
                        st.success("✅ Published & Verified")
    st.markdown("---")
    with st.expander("📄 Single Task Inspection & Content Preview", expanded=False):
        st.caption("💡 **Why:** Detailed inspection for an individual Reddit discussion thread or post topic. | **Use:** Select an item to preview its content, navigate in Chrome, or execute single-task actions.")

        is_comment_mode = "Comment" in automation_mode

        if is_comment_mode:
            selectable_tasks = sorted([t for t in controller.comment_tasks if t.destination_url], key=lambda x: getattr(x, "excel_row", 0), reverse=True)
            if not selectable_tasks:
                st.warning("No comment tasks found with valid URLs in Driving DMV worksheet.")
                selected_task = None
            else:
                default_idx = next((i for i, t in enumerate(selectable_tasks) if t.excel_row == 380), 0)
                task_labels = [f"Row {t.excel_row}: {t.thread_title[:60]}... ({t.account_id or 'Account-Independent'})" for t in selectable_tasks]
                chosen_idx = st.selectbox(
                    "Select Discussion Thread:",
                    range(len(selectable_tasks)),
                    index=default_idx,
                    format_func=lambda i: task_labels[i],
                    key="comment_task_selector",
                )
                st.caption("💡 **Why:** Chooses the exact Reddit discussion and Excel row. | **Use:** Select an item to preview its Column K response and target URL.")
                selected_task = selectable_tasks[chosen_idx]
        else:
            selectable_tasks = controller.post_tasks
            if not selectable_tasks:
                st.warning("No post tasks found in Posts worksheet.")
                selected_task = None
            else:
                task_labels = [f"Row {t.excel_row}: r/{t.subreddit} - {t.title[:60]}" for t in selectable_tasks]
                chosen_idx = st.selectbox(
                    "Select New Post Task:",
                    range(len(selectable_tasks)),
                    index=0,
                    format_func=lambda i: task_labels[i],
                    key="post_task_selector",
                )
                st.caption("💡 **Why:** Chooses the post topic to author. | **Use:** Select an item to preview its subreddit, title, and body.")
                selected_task = selectable_tasks[chosen_idx]

        if selected_task:
            col_left, col_right = st.columns([1.5, 1.5])

            with col_left:
                st.markdown(f"#### 📄 Task Intelligence: `{selected_task.task_id}`")
                st.markdown(
                    f"**Worksheet:** `{selected_task.worksheet}` | "
                    f"**Row:** `{selected_task.excel_row}` | "
                    f"**Type:** `{selected_task.task_type.upper()}` | "
                    f"**Status:** `{selected_task.status}`"
                )

                if selected_task.task_type == "comment":
                    st.markdown(f"**Thread Title:** {selected_task.thread_title}")
                    st.markdown(f"**Target URL:** [{selected_task.destination_url}]({selected_task.destination_url})")
                    st.markdown("##### 💬 Exact Column K Reply (`Shoro AI reply`)")
                    st.info(selected_task.reply_text)

                    if selected_task.why_it_matters or selected_task.suggested_reply or selected_task.revised_suggested_reply:
                        with st.expander("📝 View AI Reasoning & Progression (Columns F, G, J)", expanded=False):
                            if selected_task.why_it_matters:
                                st.caption(f"**Why it matters (Col F):**\n\n{selected_task.why_it_matters}")
                            if selected_task.suggested_reply:
                                st.caption(f"**Initial Draft (Col G):**\n\n{selected_task.suggested_reply}")
                            if selected_task.revised_suggested_reply:
                                st.caption(f"**Refined Draft (Col J):**\n\n{selected_task.revised_suggested_reply}")
                else:
                    st.markdown(f"**Subreddit:** `r/{selected_task.subreddit}`")
                    st.markdown(f"**Post Title:** {selected_task.title}")
                    st.markdown("##### 📢 Exact Post Body")
                    st.info(selected_task.body)

            with col_right:
                st.markdown("#### 🤖 OpenClaw Browser Execution")

                is_up = oc_info.get("shoro_running", False)
                if is_up:
                    st.success("🟢 Browser Session Active (`shoro-test` on CDP Port: 18801)")
                else:
                    st.info("⚪ Browser Session Ready (`shoro-test`)")

                # Quick Navigation / Stop Bar
                col_nav1, col_nav2, col_nav3 = st.columns([2, 1, 1])
                with col_nav1:
                    target_nav_url = selected_task.destination_url
                    if st.button("🌐 Open Thread in Chrome", width='stretch'):
                        with st.spinner(f"Navigating to {target_nav_url}..."):
                            ok, msg, _ = controller.open_in_openclaw(target_nav_url, active_nav_profile, task=selected_task)
                            if ok:
                                st.success(f"Navigated to {target_nav_url}")
                                if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                                    st.session_state.openclaw_overview["shoro_running"] = True
                            else:
                                st.error(msg)
                with col_nav2:
                    if st.button("🪟 Show Chrome", width='stretch', help="Brings the native desktop Chrome window to the front"):
                        controller.bring_browser_to_front("Chrome")
                        st.toast("Chrome window brought to front!")
                with col_nav3:
                    if st.button("🛑 Stop Browser", width='stretch'):
                        controller.stop_openclaw_browser(active_nav_profile, task=selected_task)
                        st.info("Browser stopped. Session preserved.")
                        if "openclaw_overview" in st.session_state and isinstance(st.session_state.openclaw_overview, dict):
                            st.session_state.openclaw_overview["shoro_running"] = False
                        st.rerun()

                st.caption("💡 **Why:** Browser control bar. | **Use:** Click 'Open Thread' to navigate, 'Show Chrome' to bring window forward, or 'Stop Browser' to close.")

                st.markdown("---")

                # Automation Actions
                if selected_task.task_type == "comment":
                    st.markdown("##### 🤖 Execution Mode")
                    st.caption("💡 **Why:** Choose between hands-free publishing or review before posting. | **Use:** 'Auto Comment' posts immediately; 'Human Submit' stages the full reply in Chrome for review.")
                    c_act1, c_act2 = st.columns(2)
                    with c_act1:
                        if st.button("🚀 Auto Comment", width='stretch', type="primary", help="Fully automates thread navigation, natural typing, proofreading, submitting comment, and verifying it live on Reddit."):
                            live_screen_expander = st.expander("🖥️ Live Browser Screen (Real-Time Monitor)", expanded=True)
                            live_viewport = live_screen_expander.empty()
                            status_box = st.status("Executing Auto Comment Workflow...", expanded=True)
                            progress_bar = st.progress(0.0)

                            def granular_cb(evt):
                                p_num = evt.get("phase", 0)
                                p_name = evt.get("phase_name", "")
                                st_msg = evt.get("status", "")
                                d_data = evt.get("details", {})
                                sc_path = evt.get("screenshot_path") or d_data.get("screenshot_path")
                                if sc_path and os.path.exists(sc_path):
                                    try:
                                        with open(sc_path, "rb") as f:
                                            img_bytes = f.read()
                                        live_viewport.image(img_bytes, caption=f"🖥️ Live Screen: Phase {p_num}/9 - {p_name} ({st_msg})", use_container_width=True)
                                    except Exception:
                                        pass
                                progress_bar.progress(min(max(p_num / 9.0, 0.0), 1.0))
                                if p_num == 0:
                                    status_box.write(f"🌐 **{st_msg}**")
                                elif p_num == 1:
                                    status_box.write(f"📜 **Phase 1: Scroll & Safety Verification** - *{st_msg}*")
                                elif p_num == 2:
                                    status_box.write(f"📖 **Phase 2: Deep Read Question** - *{st_msg}*")
                                    if d_data.get("extracted_detail"):
                                        status_box.info(f"🔍 **Extracted Detail:** {d_data.get('extracted_detail')}")
                                elif p_num == 3:
                                    status_box.write(f"💬 **Phase 3: Top 5 Comments Audit** - *{st_msg}*")
                                elif p_num == 4:
                                    status_box.write(f"✍️ **Phase 4: Open Comment Composer** - *{st_msg}*")
                                elif p_num == 5:
                                    status_box.write(f"⌨️ **Phase 5: Natural Word-by-Word Typing** - *{st_msg}*")
                                elif p_num == 6:
                                    status_box.write(f"🧐 **Phase 6: Re-Read & Proofread** - *{st_msg}*")
                                elif p_num == 7:
                                    status_box.write(f"🚀 **Phase 7: Submit Comment** - *{st_msg}*")
                                elif p_num == 8:
                                    status_box.write(f"🔄 **Phase 8: Page Refresh & Live Check** - *{st_msg}*")
                                    if d_data.get("live_url"):
                                        status_box.markdown(f"🔗 **Live Permalink:** [{d_data.get('live_url')}]({d_data.get('live_url')})")
                                elif p_num == 9:
                                    status_box.write(f"📊 **Phase 9: Excel Sync & Close** - *{st_msg}*")

                            sub_ok, sub_msg, sub_details = controller.auto_submit_comment(
                                selected_task,
                                profile_name=active_nav_profile,
                                progress_callback=granular_cb,
                            )
                            progress_bar.progress(1.0)
                            if sub_ok:
                                status_box.update(label=f"🎉 Thread Reply Completed Live!", state="complete", expanded=True)
                                st.success(f"🎉 **{sub_msg}**")
                                live_url = sub_details.get("live_url")
                                if live_url and "reddit.com" in live_url:
                                    st.markdown(f"🔗 **Live Comment URL:** [{live_url}]({live_url})")
                            else:
                                status_box.update(label=f"⚠️ Notice: {sub_msg}", state="error", expanded=True)
                                st.error(f"❌ {sub_msg}")

                    with c_act2:
                        if st.button("👤 Human Submit", width='stretch', help="Navigates to thread, inspects rules, types complete comment into editor, and stops before submitting so you can review and manually click Comment."):
                            live_screen_expander = st.expander("🖥️ Live Browser Screen (Real-Time Monitor)", expanded=True)
                            live_viewport = live_screen_expander.empty()
                            status_box = st.status("Preparing comment in browser for Human Review...", expanded=True)
                            progress_bar = st.progress(0.0)

                            def human_comment_cb(evt):
                                p_num = evt.get("phase", 0)
                                p_name = evt.get("phase_name", "")
                                st_msg = evt.get("status", "")
                                d_data = evt.get("details", {})
                                sc_path = evt.get("screenshot_path") or d_data.get("screenshot_path")
                                if sc_path and os.path.exists(sc_path):
                                    try:
                                        with open(sc_path, "rb") as f:
                                            img_bytes = f.read()
                                        live_viewport.image(img_bytes, caption=f"🖥️ Live Screen: Phase {p_num}/7 - {p_name} ({st_msg})", use_container_width=True)
                                    except Exception:
                                        pass
                                progress_bar.progress(min(max(p_num / 7.0, 0.0), 1.0))
                                status_box.write(f"📝 **Phase {p_num}: {p_name}** - *{st_msg}*")

                            prep_ok, prep_msg, prep_details = controller.human_submit_comment(
                                selected_task,
                                profile_name=active_nav_profile,
                                progress_callback=human_comment_cb,
                            )
                            progress_bar.progress(1.0)
                            st.session_state.prep_result = {
                                "success": prep_ok,
                                "message": prep_msg,
                                "details": prep_details,
                                "task_id": selected_task.task_id,
                            }
                            if prep_ok:
                                status_box.update(label="✅ Ready for Human Submission!", state="complete", expanded=True)
                                st.success(f"✅ **{prep_msg}**")
                                st.info("👉 **Action Required:** Comment content is fully prepared and typed in Chrome. Please switch to the Chrome window, review the comment, and manually click **'Comment'** to submit.")

                                final_sc = prep_details.get("screenshot_path")
                                if not final_sc or not os.path.exists(final_sc):
                                    final_sc = r"backups\live_screens\live_automation_view.jpg"
                                if os.path.exists(final_sc):
                                    st.markdown("#### 🖥️ Staged Comment Ready in Chrome:")
                                    st.image(final_sc, caption="Live snapshot of comment staged in Reddit editor", use_container_width=True)

                                col_sw1, col_sw2 = st.columns(2)
                                with col_sw1:
                                    if st.button("🖥️ Bring Chrome Window to Front", key="btn_front_comm"):
                                        controller.bring_browser_to_front("Chrome")
                                        st.toast("Brought Chrome to the front of your screen!")
                                with col_sw2:
                                    if selected_task.destination_url:
                                        st.link_button("🌐 Open Thread URL in Browser", selected_task.destination_url, use_container_width=True)
                            else:
                                status_box.update(label=f"❌ {prep_msg}", state="error", expanded=True)
                                st.error(f"❌ {prep_msg}")

                    # Persistent display of last prepared comment if ready for manual submission
                    prep_res = st.session_state.get("prep_result")
                    if prep_res and prep_res.get("success") and prep_res.get("task_id") == selected_task.task_id:
                        details = prep_res.get("details", {}) or {}
                        sc_p = details.get("screenshot_path")
                        if not sc_p or not os.path.exists(sc_p):
                            sc_p = r"backups\live_screens\live_automation_view.jpg"
                        if os.path.exists(sc_p):
                            with st.expander("📌 Staged Comment Ready in Browser (Live Snapshot)", expanded=True):
                                st.image(sc_p, caption="Staged comment ready in Chrome editor", use_container_width=True)
                                c_p1, c_p2 = st.columns(2)
                                with c_p1:
                                    if st.button("🖥️ Bring Chrome to Front", key=f"btn_front_comm_p_{selected_task.task_id}"):
                                        controller.bring_browser_to_front("Chrome")
                                        st.toast("Brought Chrome to foreground!")
                                with c_p2:
                                    t_url = details.get("target_url") or getattr(selected_task, "destination_url", None)
                                    if t_url:
                                        st.link_button("🌐 Open URL in Browser", t_url, use_container_width=True)

                elif selected_task.task_type == "post":
                    st.markdown("##### 🤖 Execution Mode")
                    st.caption("💡 **Why:** Choose between hands-free publishing or review before posting. | **Use:** 'Auto Post' submits immediately; 'Human Submit' fills title/body/flair in Chrome for review.")
                    p_act1, p_act2 = st.columns(2)
                    with p_act1:
                        if st.button("🚀 Auto Post", width='stretch', type="primary", help="Fully automates subreddit navigation, rules check, title/body drafting, flair selection, proofreading, submitting post, and verifying it live on Reddit."):
                            live_screen_expander = st.expander("🖥️ Live Browser Screen (Real-Time Monitor)", expanded=True)
                            live_viewport = live_screen_expander.empty()
                            status_box = st.status("Drafting and publishing post to Reddit...", expanded=True)
                            progress_bar = st.progress(0.0)

                            def post_sub_cb(evt):
                                p_num = evt.get("phase", 0)
                                p_name = evt.get("phase_name", "")
                                st_msg = evt.get("status", "")
                                sc_path = evt.get("screenshot_path") or (evt.get("details", {}) or {}).get("screenshot_path")
                                if sc_path and os.path.exists(sc_path):
                                    try:
                                        with open(sc_path, "rb") as f:
                                            img_bytes = f.read()
                                        live_viewport.image(img_bytes, caption=f"🖥️ Live Screen: Phase {p_num}/7 - {p_name} ({st_msg})", use_container_width=True)
                                    except Exception:
                                        pass
                                progress_bar.progress(min(p_num / 7.0, 1.0))
                                if p_num in [1, 2, 3, 4, 5]:
                                    status_box.write(f"📝 **Phase {p_num}/7: {p_name}** - *{st_msg}*")
                                elif p_num == 6:
                                    status_box.write(f"🚀 **Phase 6/7: Submission** - *{st_msg}*")
                                elif p_num == 7:
                                    status_box.write(f"🔍 **Phase 7/7: Live Verification** - *{st_msg}*")

                            sub_ok, sub_msg, sub_details = controller.auto_submit_post(
                                selected_task,
                                profile_name=active_nav_profile,
                                progress_callback=post_sub_cb,
                            )
                            progress_bar.progress(1.0)
                            st.session_state.prep_result = {
                                "success": sub_ok,
                                "message": sub_msg,
                                "details": sub_details,
                                "task_id": selected_task.task_id,
                            }
                            if sub_ok:
                                status_box.update(label="🎉 Post Published Live!", state="complete", expanded=True)
                                st.success(f"🎉 **{sub_msg}**")
                                pub_url = sub_details.get("published_url")
                                if pub_url and "reddit.com" in pub_url:
                                    st.markdown(f"🔗 **Published Live URL:** [{pub_url}]({pub_url})")
                            else:
                                status_box.update(label=f"⚠️ {sub_msg}", state="error", expanded=True)
                                st.error(f"❌ {sub_msg}")
                                pub_url = sub_details.get("published_url")
                                if pub_url and "reddit.com" in pub_url:
                                    st.markdown(f"🔗 **Post URL:** [{pub_url}]({pub_url})")

                    with p_act2:
                        if st.button("👤 Human Submit", width='stretch', help="Navigates to subreddit submission page, types title and body, selects flair, proofreads, and stops before submitting so you can review and manually click Post."):
                            live_screen_expander = st.expander("🖥️ Live Browser Screen (Real-Time Monitor)", expanded=True)
                            live_viewport = live_screen_expander.empty()
                            status_box = st.status("Drafting post in Reddit submit page for Human Review...", expanded=True)
                            progress_bar = st.progress(0.0)

                            def post_prep_cb(evt):
                                p_num = evt.get("phase", 0)
                                p_name = evt.get("phase_name", "")
                                st_msg = evt.get("status", "")
                                sc_path = evt.get("screenshot_path") or (evt.get("details", {}) or {}).get("screenshot_path")
                                if sc_path and os.path.exists(sc_path):
                                    try:
                                        with open(sc_path, "rb") as f:
                                            img_bytes = f.read()
                                        live_viewport.image(img_bytes, caption=f"🖥️ Live Screen: Phase {p_num}/5 - {p_name} ({st_msg})", use_container_width=True)
                                    except Exception:
                                        pass
                                progress_bar.progress(min(p_num / 5.0, 1.0))
                                status_box.write(f"📝 **Phase {p_num}/5: {p_name}** - *{st_msg}*")

                            prep_ok, prep_msg, prep_details = controller.human_submit_post(
                                selected_task,
                                profile_name=active_nav_profile,
                                progress_callback=post_prep_cb,
                            )
                            progress_bar.progress(1.0)
                            st.session_state.prep_result = {
                                "success": prep_ok,
                                "message": prep_msg,
                                "details": prep_details,
                                "task_id": selected_task.task_id,
                            }
                            if prep_ok:
                                status_box.update(label="✅ Ready for Human Submission!", state="complete", expanded=True)
                                st.success(f"✅ **{prep_msg}**")
                                st.info("👉 **Action Required:** Post is fully drafted with Title, Body, and Flair in Chrome. Please switch to the Chrome window, review the post, and manually click **'Post'** to submit.")

                                final_sc = prep_details.get("screenshot_path")
                                if not final_sc or not os.path.exists(final_sc):
                                    final_sc = r"backups\live_screens\live_automation_view.jpg"
                                if os.path.exists(final_sc):
                                    st.markdown("#### 🖥️ Staged Post Ready in Chrome:")
                                    st.image(final_sc, caption="Live snapshot of post staged in Reddit editor", use_container_width=True)

                                col_sw1, col_sw2 = st.columns(2)
                                with col_sw1:
                                    if st.button("🖥️ Bring Chrome Window to Front", key="btn_front_post"):
                                        controller.bring_browser_to_front("Chrome")
                                        st.toast("Brought Chrome to the front of your screen!")
                                with col_sw2:
                                    post_url = f"https://www.reddit.com/r/{selected_task.subreddit}/submit"
                                    st.link_button("🌐 Open Subreddit Submit URL", post_url, use_container_width=True)
                            else:
                                status_box.update(label=f"❌ {prep_msg}", state="error", expanded=True)
                                st.error(f"❌ {prep_msg}")

                    # Persistent display of last prepared post if ready for manual submission
                    prep_res = st.session_state.get("prep_result")
                    if prep_res and prep_res.get("success") and prep_res.get("task_id") == selected_task.task_id:
                        details = prep_res.get("details", {}) or {}
                        sc_p = details.get("screenshot_path")
                        if not sc_p or not os.path.exists(sc_p):
                            sc_p = r"backups\live_screens\live_automation_view.jpg"
                        if os.path.exists(sc_p):
                            with st.expander("📌 Staged Post Ready in Browser (Live Snapshot)", expanded=True):
                                st.image(sc_p, caption="Staged post ready in Chrome editor", use_container_width=True)
                                c_p1, c_p2 = st.columns(2)
                                with c_p1:
                                    if st.button("🖥️ Bring Chrome to Front", key=f"btn_front_post_p_{selected_task.task_id}"):
                                        controller.bring_browser_to_front("Chrome")
                                        st.toast("Brought Chrome to foreground!")
                                with c_p2:
                                    post_url = f"https://www.reddit.com/r/{selected_task.subreddit}/submit"
                                    st.link_button("🌐 Open Subreddit Submit URL", post_url, use_container_width=True)

                    st.markdown("---")
                    st.markdown("##### ⚡ Batch Publish All Posts")
                    pending_posts = [t for t in controller.post_tasks if not str(t.status or "").lower().startswith("completed")]
                    eligible_posts_count = len(pending_posts)

                    col_b_cfg, col_b_btn = st.columns([1, 2])
                    with col_b_cfg:
                        batch_cooldown = st.number_input(
                            "Cooldown (seconds)",
                            min_value=15,
                            max_value=600,
                            value=120,
                            step=15,
                            help="Anti-ban pacing pause between consecutive posts.",
                        )
                    with col_b_btn:
                        st.write("")
                        batch_clicked = st.button(
                            f"⚡ Batch Publish All ({eligible_posts_count} pending)",
                            type="primary",
                            width='stretch',
                            disabled=(eligible_posts_count == 0),
                        )

                    if batch_clicked:
                        live_screen_expander = st.expander("🖥️ Live Browser Screen (Real-Time Monitor)", expanded=True)
                        live_viewport = live_screen_expander.empty()
                        status_box = st.status(f"Publishing {eligible_posts_count} posts to Reddit...", expanded=True)
                        progress_bar = st.progress(0.0)

                        def batch_cb(evt):
                            ev_type = evt.get("type")
                            cur = evt.get("current", 0)
                            tot = evt.get("total", 1)
                            sc_path = evt.get("screenshot_path") or (evt.get("details", {}) or {}).get("screenshot_path")
                            if sc_path and os.path.exists(sc_path):
                                try:
                                    with open(sc_path, "rb") as f:
                                        img_bytes = f.read()
                                    live_viewport.image(img_bytes, caption=f"🖥️ Live Screen: Post {cur}/{tot}", use_container_width=True)
                                except Exception:
                                    pass
                            if ev_type == "item_start":
                                progress_bar.progress((cur - 1) / max(tot, 1))
                                status_box.write(f"🔄 **Post {cur}/{tot}:** `r/{evt.get('task').subreddit}` - *{evt.get('task').title[:40]}...*")
                            elif ev_type == "item_finish":
                                progress_bar.progress(cur / max(tot, 1))
                                res = evt.get("item_result", {})
                                if res.get("success"):
                                    status_box.write(f"✅ **Post {cur}/{tot} Published!** Live URL: {res.get('published_url')}")
                                else:
                                    status_box.write(f"⚠️ **Post {cur}/{tot} Notice:** {res.get('message')}")
                            elif ev_type == "cooldown":
                                status_box.caption(f"⏳ Anti-ban safety cooldown: {evt.get('remaining_cooldown')}s remaining before next post...")

                        batch_res = controller.batch_publish_posts(
                            profile_name=active_nav_profile,
                            delay_between_posts=batch_cooldown,
                            progress_callback=batch_cb,
                        )
                        progress_bar.progress(1.0)
                        if batch_res.get("published", 0) > 0:
                            status_box.update(label=f"🎉 Batch Finished: {batch_res.get('published')} published to Reddit!", state="complete", expanded=True)
                            st.success(batch_res.get("message"))
                        else:
                            status_box.update(label=f"⚠️ Batch Finished: {batch_res.get('message')}", state="error", expanded=True)
                            st.warning(batch_res.get("message"))

                        if batch_res.get("results"):
                            df_summary = pd.DataFrame([
                                {
                                    "Row": r["excel_row"],
                                    "Subreddit": f"r/{str(r['subreddit']).removeprefix('r/').removeprefix('/')}",
                                    "Title": r["title"][:45] + "...",
                                    "Status": "Published ✅" if r["success"] else "Failed ❌",
                                    "URL": r["published_url"] or "-",
                                    "Notice / Reason": r.get("message", "-"),
                                }
                                for r in batch_res["results"]
                            ])
                            st.dataframe(df_summary, width='stretch', hide_index=True)

                # Visual Inspection Tools (Clean collapsed expander)
                with st.expander("🔍 Browser Screen Diagnostics (Optional)", expanded=False):
                    st.caption("💡 **Why:** Troubleshoots what Chrome is displaying. | **Use:** Click 'Capture Screenshot' to view Chrome's current screen inside this dashboard.")
                    col_insp1, col_insp2 = st.columns(2)
                    with col_insp1:
                        if st.button("📸 Capture Screenshot", width='stretch'):
                            with st.spinner("Capturing live browser view..."):
                                success, msg, s_path = controller.capture_openclaw_screenshot(active_nav_profile, task=selected_task)
                                if success and s_path:
                                    st.session_state.review_screenshot = s_path
                                    st.success("Screenshot captured!")
                                else:
                                    st.error(msg)
                    with col_insp2:
                        if st.button("📄 DOM Snapshot", width='stretch'):
                            with st.spinner("Retrieving accessible DOM tree..."):
                                success, msg, snap_content = controller.get_openclaw_snapshot(active_nav_profile, task=selected_task)
                                if success and snap_content:
                                    st.session_state.review_snapshot = sanitize_dom_snapshot(snap_content)
                                    st.success("Snapshot retrieved!")
                                else:
                                    st.error(msg)

                if st.session_state.get("review_screenshot"):
                    resolved_s_path = os.path.expanduser(st.session_state.review_screenshot)
                    if os.path.exists(resolved_s_path):
                        st.image(resolved_s_path, caption=f"Live Browser View ({os.path.basename(resolved_s_path)})", width='stretch')

                if st.session_state.get("review_snapshot"):
                    with st.expander("📄 Accessible DOM Preview", expanded=False):
                        st.code(st.session_state.review_snapshot[:2500] + ("\n... [truncated]" if len(st.session_state.review_snapshot) > 2500 else ""))

                # Manual Status Override Fallback (Clean expander)
                with st.expander("⚙️ Manual Status Override (Administrative Fallback)", expanded=False):
                    st.caption("Use only if manual exception handling is required to adjust an Excel row status.")
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



# ====================================================================
# TAB 4: ACCOUNT MANAGER (20–25 ACCOUNTS)
# ====================================================================
with tab_accounts:
    st.subheader("👥 Multi-Account Profile Manager")
    st.caption("💡 **Why:** Manages persistent Chrome browser sessions for all 20–25 Reddit accounts. | **Use:** Click '🔑 Login in Chrome' once per account to authenticate; sessions survive computer restarts.")

    # Show persistent notification banner from prior actions
    if "account_manager_notification" in st.session_state:
        notif = st.session_state.pop("account_manager_notification")
        if notif.get("type") == "success":
            st.success(notif.get("message"))
        else:
            st.error(notif.get("message"))

    # Ensure fresh load of accounts
    controller.account_manager.load()
    accounts_list = controller.account_manager.list_accounts()

    # Metrics
    logged_in_count = sum(1 for a in accounts_list if acc_sessions.get(a.account_id, {}).get("has_cookies"))

    am_c1, am_c2, am_c3 = st.columns(3)
    am_c1.metric("👥 Total Accounts Configured", len(accounts_list))
    am_c2.metric("🟢 Active Sessions / Cookies", logged_in_count)
    am_c3.metric("📂 Profile Directory Root", "chrome_profiles/")

    st.markdown("---")

    # Add Account Form
    is_add_expanded = st.session_state.pop("add_account_expanded", False)
    with st.expander("➕ Add New Account Profile", expanded=is_add_expanded):
        st.caption("💡 **Why:** Registers a new Reddit account. | **Use:** Enter email or username to create an isolated persistent Chrome profile.")
        with st.form("form_add_account"):
            new_acc_id = st.text_input("Account ID / Email:", placeholder="e.g. schoolwaale26@gmail.com")
            col_a1, col_a2 = st.columns(2)
            with col_a1:
                new_prof_name = st.text_input("Chrome Profile Name (optional):", placeholder="e.g. Profile schoolwaale26")
            with col_a2:
                new_dir = st.text_input("User Data Directory (optional):", placeholder="e.g. chrome_profiles/schoolwaale26")

            submitted = st.form_submit_button("💾 Save Account Profile")
            if submitted:
                if not new_acc_id or not new_acc_id.strip():
                    st.error("Account ID is required.")
                else:
                    clean_id = new_acc_id.strip()
                    clean_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', clean_id.lower()).strip('_')
                    default_p_name = f"Profile {clean_slug}"
                    default_d_path = f"chrome_profiles/{clean_slug}"

                    p_name = new_prof_name.strip() if new_prof_name and new_prof_name.strip() else default_p_name
                    d_path = new_dir.strip() if new_dir and new_dir.strip() else default_d_path

                    try:
                        controller.account_manager.add_or_update(clean_id, p_name, d_path, enabled=True)
                        st.session_state["account_manager_notification"] = {
                            "type": "success",
                            "message": f"🎉 Account '{clean_id}' saved successfully with profile directory `{d_path}`! You can log in immediately using the card below."
                        }
                        st.session_state["last_added_account_id"] = clean_id
                        if "multiselect_participating" in st.session_state:
                            if clean_id not in st.session_state["multiselect_participating"]:
                                st.session_state["multiselect_participating"].append(clean_id)
                        st.session_state["add_account_expanded"] = False
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to add account: {e}")

    # Spotlight Card for Recently Added / Active Account
    recent_acc_id = st.session_state.get("last_added_account_id")
    if recent_acc_id:
        recent_acc = next((a for a in accounts_list if a.account_id.lower() == recent_acc_id.lower()), None)
        if recent_acc:
            r_status = controller.account_manager.check_session_status(recent_acc.account_id)
            r_has_cookies = r_status.get("has_cookies", False)
            r_label = "🟢 Logged In / Active" if r_has_cookies else ("🟡 Profile Created" if r_status.get("dir_exists") else "⚪ No Session")

            with st.container():
                st.markdown(f"#### ⭐ Recently Added / Active Account: `{recent_acc.account_id}`")
                st.caption("Authenticate this account once in visible Chrome so its login cookies are permanently saved.")
                c_r1, c_r2, c_r3 = st.columns([3, 2, 2])
                with c_r1:
                    st.write(f"**{recent_acc.account_id}**")
                    st.caption(f"Profile: `{recent_acc.chrome_profile_name}` | Path: `{recent_acc.user_data_directory}`")
                with c_r2:
                    st.write(f"Status: **{r_label}**")
                with c_r3:
                    if st.button("🔑 Launch Chrome & Log In Now", key="btn_spotlight_login", type="primary", use_container_width=True):
                        with st.spinner(f"Launching Chrome for {recent_acc.account_id}..."):
                            ok, msg = controller.launch_visible_browser_for_login(profile_name=recent_acc.account_id, url="https://www.reddit.com/login")
                            if ok:
                                st.success(msg)
                                st.rerun()
                            else:
                                st.error(msg)
            st.markdown("---")

    st.markdown("#### Configured Accounts & Persistent Sessions")
    st.caption("💡 **Why:** Displays all registered accounts. | **Use:** Click '🔑 Login in Chrome' to launch the browser and sign in once.")

    # Search & Filter Controls
    col_acc_s, col_acc_f = st.columns([2, 1])
    with col_acc_s:
        acc_search = st.text_input("🔍 Search Accounts:", placeholder="Filter by email or profile name...")
    with col_acc_f:
        acc_filter = st.selectbox("Filter Status:", ["All Accounts", "🟢 Logged In / Active", "🟡 Profile Created (Pending Login)", "⚪ No Session"])

    filtered_accounts = accounts_list
    if acc_search and acc_search.strip():
        q = acc_search.strip().lower()
        filtered_accounts = [
            a for a in filtered_accounts
            if q in a.account_id.lower() or q in a.chrome_profile_name.lower() or q in a.user_data_directory.lower()
        ]

    if acc_filter != "All Accounts":
        if "Logged In" in acc_filter:
            filtered_accounts = [a for a in filtered_accounts if acc_sessions.get(a.account_id, {}).get("has_cookies")]
        elif "Profile Created" in acc_filter:
            filtered_accounts = [a for a in filtered_accounts if not acc_sessions.get(a.account_id, {}).get("has_cookies") and acc_sessions.get(a.account_id, {}).get("dir_exists")]
        elif "No Session" in acc_filter:
            filtered_accounts = [a for a in filtered_accounts if not acc_sessions.get(a.account_id, {}).get("dir_exists")]

    # Sort so that if recent_acc_id is present, it appears first
    if recent_acc_id:
        filtered_accounts = sorted(
            filtered_accounts,
            key=lambda a: (0 if a.account_id.lower() == recent_acc_id.lower() else 1)
        )

    st.caption(f"Showing **{len(filtered_accounts)}** of **{len(accounts_list)}** configured accounts.")

    if not filtered_accounts:
        st.info("No matching accounts found.")

    for acc in filtered_accounts:
        acc_status = acc_sessions.get(acc.account_id) or controller.account_manager.check_session_status(acc.account_id)
        has_cookies = acc_status.get("has_cookies", False)
        status_label = "🟢 Logged In / Active" if has_cookies else ("🟡 Profile Created" if acc_status.get("dir_exists") else "⚪ No Session")

        with st.container():
            col_ac1, col_ac2, col_ac3, col_ac4 = st.columns([3, 2, 2, 1])
            with col_ac1:
                is_recent = recent_acc_id and (acc.account_id.lower() == recent_acc_id.lower())
                prefix = "⭐ " if is_recent else ""
                st.write(f"**{prefix}{acc.account_id}**")
                st.caption(f"Profile: `{acc.chrome_profile_name}`")
            with col_ac2:
                st.write(f"Status: **{status_label}**")
                st.caption(f"Directory: `{acc.user_data_directory}`")
            with col_ac3:
                if st.button("🔑 Login in Chrome", key=f"btn_login_{acc.account_id}", help="Opens visible Chrome for this account so you can log in once to Reddit. Cookies and sessions will be saved permanently."):
                    with st.spinner(f"Launching Chrome for {acc.account_id}..."):
                        ok, msg = controller.launch_visible_browser_for_login(profile_name=acc.account_id, url="https://www.reddit.com/login")
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)
            with col_ac4:
                if st.button("🗑️", key=f"btn_del_{acc.account_id}", help="Remove account"):
                    controller.account_manager.remove_account(acc.account_id)
                    st.toast(f"Removed {acc.account_id}")
                    st.rerun()
        st.divider()

# ====================================================================
# TAB 3: REAL-TIME TASK QUEUE
# ====================================================================
with tab_queue:
    st.subheader("📋 Real-Time Task Queue")
    st.caption("💡 **Why:** Real-time visibility into all workbook rows, assigned accounts, and live task execution progress. | **Use:** Filter by status or workflow type to track tasks across application restarts.")

    col_q1, col_q2 = st.columns([3, 1])
    with col_q1:
        queue_filter = st.radio(
            "Filter Task Queue:",
            ["All", "Eligible / Pending", "Draft", "Ready for Review", "Complete", "Failed", "Comments Only", "Posts Only"],
            horizontal=True,
        )
    with col_q2:
        if st.button("🔄 Refresh Task Queue", width='stretch'):
            controller.initialize(reload_accounts=False)
            st.rerun()

    all_tasks = controller.comment_tasks + controller.post_tasks
    if queue_filter == "Eligible / Pending":
        filtered = [t for t in all_tasks if not str(t.status or "").lower().startswith("complete")]
    elif queue_filter == "Complete":
        filtered = [t for t in all_tasks if str(t.status or "").lower().startswith("complete")]
    elif queue_filter == "Draft":
        filtered = [t for t in all_tasks if "draft" in str(t.status or "").lower()]
    elif queue_filter == "Ready for Review":
        filtered = [t for t in all_tasks if "ready" in str(t.status or "").lower()]
    elif queue_filter == "Failed":
        filtered = [t for t in all_tasks if "fail" in str(t.status or "").lower() or "error" in str(t.status or "").lower()]
    elif queue_filter == "Comments Only":
        filtered = controller.comment_tasks
    elif queue_filter == "Posts Only":
        filtered = controller.post_tasks
    else:
        filtered = all_tasks

    # Metrics row
    q_drafts = sum(1 for t in all_tasks if "draft" in str(t.status or "").lower())
    q_ready = sum(1 for t in all_tasks if "ready" in str(t.status or "").lower())
    q_complete = sum(1 for t in all_tasks if str(t.status or "").lower().startswith("complete"))
    q_failed = sum(1 for t in all_tasks if "fail" in str(t.status or "").lower() or "error" in str(t.status or "").lower())

    mq1, mq2, mq3, mq4, mq5 = st.columns(5)
    mq1.metric("📂 Total", len(all_tasks))
    mq2.metric("📝 Drafts", q_drafts)
    mq3.metric("📌 Ready", q_ready)
    mq4.metric("✅ Complete", q_complete)
    mq5.metric("🔴 Failed", q_failed)

    q_data = []
    for t in filtered:
        st_val = str(t.status or "Pending")
        q_data.append({
            "Task ID": t.task_id,
            "Account": t.account_id or "Account-Independent",
            "Type": t.task_type.upper(),
            "Sheet": t.worksheet,
            "Row": t.excel_row,
            "Target / Topic": getattr(t, "subreddit", None) or getattr(t, "thread_title", "")[:45] + "...",
            "Status": st_val,
            "Eligible": "✅ Yes" if t.is_eligible else "❌ No",
            "Timestamp / Note": getattr(t, "timestamp", "") or getattr(t, "skip_reason", "") or "-",
        })

    st.dataframe(pd.DataFrame(q_data), width='stretch', hide_index=True)


# ====================================================================
# TAB 4: OPENCLAW CONSOLE
# ====================================================================
with tab_openclaw:
    st.subheader("🦞 OpenClaw Infrastructure & Multi-Account Architecture")
    st.caption("💡 **Why:** Low-level Chrome debugging tools and connection status. | **Use:** Inspect OpenClaw daemon status or test isolated URLs.")

    h_col1, h_col2 = st.columns(2)
    with h_col1:
        st.markdown("#### Gateway Status")
        if is_healthy:
            st.success("🟢 **Gateway Online & Healthy** (Bound to `127.0.0.1:18789`)")
            h_data = oc_info.get("health", {})
            if isinstance(h_data, dict):
                st.write(f"- **Engine Version:** `{version_label}`")
                st.write(f"- **Event Loop Health:** `{h_data.get('event_loop', 'ok')}`")
                st.write(f"- **Check Duration:** `{h_data.get('check_duration', 'N/A')}`")
                st.write(f"- **Agents:** `{h_data.get('agents', 'main (default)')}`")
        else:
            st.error("🔴 **Gateway Offline or Unreachable**")
            if oc_info.get("health_error"):
                st.code(oc_info.get("health_error"))

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

    with h_col2:
        st.markdown("#### 👥 Multi-Account Independence")
        st.info(
            "**Account-Independent Architecture:**\n\n"
            "1. **Zero Code Changes:** Whichever Reddit account is logged into Google Chrome is automatically utilized.\n"
            "2. **Switching Accounts:** To switch accounts (e.g. from `shoro-test` to `Shoro-AI-02` or any client profile), "
            "simply click **'Launch Desktop Chrome'**, log into the new account once, and all subsequent posts and comments "
            "will publish seamlessly under that profile.\n"
            "3. **Persistent Cookie Storage:** Cookies and authentication tokens are persisted securely in "
            "`~/.openclaw/browser/shoro-test/user-data` and survive restarts without needing to re-login."
        )

        session_status = controller.get_profile_session_status("shoro-test")
        if session_status.get("exists"):
            st.markdown("#### 💾 Session Data Directory")
            st.success(f"📂 `{session_status.get('data_dir')}`")
            st.caption("Cookies and session state are persisted securely across browser sessions.")
