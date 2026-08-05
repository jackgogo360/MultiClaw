"""Static bootstrap source for sandboxed Python execution."""

from __future__ import annotations

import builtins
from typing import Any

SAFE_BUILTINS = (
    "abs",
    "all",
    "any",
    "ascii",
    "bin",
    "bool",
    "bytearray",
    "bytes",
    "callable",
    "chr",
    "complex",
    "dict",
    "dir",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "getattr",
    "hasattr",
    "hash",
    "hex",
    "id",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "oct",
    "ord",
    "pow",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "vars",
    "zip",
    "True",
    "False",
    "None",
    "Exception",
    "BaseException",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AttributeError",
    "ImportError",
    "RuntimeError",
    "StopIteration",
    "GeneratorExit",
    "SystemExit",
    "KeyboardInterrupt",
    "ArithmeticError",
    "ZeroDivisionError",
    "OverflowError",
    "FileNotFoundError",
    "IOError",
    "OSError",
    "PermissionError",
    "NotImplementedError",
    "RecursionError",
    "MemoryError",
    "NameError",
    "UnboundLocalError",
    "SyntaxError",
    "IndentationError",
    "UnicodeError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "AssertionError",
    "EOFError",
    "LookupError",
)

BLOCKED_MODULES = ("subprocess", "shutil", "ctypes", "signal")


def _restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
    root_name = name.split(".", 1)[0]
    if root_name in BLOCKED_MODULES:
        raise ImportError(f"Import of '{root_name}' is not allowed in sandbox mode")
    return builtins.__import__(name, *args, **kwargs)


def build_globals(restrict_builtins: bool) -> dict[str, Any]:
    if not restrict_builtins:
        return {"__builtins__": __builtins__, "__name__": "__main__"}

    safe_builtins = {
        name: getattr(builtins, name)
        for name in SAFE_BUILTINS
        if hasattr(builtins, name)
    }
    safe_builtins["__import__"] = _restricted_import
    return {"__builtins__": safe_builtins, "__name__": "__main__"}


def build_bootstrap(restrict_builtins: bool) -> str:
    if restrict_builtins:
        bootstrap_lines = [
            "import builtins",
            "import sys",
            f"SAFE_BUILTINS = {SAFE_BUILTINS!r}",
            f"BLOCKED_MODULES = frozenset({BLOCKED_MODULES!r})",
            "_original_import = builtins.__import__",
            "",
            "def _restricted_import(name, *args, **kwargs):",
            '    root_name = name.split(".", 1)[0]',
            "    if root_name in BLOCKED_MODULES:",
            '        raise ImportError(f"Import of \'{root_name}\' is not allowed in sandbox mode")',
            "    return _original_import(name, *args, **kwargs)",
            "",
            "builtins.__import__ = _restricted_import",
            "_safe_builtins = {",
            "    name: getattr(builtins, name)",
            "    for name in SAFE_BUILTINS",
            "    if hasattr(builtins, name)",
            "}",
            '_safe_builtins["__import__"] = _restricted_import',
            'globals_dict = {"__builtins__": _safe_builtins, "__name__": "__main__"}',
            'exec(compile(sys.stdin.read(), "<stdin>", "exec"), globals_dict)',
        ]
        return "\n".join(bootstrap_lines)
    else:
        return "\n".join(
            [
                "import sys",
                'globals_dict = {"__builtins__": __builtins__, "__name__": "__main__"}',
                'exec(compile(sys.stdin.read(), "<stdin>", "exec"), globals_dict)',
            ]
        )
