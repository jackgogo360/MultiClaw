"""Load only workspace-scoped project instructions, with bounded reads."""

from collections.abc import Iterable
import os
from pathlib import Path
import stat


def load_project_instructions(
    workspace_root: str | Path,
    *,
    paths: Iterable[str | Path] = (),
    max_bytes: int = 32768,
) -> list[tuple[str, str]]:
    """Return root then applicable directory AGENTS.md/CLAUDE.md instructions.

    Explicit paths scope subdirectory rules. Parent/home instructions and symlink
    files or directories are never read. The aggregate UTF-8 payload is bounded.
    """
    if max_bytes <= 0:
        return []
    root = Path(workspace_root).resolve()
    directories = {Path(".")}
    for path_text in paths:
        path = Path(path_text)
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError:
                continue
        if ".." in path.parts:
            continue
        candidate = root / path
        if any((root / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1)):
            continue
        directory = path if candidate.is_dir() else path.parent
        directories.add(directory)
        directories.update(directory.parents)
    loaded: list[tuple[str, str]] = []
    remaining = max_bytes
    for directory in sorted(directories, key=lambda value: (len(value.parts), str(value))):
        for filename in ("AGENTS.md", "CLAUDE.md"):
            if remaining <= 0:
                return loaded
            relative = directory / filename
            data = _read_confined(root, relative, remaining)
            if data is None:
                continue
            remaining -= len(data)
            text = data.decode("utf-8", errors="ignore")
            if text:
                loaded.append((relative.as_posix(), text))
    return loaded


def _read_confined(root: Path, relative: Path, limit: int) -> bytes | None:
    descriptors: list[int] = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in relative.parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        return os.read(descriptor, limit)
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
