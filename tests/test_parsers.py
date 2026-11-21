"""Tests for better_errors.parsers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from better_errors.parsers import (
    looks_like_traceback,
    parse_traceback,
)

SIMPLE_TRACEBACK = """\
Traceback (most recent call last):
  File "/home/user/project/app.py", line 10, in <module>
    main()
  File "/home/user/project/app.py", line 6, in main
    return compute(data)
  File "/home/user/project/app.py", line 3, in compute
    return data["total"]
KeyError: 'total'
"""

TRACEBACK_WITH_DID_YOU_MEAN = """\
Traceback (most recent call last):
  File "/home/user/project/app.py", line 4, in <module>
    reuslt = compute(2, 3)
NameError: name 'reuslt' is not defined. Did you mean: 'result'?
"""

TRACEBACK_WITH_STDLIB_FRAME = """\
Traceback (most recent call last):
  File "/home/user/project/app.py", line 2, in <module>
    json.loads("{bad json")
  File "/usr/lib/python3.11/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
  File "/usr/lib/python3.11/json/decoder.py", line 337, in decode
    obj, end = self.raw_decode(s, idx=_w(s, 0).end())
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes
"""


def test_looks_like_traceback_detects_standard_header() -> None:
    assert looks_like_traceback(SIMPLE_TRACEBACK) is True


def test_looks_like_traceback_rejects_plain_output() -> None:
    assert looks_like_traceback("Hello, world!\nDone.\n") is False


def test_parse_traceback_extracts_exception_type_and_message() -> None:
    parsed = parse_traceback(SIMPLE_TRACEBACK, read_source=False)
    assert parsed is not None
    assert parsed.exc_type == "KeyError"
    assert parsed.message == "'total'"


def test_parse_traceback_extracts_all_frames_in_order() -> None:
    parsed = parse_traceback(SIMPLE_TRACEBACK, read_source=False)
    assert parsed is not None
    assert len(parsed.frames) == 3
    assert parsed.frames[0].function == "<module>"
    assert parsed.frames[0].line == 10
    assert parsed.frames[-1].function == "compute"
    assert parsed.frames[-1].line == 3
    assert parsed.frames[-1].file == "/home/user/project/app.py"


def test_parse_traceback_extracts_did_you_mean_separately() -> None:
    parsed = parse_traceback(TRACEBACK_WITH_DID_YOU_MEAN, read_source=False)
    assert parsed is not None
    assert parsed.exc_type == "NameError"
    assert "reuslt" in parsed.message
    assert parsed.did_you_mean is not None
    assert "result" in parsed.did_you_mean
    # The did-you-mean text must not leak into the clean message used for
    # root-cause analysis.
    assert "Did you mean" not in parsed.message


def test_parse_traceback_returns_none_for_non_traceback_text() -> None:
    assert parse_traceback("just some normal program output\n") is None


def test_stdlib_frames_are_not_local() -> None:
    parsed = parse_traceback(TRACEBACK_WITH_STDLIB_FRAME, read_source=False)
    assert parsed is not None
    # The app.py frame is local; the json/__init__.py and decoder.py frames
    # are not, since they live under a stdlib path.
    local_files = {f.file for f in parsed.local_frames}
    assert local_files == {"/home/user/project/app.py"}


def test_failing_frame_prefers_local_frame() -> None:
    parsed = parse_traceback(TRACEBACK_WITH_STDLIB_FRAME, read_source=False)
    assert parsed is not None
    failing = parsed.failing_frame
    assert failing is not None
    assert failing.file == "/home/user/project/app.py"


def test_source_context_window_is_read_for_local_frames(tmp_path: Path) -> None:
    source_file = tmp_path / "app.py"
    lines = [f"line_{i}" for i in range(1, 51)]
    source_file.write_text("\n".join(lines) + "\n")

    traceback_text = (
        "Traceback (most recent call last):\n"
        f'  File "{source_file}", line 25, in <module>\n'
        "    boom()\n"
        "ValueError: boom\n"
    )

    parsed = parse_traceback(traceback_text, read_source=True)
    assert parsed is not None
    frame = parsed.frames[0]
    assert frame.is_local is True
    # Window should be [5, 45] i.e. 25 +/- 20, clipped to file bounds.
    line_numbers = [num for num, _ in frame.source]
    assert line_numbers[0] == 5
    assert line_numbers[-1] == 45
    assert len(frame.source) == 41


def test_source_context_handles_missing_file_gracefully() -> None:
    traceback_text = (
        "Traceback (most recent call last):\n"
        '  File "/nonexistent/path/app.py", line 25, in <module>\n'
        "    boom()\n"
        "ValueError: boom\n"
    )
    parsed = parse_traceback(traceback_text, read_source=True)
    assert parsed is not None
    assert parsed.frames[0].source == []


def test_parse_traceback_picks_last_traceback_when_multiple_present() -> None:
    text = (
        SIMPLE_TRACEBACK
        + "\nDuring handling of the above exception, another exception occurred:\n\n"
        + TRACEBACK_WITH_DID_YOU_MEAN
    )
    parsed = parse_traceback(text, read_source=False)
    assert parsed is not None
    assert parsed.exc_type == "NameError"
