from modules.openclaw_client import OpenClawClient
import json

client = OpenClawClient()
reply_text = "That tracks with how California handles out-of-state moves. The knowledge test is a normal part of a transfer."
reply_json = json.dumps(reply_text)

js_async = f"""async () => {{
    const expected = {reply_json};

    // Helper: wait ms
    const wait = ms => new Promise(r => setTimeout(r, ms));

    // 1. Check if editor is already open
    let editor = document.querySelector('div[slot="rte"][contenteditable="true"]') ||
                 document.querySelector('shreddit-composer div[contenteditable="true"]');

    // 2. If not open, click trigger and wait for editor to mount
    if (!editor) {{
        const trigger = document.querySelector('faceplate-textarea-input[data-testid="trigger-button"], faceplate-textarea-input');
        if (trigger) {{
            trigger.scrollIntoView({{ behavior: 'instant', block: 'center' }});
            trigger.click();
            // Also click inner button/input if present
            const inner = trigger.querySelector('button, textarea, [placeholder*="conversation" i]');
            if (inner) inner.click();
        }}

        // Poll for up to 4 seconds (40 * 100ms)
        for (let i = 0; i < 40; i++) {{
            await wait(100);
            editor = document.querySelector('div[slot="rte"][contenteditable="true"]') ||
                     document.querySelector('shreddit-composer div[contenteditable="true"]') ||
                     document.querySelector('div[contenteditable="true"]:not([class*="recaptcha"])');
            if (editor) break;
        }}
    }}

    if (!editor) {{
        return JSON.stringify({{ success: false, error: "Comment editor element could not be found after trigger activation." }});
    }}

    // 3. Focus editor
    editor.scrollIntoView({{ behavior: 'instant', block: 'center' }});
    editor.focus();
    await wait(150);

    // 4. Insert text cleanly using document.execCommand
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

    // Fallback: Lexical DOM paragraph insertion if execCommand failed
    let cur = (editor.innerText || editor.textContent || '').trim();
    if (!pasted || !cur.includes(expected.substring(0, Math.min(20, expected.length)))) {{
        editor.innerHTML = '<p class="first:mt-0 last:mb-0"><span data-lexical-text="true">' + expected + '</span></p>';
        editor.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, data: expected }}));
        editor.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }}

    await wait(200);

    // 5. Read back text for verification
    const readBack = (editor.innerText || editor.textContent || '').trim();

    return JSON.stringify({{
        success: true,
        verifiedText: readBack,
        submitted: false
    }});
}}"""

resp = client.evaluate_script("shoro-test", js_async)
print("SUCCESS:", resp.success)
print("DATA:", resp.data)
print("ERROR:", resp.error)

