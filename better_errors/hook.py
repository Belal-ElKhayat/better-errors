"""Shell integration: transparently route common dev commands through
better-errors from PowerShell or cmd.exe, without the user typing
`better-errors` themselves.

Scope and honesty note: neither PowerShell nor cmd.exe can generically
intercept *every* possible command on a system without fragile, invasive
hooking into the shell's internal command-lookup machinery. What this
module does instead -- and what every well-known tool in this space
(thefuck, direnv's shell hooks, nvm's shims, etc.) actually does -- is
shadow a known, explicit list of common interpreters/build tools with a
thin wrapper that re-invokes the real executable through
``better_errors.cli``. That covers "run my Python/Node/Java/Go/Rust/Ruby
program and catch its errors" for the overwhelming majority of real usage,
while being honest about not covering arbitrary unknown executables.

Both hooks consult :func:`better_errors.config.is_enabled` on every
invocation, so ``better-errors disable`` / ``better-errors enable`` take
effect immediately without reinstalling anything.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

#: Commands shadowed by the shell hooks. Chosen to cover the common dev
#: workflows (Python, Node/JS, JVM languages, Go, Rust, Ruby) without
#: shadowing anything a user is likely to need pristine, unwrapped access
#: to (e.g. `git`, `cd`, `ls`).
HOOKED_COMMANDS: tuple[str, ...] = (
    "python",
    "python3",
    "node",
    "npm",
    "npx",
    "pytest",
    "go",
    "cargo",
    "java",
    "ruby",
)

_MARKER_START = "# >>> better-errors hook >>>"
_MARKER_END = "# <<< better-errors hook <<<"

#: Public aliases for callers (e.g. `cli.py hook status`) that need to
#: detect whether a profile already contains an installed block, without
#: reaching into "private" module internals.
MARKER_START = _MARKER_START
MARKER_END = _MARKER_END


class HookError(Exception):
    """Raised when a shell hook cannot be installed, removed, or located."""


def _real_python_exe() -> str:
    """Return the path to the Python interpreter running this package.

    Used inside generated hook scripts so the wrapper always re-invokes
    the exact same interpreter/environment better-errors itself runs
    under, rather than relying on `python` resolving correctly at call
    time (it won't, since we've shadowed that very name).
    """
    return sys.executable


# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------


def powershell_profile_path() -> Path:
    """Best-effort path to the current user's PowerShell profile.

    Honors ``BETTER_ERRORS_PWSH_PROFILE`` for tests/overrides. Otherwise
    uses the conventional Windows PowerShell 7+ location; falls back to
    Windows PowerShell 5.1's location if that's what's present.
    """
    override = os.environ.get("BETTER_ERRORS_PWSH_PROFILE")
    if override:
        return Path(override).expanduser()

    home = Path.home()
    pwsh7 = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    winps = home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
    if pwsh7.parent.is_dir() or not winps.parent.is_dir():
        return pwsh7
    return winps


def _powershell_block() -> str:
    exe = _real_python_exe()
    func_lines = []
    for cmd in HOOKED_COMMANDS:
        func_lines.append(
            textwrap.dedent(
                f"""\
                function global:{cmd} {{
                    if ((better-errors-enabled)) {{
                        & '{exe}' -m better_errors.cli {cmd} @args
                    }} else {{
                        & {cmd}.exe @args
                    }}
                }}
                """
            )
        )

    body = "\n".join(func_lines)
    return (
        f"{_MARKER_START}\n"
        "function global:better-errors-enabled {\n"
        f"    $result = & '{exe}' -m better_errors.cli --is-enabled 2>$null\n"
        "    return ($LASTEXITCODE -eq 0)\n"
        "}\n\n"
        f"{body}\n"
        f"{_MARKER_END}\n"
    )


def install_powershell_hook(profile_path: Path | None = None) -> Path:
    """Append the better-errors shell function block to a PowerShell profile.

    Idempotent: if a block from a previous install is present, it is
    replaced rather than duplicated.

    Returns:
        The profile path that was written to.
    """
    path = profile_path or powershell_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = path.read_text() if path.is_file() else ""
    existing = _strip_marked_block(existing)
    new_content = existing.rstrip() + "\n\n" + _powershell_block()
    path.write_text(new_content.lstrip("\n"))
    return path


def uninstall_powershell_hook(profile_path: Path | None = None) -> bool:
    """Remove the better-errors block from a PowerShell profile, if present.

    Returns:
        True if a block was found and removed, False if the profile didn't
        have one (or doesn't exist).
    """
    path = profile_path or powershell_profile_path()
    if not path.is_file():
        return False

    original = path.read_text()
    stripped = _strip_marked_block(original)
    if stripped == original:
        return False

    path.write_text(stripped)
    return True


# ---------------------------------------------------------------------------
# cmd.exe
# ---------------------------------------------------------------------------


def cmd_autorun_script_path() -> Path:
    """Path to the batch script registered as cmd.exe's AutoRun script."""
    override = os.environ.get("BETTER_ERRORS_CMD_AUTORUN")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".better-errors" / "cmd_hook.bat"


def _cmd_autorun_script() -> str:
    exe = _real_python_exe()
    lines = [f'@echo off', f'rem {_MARKER_START}']
    for cmd in HOOKED_COMMANDS:
        # doskey macros: $* forwards all arguments. We check enabled status
        # on every invocation (cheap: a single fast local config read).
        lines.append(
            f'doskey {cmd}="{exe}" -m better_errors.cli {cmd} $*'
        )
    lines.append(f"rem {_MARKER_END}")
    return "\n".join(lines) + "\n"


def install_cmd_hook() -> Path:
    """Write the cmd.exe AutoRun batch script and register it in the registry.

    Uses the per-user ``HKCU\\Software\\Microsoft\\Command Processor\\AutoRun``
    value, so every new ``cmd.exe`` window picks up the doskey macros
    automatically. Requires the ``winreg`` module (Windows only).

    Returns:
        Path to the generated batch script.

    Raises:
        HookError: If not running on Windows, or the registry write fails.
    """
    if sys.platform != "win32":
        raise HookError("The cmd.exe hook is only available on Windows.")

    script_path = cmd_autorun_script_path()
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_cmd_autorun_script())

    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - non-Windows
        raise HookError("winreg module unavailable; cannot register AutoRun.") from exc

    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Command Processor", 0, winreg.KEY_SET_VALUE
        )
        try:
            existing_autorun = ""
            try:
                existing_autorun, _ = winreg.QueryValueEx(key, "AutoRun")
            except FileNotFoundError:
                pass
            call_line = f'call "{script_path}"'
            if call_line not in existing_autorun:
                combined = (existing_autorun + " & " + call_line).strip(" &") if existing_autorun else call_line
                winreg.SetValueEx(key, "AutoRun", 0, winreg.REG_SZ, combined)
        finally:
            winreg.CloseKey(key)
    except OSError as exc:
        raise HookError(f"Failed to register cmd.exe AutoRun key: {exc}") from exc

    return script_path


def uninstall_cmd_hook() -> bool:
    """Remove the better-errors AutoRun registration and batch script.

    Returns:
        True if a registration was found and removed, False otherwise.
    """
    if sys.platform != "win32":
        return False

    script_path = cmd_autorun_script_path()
    removed_anything = False

    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return False

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Command Processor", 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        )
    except FileNotFoundError:
        key = None

    if key is not None:
        try:
            try:
                existing_autorun, _ = winreg.QueryValueEx(key, "AutoRun")
            except FileNotFoundError:
                existing_autorun = ""

            call_line = f'call "{script_path}"'
            if call_line in existing_autorun:
                remaining = existing_autorun.replace(call_line, "").strip(" &")
                if remaining:
                    winreg.SetValueEx(key, "AutoRun", 0, winreg.REG_SZ, remaining)
                else:
                    try:
                        winreg.DeleteValue(key, "AutoRun")
                    except FileNotFoundError:
                        pass
                removed_anything = True
        finally:
            winreg.CloseKey(key)

    if script_path.is_file():
        script_path.unlink()
        removed_anything = True

    return removed_anything


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _strip_marked_block(text: str) -> str:
    """Remove a previously-installed ``_MARKER_START``/``_MARKER_END`` block."""
    if _MARKER_START not in text:
        return text

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        if _MARKER_START in line:
            skipping = True
            continue
        if _MARKER_END in line:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "".join(out)
