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


def _run_node(script: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_settings_approval_and_run_scope_contracts_are_present():
    app_path = Path("frontend/src/App.tsx").resolve()
    auth_store_path = Path("frontend/src/lib/auth-context-store.ts").resolve()
    auth_context_path = Path("frontend/src/lib/auth-context.tsx").resolve()
    chat_store_path = Path("frontend/src/lib/chat-store.ts").resolve()
    approval_path = Path("frontend/src/components/approval/ApprovalToolUI.tsx").resolve()
    settings_panel_path = Path("frontend/src/components/settings/SettingsPanel.tsx").resolve()
    secret_settings_path = Path("frontend/src/components/settings/SecretSettings.tsx").resolve()
    deletion_settings_path = Path("frontend/src/components/settings/DeletionSettings.tsx").resolve()
    session_provider_path = Path("frontend/src/components/session/SessionProvider.tsx").resolve()
    app_layout_path = Path("frontend/src/components/layout/AppLayout.tsx").resolve()
    index_css_path = Path("frontend/src/index.css").resolve()
    tsconfig_path = Path("frontend/tsconfig.json").resolve()
    typescript_path = _find_typescript_module().resolve()
    node_script = f"""
import fs from 'node:fs';
import ts from {typescript_path.as_uri()!r};

const files = {{
  app: {str(app_path)!r},
  authStore: {str(auth_store_path)!r},
  authContext: {str(auth_context_path)!r},
  chatStore: {str(chat_store_path)!r},
  approval: {str(approval_path)!r},
  sessionProvider: {str(session_provider_path)!r},
  appLayout: {str(app_layout_path)!r},
  settingsPanel: {str(settings_panel_path)!r},
  secretSettings: {str(secret_settings_path)!r},
  deletionSettings: {str(deletion_settings_path)!r},
  indexCss: {str(index_css_path)!r},
  tsconfig: {str(tsconfig_path)!r},
}};

const result = {{}};
for (const [key, path] of Object.entries(files)) {{
  result[`${{key}}Exists`] = fs.existsSync(path);
  result[`${{key}}Source`] = result[`${{key}}Exists`] ? fs.readFileSync(path, 'utf8') : '';
}}

const appFile = ts.createSourceFile('App.tsx', result.appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const authStoreFile = ts.createSourceFile('auth-context-store.ts', result.authStoreSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const authContextFile = ts.createSourceFile('auth-context.tsx', result.authContextSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const chatStoreFile = ts.createSourceFile('chat-store.ts', result.chatStoreSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const approvalFile = ts.createSourceFile('ApprovalToolUI.tsx', result.approvalSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

let appCapturesRunControl = false;
let appGuardsScopedEvents = false;
let appClearsRunScope = false;
let appHasWorkspaceSelector = false;

function visitApp(node) {{
  const text = node.getText(appFile);
  if (text.includes('data-run') && text.includes('run_id') && text.includes('session_id')) {{
    appCapturesRunControl = true;
  }}
  if (text.includes('activeRun') && text.includes('part.type') && text.includes('return')) {{
    appGuardsScopedEvents = true;
  }}
  if (text.includes('clearActiveRun') || text.includes('resetActiveRun') || text.includes('activeRun: null')) {{
    appClearsRunScope = true;
  }}
  ts.forEachChild(node, visitApp);
}}
visitApp(appFile);
appHasWorkspaceSelector =
  /workspace/i.test(result.appSource)
  || /workspace/i.test(result.appLayoutSource)
  || /tenant/i.test(result.appSource)
  || /tenant/i.test(result.appLayoutSource);

let authModelsPendingPurge = false;
let authAvoidsSecretPlaintext = true;
ts.forEachChild(authStoreFile, function visit(node) {{
  if (ts.isInterfaceDeclaration(node) && node.name.text === 'AuthState') {{
    const body = node.getText(authStoreFile);
    authModelsPendingPurge = body.includes('pending_purge');
    authAvoidsSecretPlaintext = !body.includes('secretPlaintext');
  }}
  ts.forEachChild(node, visit);
}});

const authContextSource = result.authContextSource;
const sessionProviderSource = result.sessionProviderSource;
const chatStoreSource = result.chatStoreSource;
const approvalSource = result.approvalSource;
const secretSettingsSource = result.secretSettingsSource;
const deletionSettingsSource = result.deletionSettingsSource;
const indexCssSource = result.indexCssSource;
const tsconfig = JSON.parse(result.tsconfigSource);

let chatStoreTracksRunScope = false;
ts.forEachChild(chatStoreFile, function visit(node) {{
  if (ts.isVariableDeclaration(node) && /activeRun/i.test(node.name.getText(chatStoreFile))) {{
    chatStoreTracksRunScope = true;
  }}
  ts.forEachChild(node, visit);
}});

const approvalUsesPersistedRecord =
  approvalSource.includes('version')
  && approvalSource.includes('getApproval')
  && approvalSource.includes('409')
  && approvalSource.includes('410')
  && approvalSource.includes('handleReloadApproval');
const sessionProviderResetsDerivedState =
  /reset/i.test(sessionProviderSource)
  && /auth/i.test(sessionProviderSource)
  && /chatStore/i.test(sessionProviderSource);
const authContextRefreshesOnAuthChanges =
  authContextSource.includes('authApi.me')
  && authContextSource.includes('logout')
  && authContextSource.includes('login')
  && authContextSource.includes('beginRecentAuthRenewal');

const settingsPanelWiredFromFooter =
  result.appLayoutSource.includes('SettingsPanel')
  && result.appLayoutSource.includes('Settings');
const secretSettingsUsesTransientInput =
  secretSettingsSource.includes('finally')
  && /set[A-Za-z0-9_]*Value\\(\"\"\\)/.test(secretSettingsSource)
  && !secretSettingsSource.includes('localStorage.setItem')
  && secretSettingsSource.includes('beginRecentAuthRenewal')
  && secretSettingsSource.includes('requiresRecentAuth')
  && secretSettingsSource.includes('Re-authenticate');
const deletionSettingsEnforcesRecoveryAndPendingPurge =
  deletionSettingsSource.includes('pending_purge')
  && deletionSettingsSource.includes('purge_after')
  && deletionSettingsSource.includes('recover')
  && deletionSettingsSource.includes('sendDeletionRecoveryCode')
  && deletionSettingsSource.includes('setTimeout')
  && deletionSettingsSource.includes('clearTimeout')
  && !deletionSettingsSource.includes('renderedAt');
const sharedSettingsCssPresent =
  indexCssSource.includes('.settings-panel')
  && indexCssSource.includes('.approval-refresh-button')
  && indexCssSource.includes('.deletion-warning')
  && indexCssSource.includes('.secret-danger-button')
  && indexCssSource.includes('.pending-purge-banner');
const componentsUseSharedCss =
  result.settingsPanelSource.includes('settings-panel')
  && secretSettingsSource.includes('secret-danger-button')
  && deletionSettingsSource.includes('deletion-warning')
  && approvalSource.includes('approval-refresh-button');
const tsBuildInfoMovedOutOfRepoRoot =
  typeof tsconfig.compilerOptions?.tsBuildInfoFile === 'string'
  && tsconfig.compilerOptions.tsBuildInfoFile.includes('node_modules');

process.stdout.write(JSON.stringify({{
  appCapturesRunControl,
  appGuardsScopedEvents,
  appClearsRunScope,
  appHasWorkspaceSelector,
  authModelsPendingPurge,
  authAvoidsSecretPlaintext,
  chatStoreTracksRunScope,
  approvalUsesPersistedRecord,
  sessionProviderResetsDerivedState,
  authContextRefreshesOnAuthChanges,
  settingsPanelExists: result.settingsPanelExists,
  secretSettingsExists: result.secretSettingsExists,
  deletionSettingsExists: result.deletionSettingsExists,
  settingsPanelWiredFromFooter,
  secretSettingsUsesTransientInput,
  deletionSettingsEnforcesRecoveryAndPendingPurge,
  sharedSettingsCssPresent,
  componentsUseSharedCss,
  tsBuildInfoMovedOutOfRepoRoot,
}}));
"""

    payload = _run_node(node_script)
    assert payload["appCapturesRunControl"] is True
    assert payload["appGuardsScopedEvents"] is True
    assert payload["appClearsRunScope"] is True
    assert payload["appHasWorkspaceSelector"] is False
    assert payload["authModelsPendingPurge"] is True
    assert payload["authAvoidsSecretPlaintext"] is True
    assert payload["chatStoreTracksRunScope"] is True
    assert payload["approvalUsesPersistedRecord"] is True
    assert payload["sessionProviderResetsDerivedState"] is True
    assert payload["authContextRefreshesOnAuthChanges"] is True
    assert payload["settingsPanelExists"] is True
    assert payload["secretSettingsExists"] is True
    assert payload["deletionSettingsExists"] is True
    assert payload["settingsPanelWiredFromFooter"] is True
    assert payload["secretSettingsUsesTransientInput"] is True
    assert payload["deletionSettingsEnforcesRecoveryAndPendingPurge"] is True
    assert payload["sharedSettingsCssPresent"] is True
    assert payload["componentsUseSharedCss"] is True
    assert payload["tsBuildInfoMovedOutOfRepoRoot"] is True
