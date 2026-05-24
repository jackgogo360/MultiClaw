import json
import subprocess
from pathlib import Path


def test_frontend_debug_logging_can_be_enabled_by_query_string():
    html_path = Path("src/multiclaw/static/index.html").resolve()
    node_script = f"""
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync({json.dumps(str(html_path))}, 'utf8');
const start = html.indexOf("const msgs = document.getElementById('messages');");
const end = html.indexOf("// ---- textarea auto-resize ----");
if (start === -1 || end === -1 || end <= start) {{
  throw new Error('failed to locate debug script snippet');
}}
const snippet = html.slice(start, end);

const logs = [];
const consoleMock = {{
  info: (...args) => logs.push(args),
  error: (...args) => logs.push(['error', ...args]),
}};

const messages = {{ appendChild() {{}}, scrollTop: 0, scrollHeight: 0 }};
const input = {{ addEventListener() {{}}, focus() {{}}, style: {{}} }};
const btn = {{}};
const document = {{
  getElementById(id) {{
    if (id === 'messages') return messages;
    if (id === 'input') return input;
    if (id === 'send-btn') return btn;
    return null;
  }},
}};

const context = {{
  console: consoleMock,
  document,
  location: {{ search: '?debug=1' }},
  localStorage: {{ getItem() {{ return null; }} }},
  Date,
  URLSearchParams,
}};

vm.createContext(context);
vm.runInContext(snippet, context);
context.debugLog('approval_required', {{ request_id: 'req-1' }});
process.stdout.write(JSON.stringify(logs));
"""

    result = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    logs = json.loads(result.stdout)
    assert logs, "expected debug log output when ?debug=1 is set"
