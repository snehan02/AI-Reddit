from modules.openclaw_client import OpenClawClient

client = OpenClawClient()

test_clear = """() => {
    let editor = document.querySelector('div[slot="rte"][contenteditable="true"]');
    if (!editor) return "No editor";
    
    editor.focus();
    const sel = window.getSelection();
    const rng = document.createRange();
    rng.selectNodeContents(editor);
    sel.removeAllRanges();
    sel.addRange(rng);
    
    const delOk = document.execCommand('delete', false, null);
    
    return {
        delOk: delOk,
        cleared: (editor.innerText || '').trim() === '',
        innerText: editor.innerText
    };
}"""

resp = client.evaluate_script("shoro-test", test_clear)
print("RESP:", resp.data)
