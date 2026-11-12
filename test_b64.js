(() => {
    const b64 = "V2h5IGRpZCBDYWxpZm9ybmlhIERNViB0ZWxsIG1lIHRvIGNhbGwgYSBudW1iZXIgZHVyaW5nIG15IGxpY2Vuc2UgdHJhbnNmZXI/CgpUaGF0IHRyYWNrcyB3aXRoIGhvdyBDYWxpZm9ybmlhIGhhbmRsZXMgb3V0LW9mLXN0YXRlIG1vdmVzLiBUaGUga25vd2xlZGdlIHRlc3QgaXMgYSBub3JtYWwgcGFydCBvZiBhIHRyYW5zZmVyLCBhbmQgdGhlIHBob25lIGNhbGwgaXMgdXN1YWxseSB0aGUgb2ZmaWNlIHJldmlld2luZyB5b3VyIGFwcGxpY2F0aW9uIGFuZCBkb2N1bWVudHMgYmVmb3JlIHRoZXkgaXNzdWUgdGhlIGNhcmQuCgpDYWxpZm9ybmlhIGxpY2Vuc2UgdHJhbnNmZXIgcnVsZXM6IGh0dHBzOi8vd3d3LnNob3JvLmFpL3VzL2xlYXJuZXJzLXBlcm1pdC9jYWxpZm9ybmlhL2RyaXZlcnMtaGFuZGJvb2s=";
    const binStr = atob(b64);
    const bytes = new Uint8Array(binStr.length);
    for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);
    const expected = new TextDecoder('utf-8').decode(bytes);

    function getAllElementsDeep(root) {
        const list = [];
        const queue = [root || document.body];
        while (queue.length > 0) {
            const cur = queue.shift();
            if (!cur) continue;
            if (cur.nodeType === 1) list.push(cur);
            if (cur.children) for (let i = 0; i < cur.children.length; i++) queue.push(cur.children[i]);
            if (cur.shadowRoot && cur.shadowRoot.children) for (let i = 0; i < cur.shadowRoot.children.length; i++) queue.push(cur.shadowRoot.children[i]);
        }
        return list;
    }

    const all = getAllElementsDeep(document.body);
    let trigger = null;
    for (const el of all) {
        if (!el || typeof el.getAttribute !== 'function') continue;
        const ph = (el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') || '').toLowerCase();
        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
        const tag = (el.tagName || '').toLowerCase();
        if (ph.includes('join the conversation') || aria.includes('join the conversation') || tag === 'faceplate-textarea-input') {
            trigger = el;
            break;
        }
    }
    if (!trigger) {
        for (const el of all) {
            if (!el) continue;
            const txt = (el.textContent || '').trim().toLowerCase();
            if (txt === 'join the conversation' || txt.startsWith('join the conversation')) {
                trigger = el;
                break;
            }
        }
    }
    if (!trigger) {
        trigger = all.find(el => el && (
            el.tagName === 'COMMENT-BODY-HEADER' ||
            el.tagName === 'SHREDDIT-COMMENT-COMPOSER' ||
            el.tagName === 'FACEPLATE-TEXTAREA-INPUT' ||
            (el.className && typeof el.className === 'string' && el.className.includes('comment-body-header'))
        ));
    }

    if (!trigger) return JSON.stringify({ success: false, error: 'Comment editor trigger not found.' });

    try {
        trigger.scrollIntoView({ behavior: 'instant', block: 'center' });
        trigger.click();
        if (typeof trigger.focus === 'function') trigger.focus();
    } catch (e) {}

    const currentAll = getAllElementsDeep(document.body);
    let editor = currentAll.find(el => {
        if (!el || typeof el.getAttribute !== 'function') return false;
        const isEdit = el.getAttribute('contenteditable') === 'true' || el.contentEditable === 'true';
        const isRecaptcha = (el.className || '').toString().includes('g-recaptcha');
        return isEdit && !isRecaptcha;
    });

    if (!editor) {
        editor = currentAll.find(el => el && !((el.className || '').toString().includes('g-recaptcha')) && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && el.type === 'text')));
    }
    if (!editor && trigger && (trigger.tagName === 'TEXTAREA' || (trigger.getAttribute && trigger.getAttribute('contenteditable') === 'true'))) {
        editor = trigger;
    }

    if (!editor) return JSON.stringify({ success: false, error: 'Comment editor could not be identified confidently.' });

    try {
        editor.scrollIntoView({ behavior: 'instant', block: 'center' });
        editor.focus();
    } catch (e) {}

    let pasted = false;
    try {
        const sel = window.getSelection();
        const rng = document.createRange();
        rng.selectNodeContents(editor);
        sel.removeAllRanges();
        sel.addRange(rng);
        pasted = document.execCommand('insertText', false, expected);
    } catch (e) {
        pasted = false;
    }

    const currentVal = editor.value || editor.innerText || editor.textContent || '';
    if (!pasted || !currentVal.includes(expected.substring(0, Math.min(20, expected.length)))) {
        if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {
            editor.value = expected;
            editor.dispatchEvent(new Event('input', { bubbles: true }));
            editor.dispatchEvent(new Event('change', { bubbles: true }));
        } else {
            const p = document.createElement('p');
            p.className = 'first:mt-0 last:mb-0';
            const span = document.createElement('span');
            span.setAttribute('data-lexical-text', 'true');
            span.textContent = expected;
            p.appendChild(span);
            editor.innerHTML = '';
            editor.appendChild(p);
            editor.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: expected }));
            editor.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }

    const readBack = (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') ? editor.value : (editor.innerText || editor.textContent || '');

    return JSON.stringify({
        success: true,
        verifiedText: readBack,
        submitted: false
    });
})();