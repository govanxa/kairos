"""Kairos OpenAI adapter — thin wrapper around the OpenAI Python SDK.

This module provides:
- OpenAIAdapter: a ModelAdapter-compliant class that calls the OpenAI API.
- openai_adapter(): a factory function that returns a step-action callable formatted
  from a prompt template.

Security contracts (ADR-016, S14, S15):
- API keys are read exclusively from OPENAI_API_KEY env var.
- Inline api_key (or any credential kwarg) in constructor or call() raises SecurityError.
- Provider exceptions are sanitized via wrap_provider_exception() before re-raising.
- Exception chains are suppressed (raise ... from None) so raw SDK exceptions
  (which may contain credentials) are never reachable via __cause__.
- HTTPS is enforced for any non-None base_url (unless localhost with allow_localhost).
- ModelResponse never carries credential fields.

ADR-003 (sync-first): call_async() raises NotImplementedError in v0.1.
ADR-012 (thin adapters): no retry, validation, or state logic here.
ADR-017 (optional deps): openai is imported at module level with try/except;
  the module still loads when the SDK is absent — only instantiation fails.

Naming note: this file is named openai_adapter.py (not openai.py) to avoid
shadowing the 'openai' package when Python resolves the import inside this file.
The SDK is imported under the alias 'openai_sdk' for the same reason.
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

# openai is an optional dependency. We set it to None when absent so the
# module loads cleanly. Instantiation raises ConfigError if it is None.
# The alias 'openai_sdk' avoids shadowing the package name in this file.
try:
    import openai as openai_sdk  # type: ignore[import-not-found]
except ImportError:
    openai_sdk = None  # type: ignore[assignment,unused-ignore]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL: str = "gpt-4o"
_DEFAULT_TIMEOUT: float = 120.0

# ---------------------------------------------------------------------------
# OpenAIAdapter
# ---------------------------------------------------------------------------


class OpenAIAdapter:
    """Thin adapter wrapping the OpenAI Python SDK.

    Normalizes OpenAI API responses into ModelResponse objects. Contains
    no retry logic, no validation, and no state management (ADR-012).

    Args:
        model: OpenAI model identifier. Defaults to "gpt-4o".
        base_url: Custom API base URL. Must be HTTPS for remote hosts (S15).
            None uses the OpenAI default endpoint.
        timeout: Request timeout in seconds. Defaults to 120.0.
        **kwargs: Any additional keyword argument is checked for credential
            names (api_key, etc.) and raises SecurityError if found (S14).

    Raises:
        SecurityError: When any credential kwarg is provided (S14).
        ConfigError: When the openai SDK is not installed, or when
            OPENAI_API_KEY is not set in the environment.
        SecurityError: When base_url is an HTTP remote URL (S15).
    """

    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        # S14: reject any inline credential kwargs immediately
        validate_no_inline_api_key(**kwargs)

        # ADR-017: fail fast if the SDK was not installed
        if openai_sdk is None:
            raise ConfigError(
                "The 'openai' package is required for OpenAIAdapter but is not installed. "
                "Install it with: pip install kairos-sdk[openai]  or  pip install openai"
            )

        # S15: enforce HTTPS for remote base URLs
        enforce_https(base_url)

        # S14: read API key exclusively from environment
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ConfigError(
                "OPENAI_API_KEY environment variable is not set. "
                "Model adapters read credentials exclusively from environment variables "
                "(ADR-016 / Security requirement S14)."
            )

        self.model: str = model
        self.timeout: float = timeout

        # Build the SDK client. base_url=None uses the OpenAI default.
        # The openai SDK accepts timeout directly in the constructor.
        client_kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url is not None:
            client_kwargs["base_url"] = base_url

        self._client: Any = cast(Any, openai_sdk).OpenAI(**client_kwargs)

    # ------------------------------------------------------------------
    # Repr (security: FIX 1)
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a safe repr that exposes only the model name.

        The default object repr would include all instance attributes — including
        _client, which holds the OpenAI SDK client initialised with the API key.
        This custom repr prevents accidental credential exposure via repr() or
        logging of the adapter object (ADR-016 / S15).
        """
        return f"OpenAIAdapter(model={self.model!r})"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def call(self, prompt: str, **kwargs: Any) -> ModelResponse:
        """Execute a synchronous model call against the OpenAI chat completions API.

        Args:
            prompt: The formatted prompt string to send as a user message.
            **kwargs: Provider-specific parameters forwarded to the SDK
                (e.g. max_tokens, temperature). Credential kwargs are always
                rejected by validate_no_inline_api_key().

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
            sdk_response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
        except Exception as exc:
            # S15: sanitize the provider exception before re-raising.
            # 'raise ... from None' suppresses the chain so the raw
            # exception (possibly containing credentials) is unreachable.
            raise wrap_provider_exception(exc, adapter_name="openai") from None

        latency_ms = (time.monotonic() - start) * 1000.0

        # Normalize the SDK response into our ModelResponse dataclass.
        # OpenAI returns: response.choices[0].message.content
        # Token fields use OpenAI naming: prompt_tokens / completion_tokens
        # Explicit casts because Pylance can't resolve openai types when
        # the SDK is not installed locally.
        text = str(sdk_response.choices[0].message.content)
        model_id = str(sdk_response.model)
        prompt_tokens = int(sdk_response.usage.prompt_tokens)
        completion_tokens = int(sdk_response.usage.completion_tokens)

        usage = TokenUsage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        return ModelResponse(
            text=text,
            model=model_id,
            usage=usage,
            latency_ms=latency_ms,
            raw=sdk_response,
            metadata={"finish_reason": str(sdk_response.choices[0].finish_reason)},
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
            "OpenAIAdapter.call_async() is not implemented in v0.1. "
            "Kairos is sync-first (ADR-003). Use call() instead."
        )


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def openai_adapter(
    prompt_template: str,
    *,
    model: str = _DEFAULT_MODEL,
    **adapter_kwargs: Any,
) -> Callable[..., dict[str, Any]]:
    """Factory that returns a step-action callable backed by OpenAIAdapter.

    This is the primary developer-facing interface for using OpenAI in a step::

        Step(name="analyze", action=openai_adapter("Analyze this data: {data}"))

    The factory:
    1. Validates kwargs for inline credentials (SecurityError if found).
    2. Creates the OpenAIAdapter eagerly — validates credentials at definition
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
        model: OpenAI model identifier. Defaults to "gpt-4o".
        **adapter_kwargs: Additional kwargs forwarded to OpenAIAdapter.__init__().
            Credential kwargs (api_key, etc.) are always rejected.

    Returns:
        A callable with signature ``(ctx: StepContext) -> dict[str, Any]``
        suitable for use as a Step action.

    Raises:
        SecurityError: If any credential kwarg is present (S14).
        ConfigError: If the openai SDK is not installed or OPENAI_API_KEY
            is missing — detected eagerly at factory call time.
    """
    # S14: reject inline credentials in factory kwargs immediately
    validate_no_inline_api_key(**adapter_kwargs)

    # Create the adapter eagerly so credential errors surface at definition
    # time, not buried inside a workflow run.
    adapter = OpenAIAdapter(model=model, **adapter_kwargs)

    def _action(ctx: StepContext) -> dict[str, Any]:
        """Step action closure — formats template and calls the OpenAI API.

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
        response = adapter.call(prompt)
        return response.to_dict()

    return _action
