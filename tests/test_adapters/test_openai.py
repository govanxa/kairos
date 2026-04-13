"""Tests for kairos.adapters.openai_adapter — written BEFORE implementation (TDD RED phase).

Test priority order (CLAUDE.md / TDD SKILL.md):
1. Failure paths  — missing SDK, missing env var, provider error, async NotImplementedError
2. Boundary       — empty prompt, default model, custom model, custom timeout
3. Happy paths    — call() returns ModelResponse with correct fields, kwargs forwarded,
                    latency_ms positive, token usage captured, finish_reason in metadata,
                    model field from response
4. Security       — inline api_key rejected (constructor / call / factory),
                    HTTP base_url rejected, HTTPS accepted,
                    provider exception sanitized, response never contains api_key,
                    exception chain suppressed (__cause__ is None)
5. Factory        — openai_adapter() returns callable, formats template from ctx.inputs,
                    returns dict, works with StepContext, includes ctx.item when present
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kairos.exceptions import ConfigError, ExecutionError, SecurityError
from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Helpers — build a fake openai response that mirrors ChatCompletion structure
# ---------------------------------------------------------------------------


def _make_sdk_response(
    text: str = "Analysis result",
    model: str = "gpt-4o",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    finish_reason: str = "stop",
) -> MagicMock:
    """Return a MagicMock that mirrors openai.types.chat.ChatCompletion structure.

    The real OpenAI SDK returns an object where:
      response.choices[0].message.content  — the text output
      response.model                        — the model used
      response.usage.prompt_tokens          — prompt token count
      response.usage.completion_tokens      — completion token count
      response.choices[0].finish_reason     — stop reason
    """
    message = MagicMock()
    message.content = text

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    response = MagicMock()
    response.choices = [choice]
    response.model = model
    response.usage = usage
    return response


def _make_mock_openai_sdk(sdk_response: MagicMock | None = None) -> MagicMock:
    """Return a mock openai SDK module whose OpenAI client returns sdk_response."""
    if sdk_response is None:
        sdk_response = _make_sdk_response()

    mock_sdk = MagicMock()
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = sdk_response
    mock_sdk.OpenAI.return_value = mock_client
    return mock_sdk


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set OPENAI_API_KEY in environment for the duration of a test."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key")


@pytest.fixture()
def mock_openai_sdk() -> MagicMock:
    """Return a ready-to-use mock openai SDK module."""
    return _make_mock_openai_sdk()


@pytest.fixture()
def minimal_step_context() -> StepContext:
    """Return a minimal StepContext suitable for factory tests."""
    state_mock = MagicMock()
    return StepContext(
        state=state_mock,
        inputs={"data": "some input text"},
        item=None,
    )


# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """Tests for failure cases — written first per TDD priority order."""

    def test_missing_sdk_raises_config_error(self, env_api_key: None) -> None:
        """When openai SDK is not installed, OpenAIAdapter raises ConfigError with pip hint."""
        with patch("kairos.adapters.openai_adapter.openai_sdk", None):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            with pytest.raises(ConfigError) as exc_info:
                OpenAIAdapter()
            error_msg = str(exc_info.value).lower()
            assert "pip install" in error_msg
            assert "openai" in error_msg

    def test_missing_env_var_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When OPENAI_API_KEY is absent, OpenAIAdapter raises ConfigError."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            with pytest.raises(ConfigError) as exc_info:
                OpenAIAdapter()
            assert "OPENAI_API_KEY" in str(exc_info.value)

    def test_provider_error_wrapped_in_execution_error(
        self, env_api_key: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider SDK exception is caught and re-raised as ExecutionError."""
        mock_sdk = _make_mock_openai_sdk()
        mock_sdk.OpenAI.return_value.chat.completions.create.side_effect = Exception(
            "Connection refused"
        )

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            with pytest.raises(ExecutionError):
                adapter.call("Hello")

    def test_call_async_raises_not_implemented(self, env_api_key: None) -> None:
        """call_async() raises NotImplementedError — sync-first per ADR-003."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            import asyncio

            with pytest.raises(NotImplementedError):
                asyncio.run(adapter.call_async("prompt"))  # FIX 8: asyncio.run not get_event_loop

    def test_provider_error_message_sanitized(self, env_api_key: None) -> None:
        """Provider exceptions have credentials stripped from their wrapped message."""
        mock_sdk = _make_mock_openai_sdk()
        mock_sdk.OpenAI.return_value.chat.completions.create.side_effect = Exception(
            "Auth failed with key sk-openai-secret-12345"
        )

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            with pytest.raises(ExecutionError) as exc_info:
                adapter.call("Hello")
            assert "sk-openai-secret-12345" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge cases — empty prompt, defaults, custom model/timeout."""

    def test_empty_prompt_works(self, env_api_key: None) -> None:
        """An empty prompt string is forwarded without error."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            result = adapter.call("")
            # SDK was called
            mock_sdk.OpenAI.return_value.chat.completions.create.assert_called_once()
            assert result.text == "Analysis result"

    def test_default_model_is_gpt4o(self, env_api_key: None) -> None:
        """Default model is 'gpt-4o'."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            assert adapter.model == "gpt-4o"

    def test_custom_model_forwarded(self, env_api_key: None) -> None:
        """A custom model string is stored and passed to the SDK."""
        mock_sdk = _make_mock_openai_sdk(_make_sdk_response(model="gpt-4-turbo"))

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter(model="gpt-4-turbo")
            assert adapter.model == "gpt-4-turbo"
            adapter.call("Test prompt")
            call_kwargs = mock_sdk.OpenAI.return_value.chat.completions.create.call_args
            assert "gpt-4-turbo" in str(call_kwargs)

    def test_custom_timeout_forwarded(self, env_api_key: None) -> None:
        """A custom timeout is stored on the adapter."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter(timeout=30.0)
            assert adapter.timeout == 30.0


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    """Correct return values, field mapping, kwarg forwarding."""

    def test_call_returns_model_response(self, env_api_key: None) -> None:
        """call() returns a ModelResponse with all required fields populated."""
        from kairos.adapters.base import ModelResponse

        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            result = adapter.call("Analyze this")
            assert isinstance(result, ModelResponse)
            assert result.text == "Analysis result"

    def test_latency_ms_is_positive(self, env_api_key: None) -> None:
        """latency_ms is always a non-negative float."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            result = adapter.call("Hello")
            assert result.latency_ms >= 0.0

    def test_token_usage_captured(self, env_api_key: None) -> None:
        """Token counts from the SDK response are captured in usage.

        OpenAI uses prompt_tokens / completion_tokens (not input/output).
        These map to TokenUsage.input_tokens and TokenUsage.output_tokens.
        """
        mock_sdk = _make_mock_openai_sdk(_make_sdk_response(prompt_tokens=42, completion_tokens=18))

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            result = adapter.call("Count tokens")
            assert result.usage.input_tokens == 42
            assert result.usage.output_tokens == 18
            assert result.usage.total_tokens == 60

    def test_finish_reason_in_metadata(self, env_api_key: None) -> None:
        """finish_reason from the SDK response appears in metadata."""
        mock_sdk = _make_mock_openai_sdk(_make_sdk_response(finish_reason="length"))

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            result = adapter.call("Write a novel")
            assert result.metadata.get("finish_reason") == "length"

    def test_model_field_from_response(self, env_api_key: None) -> None:
        """The model field on ModelResponse reflects what the SDK returned."""
        mock_sdk = _make_mock_openai_sdk(_make_sdk_response(model="gpt-4o"))

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            result = adapter.call("Test")
            assert result.model == "gpt-4o"

    def test_kwargs_forwarded_to_sdk(self, env_api_key: None) -> None:
        """Extra kwargs (e.g. max_tokens, temperature) are forwarded to the SDK."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            adapter.call("Prompt", max_tokens=512, temperature=0.7)  # type: ignore[call-arg]
            call_kwargs = mock_sdk.OpenAI.return_value.chat.completions.create.call_args
            assert "max_tokens" in call_kwargs.kwargs or "max_tokens" in str(call_kwargs)


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestSecurity:
    """Security constraints from ADR-016, S14, S15."""

    def test_rejects_inline_api_key_in_constructor(self, env_api_key: None) -> None:
        """OpenAIAdapter(api_key=...) raises SecurityError."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            with pytest.raises(SecurityError):
                OpenAIAdapter(api_key="sk-inline-key")  # type: ignore[call-arg]

    def test_rejects_api_key_in_call_kwargs(self, env_api_key: None) -> None:
        """adapter.call(prompt, api_key=...) raises SecurityError."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            with pytest.raises(SecurityError):
                adapter.call("prompt", api_key="sk-secret")  # type: ignore[call-arg]

    def test_factory_rejects_api_key(self, env_api_key: None) -> None:
        """openai_adapter('template', api_key=...) raises SecurityError."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import openai_adapter

            with pytest.raises(SecurityError):
                openai_adapter("Analyze: {data}", api_key="sk-inline")  # type: ignore[call-arg]

    def test_http_base_url_rejected(self, env_api_key: None) -> None:
        """HTTP base_url for a remote host raises SecurityError."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            with pytest.raises(SecurityError):
                OpenAIAdapter(base_url="http://remote-server.example.com/v1")

    def test_https_base_url_accepted(self, env_api_key: None) -> None:
        """HTTPS base_url for a remote host is accepted without error."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            # Should not raise
            OpenAIAdapter(base_url="https://custom.api.example.com/v1")

    def test_provider_exception_sanitized_no_credentials(self, env_api_key: None) -> None:
        """Provider exception with credential in message — credential not in ExecutionError."""
        mock_sdk = _make_mock_openai_sdk()
        mock_sdk.OpenAI.return_value.chat.completions.create.side_effect = Exception(
            "Unauthorized: Bearer eyJhbGciOiJSUzI1NiJ9.credential"
        )

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            with pytest.raises(ExecutionError) as exc_info:
                adapter.call("Test")
            error_str = str(exc_info.value)
            assert "eyJhbGciOiJSUzI1NiJ9" not in error_str

    def test_response_never_contains_api_key(self, env_api_key: None) -> None:
        """ModelResponse fields never contain the API key from the environment."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            result = adapter.call("Test")
            result_dict = result.to_dict()
            result_str = str(result_dict)
            # The env-var value set by env_api_key fixture must not appear
            assert "sk-test-openai-key" not in result_str

    def test_response_has_no_credential_fields(self, env_api_key: None) -> None:
        """ModelResponse dataclass has no api_key, token, or secret attributes."""
        from kairos.adapters.base import ModelResponse

        assert not hasattr(ModelResponse, "api_key")
        assert not hasattr(ModelResponse, "token")
        assert not hasattr(ModelResponse, "secret")
        assert not hasattr(ModelResponse, "auth")

    def test_execution_error_cause_is_none(self, env_api_key: None) -> None:
        """The ExecutionError raised from a provider error has no chained cause (from None)."""
        mock_sdk = _make_mock_openai_sdk()
        mock_sdk.OpenAI.return_value.chat.completions.create.side_effect = Exception("raw error")

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter()
            try:
                adapter.call("Hello")
            except ExecutionError as exc:
                # __cause__ must be None — the raw exception is not reachable
                assert exc.__cause__ is None

    def test_repr_contains_model_not_api_key(self, env_api_key: None) -> None:
        """repr(adapter) must include model name and must NOT include the API key value (FIX 1).

        Without a custom __repr__, the default object repr could expose _client internals
        that may reference the API key.
        """
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import OpenAIAdapter

            adapter = OpenAIAdapter(model="gpt-4-turbo")
            r = repr(adapter)
            # Must include the model name so the repr is informative
            assert "gpt-4-turbo" in r
            # Must NOT include the API key value from the env fixture
            assert "sk-test-openai-key" not in r


# ---------------------------------------------------------------------------
# Group 5: Factory function — openai_adapter()
# ---------------------------------------------------------------------------


class TestOpenAIAdapterFactory:
    """Tests for the openai_adapter() factory function."""

    def test_factory_returns_callable(self, env_api_key: None) -> None:
        """openai_adapter() returns a callable."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import openai_adapter

            action = openai_adapter("Analyze: {data}")
            assert callable(action)

    def test_factory_callable_formats_template_from_inputs(
        self,
        env_api_key: None,
        minimal_step_context: StepContext,
    ) -> None:
        """The returned callable formats the prompt_template using ctx.inputs."""
        mock_sdk = _make_mock_openai_sdk()
        captured_prompts: list[str] = []

        def capture_call(**kwargs: Any) -> MagicMock:
            messages = kwargs.get("messages", [{}])
            captured_prompts.append(messages[0].get("content", ""))
            return _make_sdk_response()

        mock_sdk.OpenAI.return_value.chat.completions.create.side_effect = capture_call

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import openai_adapter

            action = openai_adapter("Analyze: {data}")
            # minimal_step_context has inputs={"data": "some input text"}
            action(minimal_step_context)

        assert len(captured_prompts) == 1
        assert "some input text" in captured_prompts[0]

    def test_factory_callable_returns_dict(
        self,
        env_api_key: None,
        minimal_step_context: StepContext,
    ) -> None:
        """The returned callable returns a dict (from ModelResponse.to_dict())."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import openai_adapter

            action = openai_adapter("Analyze: {data}")
            result = action(minimal_step_context)
            assert isinstance(result, dict)
            assert "text" in result
            assert "model" in result
            assert "usage" in result

    def test_factory_callable_works_with_step_context(
        self,
        env_api_key: None,
    ) -> None:
        """The factory closure accepts a StepContext and uses ctx.inputs."""
        from kairos.state import StateStore

        store = StateStore()
        ctx = StepContext(
            state=store,
            inputs={"topic": "competitive landscape"},
            item=None,
        )
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import openai_adapter

            action = openai_adapter("Research {topic}")
            result = action(ctx)
            assert isinstance(result, dict)

    def test_factory_includes_item_in_format_dict_when_present(
        self,
        env_api_key: None,
    ) -> None:
        """When ctx.item is not None, {item} is available in the template."""
        from kairos.state import StateStore

        store = StateStore()
        ctx = StepContext(
            state=store,
            inputs={},
            item="banana",
        )
        mock_sdk = _make_mock_openai_sdk()
        captured_prompts: list[str] = []

        def capture_call(**kwargs: Any) -> MagicMock:
            messages = kwargs.get("messages", [{}])
            captured_prompts.append(messages[0].get("content", ""))
            return _make_sdk_response()

        mock_sdk.OpenAI.return_value.chat.completions.create.side_effect = capture_call

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import openai_adapter

            action = openai_adapter("Process item: {item}")
            action(ctx)

        assert len(captured_prompts) == 1
        assert "banana" in captured_prompts[0]

    def test_factory_custom_model_forwarded(self, env_api_key: None) -> None:
        """openai_adapter(template, model='gpt-4-turbo') uses the specified model."""
        mock_sdk = _make_mock_openai_sdk()

        with patch("kairos.adapters.openai_adapter.openai_sdk", mock_sdk):
            from kairos.adapters.openai_adapter import openai_adapter

            action = openai_adapter("Test", model="gpt-4-turbo")
            state_mock = MagicMock()
            ctx = StepContext(state=state_mock, inputs={})
            action(ctx)
            call_kwargs = mock_sdk.OpenAI.return_value.chat.completions.create.call_args
            assert "gpt-4-turbo" in str(call_kwargs)
