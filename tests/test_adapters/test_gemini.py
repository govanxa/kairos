"""Tests for kairos.adapters.gemini — written BEFORE implementation (TDD RED phase).

Test priority order (CLAUDE.md / TDD SKILL.md):
1. Failure paths  — missing SDK, missing env vars, fallback env var, provider error,
                    async NotImplementedError
2. Boundary       — empty prompt, default model, custom model, custom timeout,
                    allow_localhost defaults to False, HTTP localhost with allow_localhost=True,
                    allow_localhost=True still rejects HTTP remote
3. Happy paths    — call() returns ModelResponse with correct fields, kwargs forwarded,
                    latency_ms positive, token usage captured, finish_reason in metadata,
                    model field from response
4. Security       — inline api_key rejected (constructor / call / factory),
                    HTTP base_url rejected, HTTPS accepted,
                    provider exception sanitized (no credentials in error),
                    response never contains API key, exception chain suppressed,
                    repr shows model only (no credentials)
5. Factory        — gemini() returns callable, formats template from ctx.inputs,
                    returns dict (from ModelResponse.to_dict()), works with StepContext,
                    includes ctx.item in format dict when present, forwards allow_localhost,
                    appends retry context when ctx.retry_context is set,
                    no retry context when ctx.retry_context is None

Mocking strategy:
- The google-genai SDK is imported in the adapter as `genai_sdk`.
- Patch target: "kairos.adapters.gemini.genai_sdk"
- For missing SDK: patch with None
- Mock response shape:
    response.text
    response.usage_metadata.prompt_token_count
    response.usage_metadata.candidates_token_count
    response.candidates[0].finish_reason  (as enum-like object with .name attribute)
    response.model  (for model ID)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kairos.exceptions import ConfigError, ExecutionError, SecurityError
from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Helpers — build a fake google-genai response that mirrors the real SDK shape
# ---------------------------------------------------------------------------


def _make_finish_reason(name: str = "STOP") -> MagicMock:
    """Return a MagicMock finish_reason enum whose .name attribute returns `name`."""
    fr = MagicMock()
    fr.name = name
    return fr


def _make_sdk_response(
    text: str = "Analysis result",
    model: str = "gemini-2.0-flash",
    prompt_token_count: int = 100,
    candidates_token_count: int = 200,
    finish_reason_name: str = "STOP",
) -> MagicMock:
    """Return a MagicMock that mirrors the google-genai response structure.

    The real google-genai SDK returns:
        response.text
        response.usage_metadata.prompt_token_count
        response.usage_metadata.candidates_token_count
        response.candidates[0].finish_reason.name
        response.model  (optional — may not always be present)
    """
    usage = MagicMock()
    usage.prompt_token_count = prompt_token_count
    usage.candidates_token_count = candidates_token_count

    candidate = MagicMock()
    candidate.finish_reason = _make_finish_reason(finish_reason_name)

    response = MagicMock()
    response.text = text
    response.model = model
    response.usage_metadata = usage
    response.candidates = [candidate]
    return response


def _make_mock_genai(sdk_response: MagicMock | None = None) -> MagicMock:
    """Return a mock google.genai module whose Client returns sdk_response on generate_content."""
    if sdk_response is None:
        sdk_response = _make_sdk_response()

    mock_genai = MagicMock()
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = sdk_response
    mock_genai.Client.return_value = mock_client
    return mock_genai


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set GOOGLE_API_KEY in environment for the duration of a test."""
    monkeypatch.setenv("GOOGLE_API_KEY", "google-test-key-abc")
    # Remove the fallback to make tests deterministic
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture()
def mock_genai_module() -> MagicMock:
    """Return a ready-to-use mock google-genai module."""
    return _make_mock_genai()


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
        """When google-genai is not installed, GeminiAdapter raises ConfigError with pip hint."""
        with patch("kairos.adapters.gemini.genai_sdk", None):
            from kairos.adapters.gemini import GeminiAdapter

            with pytest.raises(ConfigError) as exc_info:
                GeminiAdapter()
            error_msg = str(exc_info.value).lower()
            assert "pip install" in error_msg
            assert "gemini" in error_msg

    def test_missing_both_env_vars_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both GOOGLE_API_KEY and GEMINI_API_KEY are absent, raises ConfigError."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            with pytest.raises(ConfigError) as exc_info:
                GeminiAdapter()
            error_msg = str(exc_info.value)
            assert "GOOGLE_API_KEY" in error_msg or "GEMINI_API_KEY" in error_msg

    def test_fallback_env_var_succeeds_when_primary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When GOOGLE_API_KEY is absent but GEMINI_API_KEY is set, construction succeeds."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-fallback-key-xyz")
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            # Should not raise — GEMINI_API_KEY is the accepted fallback
            adapter = GeminiAdapter()
            assert adapter is not None

    def test_provider_error_wrapped_in_execution_error(
        self, env_api_key: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider SDK exception is caught and re-raised as ExecutionError."""
        mock_genai = _make_mock_genai()
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception(
            "Connection refused"
        )

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            with pytest.raises(ExecutionError):
                adapter.call("Hello")

    def test_call_async_raises_not_implemented(self, env_api_key: None) -> None:
        """call_async() raises NotImplementedError — sync-first per ADR-003."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            import asyncio

            with pytest.raises(NotImplementedError):
                asyncio.run(adapter.call_async("prompt"))

    def test_provider_error_message_sanitized(self, env_api_key: None) -> None:
        """Provider exceptions have credentials stripped from the wrapped ExecutionError message."""
        mock_genai = _make_mock_genai()
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception(
            "Auth failed with key google-secret-api-key-9999"
        )

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            with pytest.raises(ExecutionError) as exc_info:
                adapter.call("Hello")
            # The raw exception message must not appear verbatim
            assert "google-secret-api-key-9999" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge cases — empty prompt, defaults, custom model/timeout, allow_localhost."""

    def test_empty_prompt_works(self, env_api_key: None) -> None:
        """An empty prompt string is forwarded without error."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("")
            mock_genai.Client.return_value.models.generate_content.assert_called_once()
            assert result.text == "Analysis result"

    def test_default_model_is_gemini_2_flash(self, env_api_key: None) -> None:
        """Default model is 'gemini-2.0-flash'."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            assert adapter.model == "gemini-2.0-flash"

    def test_custom_model_forwarded(self, env_api_key: None) -> None:
        """A custom model string is stored and passed to the SDK."""
        custom_response = _make_sdk_response(model="gemini-1.5-pro")
        mock_genai = _make_mock_genai(custom_response)

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter(model="gemini-1.5-pro")
            assert adapter.model == "gemini-1.5-pro"
            adapter.call("Test prompt")
            call_kwargs = mock_genai.Client.return_value.models.generate_content.call_args
            assert "gemini-1.5-pro" in str(call_kwargs)

    def test_custom_timeout_stored(self, env_api_key: None) -> None:
        """A custom timeout is stored on the adapter instance."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter(timeout=30.0)
            assert adapter.timeout == 30.0

    def test_allow_localhost_false_by_default(self, env_api_key: None) -> None:
        """allow_localhost defaults to False — HTTP on localhost is rejected."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            with pytest.raises(SecurityError):
                GeminiAdapter(base_url="http://localhost:8080")

    def test_allow_localhost_true_permits_http_localhost(self, env_api_key: None) -> None:
        """allow_localhost=True allows HTTP on localhost (for local model servers)."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            # Should not raise — HTTP localhost is explicitly permitted
            GeminiAdapter(base_url="http://localhost:8080", allow_localhost=True)

    def test_allow_localhost_true_still_rejects_http_remote(self, env_api_key: None) -> None:
        """allow_localhost=True does NOT relax HTTPS for remote hosts."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            with pytest.raises(SecurityError):
                GeminiAdapter(
                    base_url="http://remote-server.example.com/v1",
                    allow_localhost=True,
                )

    def test_empty_candidates_produces_unknown_finish_reason(self, env_api_key: None) -> None:
        """When SDK response has no candidates, finish_reason is 'UNKNOWN'."""
        response = _make_sdk_response()
        response.candidates = []  # empty candidates list
        mock_genai = _make_mock_genai(response)

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("test")
            assert result.metadata["finish_reason"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    """Correct return values, field mapping, kwarg forwarding."""

    def test_call_returns_model_response(self, env_api_key: None) -> None:
        """call() returns a ModelResponse with all required fields populated."""
        from kairos.adapters.base import ModelResponse

        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("Analyze this")
            assert isinstance(result, ModelResponse)
            assert result.text == "Analysis result"

    def test_latency_ms_is_positive(self, env_api_key: None) -> None:
        """latency_ms is always a non-negative float."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("Hello")
            assert result.latency_ms >= 0.0

    def test_token_usage_captured(self, env_api_key: None) -> None:
        """Token counts from the SDK response are captured in usage."""
        custom_response = _make_sdk_response(prompt_token_count=42, candidates_token_count=18)
        mock_genai = _make_mock_genai(custom_response)

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("Count tokens")
            assert result.usage.input_tokens == 42
            assert result.usage.output_tokens == 18
            assert result.usage.total_tokens == 60

    def test_finish_reason_in_metadata(self, env_api_key: None) -> None:
        """finish_reason from the SDK response appears in metadata."""
        custom_response = _make_sdk_response(finish_reason_name="MAX_TOKENS")
        mock_genai = _make_mock_genai(custom_response)

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("Write a book")
            assert result.metadata.get("finish_reason") == "MAX_TOKENS"

    def test_model_field_from_response(self, env_api_key: None) -> None:
        """The model field on ModelResponse reflects what the SDK returned."""
        custom_response = _make_sdk_response(model="gemini-2.0-flash")
        mock_genai = _make_mock_genai(custom_response)

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("Test")
            assert result.model == "gemini-2.0-flash"

    def test_kwargs_forwarded_to_sdk(self, env_api_key: None) -> None:
        """Extra kwargs (e.g. generation_config) are forwarded to the SDK."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            adapter.call("Prompt", temperature=0.7)  # type: ignore[call-arg]
            call_kwargs = mock_genai.Client.return_value.models.generate_content.call_args
            assert "temperature" in str(call_kwargs)


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestSecurity:
    """Security constraints from ADR-016, S14, S15."""

    def test_rejects_inline_api_key_in_constructor(self, env_api_key: None) -> None:
        """GeminiAdapter(api_key=...) raises SecurityError."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            with pytest.raises(SecurityError):
                GeminiAdapter(api_key="google-inline-key")  # type: ignore[call-arg]

    def test_rejects_api_key_in_call_kwargs(self, env_api_key: None) -> None:
        """adapter.call(prompt, api_key=...) raises SecurityError."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            with pytest.raises(SecurityError):
                adapter.call("prompt", api_key="google-secret")  # type: ignore[call-arg]

    def test_factory_rejects_api_key(self, env_api_key: None) -> None:
        """gemini('template', api_key=...) raises SecurityError."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            with pytest.raises(SecurityError):
                gemini("Analyze: {data}", api_key="google-inline")  # type: ignore[call-arg]

    def test_http_base_url_rejected(self, env_api_key: None) -> None:
        """HTTP base_url for a remote host raises SecurityError."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            with pytest.raises(SecurityError):
                GeminiAdapter(base_url="http://remote-server.example.com/v1")

    def test_https_base_url_accepted(self, env_api_key: None) -> None:
        """HTTPS base_url for a remote host is accepted without error."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            # Should not raise
            GeminiAdapter(base_url="https://custom.api.example.com/v1")

    def test_provider_exception_sanitized_no_credentials(self, env_api_key: None) -> None:
        """Provider exception with credential in message — credential not in ExecutionError."""
        mock_genai = _make_mock_genai()
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception(
            "Unauthorized: Bearer eyJhbGciOiJSUzI1NiJ9.super-secret-credential"
        )

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            with pytest.raises(ExecutionError) as exc_info:
                adapter.call("Test")
            error_str = str(exc_info.value)
            assert "eyJhbGciOiJSUzI1NiJ9" not in error_str

    def test_response_never_contains_api_key(self, env_api_key: None) -> None:
        """ModelResponse fields never contain the API key from the environment."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            result = adapter.call("Test")
            result_dict = result.to_dict()
            result_str = str(result_dict)
            # The env-var value set by env_api_key fixture must not appear
            assert "google-test-key-abc" not in result_str

    def test_execution_error_cause_is_none(self, env_api_key: None) -> None:
        """The ExecutionError raised from a provider error has no chained cause (from None)."""
        mock_genai = _make_mock_genai()
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception("raw error")

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter()
            try:
                adapter.call("Hello")
            except ExecutionError as exc:
                # __cause__ must be None — the raw exception is not reachable
                assert exc.__cause__ is None

    def test_repr_contains_model_not_api_key(self, env_api_key: None) -> None:
        """repr(adapter) includes model name and must NOT include the API key value."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import GeminiAdapter

            adapter = GeminiAdapter(model="gemini-1.5-pro")
            r = repr(adapter)
            assert "gemini-1.5-pro" in r
            # Must NOT include the API key value from the env fixture
            assert "google-test-key-abc" not in r


# ---------------------------------------------------------------------------
# Group 5: Factory function — gemini()
# ---------------------------------------------------------------------------


class TestGeminiFactory:
    """Tests for the gemini() factory function."""

    def test_factory_returns_callable(self, env_api_key: None) -> None:
        """gemini() returns a callable."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
            assert callable(action)

    def test_factory_callable_formats_template_from_inputs(
        self,
        env_api_key: None,
        minimal_step_context: StepContext,
    ) -> None:
        """The returned callable formats the prompt_template using ctx.inputs."""
        mock_genai = _make_mock_genai()
        captured_contents: list[str] = []

        def capture_call(**kwargs: Any) -> MagicMock:
            captured_contents.append(kwargs.get("contents", ""))
            return _make_sdk_response()

        mock_genai.Client.return_value.models.generate_content.side_effect = capture_call

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
            # minimal_step_context has inputs={"data": "some input text"}
            action(minimal_step_context)

        assert len(captured_contents) == 1
        assert "some input text" in captured_contents[0]

    def test_factory_callable_returns_dict(
        self,
        env_api_key: None,
        minimal_step_context: StepContext,
    ) -> None:
        """The returned callable returns a dict (from ModelResponse.to_dict())."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
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
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Research {topic}")
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
        mock_genai = _make_mock_genai()
        captured_contents: list[str] = []

        def capture_call(**kwargs: Any) -> MagicMock:
            captured_contents.append(kwargs.get("contents", ""))
            return _make_sdk_response()

        mock_genai.Client.return_value.models.generate_content.side_effect = capture_call

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Process item: {item}")
            action(ctx)

        assert len(captured_contents) == 1
        assert "apple" in captured_contents[0]

    def test_factory_forwards_allow_localhost(self, env_api_key: None) -> None:
        """gemini() forwards allow_localhost to GeminiAdapter."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            # With allow_localhost=True, HTTP localhost must be accepted
            action = gemini(
                "Test prompt",
                base_url="http://localhost:11434",
                allow_localhost=True,
            )
            assert callable(action)

    def test_factory_appends_retry_context_to_prompt(self, env_api_key: None) -> None:
        """When ctx.retry_context is set, the prompt is extended with sanitized RETRY CONTEXT."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                retry_context={"guidance": "Fix the format", "attempt": 2},
            )
            action(ctx)

        generate_mock = mock_genai.Client.return_value.models.generate_content
        actual_contents = generate_mock.call_args.kwargs["contents"]
        assert "[RETRY CONTEXT]" in actual_contents
        assert "Fix the format" in actual_contents
        assert "Attempt 2" in actual_contents

    def test_factory_no_retry_context_on_first_attempt(self, env_api_key: None) -> None:
        """When ctx.retry_context is None, the prompt is unchanged — no RETRY CONTEXT appended."""
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
            state_mock = MagicMock()
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
                retry_context=None,
            )
            action(ctx)

        generate_mock = mock_genai.Client.return_value.models.generate_content
        actual_contents = generate_mock.call_args.kwargs["contents"]
        assert "[RETRY CONTEXT]" not in actual_contents


# ---------------------------------------------------------------------------
# Group 6: LLM call tracking via StepContext (Step 4)
# ---------------------------------------------------------------------------


class TestFactoryLLMCallTracking:
    """Factory closure calls ctx.increment_llm_calls() after a successful adapter.call()."""

    def test_factory_action_calls_increment_llm_calls(self, env_api_key: None) -> None:
        """Factory closure calls ctx.increment_llm_calls() after adapter.call()."""
        mock_genai = _make_mock_genai()
        increment_calls: list[int] = []

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
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
        mock_genai = _make_mock_genai()
        mock_genai.Client.return_value.models.generate_content.side_effect = Exception("API error")
        increment_calls: list[int] = []

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
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
        mock_genai = _make_mock_genai()

        with patch("kairos.adapters.gemini.genai_sdk", mock_genai):
            from kairos.adapters.gemini import gemini

            action = gemini("Analyze: {data}")
            state_mock = MagicMock()
            # Standard StepContext without callback — no _llm_call_callback
            ctx = StepContext(
                state=state_mock,
                inputs={"data": "test input"},
            )
            # Must not raise
            result = action(ctx)

        assert isinstance(result, dict)
