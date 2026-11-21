"""Unified diff rendering, atomic patching, and backup/undo management.

This module never writes to disk unless explicitly told to via
:func:`apply_fix`, and it always writes a timestamped backup first so
:func:`undo_last` can restore it.
"""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import BACKUP_DIR

#: Name of the sidecar file recording which original path a backup came
#: from, so `--undo` can restore it to the right place even if the backup
#: filename itself has been made unique by a timestamp/counter suffix.
_MANIFEST_SUFFIX = ".manifest"


class PatchError(Exception):
    """Raised when a diff cannot be rendered or a patch cannot be applied."""


@dataclass(frozen=True)
class RenderedDiff:
    """A human-readable unified diff plus the data needed to apply it.

    Attributes:
        file_path: Path of the file the diff applies to.
        unified_text: The diff, as unified-diff text (for display).
        new_content: The full new file content, ready to write if approved.
        has_changes: Whether the diff actually changes anything.
    """

    file_path: str
    unified_text: str
    new_content: str
    has_changes: bool


def render_diff(file_path: str, original_code: str, fixed_code: str) -> RenderedDiff:
    """Build a unified diff for replacing ``original_code`` with ``fixed_code``.

    The replacement is a simple, exact substring replacement: it locates
    ``original_code`` verbatim within the current file contents and swaps
    in ``fixed_code``. This keeps the patch minimal and predictable and
    avoids applying a fix against source that has since changed underneath
    the model's suggestion.

    Args:
        file_path: Path to the file to be patched.
        original_code: Exact snippet the model claims to be replacing.
        fixed_code: The proposed replacement snippet.

    Returns:
        A :class:`RenderedDiff` describing the change.

    Raises:
        PatchError: If the file doesn't exist, can't be read, or
            ``original_code`` can't be found in it.
    """
    path = Path(file_path)
    if not path.is_file():
        raise PatchError(f"Cannot render diff: '{file_path}' does not exist.")

    try:
        current_content = path.read_text()
    except OSError as exc:
        raise PatchError(f"Cannot read '{file_path}': {exc}") from exc

    if original_code not in current_content:
        raise PatchError(
            f"The proposed fix no longer matches the contents of '{file_path}'. "
            "The file may have changed since the error occurred."
        )

    new_content = current_content.replace(original_code, fixed_code, 1)

    original_lines = current_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            new_lines,
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
    )

    return RenderedDiff(
        file_path=str(path),
        unified_text="".join(diff_lines),
        new_content=new_content,
        has_changes=current_content != new_content,
    )


def colorize_diff(unified_text: str) -> str:
    """Wrap a unified diff's lines in ANSI color codes for terminal display.

    Added lines are green, removed lines are red, hunk headers are cyan,
    and file headers are bold. Intended to be printed with ``rich`` or raw
    ``print`` -- it only inserts standard ANSI escapes, no markup.
    """
    RED = "\033[31m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    out_lines = []
    for line in unified_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out_lines.append(f"{BOLD}{line}{RESET}")
        elif line.startswith("@@"):
            out_lines.append(f"{CYAN}{line}{RESET}")
        elif line.startswith("+"):
            out_lines.append(f"{GREEN}{line}{RESET}")
        elif line.startswith("-"):
            out_lines.append(f"{RED}{line}{RESET}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if unified_text.endswith("\n") else "")


def _timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime())


def backup_file(file_path: str, *, backup_dir: Path | None = None) -> Path:
    """Copy ``file_path`` into the backup directory with a timestamped name.

    Also writes a ``.manifest`` sidecar file containing the original
    absolute path, so :func:`undo_last` knows where to restore it.

    Args:
        file_path: Path of the file about to be modified.
        backup_dir: Override backup directory (used in tests); defaults to
            :data:`better_errors.config.BACKUP_DIR`.

    Returns:
        Path to the created backup file.
    """
    target_dir = backup_dir or BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    source = Path(file_path).resolve()
    backup_name = f"{source.name}.{_timestamp()}.bak"
    backup_path = target_dir / backup_name

    counter = 1
    while backup_path.exists():
        backup_path = target_dir / f"{source.name}.{_timestamp()}-{counter}.bak"
        counter += 1

    shutil.copy2(source, backup_path)
    manifest_path = backup_path.with_suffix(backup_path.suffix + _MANIFEST_SUFFIX)
    manifest_path.write_text(str(source))

    return backup_path


def apply_fix(rendered: RenderedDiff, *, backup_dir: Path | None = None) -> Path:
    """Back up and atomically write ``rendered.new_content`` to disk.

    The write is atomic: content is written to a temporary file in the
    same directory, then moved into place with :func:`os.replace`, so a
    crash or interruption mid-write never leaves the target file
    truncated or corrupted.

    Args:
        rendered: The diff to apply, as produced by :func:`render_diff`.
        backup_dir: Override backup directory (used in tests).

    Returns:
        Path to the backup file that was created before patching.

    Raises:
        PatchError: If writing the new content fails.
    """
    target = Path(rendered.file_path)
    backup_path = backup_file(str(target), backup_dir=backup_dir)

    try:
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(rendered.new_content)
            # Preserve original file permissions on the new file.
            shutil.copymode(target, tmp_path)
            os.replace(tmp_path, target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise PatchError(f"Failed to write patched file '{target}': {exc}") from exc

    return backup_path


def _list_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.is_dir():
        return []
    backups = [
        p
        for p in backup_dir.iterdir()
        if p.is_file() and not p.name.endswith(_MANIFEST_SUFFIX)
    ]
    return sorted(backups, key=lambda p: p.stat().st_mtime, reverse=True)


def undo_last(*, backup_dir: Path | None = None) -> Path:
    """Restore the most recently created backup to its original location.

    Args:
        backup_dir: Override backup directory (used in tests).

    Returns:
        The path that was restored.

    Raises:
        PatchError: If there is no backup to restore, or the manifest
            recording its original location is missing/unreadable.
    """
    target_dir = backup_dir or BACKUP_DIR
    backups = _list_backups(target_dir)
    if not backups:
        raise PatchError("No backups found to undo.")

    most_recent = backups[0]
    manifest_path = most_recent.with_suffix(most_recent.suffix + _MANIFEST_SUFFIX)
    if not manifest_path.is_file():
        raise PatchError(
            f"Backup '{most_recent}' is missing its manifest; cannot determine "
            "where to restore it."
        )

    original_path = Path(manifest_path.read_text().strip())
    original_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(most_recent, original_path)

    most_recent.unlink()
    manifest_path.unlink()

    return original_path
