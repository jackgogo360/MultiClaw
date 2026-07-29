import json
import re
import subprocess
from pathlib import Path


def _find_typescript_module() -> Path:
    for parent in [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        candidate = parent / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js"
        if candidate.exists():
            return candidate
    raise AssertionError("typescript compiler API not found")


def test_welcome_copy_is_rendered_in_thread_empty_state():
    source_path = Path("frontend/src/components/assistant-ui/thread.tsx").resolve()
    typescript_path = _find_typescript_module().resolve()
    node_script = f"""
import fs from 'node:fs';
import ts from {typescript_path.as_uri()!r};

const source = fs.readFileSync({str(source_path)!r}, 'utf8');
const file = ts.createSourceFile(
  {source_path.name!r},
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

function isThreadPrimitiveEmpty(node) {{
  return ts.isJsxElement(node)
    && ts.isPropertyAccessExpression(node.openingElement.tagName)
    && ts.isIdentifier(node.openingElement.tagName.expression)
    && node.openingElement.tagName.expression.text === 'ThreadPrimitive'
    && node.openingElement.tagName.name.text === 'Empty';
}}

let payload = null;
function visit(node) {{
  if (payload) return;
  if (isThreadPrimitiveEmpty(node)) {{
    payload = {{
      childCount: node.children.filter((child) => !(ts.isJsxText(child) && child.getText(file).trim() === '')).length,
      renderedSource: node.children.map((child) => child.getText(file)).join(' '),
    }};
    return;
  }}
  ts.forEachChild(node, visit);
}}

visit(file);
process.stdout.write(JSON.stringify(payload));
"""

    result = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    rendered_text = re.sub(
        r"\{/\*.*?\*/\}",
        " ",
        payload["renderedSource"],
        flags=re.DOTALL,
    )
    rendered_text = re.sub(r"<[^>]+>", " ", rendered_text)
    rendered_text = " ".join(rendered_text.split())

    assert payload is not None
    assert payload["childCount"] > 0
    assert "Start a conversation" in rendered_text
    assert "Type a message below to begin" in rendered_text
