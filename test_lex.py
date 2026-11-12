from modules.openclaw_client import OpenClawClient

client = OpenClawClient()

test_lex = """() => {
    let editor = document.querySelector('div[slot="rte"][contenteditable="true"]');
    if (!editor) return "No editor";
    
    const keys = [];
    for (let k in editor) {
        if (k.toLowerCase().includes('lexical') || k.startsWith('__')) keys.push(k);
    }
    return keys;
}"""

resp = client.evaluate_script("shoro-test", test_lex)
print("RESP:", resp.data)

