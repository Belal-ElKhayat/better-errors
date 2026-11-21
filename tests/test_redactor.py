"""Tests for better_errors.redactor."""

from __future__ import annotations

from better_errors.redactor import redact_secrets


def test_redacts_openai_style_key() -> None:
    text = "key = sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    result = redact_secrets(text)
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz" not in result
    assert "[REDACTED]" in result


def test_redacts_bearer_token_preserving_prefix() -> None:
    text = 'headers = {"Authorization": "Bearer abcdef1234567890ABCDEF"}'
    result = redact_secrets(text)
    assert "abcdef1234567890ABCDEF" not in result
    assert "Bearer [REDACTED]" in result


def test_redacts_aws_access_key_id() -> None:
    text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    result = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result


def test_redacts_aws_secret_access_key() -> None:
    text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    result = redact_secrets(text)
    assert "wJalrXUtnFEMI" not in result
    assert "[REDACTED]" in result


def test_redacts_google_api_key() -> None:
    text = "GOOGLE_API_KEY=AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"
    result = redact_secrets(text)
    assert "AIzaSyD" not in result


def test_redacts_github_token() -> None:
    text = "token: ghp_1234567890abcdefghijklmnopqrstuvwxyz"
    result = redact_secrets(text)
    assert "ghp_1234567890" not in result


def test_redacts_database_url_credentials_but_keeps_host_and_db() -> None:
    text = "DATABASE_URL=postgres://myuser:sup3rSecret@db.example.com:5432/mydb"
    result = redact_secrets(text)
    assert "myuser:sup3rSecret" not in result
    assert "db.example.com:5432/mydb" in result
    assert "postgres://[REDACTED]@db.example.com:5432/mydb" in result


def test_redacts_env_style_secret_assignment() -> None:
    text = 'STRIPE_SECRET_KEY="sk_live_abcdefghijklmnopqrstuvwx"'
    result = redact_secrets(text)
    assert "abcdefghijklmnopqrstuvwx" not in result


def test_redacts_pem_private_key_block() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1c7+9z5Pad7OejecsQ0bu3aumgIJYRcYQxqwI9W86I2d\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    result = redact_secrets(text)
    assert "MIIEpAIBAAKCAQEA1c7" not in result
    assert "[REDACTED]" in result


def test_redacts_jwt() -> None:
    text = (
        "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    result = redact_secrets(text)
    assert "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" not in result


def test_does_not_redact_ordinary_text() -> None:
    text = "The function compute() raised a KeyError on line 42 of app.py."
    result = redact_secrets(text)
    assert result == text


def test_does_not_redact_short_harmless_assignment() -> None:
    text = "DEBUG = True\ncount = 5\nname = 'app'"
    result = redact_secrets(text)
    assert result == text


def test_empty_string_is_returned_unchanged() -> None:
    assert redact_secrets("") == ""


def test_redaction_is_idempotent_on_already_redacted_text() -> None:
    text = "key = sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    once = redact_secrets(text)
    twice = redact_secrets(once)
    assert once == twice
