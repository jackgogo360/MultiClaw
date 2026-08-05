"""Sandboxed Python code runner used by the code_exec tool."""

from __future__ import annotations

import argparse
import builtins
import json
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Any, Sequence

SAFE_BUILTINS = {
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
}

BLOCKED_MODULES = {"subprocess", "shutil", "ctypes", "signal"}


def _restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
    root_name = name.split(".", 1)[0]
    if root_name in BLOCKED_MODULES:
        raise ImportError(f"Import of '{root_name}' is not allowed in sandbox mode")
    return __import__(name, *args, **kwargs)


def build_globals(restrict_builtins: bool) -> dict[str, Any]:
    if not restrict_builtins:
        return {"__builtins__": __builtins__}

    safe_builtins = {
        name: getattr(builtins, name)
        for name in SAFE_BUILTINS
        if hasattr(builtins, name)
    }
    safe_builtins["__import__"] = _restricted_import
    return {"__builtins__": safe_builtins}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--restrict-builtins", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    code = sys.stdin.read()
    captured_stdout = StringIO()
    captured_stderr = StringIO()
    success = True
    error = ""

    try:
        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            exec(code, build_globals(args.restrict_builtins))
    except BaseException:
        success = False
        error = traceback.format_exc()

    sys.__stdout__.write(
        json.dumps(
            {
                "success": success,
                "stdout": captured_stdout.getvalue(),
                "stderr": captured_stderr.getvalue(),
                "error": error,
            },
            ensure_ascii=False,
        )
    )
    sys.__stdout__.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
