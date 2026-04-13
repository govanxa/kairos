"""Kairos Claude adapter — thin wrapper around the Anthropic Python SDK.

This module provides:
- ClaudeAdapter: a ModelAdapter-compliant class that calls the Anthropic API.
- claude(): a factory function that returns a step-action callable formatted
  from a prompt template.

Security contracts (ADR-016, S14, S15):
- API keys are read exclusively from ANTHROPIC_API_KEY env var.
- Inline api_key (or any credential kwarg) in constructor or call() raises SecurityError.
- Provider exceptions are sanitized via wrap_provider_exception() before re-raising.
- Exception chains are suppressed (raise ... from None) so raw SDK exceptions
  (which may contain credentials) are never reachable via __cause__.
- HTTPS is enforced for any non-None base_url (unless localhost with allow_localhost).
- ModelResponse never carries credential fields.

ADR-003 (sync-first): call_async() raises NotImplementedError in v0.1.
ADR-012 (thin adapters): no retry, validation, or state logic here.
ADR-017 (optional deps): anthropic is imported at module level with try/except;
  the module still loads when the SDK is absent — only instantiation fails.
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

# anthropic is an optional dependency. We set it to None when absent so the
# module loads cleanly. Instantiation raises ConfigError if it is None.
try:
    import anthropic  # type: ignore[import-not-found,unused-ignore]
except ImportError:
    anthropic = None  # type: ignore[assignment,unused-ignore]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL: str = "claude-sonnet-4-20250514"
_DEFAULT_TIMEOUT: float = 120.0

# ---------------------------------------------------------------------------
# ClaudeAdapter
# ---------------------------------------------------------------------------


class ClaudeAdapter:
    """Thin adapter wrapping the Anthropic Python SDK.

    Normalizes Anthropic API responses into ModelResponse objects. Contains
    no retry logic, no validation, and no state management (ADR-012).

    Args:
        model: Anthropic model identifier. Defaults to "claude-sonnet-4-20250514".
        base_url: Custom API base URL. Must be HTTPS for remote hosts (S15).
            None uses the Anthropic default endpoint.
        timeout: Request timeout in seconds. Defaults to 120.0.
        allow_localhost: When True, HTTP on localhost / 127.0.0.1 / ::1 is
            permitted. Intended for local models such as Ollama or LM Studio.
            Defaults to False (secure by default). Has no effect on remote hosts —
            remote URLs always require HTTPS regardless of this setting.
        **kwargs: Any additional keyword argument is checked for credential
            names (api_key, etc.) and raises SecurityError if found (S14).

    Raises:
        SecurityError: When any credential kwarg is provided (S14).
        ConfigError: When the anthropic SDK is not installed, or when
            ANTHROPIC_API_KEY is not set in the environment.
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
        if anthropic is None:
            raise ConfigError(
                "The 'anthropic' package is required for ClaudeAdapter but is not installed. "
                "Install it with: pip install kairos-sdk[anthropic]  or  pip install anthropic"
            )

        # S15: enforce HTTPS for remote base URLs.
        # allow_localhost=True permits HTTP on loopback addresses for local models
        # such as Ollama or LM Studio (never for remote hosts).
        enforce_https(base_url, allow_localhost=allow_localhost)

        # S14: read API key exclusively from environment
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "Model adapters read credentials exclusively from environment variables "
                "(ADR-016 / Security requirement S14)."
            )

        self.model: str = model
        self.timeout: float = timeout

        # Build the SDK client. base_url=None uses the Anthropic default.
        # FIX 3: forward timeout to the SDK constructor so the Anthropic client
        # actually honours the caller-specified timeout (previously it was stored
        # on self but never passed through).
        client_kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url is not None:
            client_kwargs["base_url"] = base_url

        self._client: Any = cast(Any, anthropic).Anthropic(**client_kwargs)

    # ------------------------------------------------------------------
    # Repr (security: FIX 1)
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a safe repr that exposes only the model name.

        The default object repr would include all instance attributes — including
        _client, which holds the Anthropic SDK client initialised with the API key.
        This custom repr prevents accidental credential exposure via repr() or
        logging of the adapter object (ADR-016 / S15).
        """
        return f"ClaudeAdapter(model={self.model!r})"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def call(self, prompt: str, **kwargs: Any) -> ModelResponse:
        """Execute a synchronous model call against the Anthropic API.

        Args:
            prompt: The formatted prompt string to send as a user message.
            **kwargs: Provider-specific parameters forwarded to the SDK
                (e.g. max_tokens, temperature, system). Credential kwargs
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
            sdk_response = self._client.messages.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
        except Exception as exc:
            # S15: sanitize the provider exception before re-raising.
            # 'raise ... from None' suppresses the chain so the raw
            # exception (possibly containing credentials) is unreachable.
            raise wrap_provider_exception(exc, adapter_name="claude") from None

        latency_ms = (time.monotonic() - start) * 1000.0

        # Normalize the SDK response into our ModelResponse dataclass.
        # Explicit casts because Pylance can't resolve anthropic types when
        # the SDK is not installed locally.
        text = str(sdk_response.content[0].text)
        model_id = str(sdk_response.model)
        input_toks = int(sdk_response.usage.input_tokens)
        output_toks = int(sdk_response.usage.output_tokens)

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
            metadata={"stop_reason": str(sdk_response.stop_reason)},
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
            "ClaudeAdapter.call_async() is not implemented in v0.1. "
            "Kairos is sync-first (ADR-003). Use call() instead."
        )


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def claude(
    prompt_template: str,
    *,
    model: str = _DEFAULT_MODEL,
    allow_localhost: bool = False,
    **adapter_kwargs: Any,
) -> Callable[..., dict[str, Any]]:
    """Factory that returns a step-action callable backed by ClaudeAdapter.

    This is the primary developer-facing interface for using Claude in a step::

        Step(name="analyze", action=claude("Analyze this data: {data}"))

    For local models (Ollama, LM Studio) on HTTP::

        Step(
            name="local",
            action=claude("Query: {q}", base_url="http://localhost:11434", allow_localhost=True),
        )

    The factory:
    1. Validates kwargs for inline credentials (SecurityError if found).
    2. Creates the ClaudeAdapter eagerly — validates credentials at definition
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
        model: Anthropic model identifier. Defaults to "claude-sonnet-4-20250514".
        allow_localhost: When True, HTTP on localhost / 127.0.0.1 / ::1 is
            permitted. Intended for local models such as Ollama or LM Studio.
            Defaults to False (secure by default).
        **adapter_kwargs: Additional kwargs forwarded to ClaudeAdapter.__init__().
            Credential kwargs (api_key, etc.) are always rejected.

    Returns:
        A callable with signature ``(ctx: StepContext) -> dict[str, Any]``
        suitable for use as a Step action.

    Raises:
        SecurityError: If any credential kwarg is present (S14).
        ConfigError: If the anthropic SDK is not installed or ANTHROPIC_API_KEY
            is missing — detected eagerly at factory call time.
    """
    # S14: reject inline credentials in factory kwargs immediately
    validate_no_inline_api_key(**adapter_kwargs)

    # Create the adapter eagerly so credential errors surface at definition
    # time, not buried inside a workflow run.
    adapter = ClaudeAdapter(model=model, allow_localhost=allow_localhost, **adapter_kwargs)

    def _action(ctx: StepContext) -> dict[str, Any]:
        """Step action closure — formats template and calls the Anthropic API.

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
        return response.to_dict()

    return _action
