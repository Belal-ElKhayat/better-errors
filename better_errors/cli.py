"""CLI entrypoint: subprocess streaming, pipe handling, interactive prompts.

Two invocation modes, per spec:

* Wrapper mode:  ``better-errors <command> [args...]``
* Pipe mode:     ``<command> 2>&1 | better-errors``

Plus setup/maintenance commands: ``better-errors init`` and
``better-errors --undo``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax

from . import config as config_module
from . import hook as hook_module
from .config import Config, ConfigError
from .hook import HookError
from .parsers import ParsedError, StackFrame, looks_like_traceback, parse_traceback
from .patcher import PatchError, RenderedDiff, apply_fix, render_diff, undo_last
from .providers import FixSuggestion, ProviderError, QuotaExceededError, build_provider
from .redactor import redact_secrets

console = Console()
error_console = Console(stderr=True)

_USAGE = """\
better-errors: explain and (optionally) fix runtime errors.

Usage:
  better-errors <command> [args...]   Run a command, analyze it on failure.
  <command> 2>&1 | better-errors      Analyze piped output directly.
  better-errors init                  Interactive provider/model setup.
  better-errors --undo                Restore the most recent backup.
  better-errors enable                Turn analysis back on.
  better-errors disable               Turn analysis off (commands still run normally).
  better-errors hook install          Install the PowerShell + cmd.exe shell hooks.
  better-errors hook uninstall        Remove the shell hooks.
  better-errors hook status           Show whether hooks are installed and enabled.
  better-errors --help                Show this message.
"""


@dataclass
class StreamResult:
    """Captured output and exit code from a wrapped subprocess run."""

    stdout: str
    stderr: str
    combined: str
    exit_code: int


def _stream_process(command: list[str]) -> StreamResult:
    """Run ``command`` as a subprocess, streaming output live to the user.

    Output is simultaneously written to the real stdout/stderr (so the
    wrapped program behaves exactly as if run directly, with no added
    latency) and captured in memory for later error analysis.

    Args:
        command: Argv-style command and arguments to execute.

    Returns:
        A :class:`StreamResult` with captured output and the exit code.
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        error_console.print(f"[red]better-errors:[/red] command not found: {command[0]}")
        raise SystemExit(127) from exc

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _pump(src, sink, collector: list[str]) -> None:
        for line in iter(src.readline, ""):
            sink.write(line)
            sink.flush()
            collector.append(line)
        src.close()

    stdout_thread = threading.Thread(
        target=_pump, args=(process.stdout, sys.stdout, stdout_chunks)
    )
    stderr_thread = threading.Thread(
        target=_pump, args=(process.stderr, sys.stderr, stderr_chunks)
    )
    stdout_thread.start()
    stderr_thread.start()

    exit_code = process.wait()
    stdout_thread.join()
    stderr_thread.join()

    stdout_text = "".join(stdout_chunks)
    stderr_text = "".join(stderr_chunks)
    return StreamResult(
        stdout=stdout_text,
        stderr=stderr_text,
        combined=stdout_text + stderr_text,
        exit_code=exit_code,
    )


def _build_prompt(command: str, parsed: ParsedError) -> str:
    """Assemble the (already-redacted) user prompt sent to the LLM provider."""
    parts: list[str] = []
    parts.append(f"Command that failed: {redact_secrets(command)}")
    parts.append(f"\nException: {parsed.exc_type}: {parsed.message}")
    parts.append("\nFull traceback:\n" + redact_secrets(parsed.raw_traceback))

    local_frames = parsed.local_frames
    if local_frames:
        parts.append("\nLocal project source context (not stdlib/site-packages):")
        for frame in local_frames:
            parts.append(f"\n--- {frame.file}, around line {frame.line} (in {frame.function}) ---")
            snippet = "\n".join(f"{num:>5}: {text}" for num, text in frame.source)
            parts.append(redact_secrets(snippet))
    else:
        parts.append(
            "\n(No local project source files were found in the stack; the "
            "failure occurred entirely within the standard library or an "
            "installed dependency.)"
        )

    parts.append(
        "\nNote: ignore any interpreter-generated \"Did you mean ...?\" hint when "
        "determining the root cause -- verify the real variable/type/import "
        "context yourself instead of trusting it blindly."
    )

    return "\n".join(parts)


def _print_analysis(parsed: ParsedError, fix: FixSuggestion) -> None:
    console.print()
    console.print(Panel(fix.root_cause, title="Root Cause", border_style="red", expand=False))

    if fix.misleading_note:
        console.print(
            Panel(
                fix.misleading_note,
                title="Why the default error was misleading",
                border_style="yellow",
                expand=False,
            )
        )


def _print_diff(rendered: RenderedDiff, fix: FixSuggestion) -> None:
    console.print()
    console.print(Panel(f"[bold]{rendered.file_path}[/bold]", title="Proposed Fix", border_style="green", expand=False))
    syntax = Syntax(rendered.unified_text, "diff", theme="ansi_dark", background_color="default")
    console.print(syntax)
    if fix.explanation_of_fix:
        console.print(f"[dim]{fix.explanation_of_fix}[/dim]")


def _analyze_and_maybe_fix(command_str: str, combined_output: str, *, can_rerun: bool, rerun_command: list[str] | None) -> None:
    """Shared tail-end logic for both wrapper and pipe mode.

    Parses the traceback, calls the configured provider, shows the root
    cause and diff, and -- if the user approves -- applies the fix and
    offers to re-run the original command.
    """
    parsed = parse_traceback(combined_output)
    if parsed is None:
        return

    if not config_module.is_enabled():
        error_console.print(
            "\n[dim]better-errors: analysis is disabled (`better-errors enable` to turn back on).[/dim]"
        )
        return

    try:
        cfg: Config = config_module.load_config()
    except ConfigError as exc:
        error_console.print(f"\n[yellow]better-errors:[/yellow] {exc}")
        return

    try:
        provider = build_provider(
            provider=cfg.provider,
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.resolved_base_url(),
        )
    except ProviderError as exc:
        error_console.print(f"\n[red]better-errors:[/red] {exc}")
        return

    prompt = _build_prompt(command_str, parsed)

    with console.status("[bold blue]Analyzing error...[/bold blue]"):
        try:
            fix = provider.analyze(prompt)
        except QuotaExceededError as exc:
            try:
                config_module.set_enabled(False)
                error_console.print(
                    f"\n[yellow]better-errors:[/yellow] provider quota/rate limit hit "
                    f"({exc}). Auto-disabled analysis so your commands keep running "
                    "normally. Run `better-errors enable` once you're ready again."
                )
            except ConfigError as save_exc:
                error_console.print(
                    f"\n[red]better-errors:[/red] quota exceeded and could not auto-disable: {save_exc}"
                )
            return
        except ProviderError as exc:
            error_console.print(f"\n[red]better-errors:[/red] analysis failed: {exc}")
            return

    _print_analysis(parsed, fix)

    if not fix.has_code_fix:
        return

    try:
        rendered = render_diff(fix.file_path, fix.original_code, fix.fixed_code)
    except PatchError as exc:
        error_console.print(f"\n[yellow]better-errors:[/yellow] {exc}")
        return

    if not rendered.has_changes:
        console.print("\n[dim]better-errors: proposed fix produces no changes; skipping.[/dim]")
        return

    _print_diff(rendered, fix)

    if not can_rerun and rerun_command is None:
        pass  # pipe mode without a known command is handled below via can_rerun

    approved = Confirm.ask("\nFix bug?", default=False)
    if not approved:
        console.print("[dim]No changes made.[/dim]")
        return

    try:
        backup_path = apply_fix(rendered)
    except PatchError as exc:
        error_console.print(f"[red]better-errors:[/red] failed to apply fix: {exc}")
        return

    console.print(f"[green]Applied fix.[/green] Backup saved to {backup_path}")
    console.print("[dim]Run `better-errors --undo` to revert.[/dim]")

    if can_rerun and rerun_command:
        rerun = Confirm.ask("Re-run command to verify?", default=True)
        if rerun:
            console.print()
            result = _stream_process(rerun_command)
            if result.exit_code == 0:
                console.print("\n[green]better-errors: command succeeded.[/green]")
            else:
                console.print(
                    f"\n[yellow]better-errors: command still exited with code {result.exit_code}.[/yellow]"
                )


def _run_wrapper_mode(command: list[str]) -> int:
    """Implement ``better-errors <command> [args...]``."""
    result = _stream_process(command)

    if result.exit_code != 0 and looks_like_traceback(result.combined):
        command_str = " ".join(command)
        _analyze_and_maybe_fix(
            command_str, result.combined, can_rerun=True, rerun_command=command
        )

    return result.exit_code


def _run_pipe_mode() -> int:
    """Implement ``<command> 2>&1 | better-errors``."""
    piped_text = sys.stdin.read()
    sys.stdout.write(piped_text)
    sys.stdout.flush()

    if not looks_like_traceback(piped_text):
        return 0

    _analyze_and_maybe_fix(
        "(piped input)", piped_text, can_rerun=False, rerun_command=None
    )
    return 1


def _run_init() -> int:
    try:
        config_module.run_init_wizard()
    except ConfigError as exc:
        error_console.print(f"[red]better-errors init:[/red] {exc}")
        return 1
    return 0


def _run_undo() -> int:
    try:
        restored = undo_last()
    except PatchError as exc:
        error_console.print(f"[red]better-errors --undo:[/red] {exc}")
        return 1
    console.print(f"[green]Restored[/green] {restored}")
    return 0


def _run_enable(enabled: bool) -> int:
    try:
        path = config_module.set_enabled(enabled)
    except ConfigError as exc:
        error_console.print(f"[red]better-errors:[/red] {exc}")
        return 1
    state = "enabled" if enabled else "disabled"
    console.print(f"[green]Analysis {state}.[/green] Saved to {path}")
    return 0


def _run_is_enabled() -> int:
    """Silent, exit-code-only check used by shell hooks (`--is-enabled`)."""
    return 0 if config_module.is_enabled() else 1


def _run_hook(args: list[str]) -> int:
    subcommand = args[0] if args else ""

    if subcommand == "install":
        try:
            ps_path = hook_module.install_powershell_hook()
            console.print(f"[green]Installed PowerShell hook[/green] in {ps_path}")
        except OSError as exc:
            error_console.print(f"[red]better-errors hook install:[/red] PowerShell hook failed: {exc}")

        if sys.platform == "win32":
            try:
                cmd_path = hook_module.install_cmd_hook()
                console.print(f"[green]Installed cmd.exe hook[/green] (autorun script: {cmd_path})")
            except HookError as exc:
                error_console.print(f"[red]better-errors hook install:[/red] cmd.exe hook failed: {exc}")
        console.print(
            "[dim]Open a new PowerShell or cmd.exe window for the hooks to take effect. "
            f"Hooked commands: {', '.join(hook_module.HOOKED_COMMANDS)}.[/dim]"
        )
        return 0

    if subcommand == "uninstall":
        removed_ps = hook_module.uninstall_powershell_hook()
        removed_cmd = hook_module.uninstall_cmd_hook() if sys.platform == "win32" else False
        if removed_ps or removed_cmd:
            console.print("[green]Removed better-errors shell hook(s).[/green]")
        else:
            console.print("[dim]No better-errors shell hooks were installed.[/dim]")
        return 0

    if subcommand == "status":
        ps_path = hook_module.powershell_profile_path()
        ps_installed = ps_path.is_file() and hook_module.MARKER_START in ps_path.read_text()
        console.print(f"PowerShell hook: {'[green]installed[/green]' if ps_installed else '[dim]not installed[/dim]'} ({ps_path})")
        if sys.platform == "win32":
            cmd_installed = hook_module.cmd_autorun_script_path().is_file()
            console.print(f"cmd.exe hook:    {'[green]installed[/green]' if cmd_installed else '[dim]not installed[/dim]'}")
        console.print(f"Analysis is currently: {'[green]enabled[/green]' if config_module.is_enabled() else '[yellow]disabled[/yellow]'}")
        return 0

    error_console.print("[red]better-errors hook:[/red] expected 'install', 'uninstall', or 'status'.")
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Dispatches to wrapper/pipe/init/undo modes.

    Args:
        argv: Argument vector, excluding the program name. Defaults to
            ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] in ("-h", "--help"):
        console.print(_USAGE)
        return 0

    if args and args[0] == "init":
        return _run_init()

    if args and args[0] == "--undo":
        return _run_undo()

    if args and args[0] == "enable":
        return _run_enable(True)

    if args and args[0] == "disable":
        return _run_enable(False)

    if args and args[0] == "--is-enabled":
        return _run_is_enabled()

    if args and args[0] == "hook":
        return _run_hook(args[1:])

    if args:
        return _run_wrapper_mode(args)

    if not sys.stdin.isatty():
        return _run_pipe_mode()

    console.print(_USAGE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
