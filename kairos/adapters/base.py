"""Kairos adapters base — shared types, security guards, and the ModelAdapter Protocol.

Provides:
- TokenUsage: token count and optional cost metadata from a model call.
- ModelResponse: normalized response from any LLM provider.
- ModelAdapter: Protocol that all adapter classes must satisfy.
- validate_no_inline_api_key: raises SecurityError if credential kwargs are present.
- enforce_https: raises SecurityError for non-HTTPS remote URLs.
- wrap_provider_exception: sanitizes and wraps provider exceptions as ExecutionError.

Security contracts (ADR-016, S14, S15):
- API keys may only come from environment variables, never from kwargs.
- ModelResponse never carries credential fields.
- Provider exceptions are sanitized via sanitize_exception() before wrapping.
- Exception chains are suppressed (raise ... from None) so the raw exception
  (which may contain credentials) cannot be reached via __cause__.
- HTTPS is enforced for all remote base_url values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from kairos.exceptions import ExecutionError, SecurityError
from kairos.security import sanitize_exception

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CREDENTIAL_KWARG_NAMES: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "secret_key",
        # FIX 5: expanded credential kwarg names (code review / security audit)
        "token",
        "password",
        "auth_token",
        "access_token",
        "bearer_token",
        "auth",
    }
)

# Hostname values considered "localhost" for allow_localhost enforcement.
_LOCALHOST_HOSTNAMES: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Token count metadata returned by a model call.

    Attributes:
        input_tokens: Number of tokens in the prompt sent to the model.
        output_tokens: Number of tokens in the model's response.
        total_tokens: Sum of input and output tokens.
        estimated_cost_usd: Optional estimated cost in US dollars. Defaults
            to None — Kairos does not perform cost estimation in v0.1
            (ADR-015: no token budgeting in v0.1).
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None = None


@dataclass
class ModelResponse:
    """Normalized response from any LLM provider adapter.

    This dataclass is the single currency exchanged between adapters and the
    rest of the SDK. It deliberately has no credential fields (S15).

    Attributes:
        text: The model's text output.
        model: The model identifier string (e.g. "claude-sonnet-4-20250514").
        usage: Token usage metadata.
        latency_ms: Wall-clock time for the API call in milliseconds.
        raw: The raw provider response object, kept for debugging. It is NOT
            included in to_dict() because it may not be JSON-serializable and
            may contain provider-internal data.

            WARNING (FIX 6 / S15): This field must NEVER be stored in the
            Kairos StateStore, written to any log sink, or serialized to JSON.
            It may contain provider-internal data including auth headers or
            session tokens. Use to_dict() exclusively for serialization.
        metadata: Arbitrary JSON-safe key/value pairs for caller use.
    """

    text: str
    model: str
    usage: TokenUsage
    latency_ms: float
    raw: object = None
    metadata: dict[str, Any] = field(default_factory=lambda: {})

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict.

        The ``raw`` field is intentionally excluded — it is not guaranteed to
        be JSON-serializable and may expose provider internals.

        Returns:
            A dict containing text, model, latency_ms, usage (as a sub-dict),
            and metadata. The raw field is always omitted.
        """
        return {
            "text": self.text,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.total_tokens,
                "estimated_cost_usd": self.usage.estimated_cost_usd,
            },
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelAdapter(Protocol):
    """Protocol that all Kairos model adapters must satisfy.

    Adapters normalize provider-specific responses into ``ModelResponse``
    objects. They are thin translation layers — no retry logic, no validation,
    no state management (ADR-012).

    ``call_async`` raises ``NotImplementedError`` in v0.1 adapters. Kairos is
    sync-first (ADR-003). Async support is deferred.
    """

    def call(self, prompt: str, **kwargs: Any) -> ModelResponse:
        """Execute a synchronous model call.

        Args:
            prompt: The formatted prompt string to send to the model.
            **kwargs: Provider-specific parameters (temperature, max_tokens, …).
                Credential kwargs (api_key, etc.) are always rejected by
                validate_no_inline_api_key() before this method is called.

        Returns:
            A normalized ModelResponse.
        """
        ...

    async def call_async(self, prompt: str, **kwargs: Any) -> ModelResponse:
        """Execute an asynchronous model call.

        Args:
            prompt: The formatted prompt string.
            **kwargs: Provider-specific parameters.

        Returns:
            A normalized ModelResponse.

        Raises:
            NotImplementedError: In v0.1 adapters. Sync-first per ADR-003.
        """
        ...


# ---------------------------------------------------------------------------
# Security guards
# ---------------------------------------------------------------------------


def validate_no_inline_api_key(**kwargs: Any) -> None:
    """Raise SecurityError if any credential kwarg is present.

    This is the enforcement point for ADR-016 / S14: API keys must come from
    environment variables only. Any kwarg whose name is in
    ``_CREDENTIAL_KWARG_NAMES`` is treated as an attempt to pass a credential
    inline and is rejected immediately.

    Args:
        **kwargs: Keyword arguments to inspect (typically from an adapter's
            ``__init__`` or ``call`` method).

    Raises:
        SecurityError: When any kwarg name matches a known credential name.
            The error message names the offending kwarg.
    """
    for name in kwargs:
        if name in _CREDENTIAL_KWARG_NAMES:
            raise SecurityError(
                f"Inline credential detected: '{name}' must not be passed as a kwarg. "
                "Read credentials exclusively from environment variables. "
                "(ADR-016 / Security requirement S14)"
            )


def enforce_https(base_url: str | None, *, allow_localhost: bool = False) -> None:
    """Raise SecurityError if base_url is not HTTPS (for remote URLs).

    None is always accepted — it means "use the provider's default endpoint",
    which is always HTTPS.

    HTTPS is enforced to prevent credential interception in transit (S15).
    Localhost is exempt when allow_localhost=True, which is intended for
    local-only models such as Ollama.

    Args:
        base_url: The base URL to validate, or None.
        allow_localhost: When True, HTTP to localhost / 127.0.0.1 / ::1 is
            permitted. Defaults to False.

    Raises:
        SecurityError: When base_url is a non-HTTPS remote URL, or an HTTP
            localhost URL when allow_localhost=False.
    """
    if base_url is None:
        return

    lower = base_url.lower()

    if lower.startswith("https://"):
        return  # Always acceptable

    if lower.startswith("http://"):
        # FIX 4: use urlparse to extract the exact hostname — substring matching
        # would incorrectly treat "http://localhost.evil.com" as localhost because
        # "localhost" appears as a substring of the hostname.
        parsed = urlparse(base_url)
        hostname = (parsed.hostname or "").lower()
        is_localhost = hostname in _LOCALHOST_HOSTNAMES

        if is_localhost and allow_localhost:
            return
        raise SecurityError(
            f"HTTPS is required for remote adapter base URLs. "
            f"Got: {base_url!r}. Use HTTPS or set allow_localhost=True for local models. "
            "(ADR-016 / Security requirement S15)"
        )

    # Any other scheme (ftp, ws, etc.) is always rejected.
    raise SecurityError(
        f"Unsupported URL scheme in base_url: {base_url!r}. "
        "Only HTTPS (or HTTP localhost with allow_localhost=True) is permitted. "
        "(ADR-016 / Security requirement S15)"
    )


def wrap_provider_exception(exc: Exception, *, adapter_name: str) -> ExecutionError:
    """Sanitize and wrap a provider SDK exception as an ExecutionError.

    This function is the security boundary between provider SDKs and the
    Kairos execution engine. It:

    1. Calls ``sanitize_exception()`` to redact credentials and strip paths.
    2. Builds a structured message that names the adapter but contains only
       the sanitized error content.
    3. Returns the ExecutionError — it does NOT raise, so the caller controls
       whether to raise or return the error.

    The returned ExecutionError has ``__cause__ = None`` and
    ``__context__ = None``. This is achieved via ``raise ... from None``
    semantics applied to the construction: the raw exception is never chained,
    preventing callers from reaching the unsanitized original via
    ``__cause__`` or ``__context__``.

    Args:
        exc: The raw exception from the provider SDK.
        adapter_name: Human-readable adapter name for the error message
            (e.g. "claude", "openai").

    Returns:
        An ExecutionError with a sanitized message. __cause__ is None.
    """
    error_type, sanitized_message = sanitize_exception(exc)
    message = f"[{adapter_name}] {error_type}: {sanitized_message}"

    # Build the ExecutionError and suppress the exception chain so the raw
    # exception (which may contain credentials) is not reachable via __cause__.
    wrapped = ExecutionError(message)
    wrapped.__cause__ = None
    wrapped.__context__ = None
    return wrapped
