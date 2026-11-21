# better-errors

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Providers](https://img.shields.io/badge/providers-OpenAI%20%7C%20Groq%20%7C%20Anthropic%20%7C%20Gemini%20%7C%20Ollama-5a5a5a)](#supported-providers)
[![Platform](https://img.shields.io/badge/shell%20hooks-Windows-0078D6)](#shell-hooks-windows-powershell--cmdexe)

> Plain-English error explanations, reviewable diffs, and `y/N` patches — powered by whatever LLM API you already use.

`better-errors` catches runtime errors from your commands, explains the **real root cause** instead of blindly repeating misleading interpreter hints, previews a unified diff, and asks for your permission (`Fix bug? [y/N]`) before touching any file on disk.

<br>

<table>
<tr><td>

**📑 Contents** — [Why](#why-better-errors) · [Install](#installation) · [Quickstart](#quickstart) · [Shell Hooks](#shell-hooks-windows-powershell--cmdexe) · [Providers](#supported-providers) · [How it Works](#how-it-works--security) · [Structure](#project-structure) · [Dev](#development) · [Roadmap](#roadmap)

</td></tr>
</table>

```text
$ better-errors python app.py
Traceback (most recent call last):
  File "app.py", line 12, in <module>
    total = compute(order)
  File "app.py", line 7, in compute
    return order["total"]
KeyError: 'total'

╭─ Root Cause ─────────────────────────────────────────────────────────╮
│ `compute()` assumes every order dict has a "total" key, but orders   │
│ from the checkout flow do not set it until payment succeeds.         │
╰──────────────────────────────────────────────────────────────────────╯

╭─ Proposed Fix: app.py ───────────────────────────────────────────────╮
│ --- a/app.py                                                         │
│ +++ b/app.py                                                         │
│ @@ -5,4 +5,4 @@                                                      │
│  def compute(order):                                                 │
│ -    return order["total"]                                           │
│ +    return order.get("total", 0)                                    │
╰──────────────────────────────────────────────────────────────────────╯
Fix bug? [y/N]:
```

---

## Why `better-errors`?

Default tracebacks and built-in hints (like `Did you mean ...?`) often point you toward surface-level guesses rather than the actual cause. `better-errors` inspects the traceback and relevant local lines around the failure, redacts sensitive secrets before any API call, explains what actually went wrong, and lets you approve or reject the fix.

| | |
|---|---|
| 🧠 | **Real root cause**, not a restated error message |
| 🔍 | **Reviewable diff** before anything touches disk |
| 🔐 | **Secrets redacted** before any text leaves your machine |
| ↩️ | **One-command undo** via timestamped backups |
| 🔌 | **Bring your own provider** — OpenAI, Groq, Anthropic, Gemini, or fully local Ollama |

---

## Installation

`better-errors` is currently installed from source (requires **Python 3.10+**):

```bash
git clone https://github.com/Belal-ElKhayat/better-errors.git
cd better-errors
pip install -e .
```

> **If `better-errors` isn't recognized as a command after installing,** your Python Scripts
> directory isn't on PATH yet — this is a common first-run issue on Windows, not a broken
> install. Every command in this README works exactly the same with `python -m better_errors.cli`
> in place of `better-errors`:
>
> ```bash
> python -m better_errors.cli init
> python -m better_errors.cli python app.py
> ```
>
> To make the short `better-errors` form work instead, add the folder pip warned about (shown
> in the `pip install` output as *"The script better-errors.exe is installed in '...'"*) to your
> PATH, then open a **new** terminal window. On Windows: `Win` → search **"Environment Variables"**
> → **Edit the system environment variables** → **Environment Variables** → under **User
> variables**, select `Path` → **Edit** → **New** → paste that folder → OK everywhere.

---

## Quickstart

### 1. Set up your provider
```bash
better-errors init
```
Choose your provider, model, and API key. Saved to `~/.config/better-errors/config.toml`.

> **Heads up on model names:** Model availability depends on your provider account and tier. If a default or selected model returns `404` or a model-not-found error, switch to an active model ID available on your account (e.g. for Groq, check `https://api.groq.com/openai/v1/models` with your key).

### 2. Run a command or pipe output
```bash
# Wrapper mode (streams output live; analyzes on non-zero exit / error)
better-errors python app.py
better-errors pytest

# Pipe mode
python app.py 2>&1 | better-errors
```

### 3. Control analysis & undo fixes
```bash
better-errors disable   # bypass analysis while running commands normally
better-errors enable    # re-enable analysis
better-errors --undo    # restore the most recent backed-up file
```

---

## Shell Hooks (Windows PowerShell & `cmd.exe`)

Want errors caught automatically without typing `better-errors` every time? You can hook common dev commands (`python`, `python3`, `node`, `npm`, `npx`, `pytest`, `go`, `cargo`, `java`, `ruby`):

```powershell
better-errors hook install     # install PowerShell and cmd.exe hooks
better-errors hook status      # check hook status and whether analysis is enabled
better-errors hook uninstall   # remove hooks cleanly
```

Open a new terminal window after installing so the hook loads. Toggle analysis anytime with `better-errors disable` and `better-errors enable`. If a provider returns a rate-limit/quota error (`429` or `402`), `better-errors` automatically disables itself so your workflow isn't blocked until you run `better-errors enable`.

This hooks a fixed, explicit list of commands (see `HOOKED_COMMANDS` in `hook.py`) — it is not a
system-wide catch-all for every possible executable, since neither shell can do that safely.
Anything not on that list (PHP, C/C++ compilers, .NET, build tools, etc.) runs completely
unmodified.

---

## Supported Providers

Configure via `better-errors init`, `~/.config/better-errors/config.toml`, or environment variables:

| Provider | How It Connects | Notes |
| :--- | :--- | :--- |
| `openai` | OpenAI-compatible Chat Completions | Supports custom `base_url` |
| `openrouter` | OpenAI-compatible | Access many hosted models via one key |
| `groq` | OpenAI-compatible | Fast inference; verify model ID on your account |
| `ollama` | OpenAI-compatible (local) | Runs locally with no API key required |
| `anthropic` | Native Messages API | Claude models via `x-api-key` |
| `gemini` | Native `generateContent` API | Google Gemini models |

### Environment Variable Overrides
```bash
export BETTER_ERRORS_PROVIDER=openai
export BETTER_ERRORS_MODEL=gpt-4o-mini
export BETTER_ERRORS_API_KEY=sk-...
export BETTER_ERRORS_BASE_URL=https://api.openai.com/v1
```

---

## How It Works & Security

1. **Capture:** Streams command output live in wrapper mode or reads piped input (`stdin`).
2. **Parse (`parsers.py`):** Extracts the exception, message, and local stack frames (skipping stdlib and `site-packages` noise) with ~±20 lines of context.
3. **Redact (`redactor.py`):** Scrubs API keys, Bearer tokens, AWS/GCP credentials, database URLs with passwords, private keys, JWTs, and secret assignments with `[REDACTED]` before sending data to an LLM. *(Always double-check sensitive codebases, and use `ollama` if you want zero external calls.)*
4. **Analyze & Diff (`providers.py`, `patcher.py`):** Separates the plain-English root cause from the code patch, shows a unified diff against the current file, and prompts `Fix bug? [y/N]`.
5. **Safe Apply & Backup:** Saves a timestamped backup before applying changes atomically, and offers to re-run your command to verify.

---

## Project Structure

```text
better-errors/
├── better_errors/
│   ├── __init__.py
│   ├── cli.py          # Entrypoint, subprocess/pipe runner, interactive prompts
│   ├── config.py       # Config loading, env overrides, enable/disable state, `init` wizard
│   ├── parsers.py      # Traceback parser & local code snippet extractor
│   ├── providers.py    # Adapters for OpenAI, OpenRouter, Groq, Ollama, Anthropic, Gemini
│   ├── redactor.py     # Secret & credential scrubber
│   ├── patcher.py      # Diff preview, atomic file writer, backup & `--undo`
│   └── hook.py         # PowerShell & `cmd.exe` hook manager
├── tests/
│   ├── test_parsers.py
│   ├── test_redactor.py
│   ├── test_patcher.py
│   └── test_config.py
├── pyproject.toml
└── README.md
```

---

## Development

```bash
pip install -e ".[dev]"
pytest
pytest --cov=better_errors --cov-report=term-missing
```

---

## Roadmap

- Parsers for Node.js, Rust, and Go errors/panics
- Multi-file patch support
- `--dry-run` mode to inspect explanations and diffs without prompting to apply

---

## License

MIT — see [LICENSE](LICENSE).
