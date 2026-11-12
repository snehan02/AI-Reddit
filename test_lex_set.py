from modules.openclaw_client import OpenClawClient
import json

client = OpenClawClient()
reply_text = "Why did California DMV tell me to call a number during my license transfer?\n\nThat tracks with how California handles out-of-state moves. The knowledge test is a normal part of a transfer, and the phone call is usually the office reviewing your application and documents before they issue the card.\n\nBut a request to call a number is not enough to tell you why your particular application was flagged. Verify the phone number through the official California DMV contact page, and ask what specific review or document is outstanding. Do not assume it was just a routine call without seeing the notice.\n\nYou can only hold one valid license at a time, so expect to surrender the old one when the California license arrives. Keep the application or receipt number until your California licence arrives; if it does not show up in the time they quoted, call back with that number ready.\n\nCalifornia license transfer rules: https://www.shoro.ai/us/learners-permit/california/drivers-handbook"

js = f"""() => {{
    const expected = {json.dumps(reply_text)};
    let editor = document.querySelector('div[slot="rte"][contenteditable="true"]');
    if (!editor || !editor.__lexicalEditor) return "No lexical editor";

    const paragraphs = expected.split('\\n\\n').map(para => ({{
        children: [{{
            detail: 0,
            format: 0,
            mode: 'normal',
            style: '',
            text: para,
            type: 'text',
            version: 1
        }}],
        direction: 'ltr',
        format: '',
        indent: 0,
        type: 'paragraph',
        version: 1
    }}));

    const stateObj = {{
        root: {{
            children: paragraphs,
            direction: 'ltr',
            format: '',
            indent: 0,
            type: 'root',
            version: 1
        }}
    }};

    const editorState = editor.__lexicalEditor.parseEditorState(JSON.stringify(stateObj));
    editor.__lexicalEditor.setEditorState(editorState);

    const verified = (editor.innerText || editor.textContent || '').trim();
    return {{
        success: true,
        verified: verified,
        exactMatch: verified === expected.trim()
    }};
}}"""

resp = client.evaluate_script("shoro-test", js)
print("RESP:", resp.data)

