"""Kairos exception hierarchy — all SDK-specific errors."""


class KairosError(Exception):
    """Base exception for all Kairos errors."""

    def __init__(self, message: str = "") -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class PlanError(KairosError):
    """Invalid plan structure — cycles, missing deps, malformed graph."""

    def __init__(self, message: str = "", *, step_id: str | None = None) -> None:
        self.step_id = step_id
        super().__init__(message)


class ExecutionError(KairosError):
    """Step runtime failure — timeout, crash, retry exhaustion."""

    def __init__(
        self,
        message: str = "",
        *,
        step_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
        self.step_id = step_id
        self.attempt = attempt
        super().__init__(message)


class ValidationError(KairosError):
    """Contract violation — type mismatch, range exceeded, pattern fail."""

    def __init__(
        self,
        message: str = "",
        *,
        step_id: str | None = None,
        field: str | None = None,
    ) -> None:
        self.step_id = step_id
        self.field = field
        super().__init__(message)


class StateError(KairosError):
    """State access issue — missing key, size limit, non-serializable value."""

    def __init__(self, message: str = "", *, key: str | None = None) -> None:
        self.key = key
        super().__init__(message)


class PolicyError(KairosError):
    """Invalid failure policy configuration."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class SecurityError(KairosError):
    """Security violation — credential leak, path traversal, unauthorized access."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class ConfigError(KairosError):
    """Configuration error — missing API key, invalid adapter config."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
