"""Tests for kairos.adapters.base — written BEFORE implementation (TDD RED phase).

Test priority order (CLAUDE.md):
1. Failure paths — credential kwargs rejected, HTTP rejected, exception wrapping
2. Boundary conditions — TokenUsage defaults, ModelResponse defaults, zero tokens
3. Happy paths — Protocol runtime-checkable, conforming class passes isinstance
4. Security — no credential fields on ModelResponse, to_dict() is JSON-safe
5. Serialization — to_dict() round-trip through json.dumps/loads
"""

from __future__ import annotations

import json

import pytest

from kairos.adapters.base import (
    _CREDENTIAL_KWARG_NAMES,
    ModelAdapter,
    ModelResponse,
    TokenUsage,
    enforce_https,
    validate_no_inline_api_key,
    wrap_provider_exception,
)
from kairos.exceptions import ExecutionError, SecurityError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_token_usage(
    input_tokens: int = 10,
    output_tokens: int = 20,
    total_tokens: int = 30,
    estimated_cost_usd: float | None = None,
) -> TokenUsage:
    """Helper to build a TokenUsage with sensible defaults."""
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def _make_response(
    text: str = "Hello world",
    model: str = "test-model",
    usage: TokenUsage | None = None,
    latency_ms: float = 250.0,
) -> ModelResponse:
    """Helper to build a ModelResponse with sensible defaults."""
    return ModelResponse(
        text=text,
        model=model,
        usage=usage or _make_token_usage(),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """validate_no_inline_api_key, enforce_https, wrap_provider_exception failures."""

    # --- validate_no_inline_api_key ---

    def test_api_key_kwarg_raises_security_error(self) -> None:
        """Passing api_key as a kwarg must raise SecurityError immediately."""
        with pytest.raises(SecurityError, match="api_key"):
            validate_no_inline_api_key(api_key="sk-abc123")

    def test_apikey_kwarg_raises_security_error(self) -> None:
        """Alternative spelling 'apikey' must also be rejected."""
        with pytest.raises(SecurityError, match="apikey"):
            validate_no_inline_api_key(apikey="sk-abc123")

    def test_api_secret_kwarg_raises_security_error(self) -> None:
        """api_secret kwarg must be rejected."""
        with pytest.raises(SecurityError, match="api_secret"):
            validate_no_inline_api_key(api_secret="my-secret")

    def test_secret_key_kwarg_raises_security_error(self) -> None:
        """secret_key kwarg must be rejected."""
        with pytest.raises(SecurityError, match="secret_key"):
            validate_no_inline_api_key(secret_key="my-secret")

    def test_multiple_credential_kwargs_raise_on_first(self) -> None:
        """When multiple credential kwargs are present, at least one raises."""
        with pytest.raises(SecurityError):
            validate_no_inline_api_key(api_key="sk-abc", secret_key="shhh")

    # --- enforce_https ---

    def test_http_base_url_raises_security_error(self) -> None:
        """Plain HTTP for a remote URL must raise SecurityError."""
        with pytest.raises(SecurityError, match="HTTPS"):
            enforce_https("http://api.example.com/v1")

    def test_http_localhost_raises_by_default(self) -> None:
        """HTTP to localhost raises by default (allow_localhost=False)."""
        with pytest.raises(SecurityError):
            enforce_https("http://localhost:11434", allow_localhost=False)

    def test_http_127_raises_by_default(self) -> None:
        """HTTP to 127.0.0.1 raises when allow_localhost=False."""
        with pytest.raises(SecurityError):
            enforce_https("http://127.0.0.1:8080", allow_localhost=False)

    def test_arbitrary_scheme_raises_security_error(self) -> None:
        """Non-http/https schemes (ftp, ws) must be rejected."""
        with pytest.raises(SecurityError):
            enforce_https("ftp://files.example.com")

    def test_enforce_https_rejects_localhost_subdomain_with_allow_localhost(self) -> None:
        """allow_localhost=True must NOT exempt subdomains of localhost.

        'http://localhost.evil.com' contains 'localhost' as a substring but is
        a remote host. Substring matching would incorrectly allow it — urlparse
        must be used to check the actual hostname (FIX 4).
        """
        with pytest.raises(SecurityError):
            enforce_https("http://localhost.evil.com/v1", allow_localhost=True)

    # --- expanded _CREDENTIAL_KWARG_NAMES (FIX 5) ---

    def test_token_kwarg_raises_security_error(self) -> None:
        """'token' must be treated as a credential kwarg and rejected."""
        with pytest.raises(SecurityError, match="token"):
            validate_no_inline_api_key(token="my-token-value")

    def test_password_kwarg_raises_security_error(self) -> None:
        """'password' must be treated as a credential kwarg and rejected."""
        with pytest.raises(SecurityError, match="password"):
            validate_no_inline_api_key(password="s3cret")

    def test_access_token_kwarg_raises_security_error(self) -> None:
        """'access_token' must be treated as a credential kwarg and rejected."""
        with pytest.raises(SecurityError, match="access_token"):
            validate_no_inline_api_key(access_token="eyJhbGci")

    # --- wrap_provider_exception ---

    def test_wrap_returns_execution_error(self) -> None:
        """wrap_provider_exception must return an ExecutionError."""
        exc = ValueError("something went wrong")
        result = wrap_provider_exception(exc, adapter_name="test")
        assert isinstance(result, ExecutionError)

    def test_wrap_suppresses_exception_chain(self) -> None:
        """The returned ExecutionError must have __cause__ == None (from None)."""
        exc = RuntimeError("internal failure")
        wrapped = wrap_provider_exception(exc, adapter_name="test")
        assert wrapped.__cause__ is None

    def test_wrap_sanitizes_api_key_from_message(self) -> None:
        """Credential patterns in the original exception must be redacted."""
        exc = ConnectionError("Auth failed with key sk-proj-abc123xyz")
        wrapped = wrap_provider_exception(exc, adapter_name="my-adapter")
        assert "sk-proj-abc123xyz" not in str(wrapped)
        assert "sk-proj-abc123xyz" not in wrapped.message

    def test_wrap_sanitizes_bearer_token(self) -> None:
        """Bearer tokens in the original exception must be redacted."""
        exc = PermissionError("Request failed: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9")
        wrapped = wrap_provider_exception(exc, adapter_name="my-adapter")
        assert "eyJhbGciOiJIUzI1NiJ9" not in str(wrapped)

    def test_wrap_includes_adapter_name(self) -> None:
        """The wrapped error message must reference the adapter name."""
        exc = TimeoutError("timed out")
        wrapped = wrap_provider_exception(exc, adapter_name="claude")
        assert "claude" in wrapped.message

    def test_wrap_long_message_truncated(self) -> None:
        """Messages longer than 500 chars must be truncated by sanitize_exception.

        The original 600-character content must NOT appear intact in the wrapped message —
        this verifies truncation actually happened, not just that the total length is bounded.
        """
        long_msg = "x" * 600
        exc = RuntimeError(long_msg)
        wrapped = wrap_provider_exception(exc, adapter_name="test")
        # The raw 600-char content must not appear intact — it was truncated to 500 chars
        assert long_msg not in wrapped.message


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Zero tokens, None cost, empty text, None raw, empty metadata."""

    def test_token_usage_zero_tokens(self) -> None:
        """TokenUsage must accept zero for all token counts."""
        usage = TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_tokens == 0

    def test_token_usage_estimated_cost_defaults_to_none(self) -> None:
        """estimated_cost_usd must default to None."""
        usage = TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)
        assert usage.estimated_cost_usd is None

    def test_model_response_raw_defaults_to_none(self) -> None:
        """ModelResponse.raw must default to None."""
        resp = _make_response()
        assert resp.raw is None

    def test_model_response_metadata_defaults_to_empty_dict(self) -> None:
        """ModelResponse.metadata must default to an empty dict."""
        resp = _make_response()
        assert resp.metadata == {}

    def test_model_response_empty_text_allowed(self) -> None:
        """Empty string is a valid model response text."""
        resp = _make_response(text="")
        assert resp.text == ""

    def test_validate_no_credential_kwargs_passes(self) -> None:
        """Non-credential kwargs must pass without error."""
        # Should not raise — temperature, max_tokens, etc. are fine
        validate_no_inline_api_key(temperature=0.7, max_tokens=1024)

    def test_validate_empty_kwargs_passes(self) -> None:
        """No kwargs at all must pass without error."""
        validate_no_inline_api_key()  # must not raise

    def test_enforce_https_accepts_none(self) -> None:
        """None base_url means no custom URL — must pass without error."""
        enforce_https(None)  # must not raise

    def test_enforce_https_accepts_https(self) -> None:
        """A proper HTTPS URL must pass."""
        enforce_https("https://api.anthropic.com")  # must not raise

    def test_enforce_https_localhost_allowed_when_flag_set(self) -> None:
        """HTTP to localhost must pass when allow_localhost=True."""
        enforce_https("http://localhost:11434", allow_localhost=True)  # must not raise

    def test_enforce_https_127_allowed_when_flag_set(self) -> None:
        """HTTP to 127.0.0.1 must pass when allow_localhost=True."""
        enforce_https("http://127.0.0.1:11434", allow_localhost=True)  # must not raise


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    """Protocol runtime-checkability, conforming class, dataclass fields."""

    def test_token_usage_stores_all_fields(self) -> None:
        """TokenUsage must store all four fields correctly."""
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=200,
            total_tokens=300,
            estimated_cost_usd=0.005,
        )
        assert usage.input_tokens == 100
        assert usage.output_tokens == 200
        assert usage.total_tokens == 300
        assert usage.estimated_cost_usd == 0.005

    def test_model_response_stores_all_fields(self) -> None:
        """ModelResponse must store text, model, usage, latency_ms, raw, metadata."""
        usage = _make_token_usage()
        raw_obj = {"provider": "test"}
        meta = {"run_id": "abc"}
        resp = ModelResponse(
            text="hello",
            model="test-model",
            usage=usage,
            latency_ms=123.4,
            raw=raw_obj,
            metadata=meta,
        )
        assert resp.text == "hello"
        assert resp.model == "test-model"
        assert resp.usage is usage
        assert resp.latency_ms == 123.4
        assert resp.raw is raw_obj
        assert resp.metadata is meta

    def test_model_adapter_is_runtime_checkable(self) -> None:
        """ModelAdapter must be decorated with @runtime_checkable."""

        # A class with the required methods should pass isinstance check
        class ConformingAdapter:
            def call(self, prompt: str, **kwargs: object) -> ModelResponse:  # type: ignore[return]
                ...

            async def call_async(  # type: ignore[return]
                self, prompt: str, **kwargs: object
            ) -> ModelResponse: ...

        adapter = ConformingAdapter()
        assert isinstance(adapter, ModelAdapter)

    def test_non_conforming_class_fails_isinstance(self) -> None:
        """A class missing the required methods must fail the isinstance check."""

        class BadAdapter:
            pass

        assert not isinstance(BadAdapter(), ModelAdapter)

    def test_credential_kwarg_names_contains_expected(self) -> None:
        """_CREDENTIAL_KWARG_NAMES must contain all required credential names (FIX 5)."""
        # Original four
        assert "api_key" in _CREDENTIAL_KWARG_NAMES
        assert "apikey" in _CREDENTIAL_KWARG_NAMES
        assert "api_secret" in _CREDENTIAL_KWARG_NAMES
        assert "secret_key" in _CREDENTIAL_KWARG_NAMES
        # Expanded set (FIX 5)
        assert "token" in _CREDENTIAL_KWARG_NAMES
        assert "password" in _CREDENTIAL_KWARG_NAMES
        assert "auth_token" in _CREDENTIAL_KWARG_NAMES
        assert "access_token" in _CREDENTIAL_KWARG_NAMES
        assert "bearer_token" in _CREDENTIAL_KWARG_NAMES
        assert "auth" in _CREDENTIAL_KWARG_NAMES

    def test_credential_kwarg_names_is_frozenset(self) -> None:
        """_CREDENTIAL_KWARG_NAMES must be a frozenset (immutable)."""
        assert isinstance(_CREDENTIAL_KWARG_NAMES, frozenset)

    def test_enforce_https_passes_for_https_with_path(self) -> None:
        """HTTPS URL with a path component must pass."""
        enforce_https("https://api.openai.com/v1")  # must not raise


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestSecurity:
    """S14 and S15 security requirements for the base module."""

    def test_model_response_has_no_api_key_attribute(self) -> None:
        """ModelResponse must not have api_key, apikey, secret, token attributes."""
        resp = _make_response()
        forbidden = {"api_key", "apikey", "api_secret", "secret_key", "token", "bearer"}
        actual_attrs = set(vars(resp).keys())
        overlap = forbidden & actual_attrs
        assert not overlap, f"ModelResponse has credential attributes: {overlap}"

    def test_model_response_to_dict_has_no_credentials(self) -> None:
        """to_dict() output must contain no credential-named keys."""
        resp = _make_response()
        d = resp.to_dict()
        forbidden = {"api_key", "apikey", "api_secret", "secret_key", "token", "bearer"}
        overlap = forbidden & set(d.keys())
        assert not overlap, f"to_dict() contains credential keys: {overlap}"

    def test_wrap_does_not_expose_raw_exception_message(self) -> None:
        """The raw exception message with credentials must not appear in the wrapped error."""
        raw_secret = "password=s3cr3tP@ssw0rd"
        exc = RuntimeError(f"Connection failed: {raw_secret}")
        wrapped = wrap_provider_exception(exc, adapter_name="test")
        assert "s3cr3tP@ssw0rd" not in wrapped.message

    def test_enforce_https_rejects_http_for_remote(self) -> None:
        """Remote HTTP URL must always be rejected (S15: HTTPS enforced)."""
        with pytest.raises(SecurityError):
            enforce_https("http://api.example.com")

    def test_validate_rejects_all_credential_kwarg_names(self) -> None:
        """Every name in _CREDENTIAL_KWARG_NAMES must be individually rejected."""
        for name in _CREDENTIAL_KWARG_NAMES:
            with pytest.raises(SecurityError, match=name):
                validate_no_inline_api_key(**{name: "value"})

    def test_wrap_exception_chain_suppressed(self) -> None:
        """Exception chain must be suppressed — no __cause__ linking to raw exception.

        This prevents the raw exception (which may contain credentials) from
        being visible through exception chaining.
        """
        original = ValueError("secret token=abc123")
        wrapped = wrap_provider_exception(original, adapter_name="test")
        assert wrapped.__cause__ is None
        assert wrapped.__context__ is None


# ---------------------------------------------------------------------------
# Group 5: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """to_dict() produces JSON-safe output that round-trips correctly."""

    def test_token_usage_fields_are_json_primitives(self) -> None:
        """All TokenUsage fields must be JSON-serializable (int, float, None)."""
        usage = _make_token_usage(estimated_cost_usd=0.002)
        # No to_dict on TokenUsage directly, but ModelResponse.to_dict() nests it
        resp = _make_response(usage=usage)
        d = resp.to_dict()
        # Must not raise
        json_str = json.dumps(d)
        assert json_str  # non-empty

    def test_model_response_to_dict_round_trip(self) -> None:
        """to_dict() must produce a dict that survives json.dumps / json.loads."""
        resp = _make_response(text="test output", model="my-model", latency_ms=99.9)
        d = resp.to_dict()
        restored = json.loads(json.dumps(d))
        assert restored["text"] == "test output"
        assert restored["model"] == "my-model"
        assert restored["latency_ms"] == 99.9

    def test_model_response_to_dict_excludes_raw(self) -> None:
        """The raw provider object must NOT appear in to_dict() — it may not be JSON-safe."""
        raw_obj = object()  # not JSON-serializable
        resp = ModelResponse(
            text="hi",
            model="m",
            usage=_make_token_usage(),
            latency_ms=10.0,
            raw=raw_obj,
        )
        d = resp.to_dict()
        assert "raw" not in d

    def test_model_response_to_dict_contains_required_keys(self) -> None:
        """to_dict() must include text, model, latency_ms, and usage sub-dict."""
        resp = _make_response()
        d = resp.to_dict()
        assert "text" in d
        assert "model" in d
        assert "latency_ms" in d
        assert "usage" in d

    def test_token_usage_nested_in_to_dict(self) -> None:
        """The usage sub-dict must contain input_tokens, output_tokens, total_tokens."""
        usage = TokenUsage(input_tokens=5, output_tokens=10, total_tokens=15)
        resp = _make_response(usage=usage)
        d = resp.to_dict()
        assert d["usage"]["input_tokens"] == 5
        assert d["usage"]["output_tokens"] == 10
        assert d["usage"]["total_tokens"] == 15

    def test_metadata_included_in_to_dict(self) -> None:
        """Custom metadata must appear in the to_dict() output."""
        resp = ModelResponse(
            text="x",
            model="m",
            usage=_make_token_usage(),
            latency_ms=1.0,
            metadata={"run_id": "abc", "step": "analyze"},
        )
        d = resp.to_dict()
        assert d["metadata"] == {"run_id": "abc", "step": "analyze"}

    def test_to_dict_is_json_safe_without_raw(self) -> None:
        """to_dict() without raw must be serializable with json.dumps."""
        resp = _make_response()
        # Must not raise TypeError
        result = json.dumps(resp.to_dict())
        assert isinstance(result, str)

    def test_estimated_cost_none_survives_json_roundtrip(self) -> None:
        """None estimated_cost_usd must round-trip as JSON null."""
        usage = TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)
        resp = _make_response(usage=usage)
        d = resp.to_dict()
        restored = json.loads(json.dumps(d))
        assert restored["usage"]["estimated_cost_usd"] is None
