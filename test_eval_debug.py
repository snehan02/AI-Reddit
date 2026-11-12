from modules.workflow_controller import WorkflowController
from config import AppSettings

ctrl = WorkflowController(AppSettings.load())
ctrl.initialize()
task = next((t for t in ctrl.comment_tasks if t.excel_row == 380), None)

# Simulate step 1 to 8:
nav_resp = ctrl.openclaw_client.open_url(task.destination_url, "shoro-test")
print("nav_resp.data:", nav_resp.data)
tab_id = nav_resp.data.get("target_id")
print("tab_id:", tab_id)

import json
reply_json = json.dumps(task.reply_text)

# Let's inspect what workflow_controller's js_script returns:
eval_resp = ctrl.openclaw_client.evaluate_script("shoro-test", ctrl._get_prep_script(task.reply_text) if hasattr(ctrl, '_get_prep_script') else f"""() => {{
    const expected = {reply_json};
    let editor = document.querySelector('div[slot="rte"][contenteditable="true"]') ||
                 document.querySelector('shreddit-composer div[contenteditable="true"]') ||
                 document.querySelector('div[contenteditable="true"]:not([class*="recaptcha"])');

    if (!editor) {{
        let trigger = document.querySelector('faceplate-textarea-input[data-testid="trigger-button"], [placeholder*="conversation" i], [aria-label*="Join the conversation" i], faceplate-textarea-input');
        if (trigger) {{
            trigger.scrollIntoView({{ behavior: 'instant', block: 'center' }});
            trigger.click();
            if (typeof trigger.focus === 'function') trigger.focus();
        }}
        editor = document.querySelector('div[slot="rte"][contenteditable="true"]') ||
                 document.querySelector('shreddit-composer div[contenteditable="true"]') ||
                 document.querySelector('div[contenteditable="true"]:not([class*="recaptcha"])') ||
                 document.querySelector('textarea[name="content"], textarea[placeholder*="conversation" i]');
    }}

    if (!editor) {{
        return JSON.stringify({{ success: false, error: 'Comment editor could not be identified confidently.' }});
    }}

    editor.scrollIntoView({{ behavior: 'instant', block: 'center' }});
    editor.focus();

    let pasted = false;
    try {{
        const sel = window.getSelection();
        const rng = document.createRange();
        rng.selectNodeContents(editor);
        sel.removeAllRanges();
        sel.addRange(rng);
        pasted = document.execCommand('insertText', false, expected);
    }} catch (e) {{
        pasted = false;
    }}

    const currentVal = (editor.innerText || editor.textContent || editor.value || '').trim();
    if (!pasted || !currentVal.includes(expected.substring(0, Math.min(20, expected.length)))) {{
        editor.innerHTML = '<p class="first:mt-0 last:mb-0"><span data-lexical-text="true">' + expected + '</span></p>';
        editor.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, data: expected }}));
        editor.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }}

    const readBack = ((editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT')
        ? editor.value
        : (editor.innerText || editor.textContent || '')).trim();

    return JSON.stringify({{
        success: true,
        verifiedText: readBack,
        submitted: false,
    }});
}}""", target_id=tab_id)

print("eval_resp.success:", eval_resp.success)
print("eval_resp.data:", eval_resp.data)
print("eval_resp.error:", eval_resp.error)
