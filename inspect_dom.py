import subprocess
import os
import shutil

node = shutil.which('node')
mjs = os.path.expandvars(r'%AppData%\npm\node_modules\openclaw\openclaw.mjs')

js = """() => {
    const q1 = document.querySelector('div[slot="rte"]');
    const q2 = document.querySelector('div[contenteditable="true"]');
    const q3 = document.querySelector('shreddit-composer');
    const q4 = q3 ? q3.shadowRoot : null;
    const q5 = q4 ? q4.querySelector('div[contenteditable="true"]') : null;
    return JSON.stringify({
        q1: !!q1,
        q2: !!q2,
        q3: !!q3,
        q4: !!q4,
        q5: !!q5
    });
}"""

r = subprocess.run([node, mjs, 'browser', 'evaluate', '--fn', js, '--browser-profile', 'shoro-test'], capture_output=True, text=True)
print("OUT:", r.stdout)
