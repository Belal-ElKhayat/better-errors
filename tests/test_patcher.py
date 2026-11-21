"""Tests for better_errors.patcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from better_errors.patcher import (
    PatchError,
    apply_fix,
    backup_file,
    render_diff,
    undo_last,
)

ORIGINAL_SOURCE = """\
def compute(data):
    return data["total"]


def main():
    return compute({})
"""


def _write_source(tmp_path: Path) -> Path:
    file_path = tmp_path / "app.py"
    file_path.write_text(ORIGINAL_SOURCE)
    return file_path


def test_render_diff_produces_unified_diff_with_additions_and_removals(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    rendered = render_diff(
        str(file_path),
        original_code='    return data["total"]',
        fixed_code='    return data.get("total", 0)',
    )
    assert rendered.has_changes is True
    assert '-    return data["total"]' in rendered.unified_text
    assert '+    return data.get("total", 0)' in rendered.unified_text
    assert 'data.get("total", 0)' in rendered.new_content


def test_render_diff_raises_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.py"
    with pytest.raises(PatchError):
        render_diff(str(missing), "x", "y")


def test_render_diff_raises_when_original_snippet_not_found(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    with pytest.raises(PatchError):
        render_diff(str(file_path), "this snippet does not exist", "replacement")


def test_render_diff_no_changes_when_original_equals_fixed(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    rendered = render_diff(
        str(file_path),
        original_code='    return data["total"]',
        fixed_code='    return data["total"]',
    )
    assert rendered.has_changes is False


def test_backup_file_creates_copy_and_manifest(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_path = backup_file(str(file_path), backup_dir=backup_dir)

    assert backup_path.is_file()
    assert backup_path.read_text() == ORIGINAL_SOURCE

    manifest_path = backup_path.with_suffix(backup_path.suffix + ".manifest")
    assert manifest_path.is_file()
    assert manifest_path.read_text().strip() == str(file_path.resolve())


def test_apply_fix_writes_new_content_and_preserves_backup(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    backup_dir = tmp_path / "backups"

    rendered = render_diff(
        str(file_path),
        original_code='    return data["total"]',
        fixed_code='    return data.get("total", 0)',
    )
    backup_path = apply_fix(rendered, backup_dir=backup_dir)

    assert file_path.read_text() == rendered.new_content
    assert "data.get" in file_path.read_text()
    assert backup_path.read_text() == ORIGINAL_SOURCE


def test_apply_fix_is_atomic_and_file_always_fully_written(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    backup_dir = tmp_path / "backups"

    rendered = render_diff(
        str(file_path),
        original_code='    return data["total"]',
        fixed_code='    return data.get("total", 0)',
    )
    apply_fix(rendered, backup_dir=backup_dir)

    # No leftover temp files in the directory after a successful write.
    leftover_tmp_files = list(tmp_path.glob(".app.py.*.tmp"))
    assert leftover_tmp_files == []


def test_undo_last_restores_most_recent_backup(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    backup_dir = tmp_path / "backups"

    rendered = render_diff(
        str(file_path),
        original_code='    return data["total"]',
        fixed_code='    return data.get("total", 0)',
    )
    apply_fix(rendered, backup_dir=backup_dir)
    assert "data.get" in file_path.read_text()

    restored_path = undo_last(backup_dir=backup_dir)

    assert restored_path == file_path.resolve()
    assert file_path.read_text() == ORIGINAL_SOURCE


def test_undo_last_raises_when_no_backups_exist(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    with pytest.raises(PatchError):
        undo_last(backup_dir=backup_dir)


def test_undo_last_removes_backup_and_manifest_after_restoring(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    backup_dir = tmp_path / "backups"

    rendered = render_diff(
        str(file_path),
        original_code='    return data["total"]',
        fixed_code='    return data.get("total", 0)',
    )
    apply_fix(rendered, backup_dir=backup_dir)
    undo_last(backup_dir=backup_dir)

    remaining = list(backup_dir.iterdir())
    assert remaining == []


def test_undo_twice_restores_two_sequential_fixes_in_reverse_order(tmp_path: Path) -> None:
    file_path = _write_source(tmp_path)
    backup_dir = tmp_path / "backups"

    first = render_diff(
        str(file_path),
        original_code='    return data["total"]',
        fixed_code='    return data.get("total", 0)',
    )
    apply_fix(first, backup_dir=backup_dir)

    second = render_diff(
        str(file_path),
        original_code='def main():',
        fixed_code='def main() -> int:',
    )
    apply_fix(second, backup_dir=backup_dir)

    # Undo the second fix first.
    undo_last(backup_dir=backup_dir)
    content_after_first_undo = file_path.read_text()
    assert "def main():" in content_after_first_undo
    assert "data.get" in content_after_first_undo

    # Undo the first fix next, returning to the pristine original.
    undo_last(backup_dir=backup_dir)
    assert file_path.read_text() == ORIGINAL_SOURCE
