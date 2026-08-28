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


def _run_node(script: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_api_security_contracts_require_csrf_module_memory_and_metadata_only_secrets():
    api_path = Path("frontend/src/lib/api.ts").resolve()
    security_path = Path("frontend/src/lib/security.ts").resolve()
    typescript_path = _find_typescript_module().resolve()
    node_script = f"""
import fs from 'node:fs';
import ts from {typescript_path.as_uri()!r};

const apiPath = {str(api_path)!r};
const securityPath = {str(security_path)!r};
const apiSource = fs.readFileSync(apiPath, 'utf8');
const apiFile = ts.createSourceFile('api.ts', apiSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);

const securityExists = fs.existsSync(securityPath);
const securitySource = securityExists ? fs.readFileSync(securityPath, 'utf8') : '';
const securityFile = securityExists
  ? ts.createSourceFile('security.ts', securitySource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  : null;

let hasEnsureCsrfImport = false;
    let hasCsrfHeader = false;
    let csrfHeaderUsesEnsureToken = false;
    let approveUsesDecisionRoute = false;
    let approveBodyHasVersion = false;
    let secretMetadataOnly = false;
    let secretSensitiveShapeLeaked = false;
    let deletionRequestStatusIsScheduled = false;
    let deletionStatusIsPendingPurge = false;

function literalValue(node) {{
  return ts.isStringLiteralLike(node) || ts.isNoSubstitutionTemplateLiteral(node)
    ? node.text
    : null;
}}

function visitApi(node) {{
  if (ts.isImportDeclaration(node) && node.moduleSpecifier.getText(apiFile).includes('./security')) {{
    const clause = node.importClause;
    if (clause && clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {{
      hasEnsureCsrfImport = clause.namedBindings.elements.some((element) => element.name.text === 'ensureCsrfToken');
    }}
  }}

      if (
        ts.isCallExpression(node)
        && ts.isPropertyAccessExpression(node.expression)
        && node.expression.name.text === 'set'
        && node.arguments.length >= 2
        && literalValue(node.arguments[0]) === 'X-CSRF-Token'
      ) {{
        hasCsrfHeader = true;
        csrfHeaderUsesEnsureToken = node.arguments[1].getText(apiFile).includes('ensureCsrfToken');
      }}

  if (
    ts.isPropertyAssignment(node)
    && ts.isIdentifier(node.name)
    && node.name.text === 'submit'
    && node.initializer.getText(apiFile).includes('/approvals/${{approvalId}}/decision')
  ) {{
    approveUsesDecisionRoute = true;
    approveBodyHasVersion = node.initializer.getText(apiFile).includes('version');
  }}

  if (
    ts.isInterfaceDeclaration(node)
    && node.name.text.toLowerCase().includes('secret')
    && node.members.length > 0
  ) {{
    const names = node.members
      .filter(ts.isPropertySignature)
      .map((member) => member.name.getText(apiFile).replace(/['"]/g, ''));
    if (['providerKind', 'providerName', 'secretName', 'maskedValue', 'updatedAt'].every((name) => names.includes(name))) {{
      secretMetadataOnly = true;
      secretSensitiveShapeLeaked = names.some((name) =>
        ['value', 'plaintext', 'ciphertext', 'keyVersion', 'nonce'].includes(name)
      );
    }}
  }}

  if (ts.isInterfaceDeclaration(node) && node.name.text === 'AccountDeletionRequest') {{
    const statusMember = node.members.find((member) =>
      ts.isPropertySignature(member)
      && member.name?.getText(apiFile).replace(/['"]/g, '') === 'status'
    );
    deletionRequestStatusIsScheduled =
      !!statusMember
      && statusMember.type?.getText(apiFile) === '"scheduled"';
  }}

  if (ts.isInterfaceDeclaration(node) && node.name.text === 'AccountDeletionStatus') {{
    const statusMember = node.members.find((member) =>
      ts.isPropertySignature(member)
      && member.name?.getText(apiFile).replace(/['"]/g, '') === 'status'
    );
    deletionStatusIsPendingPurge =
      !!statusMember
      && statusMember.type?.getText(apiFile) === '"pending_purge"';
  }}

  ts.forEachChild(node, visitApi);
}}

visitApi(apiFile);

let securityExportsEnsureCsrfToken = false;
let securityUsesModuleMemory = false;
let securityTouchesWebStorage = false;

if (securityFile) {{
  function visitSecurity(node) {{
    if (
      ts.isFunctionDeclaration(node)
      && node.name?.text === 'ensureCsrfToken'
      && node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)
    ) {{
      securityExportsEnsureCsrfToken = true;
    }}
    if (ts.isVariableDeclaration(node) && node.name.getText(securityFile) === 'csrfToken') {{
      securityUsesModuleMemory = true;
    }}
    if (ts.isPropertyAccessExpression(node)) {{
      const text = node.getText(securityFile);
      if (text.startsWith('localStorage.') || text.startsWith('sessionStorage.')) {{
        securityTouchesWebStorage = true;
      }}
    }}
    ts.forEachChild(node, visitSecurity);
  }}
  visitSecurity(securityFile);
}}

process.stdout.write(JSON.stringify({{
  securityExists,
  hasEnsureCsrfImport,
  hasCsrfHeader,
  csrfHeaderUsesEnsureToken,
  approveUsesDecisionRoute,
  approveBodyHasVersion,
  secretMetadataOnly,
  secretSensitiveShapeLeaked,
  deletionRequestStatusIsScheduled,
  deletionStatusIsPendingPurge,
  securityExportsEnsureCsrfToken,
  securityUsesModuleMemory,
  securityTouchesWebStorage,
}}));
"""

    payload = _run_node(node_script)
    assert payload["securityExists"] is True
    assert payload["hasEnsureCsrfImport"] is True
    assert payload["hasCsrfHeader"] is True
    assert payload["csrfHeaderUsesEnsureToken"] is True
    assert payload["approveUsesDecisionRoute"] is True
    assert payload["approveBodyHasVersion"] is True
    assert payload["secretMetadataOnly"] is True
    assert payload["secretSensitiveShapeLeaked"] is False
    assert payload["deletionRequestStatusIsScheduled"] is True
    assert payload["deletionStatusIsPendingPurge"] is True
    assert payload["securityExportsEnsureCsrfToken"] is True
    assert payload["securityUsesModuleMemory"] is True
    assert payload["securityTouchesWebStorage"] is False
