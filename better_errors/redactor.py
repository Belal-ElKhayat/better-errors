"""Secret and credential redaction.

Anything captured from a user's terminal (commands, tracebacks, source
snippets) may contain API keys, tokens, or credentials embedded in URLs.
Before any of that text is sent to an external LLM provider, it is run
through :func:`redact_secrets`, which replaces recognizable sensitive
substrings with ``[REDACTED]`` while leaving the rest of the text intact
so the error context still makes sense to a model.

The patterns here favor recall over precision: it is far worse to leak a
credential than to over-redact a value that merely looks like one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MASK = "[REDACTED]"


@dataclass(frozen=True)
class _Rule:
    """A single redaction rule.

    Attributes:
        name: Human-readable identifier, useful for debugging/tests.
        pattern: Compiled regex. If it defines a group named ``secret``,
            only that group is replaced; otherwise the whole match is
            replaced.
    """

    name: str
    pattern: re.Pattern[str]


def _rule(name: str, pattern: str, flags: int = 0) -> _Rule:
    return _Rule(name=name, pattern=re.compile(pattern, flags))


_RULES: list[_Rule] = [
    # OpenAI-style secret keys: sk-..., sk-proj-..., sk-ant-...
    _rule("openai_style_key", r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{16,}\b"),
    # Anthropic API keys
    _rule("anthropic_key", r"\bsk-ant-api\d{2}-[A-Za-z0-9_\-]{10,}\b"),
    # Generic "Bearer <token>" authorization headers.
    _rule("bearer_token", r"(?i)\bBearer\s+(?P<secret>[A-Za-z0-9\-._~+/]{8,}=*)"),
    # Generic "Authorization: <scheme> <token>" headers (non-bearer schemes).
    _rule(
        "authorization_header",
        r"(?i)\bAuthorization\s*:\s*(?P<secret>\S+\s+\S+)",
    ),
    # AWS access key IDs.
    _rule("aws_access_key_id", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # AWS secret access keys when explicitly labeled.
    _rule(
        "aws_secret_key",
        r"(?i)\baws_secret_access_key\s*=\s*(?P<secret>[A-Za-z0-9/+=]{30,})",
    ),
    # Google API keys.
    _rule("google_api_key", r"\bAIza[A-Za-z0-9_\-]{30,}\b"),
    # Google OAuth / service account style tokens.
    _rule("google_oauth_token", r"\bya29\.[A-Za-z0-9_\-]{20,}\b"),
    # GCP service-account "private_key" fields (JSON key files).
    _rule(
        "gcp_private_key",
        r'"private_key"\s*:\s*"(?P<secret>-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----)"',
        re.DOTALL,
    ),
    # GitHub tokens (classic and fine-grained).
    _rule("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # Slack tokens.
    _rule("slack_token", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    # Stripe keys.
    _rule("stripe_key", r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    # Generic PEM private key blocks (SSH, TLS, etc.).
    _rule(
        "pem_private_key",
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Database connection strings with embedded credentials, e.g.
    # postgres://user:pass@host:5432/db, mysql://..., mongodb+srv://...
    _rule(
        "db_connection_string",
        r"(?i)\b(?P<scheme>postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)://"
        r"(?P<secret>[^\s:@/]+:[^\s@/]+)@",
    ),
    # Generic "key=value" / "key: value" secrets for common env var names,
    # e.g. API_KEY=..., SECRET_KEY: ..., PASSWORD=...
    _rule(
        "env_style_secret",
        r"(?im)\b(?P<keyname>[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|ACCESS_KEY|PRIVATE_KEY|CLIENT_SECRET)[A-Z0-9_]*)"
        r"\s*[:=]\s*[\"']?(?P<secret>[^\s\"'#]{6,})[\"']?",
    ),
    # Generic JWTs (three base64url segments separated by dots).
    _rule(
        "jwt",
        r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b",
    ),
]


def redact_secrets(text: str) -> str:
    """Return ``text`` with recognizable secrets replaced by ``[REDACTED]``.

    This is intentionally conservative: every rule is applied independently
    and matches are replaced in a single left-to-right pass per rule, so
    overlapping concerns (e.g. a DB URL that also contains a bearer token)
    are each cleaned up.

    Args:
        text: Raw text that may contain secrets (command strings,
            tracebacks, source snippets, environment dumps, etc.).

    Returns:
        A copy of ``text`` with sensitive substrings replaced by
        ``[REDACTED]``. Non-sensitive text is left byte-for-byte identical.
    """
    if not text:
        return text

    redacted = text
    for rule in _RULES:
        redacted = _apply_rule(redacted, rule)
    return redacted


def _apply_rule(text: str, rule: _Rule) -> str:
    def _replace(match: re.Match[str]) -> str:
        groups = match.groupdict()
        if "secret" in groups and groups["secret"] is not None:
            # Replace only the captured secret portion, preserving any
            # surrounding context (scheme, key name, "Bearer " prefix, etc.)
            start, end = match.span("secret")
            full = match.group(0)
            local_start = start - match.start()
            local_end = end - match.start()
            return full[:local_start] + _MASK + full[local_end:]
        return _MASK

    return rule.pattern.sub(_replace, text)


def redaction_rule_names() -> list[str]:
    """Return the names of all active redaction rules (for diagnostics/tests)."""
    return [rule.name for rule in _RULES]
