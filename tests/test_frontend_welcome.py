import json
import subprocess
from pathlib import Path


def test_hide_welcome_targets_current_welcome_node():
    html_path = Path("src/multiclaw/static/index.html").resolve()
    node_script = f"""
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync({json.dumps(str(html_path))}, 'utf8');
const start = html.indexOf("const msgs = document.getElementById('messages');");
const end = html.indexOf("// ---- thinking ----");
if (start === -1 || end === -1 || end <= start) {{
  throw new Error('failed to locate welcome script snippet');
}}
const snippet = html.slice(start, end);

let currentWelcome = {{ style: {{ display: '' }} }};
const messages = {{ appendChild() {{}}, scrollTop: 0, scrollHeight: 0 }};
const input = {{
  style: {{}},
  addEventListener() {{}},
  focus() {{}},
}};
const btn = {{}};

const document = {{
  getElementById(id) {{
    if (id === 'messages') return messages;
    if (id === 'input') return input;
    if (id === 'send-btn') return btn;
    if (id === 'welcome') return currentWelcome;
    return null;
  }},
}};

    const context = {{
      document,
      console,
      globalThis: {{}},
      URLSearchParams,
      location: {{ search: '' }},
      localStorage: {{ getItem() {{ return null; }} }},
      Date,
    }};
vm.createContext(context);
vm.runInContext(snippet, context);

currentWelcome = {{ style: {{ display: '' }} }};
context.hideWelcome();
process.stdout.write(JSON.stringify({{ display: currentWelcome.style.display }}));
"""

    result = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"display": "none"}
