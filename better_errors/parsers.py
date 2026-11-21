"""Error parsing and local source-context extraction.

The first-class parser here targets Python tracebacks. The module is
structured so support for other languages (Node.js, Rust, Go) can be added
as sibling functions/classes without touching the public API that
``cli.py`` depends on: :func:`parse_traceback` and :class:`ParsedError`.
"""

from __future__ import annotations

import os
import re
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

# Matches a single traceback frame line, e.g.:
#   File "/home/user/app.py", line 42, in main
_FRAME_RE = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+?)\s*$',
    re.MULTILINE,
)

# Matches the final "ExceptionType: message" line of a Python traceback.
# The exception name may be dotted (e.g. requests.exceptions.ConnectionError)
# and the message is optional (e.g. bare "KeyboardInterrupt").
_EXC_LINE_RE = re.compile(
    r"^(?P<exc_type>[A-Za-z_][A-Za-z0-9_.]*)(?::\s*(?P<message>.*))?$"
)

_TRACEBACK_HEADER_RE = re.compile(
    r"^Traceback \(most recent call last\):\s*$", re.MULTILINE
)

#: Lines of built-in interpreter suggestions we deliberately ignore when
#: forming root-cause analysis, per spec section 3. These are informative
#: for a human skimming a terminal, but can anchor an LLM on the wrong fix
#: (e.g. suggesting a typo correction when the real bug is a missing import
#: or a stale variable scope).
_DID_YOU_MEAN_RE = re.compile(r"\.\s*Did you mean[:]?.*$", re.IGNORECASE)

#: Context window on either side of the failing line, in source lines.
CONTEXT_RADIUS = 20


@dataclass
class StackFrame:
    """A single frame in a parsed traceback.

    Attributes:
        file: Absolute or as-captured path to the source file.
        line: 1-based line number where execution was at this frame.
        function: Name of the function/method active in this frame.
        is_local: Whether this frame belongs to the user's own project
            (as opposed to the standard library or an installed package).
        source: Lines of code surrounding ``line`` (populated only for
            local frames), as a list of ``(line_number, text)`` tuples.
    """

    file: str
    line: int
    function: str
    is_local: bool = False
    source: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class ParsedError:
    """A fully parsed error, ready to hand to an LLM provider.

    Attributes:
        exc_type: The exception class name, e.g. ``"KeyError"``.
        message: The exception's message text, if any.
        frames: Stack frames, outermost call first (as Python prints them).
        did_you_mean: The interpreter's own "Did you mean ...?" suggestion,
            if present. Kept separately so callers can deliberately exclude
            it from root-cause prompts (per spec) while still surfacing it
            to the user if desired.
        raw_traceback: The original traceback text, unmodified.
        language: Source language of the error. Only ``"python"`` is
            supported today; the field exists so new parsers can reuse
            this dataclass.
    """

    exc_type: str
    message: str
    frames: list[StackFrame]
    did_you_mean: str | None
    raw_traceback: str
    language: str = "python"

    @property
    def failing_frame(self) -> StackFrame | None:
        """The most specific local-project frame, or the last frame overall."""
        for frame in reversed(self.frames):
            if frame.is_local:
                return frame
        return self.frames[-1] if self.frames else None

    @property
    def local_frames(self) -> list[StackFrame]:
        """Stack frames that belong to the user's own project."""
        return [f for f in self.frames if f.is_local]


def _stdlib_and_site_package_dirs() -> list[str]:
    """Return directories whose files should be treated as non-local."""
    dirs: set[str] = set()
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = paths.get(key)
        if value:
            dirs.add(os.path.normcase(os.path.realpath(value)))
    return sorted(dirs)


_NON_LOCAL_DIRS = _stdlib_and_site_package_dirs()


def _is_local_file(path: str) -> bool:
    """Heuristic: a frame is "local" if it is not in stdlib/site-packages.

    Also excludes anything living in a ``site-packages`` or
    ``dist-packages`` directory even if ``sysconfig`` didn't report it
    (common with virtualenvs and some Linux distro layouts), and excludes
    frozen/builtin frames such as ``<frozen importlib._bootstrap>``.
    """
    if path.startswith("<"):
        return False

    normalized = os.path.normcase(os.path.realpath(path))

    if "site-packages" in normalized or "dist-packages" in normalized:
        return False

    for non_local in _NON_LOCAL_DIRS:
        if normalized.startswith(non_local + os.sep) or normalized == non_local:
            return False

    # Fallback heuristic for tracebacks produced by a *different* Python
    # interpreter/version than the one running better-errors (e.g. a CI log
    # from Python 3.11 inspected on a machine running 3.13): sysconfig's
    # paths only reflect the current interpreter, so also recognize the
    # conventional stdlib layout by pattern.
    if re.search(r"[/\\]lib[/\\]python\d+(?:\.\d+)?[/\\]", normalized):
        return False
    if re.search(r"[/\\]lib[/\\]python\d+(?:\.\d+)?[/\\]", path):
        return False

    return True


def _read_source_window(
    path: str, line: int, radius: int = CONTEXT_RADIUS
) -> list[tuple[int, str]]:
    """Read up to ``radius`` lines before and after ``line`` from ``path``.

    Returns an empty list if the file cannot be read (e.g. it was deleted,
    is a ``<string>`` pseudo-file, or permissions deny access) -- this is a
    best-effort convenience, not a hard requirement.
    """
    try:
        file_path = Path(path)
        if not file_path.is_file():
            return []
        lines = file_path.read_text(errors="replace").splitlines()
    except OSError:
        return []

    if not lines:
        return []

    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    return [(i, lines[i - 1]) for i in range(start, end + 1)]


def looks_like_traceback(text: str) -> bool:
    """Quick heuristic check used by the CLI to decide whether to analyze.

    Args:
        text: Captured stdout/stderr text.

    Returns:
        True if ``text`` appears to contain a Python traceback.
    """
    if _TRACEBACK_HEADER_RE.search(text):
        return True
    # Fallback: a "File "..." line N, in ..." frame followed eventually by
    # an "ExceptionType: message" line is a strong signal even without the
    # standard header (e.g. truncated output piped through `tail`).
    return bool(_FRAME_RE.search(text)) and bool(
        re.search(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning)\b", text, re.M)
    )


def parse_traceback(text: str, *, read_source: bool = True) -> ParsedError | None:
    """Parse a Python traceback out of captured text.

    Args:
        text: Raw stdout/stderr text, possibly containing other noise
            before/after the traceback (e.g. pytest banners, print output).
        read_source: Whether to read ``CONTEXT_RADIUS`` lines of source
            around each local frame. Disable for fast unit tests or when
            source files are unavailable.

    Returns:
        A :class:`ParsedError`, or ``None`` if no traceback could be found.
    """
    lines = text.splitlines()

    # Find the start of the last traceback in the text (there can be more
    # than one, e.g. "During handling of the above exception..."). We want
    # the most recent/innermost one, which is the one actually printed last.
    header_indices = [i for i, line in enumerate(lines) if _TRACEBACK_HEADER_RE.match(line)]
    start = header_indices[-1] if header_indices else None

    if start is None:
        # No explicit header; try to locate frames directly (pipe mode may
        # receive partial output).
        frame_indices = [i for i, line in enumerate(lines) if _FRAME_RE.match(line)]
        if not frame_indices:
            return None
        start = frame_indices[0]

    block = lines[start:]

    frames: list[StackFrame] = []
    i = 0
    while i < len(block):
        match = _FRAME_RE.match(block[i])
        if match:
            file_path = match.group("file")
            line_no = int(match.group("line"))
            func = match.group("func")
            is_local = _is_local_file(file_path)
            source = (
                _read_source_window(file_path, line_no) if (read_source and is_local) else []
            )
            frames.append(
                StackFrame(
                    file=file_path,
                    line=line_no,
                    function=func,
                    is_local=is_local,
                    source=source,
                )
            )
            # Skip the one or two "code context" lines the interpreter
            # sometimes prints directly under a frame.
            i += 1
            while i < len(block) and block[i].startswith("    ") and not _FRAME_RE.match(block[i]):
                i += 1
            continue
        i += 1

    # The exception line is the last non-empty line of the block that isn't
    # itself a frame or indented code context, and isn't part of a nested
    # "During handling of the above exception..." preamble.
    exc_type = "UnknownError"
    message = ""
    did_you_mean: str | None = None

    for raw_line in reversed(block):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("Traceback (") or _FRAME_RE.match(raw_line):
            continue
        dym_match = _DID_YOU_MEAN_RE.search(stripped)
        if dym_match:
            did_you_mean = dym_match.group(0).lstrip(". ").strip()
            stripped = _DID_YOU_MEAN_RE.sub("", stripped).rstrip()
        exc_match = _EXC_LINE_RE.match(stripped)
        if exc_match:
            exc_type = exc_match.group("exc_type")
            message = (exc_match.group("message") or "").strip()
            break

    if not frames and exc_type == "UnknownError":
        return None

    return ParsedError(
        exc_type=exc_type,
        message=message,
        frames=frames,
        did_you_mean=did_you_mean,
        raw_traceback="\n".join(block),
    )
