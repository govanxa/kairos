"""Kairos Gemini adapter — thin wrapper around the google-genai Python SDK.

This module provides:
- GeminiAdapter: a ModelAdapter-compliant class that calls the Google Gemini API.
- gemini(): a factory function that returns a step-action callable formatted
  from a prompt template.

Security contracts (ADR-016, S14, S15):
- API keys are read exclusively from GOOGLE_API_KEY env var, with fallback to
  GEMINI_API_KEY. Inline api_key raises SecurityError.
- Inline api_key (or any credential kwarg) in constructor or call() raises SecurityError.
- Provider exceptions are sanitized via wrap_provider_exception() before re-raising.
- Exception chains are suppressed (raise ... from None) so raw SDK exceptions
  (which may contain credentials) are never reachable via __cause__.
- HTTPS is enforced for any non-None base_url (unless localhost with allow_localhost).
- ModelResponse never carries credential fields.

ADR-003 (sync-first): call_async() raises NotImplementedError in v0.1.
ADR-012 (thin adapters): no retry, validation, or state logic here.
ADR-017 (optional deps): google-genai is imported at module level with try/except;
  the module still loads when the SDK is absent — only instantiation fails.

Naming note: this file is named gemini.py. The SDK is imported under the alias
'genai_sdk' to avoid potential shadowing issues and to make patching in tests
straightforward via "kairos.adapters.gemini.genai_sdk".
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, cast

from kairos.adapters.base import (
    ModelResponse,
    TokenUsage,
    enforce_https,
    validate_no_inline_api_key,
    wrap_provider_exception,
)
from kairos.exceptions import ConfigError
from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Optional SDK import (ADR-017)
# ---------------------------------------------------------------------------

# google-genai is an optional dependency. We set it to None when absent so the
# module loads cleanly. Instantiation raises ConfigError if it is None.
# The alias 'genai_sdk' is the patch target used in tests:
#   @patch("kairos.adapters.gemini.genai_sdk", ...)
try:
    from google import genai as genai_sdk  # type: ignore[import-not-found,unused-ignore]
except ImportError:
    genai_sdk = None  # type: ignore[assignment,unused-ignore]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENV_VAR: str = "GOOGLE_API_KEY"
_FALLBACK_ENV_VAR: str = "GEMINI_API_KEY"
_DEFAULT_MODEL: str = "gemini-2.0-flash"
_DEFAULT_TIMEOUT: float = 120.0

# ---------------------------------------------------------------------------
# GeminiAdapter
# ---------------------------------------------------------------------------


class GeminiAdapter:
    """Thin adapter wrapping the google-genai Python SDK.

    Normalizes Google Gemini API responses into ModelResponse objects. Contains
    no retry logic, no validation, and no state management (ADR-012).

    The google-genai Client constructor only accepts `api_key`. Parameters like
    `base_url` and `timeout` are stored on the adapter instance for future use
    or subclass extension, but are not passed to the Client constructor directly
    (the google-genai SDK does not expose these at the Client level in v1.x).

    Args:
        model: Gemini model identifier. Defaults to "gemini-2.0-flash".
        base_url: Custom API base URL. Must be HTTPS for remote hosts (S15).
            None uses the Google default endpoint. Note: the google-genai Client
            does not accept base_url directly; this parameter is validated for
            security (HTTPS enforcement) and stored on self.
        timeout: Request timeout in seconds. Defaults to 120.0. Stored on self.
        allow_localhost: When True, HTTP on localhost / 127.0.0.1 / ::1 is
            permitted. Intended for local model servers. Defaults to False
            (secure by default). Has no effect on remote hosts — remote URLs
            always require HTTPS regardless of this setting.
        **kwargs: Any additional keyword argument is checked for credential
            names (api_key, etc.) and raises SecurityError if found (S14).

    Raises:
        SecurityError: When any credential kwarg is provided (S14).
        ConfigError: When the google-genai SDK is not installed, or when
            neither GOOGLE_API_KEY nor GEMINI_API_KEY is set in the environment.
        SecurityError: When base_url is an HTTP remote URL (S15).
    """

    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        allow_localhost: bool = False,
        **kwargs: Any,
    ) -> None:
        # S14: reject any inline credential kwargs immediately
        validate_no_inline_api_key(**kwargs)

        # ADR-017: fail fast if the SDK was not installed
        if genai_sdk is None:
            raise ConfigError(
                "The 'google-genai' package is required for GeminiAdapter but is not installed. "
                "Install it with: pip install kairos-ai[gemini]  or  pip install google-genai"
            )

        # S15: enforce HTTPS for remote base URLs.
        # allow_localhost=True permits HTTP on loopback addresses for local models.
        enforce_https(base_url, allow_localhost=allow_localhost)

        # S14: read API key exclusively from environment, with GEMINI_API_KEY fallback
        api_key = os.environ.get(_ENV_VAR) or os.environ.get(_FALLBACK_ENV_VAR)
        if not api_key:
            raise ConfigError(
                f"{_ENV_VAR} (or fallback {_FALLBACK_ENV_VAR}) environment variable is not set. "
                "Model adapters read credentials exclusively from environment variables "
                "(ADR-016 / Security requirement S14)."
            )

        self.model: str = model
        self.timeout: float = timeout

        # Create the SDK client. The google-genai Client only accepts api_key.
        # base_url and timeout are not forwarded to the constructor — they are
        # stored on self for reference. The cast(Any, ...) silences Pylance's
        # "genai_sdk could be None" complaint (we've already guarded above).
        self._client: Any = cast(Any, genai_sdk).Client(api_key=api_key)

    # ------------------------------------------------------------------
    # Repr (security)
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a safe repr that exposes only the model name.

        The default object repr would include all instance attributes, including
        _client which is constructed with the API key. This custom repr prevents
        accidental credential exposure via repr() or logging (ADR-016 / S15).
        """
        return f"GeminiAdapter(model={self.model!r})"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def call(self, prompt: str, **kwargs: Any) -> ModelResponse:
        """Execute a synchronous model call against the Google Gemini API.

        Args:
            prompt: The formatted prompt string to send to the model.
            **kwargs: Provider-specific parameters forwarded to the SDK
                (e.g. temperature, generation_config). Credential kwargs
                are always rejected by validate_no_inline_api_key().

        Returns:
            A normalized ModelResponse.

        Raises:
            SecurityError: If any credential kwarg is present (S14).
            ExecutionError: If the SDK raises any exception (sanitized, S15).
        """
        # S14: reject credential kwargs on every call, not just construction
        validate_no_inline_api_key(**kwargs)

        start = time.monotonic()
        try:
            sdk_response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                **kwargs,
            )
        except Exception as exc:
            # S15: sanitize the provider exception before re-raising.
            # 'raise ... from None' suppresses the chain so the raw
            # exception (possibly containing credentials) is unreachable.
            raise wrap_provider_exception(exc, adapter_name="gemini") from None

        latency_ms = (time.monotonic() - start) * 1000.0

        # Normalize the SDK response into our ModelResponse dataclass.
        # The google-genai SDK response shape:
        #   response.text                                    → full text
        #   response.usage_metadata.prompt_token_count       → input tokens
        #   response.usage_metadata.candidates_token_count   → output tokens
        #   response.candidates[0].finish_reason.name        → finish reason
        #   response.model                                   → model identifier
        text = str(sdk_response.text)
        model_id = str(sdk_response.model)
        input_toks = int(sdk_response.usage_metadata.prompt_token_count)
        output_toks = int(sdk_response.usage_metadata.candidates_token_count)

        # finish_reason is an enum in the real SDK; .name gives the string name.
        if sdk_response.candidates:
            finish_reason = str(sdk_response.candidates[0].finish_reason.name)
        else:
            finish_reason = "UNKNOWN"

        usage = TokenUsage(
            input_tokens=input_toks,
            output_tokens=output_toks,
            total_tokens=input_toks + output_toks,
        )

        return ModelResponse(
            text=text,
            model=model_id,
            usage=usage,
            latency_ms=latency_ms,
            raw=sdk_response,
            metadata={"finish_reason": finish_reason},
        )

    async def call_async(self, prompt: str, **kwargs: Any) -> ModelResponse:
        """Async model call — not implemented in v0.1 (ADR-003, sync-first).

        Args:
            prompt: The prompt string.
            **kwargs: Provider-specific parameters.

        Raises:
            NotImplementedError: Always. Use call() for sync execution.
        """
        raise NotImplementedError(
            "GeminiAdapter.call_async() is not implemented in v0.1. "
            "Kairos is sync-first (ADR-003). Use call() instead."
        )


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def gemini(
    prompt_template: str,
    *,
    model: str = _DEFAULT_MODEL,
    allow_localhost: bool = False,
    **adapter_kwargs: Any,
) -> Callable[..., dict[str, Any]]:
    """Factory that returns a step-action callable backed by GeminiAdapter.

    This is the primary developer-facing interface for using Gemini in a step::

        Step(name="analyze", action=gemini("Analyze this data: {data}"))

    For local model servers on HTTP::

        Step(
            name="local",
            action=gemini(
                "Query: {q}", base_url="http://localhost:8080", allow_localhost=True
            ),
        )

    The factory:
    1. Validates kwargs for inline credentials (SecurityError if found).
    2. Creates the GeminiAdapter eagerly — validates credentials at definition
       time, not at step execution time.
    3. Returns a closure that formats the prompt_template with ctx.inputs
       (and ctx.item if not None) at execution time, calls the adapter, and
       returns ModelResponse.to_dict() as the step output.

    Template formatting uses str.format_map() — only keys present in ctx.inputs
    (plus "item" when ctx.item is not None) are substituted. This is safer than
    f-strings because it only accesses keys explicitly referenced in the template.

    Args:
        prompt_template: A str.format_map()-compatible template string.
            Reference step inputs as ``{key_name}`` and the foreach item
            as ``{item}``.
        model: Gemini model identifier. Defaults to "gemini-2.0-flash".
        allow_localhost: When True, HTTP on localhost / 127.0.0.1 / ::1 is
            permitted. Intended for local model servers. Defaults to False
            (secure by default).
        **adapter_kwargs: Additional kwargs forwarded to GeminiAdapter.__init__().
            Credential kwargs (api_key, etc.) are always rejected.

    Returns:
        A callable with signature ``(ctx: StepContext) -> dict[str, Any]``
        suitable for use as a Step action.

    Raises:
        SecurityError: If any credential kwarg is present (S14).
        ConfigError: If the google-genai SDK is not installed or neither
            GOOGLE_API_KEY nor GEMINI_API_KEY is set — detected eagerly at
            factory call time.
    """
    # S14: reject inline credentials in factory kwargs immediately
    validate_no_inline_api_key(**adapter_kwargs)

    # Create the adapter eagerly so credential errors surface at definition
    # time, not buried inside a workflow run.
    adapter = GeminiAdapter(model=model, allow_localhost=allow_localhost, **adapter_kwargs)

    def _action(ctx: StepContext) -> dict[str, Any]:
        """Step action closure — formats template and calls the Gemini API.

        Args:
            ctx: Runtime step context supplied by the executor.

        Returns:
            A JSON-safe dict from ModelResponse.to_dict().
        """
        # Build the format dict from ctx.inputs, adding "item" if in foreach.
        format_dict: dict[str, Any] = dict(ctx.inputs)  # type: ignore[arg-type,unused-ignore]
        if ctx.item is not None:
            format_dict["item"] = ctx.item

        prompt = prompt_template.format_map(format_dict)

        # Smart retry: append sanitized context so the LLM can self-correct.
        # ctx.retry_context is already sanitized by sanitize_retry_context() —
        # it contains only structured metadata (guidance text, attempt number),
        # never raw output, raw exceptions, or credentials.
        if ctx.retry_context:
            retry_info = "\n\n[RETRY CONTEXT] Your previous response was rejected. "
            if "guidance" in ctx.retry_context:
                retry_info += str(ctx.retry_context["guidance"])
            if "attempt" in ctx.retry_context:
                retry_info += f" (Attempt {ctx.retry_context['attempt']})"
            prompt += retry_info

        response = adapter.call(prompt)
        ctx.increment_llm_calls()  # participate in the circuit breaker
        return response.to_dict()

    return _action
