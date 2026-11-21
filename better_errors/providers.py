"""LLM provider adapters.

All adapters implement the same small interface (:class:`LLMProvider`) and
return a structured :class:`FixSuggestion` so the rest of the application
never has to care which backend produced it. Providers are responsible for
coaxing their respective APIs into returning well-formed JSON; parsing and
validation of that JSON is centralized in :func:`_parse_fix_json`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

#: Shared instruction block appended to every provider's prompt. Keeping it
#: in one place guarantees the two REST-based adapters and the
#: OpenAI-compatible adapter all ask the model for the same JSON contract.
_RESPONSE_CONTRACT = """
Respond with ONLY a single JSON object (no markdown fences, no commentary
outside the JSON) with exactly these keys:

{
  "root_cause": "2-4 plain-English sentences explaining the TRUE underlying bug",
  "misleading_note": "1-2 sentences on why the interpreter's own message/hint ' +
                       'could mislead someone, or an empty string if not applicable",
  "file_path": "absolute or relative path of the file to fix, or empty string if none",
  "original_code": "the exact original lines of code being replaced, verbatim",
  "fixed_code": "the corrected replacement lines of code",
  "explanation_of_fix": "1-3 sentences on what the fix changes and why it works"
}

Do not blindly trust the interpreter's own "Did you mean ...?" suggestions --
verify the actual variable, type, and import context yourself before deciding
on a root cause. If you cannot determine a safe, concrete code fix, set
"file_path", "original_code", and "fixed_code" to empty strings but still
provide "root_cause".
""".strip()

SYSTEM_PROMPT = (
    "You are an expert debugger embedded in a command-line tool called "
    "better-errors. You are given a program's command, its traceback, and "
    "surrounding source code. Your job is to identify the TRUE root cause "
    "of the failure in plain English, and propose a minimal, correct code "
    "fix.\n\n" + _RESPONSE_CONTRACT
)


class ProviderError(Exception):
    """Raised when a provider request fails or returns an unusable response."""


class QuotaExceededError(ProviderError):
    """Raised specifically when a provider reports rate-limit/quota exhaustion.

    Kept distinct from the generic :class:`ProviderError` so callers (the
    CLI, shell hooks) can react differently -- e.g. automatically disabling
    further analysis via ``better-errors disable`` rather than just
    printing an error for this one run.
    """


def _raise_for_status(response: httpx.Response, url: str) -> None:
    """Raise the appropriate error type for a non-2xx HTTP response.

    Status 429 (rate limited) and 402 (payment required, used by some
    providers for exhausted quota) are raised as :class:`QuotaExceededError`;
    everything else is a generic :class:`ProviderError`.
    """
    if response.status_code in (429, 402):
        raise QuotaExceededError(
            f"{url} reported rate-limit/quota exhaustion (HTTP {response.status_code}): "
            f"{response.text[:500]}"
        )
    if response.status_code >= 400:
        raise ProviderError(f"{url} returned HTTP {response.status_code}: {response.text[:500]}")


@dataclass(frozen=True)
class FixSuggestion:
    """A structured explanation + fix returned by an :class:`LLMProvider`.

    Attributes:
        root_cause: Plain-English explanation of the true root cause.
        misleading_note: Why the default interpreter message/hint might
            mislead a reader, or an empty string if not applicable.
        file_path: Path of the file to patch, or empty string if the model
            could not propose a concrete fix.
        original_code: Verbatim original lines being replaced.
        fixed_code: Proposed replacement lines.
        explanation_of_fix: Short note on why the fix works.
    """

    root_cause: str
    misleading_note: str
    file_path: str
    original_code: str
    fixed_code: str
    explanation_of_fix: str

    @property
    def has_code_fix(self) -> bool:
        """Whether the model proposed an actual, concrete code change."""
        return bool(self.file_path and self.original_code and self.fixed_code)


def _parse_fix_json(raw_text: str) -> FixSuggestion:
    """Parse a provider's raw text response into a :class:`FixSuggestion`.

    Tolerates responses wrapped in markdown code fences, since models
    sometimes ignore the "no markdown fences" instruction.

    Raises:
        ProviderError: If the text cannot be parsed as the expected JSON
            object.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Some models prepend prose before the JSON object despite instructions.
    # Fall back to extracting the first {...} balanced-looking span.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ProviderError(f"Provider response was not valid JSON: {raw_text[:200]!r}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Provider response was not valid JSON: {raw_text[:200]!r}") from exc

    if not isinstance(data, dict):
        raise ProviderError("Provider response JSON was not an object.")

    required_text_field = "root_cause"
    if required_text_field not in data or not str(data[required_text_field]).strip():
        raise ProviderError("Provider response was missing a 'root_cause' field.")

    return FixSuggestion(
        root_cause=str(data.get("root_cause", "")).strip(),
        misleading_note=str(data.get("misleading_note", "")).strip(),
        file_path=str(data.get("file_path", "")).strip(),
        original_code=str(data.get("original_code", "")),
        fixed_code=str(data.get("fixed_code", "")),
        explanation_of_fix=str(data.get("explanation_of_fix", "")).strip(),
    )


class LLMProvider(ABC):
    """Base class for all LLM backends used to analyze errors.

    Subclasses only need to implement :meth:`_complete`, which sends a
    system + user prompt pair and returns the raw text of the model's
    response. All JSON-contract parsing is shared via :meth:`analyze`.
    """

    def __init__(self, *, model: str, api_key: str = "", base_url: str | None = None, timeout: float = 60.0) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    @abstractmethod
    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        """Send a chat completion request and return the raw text reply."""
        raise NotImplementedError

    def analyze(self, user_prompt: str) -> FixSuggestion:
        """Request a root-cause analysis and fix suggestion from the model.

        Args:
            user_prompt: The fully-assembled, already-redacted prompt
                describing the error, stack, and surrounding source.

        Returns:
            A parsed :class:`FixSuggestion`.

        Raises:
            ProviderError: On network failure, API error, or malformed
                response.
        """
        try:
            raw_text = self._complete(SYSTEM_PROMPT, user_prompt)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Request to {self.__class__.__name__} failed: {exc}") from exc
        return _parse_fix_json(raw_text)


class OpenAICompatibleProvider(LLMProvider):
    """Adapter for any OpenAI-compatible chat-completions API.

    Covers OpenAI, OpenRouter, Groq, and local Ollama (via its OpenAI-
    compatible ``/v1`` endpoint) -- they differ only in ``base_url``,
    ``api_key``, and ``model``.
    """

    def __init__(self, *, model: str, api_key: str = "", base_url: str | None = None, timeout: float = 60.0) -> None:
        if not base_url:
            raise ProviderError("OpenAICompatibleProvider requires a base_url.")
        super().__init__(model=model, api_key=api_key, base_url=base_url, timeout=timeout)

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
        _raise_for_status(response, url)

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Unexpected response shape from {url}: {data!r}") from exc


class AnthropicProvider(LLMProvider):
    """Adapter for Anthropic's Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, *, model: str, api_key: str = "", base_url: str | None = None, timeout: float = 60.0) -> None:
        if not api_key:
            raise ProviderError("AnthropicProvider requires an api_key.")
        super().__init__(model=model, api_key=api_key, base_url=base_url, timeout=timeout)

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        url = self.base_url or self.API_URL
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "temperature": 0.1,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
        _raise_for_status(response, url)

        data = response.json()
        try:
            blocks = data["content"]
            text_blocks = [b["text"] for b in blocks if b.get("type") == "text"]
            return "".join(text_blocks)
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected response shape from {url}: {data!r}") from exc


class GeminiProvider(LLMProvider):
    """Adapter for Google's Gemini API (REST ``generateContent`` endpoint)."""

    API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, *, model: str, api_key: str = "", base_url: str | None = None, timeout: float = 60.0) -> None:
        if not api_key:
            raise ProviderError("GeminiProvider requires an api_key.")
        super().__init__(model=model, api_key=api_key, base_url=base_url, timeout=timeout)

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        url = (self.base_url or self.API_URL_TEMPLATE.format(model=self.model))
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
        _raise_for_status(response, url)

        data = response.json()
        try:
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected response shape from {url}: {data!r}") from exc


#: Maps provider name (from config) to the adapter class that implements it.
PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "openai": OpenAICompatibleProvider,
    "openrouter": OpenAICompatibleProvider,
    "groq": OpenAICompatibleProvider,
    "ollama": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def build_provider(
    *, provider: str, model: str, api_key: str = "", base_url: str | None = None
) -> LLMProvider:
    """Construct the correct :class:`LLMProvider` subclass for ``provider``.

    Args:
        provider: Provider name, e.g. ``"openai"``, ``"anthropic"``.
        model: Model identifier to request.
        api_key: API key/secret, if required by the provider.
        base_url: Override base URL (required for OpenAI-compatible
            providers; optional override for the others).

    Returns:
        A ready-to-use :class:`LLMProvider` instance.

    Raises:
        ProviderError: If ``provider`` is not recognized.
    """
    cls = PROVIDER_REGISTRY.get(provider.lower())
    if cls is None:
        known = ", ".join(sorted(PROVIDER_REGISTRY))
        raise ProviderError(f"Unknown provider '{provider}'. Known providers: {known}")
    return cls(model=model, api_key=api_key, base_url=base_url)
