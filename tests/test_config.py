"""Tests for better_errors.config: the enable/disable toggle and persistence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from better_errors import config as config_module


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONFIG_DIR/CONFIG_PATH at a throwaway directory for every test."""
    config_dir = tmp_path / "config"
    config_path = config_dir / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.delenv("BETTER_ERRORS_ENABLED", raising=False)
    monkeypatch.delenv("BETTER_ERRORS_PROVIDER", raising=False)
    monkeypatch.delenv("BETTER_ERRORS_MODEL", raising=False)
    monkeypatch.delenv("BETTER_ERRORS_API_KEY", raising=False)
    monkeypatch.delenv("BETTER_ERRORS_BASE_URL", raising=False)
    return config_path


def test_is_enabled_defaults_true_with_no_config_file() -> None:
    assert config_module.is_enabled() is True


def test_set_enabled_false_persists_and_is_read_back() -> None:
    config_module.set_enabled(False)
    assert config_module.is_enabled() is False


def test_set_enabled_true_after_false_round_trips() -> None:
    config_module.set_enabled(False)
    assert config_module.is_enabled() is False
    config_module.set_enabled(True)
    assert config_module.is_enabled() is True


def test_env_var_overrides_file_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    config_module.set_enabled(True)
    monkeypatch.setenv("BETTER_ERRORS_ENABLED", "0")
    assert config_module.is_enabled() is False


def test_env_var_accepts_common_falsey_spellings(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("0", "false", "False", "no", "off"):
        monkeypatch.setenv("BETTER_ERRORS_ENABLED", value)
        assert config_module.is_enabled() is False
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("BETTER_ERRORS_ENABLED", value)
        assert config_module.is_enabled() is True


def test_set_enabled_preserves_existing_provider_section(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_module.Config(provider="groq", model="llama-3.1-8b-instant", api_key="gsk_test")
    config_module.save_config(cfg)

    config_module.set_enabled(False)

    loaded = config_module.load_config()
    assert loaded.provider == "groq"
    assert loaded.model == "llama-3.1-8b-instant"
    assert loaded.api_key == "gsk_test"
    assert loaded.enabled is False


def test_save_config_preserves_existing_general_section() -> None:
    config_module.set_enabled(False)

    cfg = config_module.Config(provider="openai", model="gpt-4o-mini", api_key="sk-test")
    config_module.save_config(cfg)

    assert config_module.is_enabled() is False


def test_load_config_reflects_enabled_flag_on_resolved_config() -> None:
    config_module.set_enabled(False)
    cfg = config_module.Config(provider="anthropic", model="claude-sonnet-4-5", api_key="sk-ant-test")
    config_module.save_config(cfg)

    loaded = config_module.load_config()
    assert loaded.enabled is False
