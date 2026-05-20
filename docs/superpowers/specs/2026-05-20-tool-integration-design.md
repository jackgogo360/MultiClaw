# Tool Integration Design

## Summary

Integrate 4 new tools (Shell, CodeExec, WebFetch, WebSearch) from the agent-code reference implementations into MultiClaw. Also refactor existing `builtin.py` by splitting each tool into its own file.

## File Structure

```
src/multiclaw/tools/
├── __init__.py          # re-exports, update imports
├── base.py              # ToolBuilder, ToolInvocation, ToolExecutionResult, ToolStatus (unchanged)
├── _common.py           # shared utilities extracted from builtin.py
├── builtin.py           # → DELETED
├── read_file.py         # ReadFileToolBuilder + ReadFileInvocation
├── write_file.py        # WriteFileToolBuilder + WriteFileInvocation
├── edit_file.py         # EditFileToolBuilder + EditFileInvocation + UndoEditToolBuilder + UndoEditInvocation
├── glob.py              # GlobToolBuilder + GlobInvocation
├── list_dir.py          # ListDirToolBuilder + ListDirInvocation
├── grep.py              # GrepToolBuilder + GrepInvocation
├── find_dir.py          # FindDirToolBuilder + FindDirInvocation
├── shell.py             # [NEW] ShellToolBuilder + ShellInvocation
├── code_exec.py         # [NEW] CodeExecToolBuilder + CodeExecInvocation
├── web_fetch.py         # [NEW] WebFetchToolBuilder + WebFetchInvocation + 3 fetchers
├── web_search.py        # [NEW] WebSearchToolBuilder + WebSearchInvocation + 3 engines
├── registry.py          # unchanged
└── scheduler.py         # unchanged
```

## `_common.py` — Shared Utilities

Extracted from `builtin.py`:

- `PathPolicy` dataclass (workspace validation, deny patterns)
- `WorkspaceToolBuilder` base class
- `_success()`, `_error()` result helpers
- `_resolve_path()`, `_run_command()`, `_policy_for_invocation()`
- `_expand_include()`, `_human_size()`, `_detect_binary()`
- `_generate_diff()`, `_truncate_diff()`, `_levenshtein()`
- All constants (`MAX_READ_LINES_DEFAULT`, `MAX_GLOB_RESULTS`, etc.)

## New Tools — Adaptation Pattern

Each source tool follows a `class XxxTool` pattern with dataclass `ToolResult`. Adapt to the project's Pydantic-based pattern:

```
XxxParams(BaseModel) → XxxToolBuilder(WorkspaceToolBuilder) → XxxInvocation(ToolInvocation)
```

### ShellTool

- **Params**: `command: str`, `timeout: float | None`, `cwd: str | None`
- **Logic**: async subprocess via `asyncio.create_subprocess_shell`, with safety checks (dangerous command patterns, blocked commands), output truncation (30k chars), env sanitization (strip secrets)
- **Source**: `20260519-search-tools/src/shell_tool.py` (identical to exec-tools version)

### CodeExecTool

- **Params**: `code: str`, `timeout: float | None`
- **Logic**: `multiprocessing.Process` sandbox with restricted builtins, blocked module imports (subprocess, shutil, ctypes, signal), stdout/stderr capture, output truncation
- **Source**: `20260519-search-tools/src/code_exec_tool.py`

### WebFetchTool

- **Params**: `url: str`, `mode: str` (light/markdown/browser/auto, default auto)
- **Logic**: Auto mode selection (URL heuristics → light → markdown → browser fallback). Three internal fetcher backends:
  - `LightFetcher`: httpx + trafilatura for article text extraction
  - `MarkdownFetcher`: httpx + html2text for structure-preserving markdown
  - `BrowserFetcher`: Playwright for JS-rendered pages
- **Dependencies**: httpx, trafilatura, html2text, playwright (all optional, graceful fallback)
- **Source**: `20260520-web-fetch/src/`

### WebSearchTool

- **Params**: `query: str`, `max_results: int` (default 5), `engine: str | None`
- **Logic**: Engine fallback chain (primary → fallbacks). Three internal engine backends:
  - `DuckDuckGoSearch`: duckduckgo-search package
  - `BingSearch`: Bing API
  - `BaiduSearch`: Baidu scraping
- **Dependencies**: duckduckgo-search, httpx (optional, graceful fallback)
- **Source**: `20260520-web-search/src/`

## Out of Scope

- `base.py`, `registry.py`, `scheduler.py` unchanged
- `__init__.py` exports unchanged (only import sources updated)
- No behavioral changes to existing tools

## Existing Tool Split

Each existing tool class moves from `builtin.py` into its own file. No logic changes — pure extraction. The `edit_file.py` file also contains `UndoEditToolBuilder` + `UndoEditInvocation` since they're tightly coupled.
