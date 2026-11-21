"""Configuration loading, environment overrides, and the `init` wizard.

Config precedence (highest to lowest):

1. Environment variables: ``BETTER_ERRORS_PROVIDER``, ``BETTER_ERRORS_MODEL``,
   ``BETTER_ERRORS_API_KEY``, ``BETTER_ERRORS_BASE_URL``.
2. ``~/.config/better-errors/config.toml``.
3. Built-in defaults (none for provider/api_key -- those must be set by
   the user via ``better-errors init`` or environment variables).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python <3.11 fallback
    import tomli as tomllib

CONFIG_DIR = Path(os.environ.get("BETTER_ERRORS_CONFIG_DIR", "~/.config/better-errors")).expanduser()
CONFIG_PATH = CONFIG_DIR / "config.toml"

DATA_DIR = Path(os.environ.get("BETTER_ERRORS_DATA_DIR", "~/.local/share/better-errors")).expanduser()
BACKUP_DIR = DATA_DIR / "backups"

#: Known providers and a representative default model for each, shown
#: during `init` and used as a fallback if the user doesn't pick one.
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openrouter": "anthropic/claude-3.5-sonnet",
    "groq": "llama-3.3-70b-versatile",
    "ollama": "llama3.1",
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.0-flash",
}

#: Providers that speak the OpenAI-compatible chat-completions protocol,
#: distinguished only by base_url/default model. See providers.py.
OPENAI_COMPATIBLE_PROVIDERS = {"openai", "openrouter", "groq", "ollama"}

_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Resolved configuration used to construct an LLM provider.

    Attributes:
        provider: One of the keys in :data:`PROVIDER_DEFAULT_MODELS`.
        model: Model identifier to request from the provider.
        api_key: Secret API key (empty string is valid for local Ollama).
        base_url: Override for the provider's API base URL.
    """

    provider: str
    model: str
    api_key: str = ""
    base_url: str | None = None
    enabled: bool = True

    def resolved_base_url(self) -> str | None:
        """Return the effective base URL, falling back to provider defaults."""
        if self.base_url:
            return self.base_url
        return _DEFAULT_BASE_URLS.get(self.provider)


def _load_toml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _toml_escape(value: str) -> str:
    """Escape a string for use inside a double-quoted TOML string."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f'"{_toml_escape(str(value))}"'


def _dump_toml(data: dict[str, dict[str, Any]]) -> str:
    """Serialize a two-level ``{section: {key: scalar}}`` mapping to TOML text.

    This project's config file only ever has this shape (a handful of flat
    ``[section]`` tables with string/bool values), so a hand-rolled writer
    avoids an extra third-party dependency (``tomli_w``) just to write back
    what is otherwise a very small file. Not a general-purpose TOML writer.
    """
    lines: list[str] = []
    for section, values in data.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_scalar(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_config(*, require_api_key: bool = True) -> Config:
    """Load configuration from file, then apply environment overrides.

    Args:
        require_api_key: If True (default), raise :class:`ConfigError` when
            no API key is resolved and the provider isn't a local one that
            doesn't need one (Ollama).

    Returns:
        A fully resolved :class:`Config`.

    Raises:
        ConfigError: If no provider could be determined, or if an API key
            is required but missing.
    """
    file_data = _load_toml_file(CONFIG_PATH)
    provider_section: dict[str, Any] = file_data.get("provider", {})
    general_section: dict[str, Any] = file_data.get("general", {})

    provider = os.environ.get("BETTER_ERRORS_PROVIDER") or provider_section.get("name")
    model = os.environ.get("BETTER_ERRORS_MODEL") or provider_section.get("model")
    api_key = os.environ.get("BETTER_ERRORS_API_KEY") or provider_section.get("api_key") or ""
    base_url = os.environ.get("BETTER_ERRORS_BASE_URL") or provider_section.get("base_url")

    env_enabled = os.environ.get("BETTER_ERRORS_ENABLED")
    if env_enabled is not None:
        enabled = env_enabled.strip().lower() not in ("0", "false", "no", "off")
    else:
        enabled = bool(general_section.get("enabled", True))

    if not provider:
        raise ConfigError(
            "No provider configured. Run `better-errors init` to set one up, "
            "or set the BETTER_ERRORS_PROVIDER environment variable."
        )

    provider = provider.lower()
    if not model:
        model = PROVIDER_DEFAULT_MODELS.get(provider)
    if not model:
        raise ConfigError(
            f"No model configured for provider '{provider}'. Set BETTER_ERRORS_MODEL "
            "or re-run `better-errors init`."
        )

    if require_api_key and not api_key and provider != "ollama":
        raise ConfigError(
            f"No API key configured for provider '{provider}'. Run `better-errors init`, "
            "or set the BETTER_ERRORS_API_KEY environment variable."
        )

    return Config(provider=provider, model=model, api_key=api_key, base_url=base_url, enabled=enabled)


def is_enabled() -> bool:
    """Cheaply check whether analysis is enabled, without requiring full config.

    Used by shell hooks and the CLI's pass-through check so that a missing
    or incomplete provider config doesn't prevent this lightweight check.
    Honors the same precedence as :func:`load_config`: the
    ``BETTER_ERRORS_ENABLED`` environment variable, then the config file's
    ``[general] enabled`` key, then the default of ``True``.
    """
    env_enabled = os.environ.get("BETTER_ERRORS_ENABLED")
    if env_enabled is not None:
        return env_enabled.strip().lower() not in ("0", "false", "no", "off")

    file_data = _load_toml_file(CONFIG_PATH)
    general_section: dict[str, Any] = file_data.get("general", {})
    return bool(general_section.get("enabled", True))


def set_enabled(enabled: bool) -> Path:
    """Persist the enabled/disabled toggle to the config file's [general] section.

    Preserves any existing ``[provider]`` section untouched. If no config
    file exists yet, creates one with only the ``[general]`` section set --
    a provider can be configured later via ``better-errors init``.

    Returns:
        The path the config was written to.
    """
    file_data = _load_toml_file(CONFIG_PATH)
    file_data["general"] = {**file_data.get("general", {}), "enabled": enabled}

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_dump_toml(file_data))
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass

    return CONFIG_PATH


def save_config(config: Config) -> Path:
    """Persist ``config`` to :data:`CONFIG_PATH` in TOML format.

    Returns:
        The path the config was written to.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_toml_file(CONFIG_PATH)
    payload: dict[str, Any] = {
        "general": existing.get("general", {"enabled": config.enabled}),
        "provider": {
            "name": config.provider,
            "model": config.model,
            "api_key": config.api_key,
        },
    }
    if config.base_url:
        payload["provider"]["base_url"] = config.base_url

    CONFIG_PATH.write_text(_dump_toml(payload))

    # Config contains a secret API key -- keep it readable only by the owner.
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass

    return CONFIG_PATH


def run_init_wizard(
    *,
    input_fn: Any = input,
    print_fn: Any = print,
) -> Config:
    """Run the interactive `better-errors init` setup wizard.

    Args:
        input_fn: Injectable input function, for testing.
        print_fn: Injectable print function, for testing.

    Returns:
        The :class:`Config` that was saved.
    """
    providers = list(PROVIDER_DEFAULT_MODELS.keys())

    print_fn("better-errors setup")
    print_fn("====================")
    print_fn("Choose an LLM provider:")
    for idx, name in enumerate(providers, start=1):
        print_fn(f"  {idx}. {name}")

    choice = input_fn(f"Provider [1-{len(providers)}]: ").strip()
    try:
        provider = providers[int(choice) - 1]
    except (ValueError, IndexError) as exc:
        raise ConfigError(f"Invalid provider choice: {choice!r}") from exc

    default_model = PROVIDER_DEFAULT_MODELS[provider]
    model = input_fn(f"Model [{default_model}]: ").strip() or default_model

    api_key = ""
    if provider != "ollama":
        api_key = input_fn(f"API key for {provider}: ").strip()
        if not api_key:
            raise ConfigError("An API key is required for this provider.")

    base_url = ""
    if provider == "ollama":
        default_base = _DEFAULT_BASE_URLS["ollama"]
        base_url = input_fn(f"Ollama base URL [{default_base}]: ").strip() or default_base

    config = Config(provider=provider, model=model, api_key=api_key, base_url=base_url or None)
    path = save_config(config)
    print_fn(f"\nSaved configuration to {path}")
    print_fn(f"Provider: {config.provider}  Model: {config.model}")
    return config


def with_overrides(config: Config, **overrides: Any) -> Config:
    """Return a copy of ``config`` with the given fields overridden."""
    return replace(config, **overrides)
