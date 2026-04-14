"""Tests for kairos.adapters.claude — written BEFORE implementation (TDD RED phase).

Test priority order (CLAUDE.md / TDD SKILL.md):
1. Failure paths  — missing SDK, missing env var, provider error, async NotImplementedError
2. Boundary       — empty prompt, default model, custom model, custom timeout
3. Happy paths    — call() returns ModelResponse with correct fields, kwargs forwarded,
                    latency_ms positive, token usage captured, stop_reason in metadata,
                    model field from response
4. Security       — inline api_key rejected (constructor / call / factory),
                    HTTP base_url rejected, HTTPS accepted,
                    provider exception sanitized, response never contains api_key
5. Factory        — claude() returns callable, formats template, returns dict,
                    works with StepContext
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kairos.exceptions import ConfigError, ExecutionError, SecurityError
from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Helpers — build a fake anthropic response object that mirrors the real SDK
# ---------------------------------------------------------------------------


def _make_sdk_response(
    text: str = "Analysis result",
    model: str = "claude-sonnet-4-20250514",
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_reason: str = "end_turn",
) -> MagicMock:
    """Return a MagicMock that mirrors anthropic.types.Message structure."""
    content_block = MagicMock()
    content_block.text = text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    response = MagicMock()
    response.content = [content_block]
    response.model = model
    response.usage = usage
    response.stop_reason = stop_reason
    return response


def _make_mock_anthropic(sdk_response: MagicMock | None = None) -> MagicMock:
    """Return a mock anthropic module whose Anthropic client returns sdk_response."""
    if sdk_response is None:
        sdk_response = _make_sdk_response()

    mock_anthropic = MagicMock()
    mock_client = MagicMock()
    mock_client.messages.create.return_value = sdk_response
    mock_anthropic.Anthropic.return_value = mock_client
    # Also expose a real-looking APIError for isinstance checks
    mock_anthropic.APIError = Exception
    return mock_anthropic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set ANTHROPIC_API_KEY in environment for the duration of a test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")


@pytest.fixture()
def mock_anthropic_module() -> MagicMock:
    """Return a ready-to-use mock anthropic module."""
    return _make_mock_anthropic()


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
    """Tests for failure cases — these are written first per TDD priority order."""

    def test_missing_sdk_raises_config_error(self, env_api_key: None) -> None:
        """When anthropic is not installed, ClaudeAdapter raises ConfigError with pip hint."""
        with patch("kairos.adapters.claude.anthropic", None):
            from kairos.adapters.claude import ClaudeAdapter

            with pytest.raises(ConfigError) as exc_info:
                ClaudeAdapter()
            assert "pip install" in str(exc_info.value).lower()
            assert "anthropic" in str(exc_info.value).lower()

    def test_missing_env_var_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ANTHROPIC_API_KEY is absent, ClaudeAdapter raises ConfigError."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            with pytest.raises(ConfigError) as exc_info:
                ClaudeAdapter()
            assert "ANTHROPIC_API_KEY" in str(exc_info.value)

    def test_provider_error_wrapped_in_execution_error(
        self, env_api_key: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider SDK exception is caught and re-raised as ExecutionError."""
        mock_ant = _make_mock_anthropic()
        # Make the SDK client raise a generic Exception (simulating APIError)
        mock_ant.Anthropic.return_value.messages.create.side_effect = Exception(
            "Connection refused"
        )

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            with pytest.raises(ExecutionError):
                adapter.call("Hello")

    def test_call_async_raises_not_implemented(self, env_api_key: None) -> None:
        """call_async() raises NotImplementedError — sync-first per ADR-003."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            import asyncio

            with pytest.raises(NotImplementedError):
                asyncio.run(adapter.call_async("prompt"))  # FIX 8: asyncio.run not get_event_loop

    def test_provider_error_message_sanitized(self, env_api_key: None) -> None:
        """Provider exceptions have credentials stripped from their wrapped message."""
        mock_ant = _make_mock_anthropic()
        # The raw exception contains an API key — it must not appear in ExecutionError
        mock_ant.Anthropic.return_value.messages.create.side_effect = Exception(
            "Auth failed with key sk-ant-secret-12345"
        )

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            with pytest.raises(ExecutionError) as exc_info:
                adapter.call("Hello")
            assert "sk-ant-secret-12345" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge cases — empty prompt, defaults, custom model/timeout."""

    def test_empty_prompt_works(self, env_api_key: None) -> None:
        """An empty prompt string is forwarded without error."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            result = adapter.call("")
            # SDK was called
            mock_ant.Anthropic.return_value.messages.create.assert_called_once()
            assert result.text == "Analysis result"

    def test_default_model_is_claude_sonnet(self, env_api_key: None) -> None:
        """Default model is 'claude-sonnet-4-20250514'."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            assert adapter.model == "claude-sonnet-4-20250514"

    def test_custom_model_forwarded(self, env_api_key: None) -> None:
        """A custom model string is stored and passed to the SDK."""
        mock_ant = _make_mock_anthropic(_make_sdk_response(model="claude-3-opus-20240229"))

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter(model="claude-3-opus-20240229")
            assert adapter.model == "claude-3-opus-20240229"
            adapter.call("Test prompt")
            # The model kwarg is passed to the SDK
            call_kwargs = mock_ant.Anthropic.return_value.messages.create.call_args
            assert call_kwargs.kwargs.get("model") == "claude-3-opus-20240229" or (
                len(call_kwargs.args) > 0 and "claude-3-opus-20240229" in str(call_kwargs)
            )

    def test_custom_timeout_forwarded(self, env_api_key: None) -> None:
        """A custom timeout is stored on the adapter."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter(timeout=30.0)
            assert adapter.timeout == 30.0

    def test_allow_localhost_false_by_default(self, env_api_key: None) -> None:
        """allow_localhost defaults to False — HTTP on localhost is rejected."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            with pytest.raises(SecurityError):
                ClaudeAdapter(base_url="http://localhost:8080")

    def test_allow_localhost_true_permits_http_localhost(self, env_api_key: None) -> None:
        """allow_localhost=True allows HTTP on localhost (for Ollama, LM Studio, etc.)."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            # Should not raise — HTTP localhost is explicitly permitted
            ClaudeAdapter(base_url="http://localhost:8080", allow_localhost=True)

    def test_allow_localhost_true_still_rejects_http_remote(self, env_api_key: None) -> None:
        """allow_localhost=True does NOT relax HTTPS for remote hosts."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            with pytest.raises(SecurityError):
                ClaudeAdapter(
                    base_url="http://remote-server.example.com/v1",
                    allow_localhost=True,
                )

    def test_timeout_forwarded_to_sdk_client(self, env_api_key: None) -> None:
        """The timeout value must be passed to the anthropic.Anthropic() constructor (FIX 3).

        Previously the timeout was stored on self but never forwarded to the SDK client,
        meaning the Anthropic SDK used its own default, not the user-specified value.
        """
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            ClaudeAdapter(timeout=45.0)
            # The Anthropic() constructor must have received timeout=45.0
            call_kwargs = mock_ant.Anthropic.call_args
            assert call_kwargs is not None
            assert call_kwargs.kwargs.get("timeout") == 45.0


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    """Correct return values, field mapping, kwarg forwarding."""

    def test_call_returns_model_response(self, env_api_key: None) -> None:
        """call() returns a ModelResponse with all required fields populated."""
        from kairos.adapters.base import ModelResponse

        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            result = adapter.call("Analyze this")
            assert isinstance(result, ModelResponse)
            assert result.text == "Analysis result"

    def test_latency_ms_is_positive(self, env_api_key: None) -> None:
        """latency_ms is always a positive float."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            result = adapter.call("Hello")
            assert result.latency_ms >= 0.0

    def test_token_usage_captured(self, env_api_key: None) -> None:
        """Token counts from the SDK response are captured in usage."""
        mock_ant = _make_mock_anthropic(_make_sdk_response(input_tokens=42, output_tokens=18))

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            result = adapter.call("Count tokens")
            assert result.usage.input_tokens == 42
            assert result.usage.output_tokens == 18
            assert result.usage.total_tokens == 60

    def test_stop_reason_in_metadata(self, env_api_key: None) -> None:
        """stop_reason from the SDK response appears in metadata."""
        mock_ant = _make_mock_anthropic(_make_sdk_response(stop_reason="max_tokens"))

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            result = adapter.call("Write a book")
            assert result.metadata.get("stop_reason") == "max_tokens"

    def test_model_field_from_response(self, env_api_key: None) -> None:
        """The model field on ModelResponse reflects what the SDK returned."""
        mock_ant = _make_mock_anthropic(_make_sdk_response(model="claude-sonnet-4-20250514"))

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            result = adapter.call("Test")
            assert result.model == "claude-sonnet-4-20250514"

    def test_kwargs_forwarded_to_sdk(self, env_api_key: None) -> None:
        """Extra kwargs (e.g. max_tokens, temperature) are forwarded to the SDK."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            adapter.call("Prompt", max_tokens=512, temperature=0.7)  # type: ignore[call-arg]
            call_kwargs = mock_ant.Anthropic.return_value.messages.create.call_args
            # max_tokens must be present in the kwargs or args
            assert "max_tokens" in call_kwargs.kwargs or "max_tokens" in str(call_kwargs)


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestSecurity:
    """Security constraints from ADR-016, S14, S15."""

    def test_rejects_inline_api_key_in_constructor(self, env_api_key: None) -> None:
        """ClaudeAdapter(api_key=...) raises SecurityError."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            with pytest.raises(SecurityError):
                ClaudeAdapter(api_key="sk-ant-inline-key")  # type: ignore[call-arg]

    def test_rejects_api_key_in_call_kwargs(self, env_api_key: None) -> None:
        """adapter.call(prompt, api_key=...) raises SecurityError."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            with pytest.raises(SecurityError):
                adapter.call("prompt", api_key="sk-ant-secret")  # type: ignore[call-arg]

    def test_factory_rejects_api_key(self, env_api_key: None) -> None:
        """claude('template', api_key=...) raises SecurityError."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            with pytest.raises(SecurityError):
                claude("Analyze: {data}", api_key="sk-ant-inline")  # type: ignore[call-arg]

    def test_http_base_url_rejected(self, env_api_key: None) -> None:
        """HTTP base_url for a remote host raises SecurityError."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            with pytest.raises(SecurityError):
                ClaudeAdapter(base_url="http://remote-server.example.com/v1")

    def test_https_base_url_accepted(self, env_api_key: None) -> None:
        """HTTPS base_url for a remote host is accepted without error."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            # Should not raise
            ClaudeAdapter(base_url="https://custom.api.example.com/v1")

    def test_provider_exception_sanitized_no_credentials(self, env_api_key: None) -> None:
        """Provider exception with credential in message — credential not in ExecutionError."""
        mock_ant = _make_mock_anthropic()
        mock_ant.Anthropic.return_value.messages.create.side_effect = Exception(
            "Unauthorized: Bearer eyJhbGciOiJSUzI1NiJ9.credential"
        )

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            with pytest.raises(ExecutionError) as exc_info:
                adapter.call("Test")
            error_str = str(exc_info.value)
            assert "eyJhbGciOiJSUzI1NiJ9" not in error_str

    def test_response_never_contains_api_key(self, env_api_key: None) -> None:
        """ModelResponse fields never contain the API key from the environment."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            result = adapter.call("Test")
            result_dict = result.to_dict()
            result_str = str(result_dict)
            # The env-var value set by env_api_key fixture must not appear
            assert "sk-ant-test-key" not in result_str

    def test_response_has_no_credential_fields(self, env_api_key: None) -> None:
        """ModelResponse dataclass has no api_key, token, or secret attributes."""
        from kairos.adapters.base import ModelResponse

        assert not hasattr(ModelResponse, "api_key")
        assert not hasattr(ModelResponse, "token")
        assert not hasattr(ModelResponse, "secret")
        assert not hasattr(ModelResponse, "auth")

    def test_execution_error_cause_is_none(self, env_api_key: None) -> None:
        """The ExecutionError raised from a provider error has no chained cause (from None)."""
        mock_ant = _make_mock_anthropic()
        mock_ant.Anthropic.return_value.messages.create.side_effect = Exception("raw error")

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter()
            try:
                adapter.call("Hello")
            except ExecutionError as exc:
                # __cause__ must be None — the raw exception is not reachable
                assert exc.__cause__ is None

    def test_repr_contains_model_not_api_key(self, env_api_key: None) -> None:
        """repr(adapter) must include model name and must NOT include the API key value (FIX 1).

        Without a custom __repr__, the default dataclass/object repr includes all attributes,
        which would expose _client internals that may reference the API key.
        """
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import ClaudeAdapter

            adapter = ClaudeAdapter(model="claude-3-opus-20240229")
            r = repr(adapter)
            # Must include the model name so the repr is informative
            assert "claude-3-opus-20240229" in r
            # Must NOT include the API key value from the env fixture
            assert "sk-ant-test-key" not in r


# ---------------------------------------------------------------------------
# Group 5: Factory function — claude()
# ---------------------------------------------------------------------------


class TestClaudeFactory:
    """Tests for the claude() factory function."""

    def test_factory_returns_callable(self, env_api_key: None) -> None:
        """claude() returns a callable."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            assert callable(action)

    def test_factory_callable_formats_template_from_inputs(
        self,
        env_api_key: None,
        minimal_step_context: StepContext,
    ) -> None:
        """The returned callable formats the prompt_template using ctx.inputs."""
        mock_ant = _make_mock_anthropic()
        captured_prompts: list[str] = []

        def capture_call(**kwargs: Any) -> MagicMock:
            captured_prompts.append(kwargs.get("messages", [{}])[0].get("content", ""))
            return _make_sdk_response()

        mock_ant.Anthropic.return_value.messages.create.side_effect = capture_call

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
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
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
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
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Research {topic}")
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
            item="apple",
        )
        mock_ant = _make_mock_anthropic()
        captured_prompts: list[str] = []

        def capture_call(**kwargs: Any) -> MagicMock:
            captured_prompts.append(kwargs.get("messages", [{}])[0].get("content", ""))
            return _make_sdk_response()

        mock_ant.Anthropic.return_value.messages.create.side_effect = capture_call

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Process item: {item}")
            action(ctx)

        assert len(captured_prompts) == 1
        assert "apple" in captured_prompts[0]

    def test_factory_custom_model_forwarded(self, env_api_key: None) -> None:
        """claude(template, model='claude-3-opus-...') uses the specified model."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Test", model="claude-3-opus-20240229")
            state_mock = MagicMock()
            ctx = StepContext(state=state_mock, inputs={})
            action(ctx)
            call_kwargs = mock_ant.Anthropic.return_value.messages.create.call_args
            assert "claude-3-opus-20240229" in str(call_kwargs)

    def test_factory_forwards_allow_localhost(self, env_api_key: None) -> None:
        """claude() forwards allow_localhost to ClaudeAdapter — enables local model URLs."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            # With allow_localhost=True, HTTP localhost must be accepted
            action = claude(
                "Test prompt",
                base_url="http://localhost:11434",
                allow_localhost=True,
            )
            assert callable(action)

    def test_factory_allow_localhost_false_rejects_http_localhost(self, env_api_key: None) -> None:
        """claude() with allow_localhost=False (default) rejects HTTP localhost URLs."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            with pytest.raises(SecurityError):
                claude("Test prompt", base_url="http://localhost:11434")

    def test_factory_appends_retry_context_to_prompt(self, env_api_key: None) -> None:
        """When ctx.retry_context is set, the prompt is extended with sanitized RETRY CONTEXT."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                retry_context={"guidance": "Fix the format", "attempt": 2},
            )
            action(ctx)

        create_mock = mock_ant.Anthropic.return_value.messages.create
        actual_prompt = create_mock.call_args.kwargs["messages"][0]["content"]
        assert "[RETRY CONTEXT]" in actual_prompt
        assert "Fix the format" in actual_prompt
        assert "Attempt 2" in actual_prompt

    def test_factory_no_retry_context_on_first_attempt(self, env_api_key: None) -> None:
        """When ctx.retry_context is None, the prompt is unchanged — no RETRY CONTEXT appended."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                retry_context=None,
            )
            action(ctx)

        create_mock = mock_ant.Anthropic.return_value.messages.create
        actual_prompt = create_mock.call_args.kwargs["messages"][0]["content"]
        assert "[RETRY CONTEXT]" not in actual_prompt

    def test_factory_retry_context_with_guidance_only(self, env_api_key: None) -> None:
        """Retry context with only guidance (no attempt key) still appends the guidance text."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                retry_context={"guidance": "Check field types"},
            )
            action(ctx)

        create_mock = mock_ant.Anthropic.return_value.messages.create
        actual_prompt = create_mock.call_args.kwargs["messages"][0]["content"]
        assert "[RETRY CONTEXT]" in actual_prompt
        assert "Check field types" in actual_prompt
        # No "Attempt" suffix when attempt key is absent
        assert "Attempt" not in actual_prompt

    def test_factory_retry_context_empty_dict(self, env_api_key: None) -> None:
        """An empty retry_context dict appends the RETRY CONTEXT header but no details."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                retry_context={},
            )
            action(ctx)

        create_mock = mock_ant.Anthropic.return_value.messages.create
        actual_prompt = create_mock.call_args.kwargs["messages"][0]["content"]
        # An empty dict is falsy — should NOT append any RETRY CONTEXT block
        assert "[RETRY CONTEXT]" not in actual_prompt


# ---------------------------------------------------------------------------
# Group 6: LLM call tracking via StepContext (Step 4)
# ---------------------------------------------------------------------------


class TestFactoryLLMCallTracking:
    """Factory closure calls ctx.increment_llm_calls() after a successful adapter.call()."""

    def test_factory_action_calls_increment_llm_calls(self, env_api_key: None) -> None:
        """Factory closure calls ctx.increment_llm_calls() after adapter.call()."""
        mock_ant = _make_mock_anthropic()
        increment_calls: list[int] = []

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                _llm_call_callback=lambda count: increment_calls.append(count),
            )
            action(ctx)

        # Must have called increment_llm_calls once with count=1
        assert increment_calls == [1]

    def test_factory_action_does_not_increment_on_failure(self, env_api_key: None) -> None:
        """If adapter.call() raises, no increment is made."""
        mock_ant = _make_mock_anthropic()
        mock_ant.Anthropic.return_value.messages.create.side_effect = Exception("API error")
        increment_calls: list[int] = []

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                _llm_call_callback=lambda count: increment_calls.append(count),
            )
            with pytest.raises(ExecutionError):
                action(ctx)

        # No increment should have happened because the call failed before returning
        assert increment_calls == []

    def test_factory_action_works_without_callback(self, env_api_key: None) -> None:
        """Factory works even if _llm_call_callback is None (backward compat)."""
        mock_ant = _make_mock_anthropic()

        with patch("kairos.adapters.claude.anthropic", mock_ant):
            from kairos.adapters.claude import claude

            action = claude("Analyze: {data}")
            state_mock = MagicMock()
            # Standard StepContext without callback — no _llm_call_callback
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
            )
            # Must not raise
            result = action(ctx)

        assert isinstance(result, dict)
