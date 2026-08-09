import json
import subprocess
from pathlib import Path


def _find_typescript_module() -> Path:
    for parent in [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        candidate = parent / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js"
        if candidate.exists():
            return candidate
    raise AssertionError("typescript compiler API not found")


def test_should_log_chat_debug_for_localhost_only_by_default():
    module_path = Path("frontend/src/chat-debug.ts").resolve()
    typescript_path = _find_typescript_module().resolve()
    node_script = f"""
import fs from 'node:fs';
import ts from {typescript_path.as_uri()!r};

const source = fs.readFileSync({json.dumps(str(module_path))}, 'utf8');
const transpiled = ts.transpileModule(source, {{
  compilerOptions: {{
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020,
  }},
}});
const moduleUrl = `data:text/javascript;base64,${{Buffer.from(transpiled.outputText).toString('base64')}}`;
const {{ shouldLogChatDebug }} = await import(moduleUrl);

const results = {{
  localhost: shouldLogChatDebug({{
    hostname: 'localhost',
    search: '',
    debugFlag: null,
  }}),
  production: shouldLogChatDebug({{
    hostname: 'example.com',
    search: '',
    debugFlag: null,
  }}),
}};

process.stdout.write(JSON.stringify(results));
"""

    result = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "localhost": True,
        "production": False,
    }
