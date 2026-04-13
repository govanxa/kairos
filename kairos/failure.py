"""Kairos failure — FailurePolicy, FailureEvent, RecoveryDecision, and FailureRouter.

The Failure Router decides what happens when a step fails — either from an
execution error or a validation failure. It consults the step's failure policy
and routes to one of five actions: retry, re-plan, skip, abort, or custom.

Security guarantees:
- Retry context is always produced via sanitize_retry_context() — never includes
  raw step output, raw exception messages, or raw LLM responses.
- RecoveryDecision.reason uses only the exception class name (from sanitize_exception),
  never the raw message.
- custom_handler is never reconstructed during deserialization (from_dict).
- FailurePolicy.to_dict() excludes the custom_handler callable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from kairos.enums import FailureAction, FailureType
from kairos.exceptions import ConfigError, PolicyError
from kairos.schema import ValidationResult
from kairos.security import sanitize_exception, sanitize_retry_context, sanitize_validation_token

# ---------------------------------------------------------------------------
# FailurePolicy
# ---------------------------------------------------------------------------


@dataclass
class FailurePolicy:
    """Configuration for how a step or workflow handles failures.

    Three-level resolution: step policy → workflow policy → KAIROS_DEFAULTS.
    The most specific level wins — policies are not merged field-by-field.

    Attributes:
        on_validation_fail: Action when the step output fails schema validation.
        on_execution_fail: Action when the step action raises an exception.
        max_retries: Maximum number of retry attempts before falling back.
            Check is ``attempt_number >= max_retries``. 0 means no retries.
        max_replans: Maximum number of re-plan attempts for the workflow.
        retry_with_feedback: If True, sanitized error context is injected into
            the retry via StepContext.retry_context. Never includes raw content.
        retry_delay: Base delay in seconds between retry attempts. 0 disables delay.
        retry_backoff: Exponential backoff multiplier per attempt (1.0 = flat delay).
        fallback_action: Action when retries or replans are exhausted.
            Must NOT be RETRY (would create an infinite loop).
        custom_handler: Optional callable for CUSTOM action (Phase 4 stub).
            Never serialized. Never reconstructed from dict.
    """

    on_validation_fail: FailureAction = FailureAction.RETRY
    on_execution_fail: FailureAction = FailureAction.RETRY
    max_retries: int = 2
    max_replans: int = 2
    retry_with_feedback: bool = True
    retry_delay: float = 0.0
    retry_backoff: float = 1.0
    fallback_action: FailureAction = FailureAction.ABORT
    custom_handler: Callable[..., Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate policy fields at construction time.

        Raises:
            PolicyError: When any numeric field is negative, or when
                fallback_action is RETRY (infinite loop prevention).
        """
        if self.max_retries < 0:
            raise PolicyError(
                f"max_retries must be >= 0, got {self.max_retries!r}. Set to 0 to disable retries."
            )
        if self.max_replans < 0:
            raise PolicyError(
                f"max_replans must be >= 0, got {self.max_replans!r}. "
                "Set to 0 to disable re-planning."
            )
        if self.retry_delay < 0:
            raise PolicyError(f"retry_delay must be >= 0, got {self.retry_delay!r}.")
        if self.retry_backoff < 0:
            raise PolicyError(f"retry_backoff must be >= 0, got {self.retry_backoff!r}.")
        if self.fallback_action == FailureAction.RETRY:
            raise PolicyError(
                "fallback_action cannot be RETRY — this would create an infinite retry loop. "
                "Use ABORT, SKIP, or REPLAN as a fallback."
            )

    # --- Serialization ---

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict.

        The ``custom_handler`` field is excluded because callables are not
        JSON-serializable and reconstructing them from serialized data would
        be a security risk.

        Returns:
            A dict with all fields except ``custom_handler``.
        """
        return {
            "on_validation_fail": str(self.on_validation_fail),
            "on_execution_fail": str(self.on_execution_fail),
            "max_retries": self.max_retries,
            "max_replans": self.max_replans,
            "retry_with_feedback": self.retry_with_feedback,
            "retry_delay": self.retry_delay,
            "retry_backoff": self.retry_backoff,
            "fallback_action": str(self.fallback_action),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FailurePolicy:
        """Deserialize from a dict produced by ``to_dict()``.

        SECURITY: ``custom_handler`` is NEVER reconstructed. The returned policy
        will always have ``custom_handler=None``.

        Args:
            data: Dict with FailurePolicy fields (as produced by ``to_dict()``).

        Returns:
            A new FailurePolicy instance.

        Raises:
            ConfigError: When a required key is missing from ``data``.
            PolicyError: When a FailureAction value string is not recognized,
                or when the reconstructed policy fails ``__post_init__`` validation.
        """
        required_keys = [
            "on_validation_fail",
            "on_execution_fail",
            "max_retries",
            "max_replans",
            "retry_with_feedback",
            "retry_delay",
            "retry_backoff",
            "fallback_action",
        ]
        for key in required_keys:
            if key not in data:
                raise ConfigError(f"FailurePolicy.from_dict: missing required key {key!r}.")

        def _parse_action(key: str) -> FailureAction:
            raw = str(data[key])
            try:
                return FailureAction(raw)
            except ValueError as exc:
                raise PolicyError(
                    f"FailurePolicy.from_dict: invalid FailureAction value {raw!r} "
                    f"for key {key!r}. Valid values: "
                    f"{[a.value for a in FailureAction]}."
                ) from exc

        return cls(
            on_validation_fail=_parse_action("on_validation_fail"),
            on_execution_fail=_parse_action("on_execution_fail"),
            max_retries=int(cast(float, data["max_retries"])),
            max_replans=int(cast(float, data["max_replans"])),
            retry_with_feedback=bool(data["retry_with_feedback"]),
            retry_delay=float(cast(str, data["retry_delay"])),
            retry_backoff=float(cast(str, data["retry_backoff"])),
            fallback_action=_parse_action("fallback_action"),
            # custom_handler intentionally omitted — never deserialized
        )


# ---------------------------------------------------------------------------
# KAIROS_DEFAULTS — the baseline level of the three-level policy hierarchy
# ---------------------------------------------------------------------------

#: Module-level constant used as the base (lowest-priority) policy level.
#: Step policy → workflow policy → KAIROS_DEFAULTS.
KAIROS_DEFAULTS: FailurePolicy = FailurePolicy()


# ---------------------------------------------------------------------------
# FailureEvent
# ---------------------------------------------------------------------------


@dataclass
class FailureEvent:
    """A failure event produced by the executor or validator and consumed by the router.

    The ``error`` field holds either the raw exception (EXECUTION failures) or
    a ``ValidationResult`` (VALIDATION failures).  Use ``to_dict()`` when
    storing or logging — it sanitizes the error content.

    Note: ``state_snapshot`` is intentionally omitted in MVP — re-plan state
    rollback is deferred to future phases.

    Attributes:
        step_id: ID of the step that failed.
        failure_type: Whether this is an execution or validation failure.
        error: The raw exception or ValidationResult. NOT sanitized here —
            sanitization happens in ``to_dict()`` and in the router.
            The ``dict[str, object]`` variant covers deserialized forms
            from ``from_dict()``.
        attempt_number: 1-based attempt counter for this step.
        timestamp: When the failure occurred.
    """

    step_id: str
    failure_type: FailureType
    error: Exception | ValidationResult | dict[str, object]
    attempt_number: int
    timestamp: datetime

    # --- Serialization ---

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict with sanitized error representation.

        Execution errors: sanitized to (class_name, cleaned_message) via
        ``sanitize_exception()`` — strips credentials and file paths.

        Validation errors: stored as a safe structural summary (field names,
        error count) — never the raw message text.

        Returns:
            A JSON-serializable dict.
        """
        if self.failure_type == FailureType.EXECUTION and isinstance(self.error, Exception):
            error_type, error_msg = sanitize_exception(self.error)
            error_repr: object = {"error_class": error_type, "sanitized_message": error_msg}
        elif self.failure_type == FailureType.VALIDATION and isinstance(
            self.error, ValidationResult
        ):
            # Store a structural summary — never the raw message text from errors
            result: ValidationResult = self.error
            error_repr = {
                "valid": result.valid,
                "error_count": len(result.errors),
                # SECURITY: field names are sanitized via sanitize_validation_token
                # to prevent injection payloads (e.g., "IGNORE ALL INSTRUCTIONS")
                # from reaching log output or downstream consumers.
                "failed_fields": [sanitize_validation_token(e.field) for e in result.errors],
            }
        else:
            # Fallback for unexpected error types — store class name only
            error_repr = {"error_class": type(self.error).__name__}

        return {
            "step_id": self.step_id,
            "failure_type": str(self.failure_type),
            "error": error_repr,
            "attempt_number": self.attempt_number,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FailureEvent:
        """Restore a FailureEvent from a serialized dict.

        Note: The ``error`` field is restored as a plain dict (the sanitized
        representation). The original exception or ValidationResult cannot be
        recovered from serialized form — this is intentional.

        Args:
            data: Dict produced by ``to_dict()``.

        Returns:
            A FailureEvent with ``error`` set to the sanitized dict representation.
        """
        return cls(
            step_id=str(data["step_id"]),
            failure_type=FailureType(str(data["failure_type"])),
            # sanitized dict representation — original exception is not recoverable
            error=cast(dict[str, object], data.get("error", {})),
            attempt_number=int(cast(float, data["attempt_number"])),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
        )


# ---------------------------------------------------------------------------
# RecoveryDecision
# ---------------------------------------------------------------------------


@dataclass
class RecoveryDecision:
    """The outcome of the failure router — what the executor should do next.

    Attributes:
        action: What action to take (RETRY, REPLAN, SKIP, ABORT, CUSTOM).
        reason: Human-readable explanation for the decision. Never includes
            raw exception messages — only class names and policy context.
        retry_context: Sanitized metadata to inject into the retry attempt
            (via StepContext.retry_context). None if not retrying or if
            retry_with_feedback=False.
        rollback_to: State snapshot to roll back to for REPLAN. Always None in
            MVP — full state rollback is a future phase feature.
    """

    action: FailureAction
    reason: str
    retry_context: dict[str, object] | None
    rollback_to: object | None  # Always None in MVP

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict.

        Returns:
            A dict with action, reason, retry_context, and rollback_to.
        """
        return {
            "action": str(self.action),
            "reason": self.reason,
            "retry_context": self.retry_context,
            "rollback_to": self.rollback_to,  # always None in MVP
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RecoveryDecision:
        """Deserialize from a dict produced by ``to_dict()``.

        SECURITY: ``rollback_to`` is always restored as None — state rollback
        is not supported in MVP and must not be reconstructed from external data.

        Args:
            data: Dict produced by ``to_dict()``.

        Returns:
            A new RecoveryDecision instance.

        Raises:
            PolicyError: When the action value string is not a valid FailureAction.
        """
        raw_action = str(data["action"])
        try:
            action = FailureAction(raw_action)
        except ValueError as exc:
            raise PolicyError(
                f"RecoveryDecision.from_dict: invalid FailureAction value {raw_action!r}. "
                f"Valid values: {[a.value for a in FailureAction]}."
            ) from exc

        retry_context_raw = data.get("retry_context")
        retry_context: dict[str, object] | None = None
        if isinstance(retry_context_raw, dict):
            retry_context = cast(dict[str, object], retry_context_raw)

        return cls(
            action=action,
            reason=str(data.get("reason", "")),
            retry_context=retry_context,
            rollback_to=None,  # always None in MVP — never reconstructed
        )


# ---------------------------------------------------------------------------
# FailureRouter
# ---------------------------------------------------------------------------


class FailureRouter:
    """Routes failure events to recovery decisions based on policy hierarchy.

    The router consults three levels of policy in order of decreasing priority:
    1. step_policy (passed to ``handle()``)
    2. workflow_policy (set at construction time)
    3. defaults (set at construction time, defaults to KAIROS_DEFAULTS)

    The most specific level wins — whole-policy overlay, not field-level merge.

    The router owns policy resolution and context construction.
    The executor owns step invocation and lifecycle management.

    Args:
        workflow_policy: Optional workflow-level failure policy.
        defaults: Optional custom defaults (falls back to KAIROS_DEFAULTS).
    """

    def __init__(
        self,
        workflow_policy: FailurePolicy | None = None,
        defaults: FailurePolicy | None = None,
    ) -> None:
        self._workflow_policy = workflow_policy
        self._defaults = defaults if defaults is not None else KAIROS_DEFAULTS

    def resolve_policy(self, step_policy: FailurePolicy | None = None) -> FailurePolicy:
        """Select the effective policy from the three-level hierarchy.

        Whole-policy overlay: the most specific level that is not None wins.
        No field-level merging.

        Args:
            step_policy: Optional step-level policy (highest priority).

        Returns:
            The effective FailurePolicy to apply.
        """
        if step_policy is not None:
            return step_policy
        if self._workflow_policy is not None:
            return self._workflow_policy
        return self._defaults

    def handle(
        self,
        event: FailureEvent,
        step_policy: FailurePolicy | None = None,
        replan_count: int = 0,
    ) -> RecoveryDecision:
        """Determine the recovery action for a failure event.

        Decision flow:
        1. Resolve effective policy via three-level hierarchy.
        2. Select initial action based on failure_type (execution vs validation).
        3. If RETRY: check attempt_number >= max_retries → fallback_action.
        4. If REPLAN: check replan_count >= max_replans → fallback_action.
        5. Build human-readable reason (sanitized — no raw exception messages).
        6. If RETRY and retry_with_feedback: build sanitized retry_context.
        7. Return RecoveryDecision.

        Args:
            event: The failure event from the executor or validator.
            step_policy: Optional step-level failure policy.
            replan_count: How many re-plans have already been attempted this run.

        Returns:
            A RecoveryDecision telling the executor what to do next.
        """
        policy = self.resolve_policy(step_policy)

        # Step 2: Select initial action based on failure type
        if event.failure_type == FailureType.EXECUTION:
            action = policy.on_execution_fail
        else:
            action = policy.on_validation_fail

        # Step 3: Enforce RETRY limit
        if action == FailureAction.RETRY and event.attempt_number >= policy.max_retries:
            action = policy.fallback_action

        # Step 4: Enforce REPLAN limit
        if action == FailureAction.REPLAN and replan_count >= policy.max_replans:
            action = policy.fallback_action

        # Step 5: Build sanitized reason
        reason = self._build_reason(event, action, policy)

        # Step 6: Build retry_context (only for RETRY + feedback enabled)
        retry_context: dict[str, object] | None = None
        if action == FailureAction.RETRY and policy.retry_with_feedback:
            retry_context = self._build_retry_context(event)

        return RecoveryDecision(
            action=action,
            reason=reason,
            retry_context=retry_context,
            rollback_to=None,  # always None in MVP
        )

    def _build_reason(
        self,
        event: FailureEvent,
        action: FailureAction,
        policy: FailurePolicy,
    ) -> str:
        """Build a human-readable, sanitized reason string for the decision.

        Uses only the exception class name (from sanitize_exception) — never
        the raw exception message, which may contain credentials or injected text.

        Args:
            event: The failure event.
            action: The resolved action.
            policy: The effective policy.

        Returns:
            A sanitized reason string.
        """
        step_id = event.step_id
        attempt = event.attempt_number
        failure_label = str(event.failure_type)

        # Extract class name only — never the raw message
        if isinstance(event.error, Exception):
            error_class, _ = sanitize_exception(event.error)
        elif isinstance(event.error, ValidationResult):
            error_class = "ValidationResult"
        else:
            error_class = type(event.error).__name__

        match action:
            case FailureAction.RETRY:
                return (
                    f"Step '{step_id}' failed ({failure_label}, attempt {attempt}): "
                    f"{error_class}. Retrying (attempt {attempt + 1} of {policy.max_retries})."
                )
            case FailureAction.REPLAN:
                return (
                    f"Step '{step_id}' failed ({failure_label}): {error_class}. Triggering re-plan."
                )
            case FailureAction.SKIP:
                return (
                    f"Step '{step_id}' failed ({failure_label}): "
                    f"{error_class}. Skipping step and proceeding."
                )
            case FailureAction.ABORT:
                return (
                    f"Step '{step_id}' failed ({failure_label}): {error_class}. Aborting workflow."
                )
            case FailureAction.CUSTOM:
                return (
                    f"Step '{step_id}' failed ({failure_label}): "
                    f"{error_class}. Routing to custom handler."
                )
            case _:
                return (
                    f"Step '{step_id}' failed ({failure_label}): {error_class}. Action: {action}."
                )

    def _build_retry_context(self, event: FailureEvent) -> dict[str, object]:
        """Build sanitized retry context metadata for the retry attempt.

        Delegates entirely to ``sanitize_retry_context()`` from security.py.
        For validation failures, extracts field/expected/actual from
        ValidationResult.errors — never the raw message text.

        SECURITY: step_output is always passed as None. Exception messages
        are never included. Only structured metadata is returned.

        Args:
            event: The failure event (must be RETRY-eligible).

        Returns:
            A sanitized dict safe to inject into the retry prompt.
        """
        if event.failure_type == FailureType.VALIDATION and isinstance(
            event.error, ValidationResult
        ):
            # Extract only field names and type strings — never raw messages
            validation_errors: list[dict[str, str]] = [
                {
                    "field": e.field,
                    "expected": e.expected,
                    "actual": e.actual,
                }
                for e in event.error.errors
            ]
            return sanitize_retry_context(
                step_output=None,
                exception=None,
                attempt=event.attempt_number,
                failure_type="validation",
                validation_errors=validation_errors,
            )
        else:
            # Execution failure — pass exception for class name extraction only
            exc = event.error if isinstance(event.error, Exception) else None
            return sanitize_retry_context(
                step_output=None,
                exception=exc,
                attempt=event.attempt_number,
                failure_type="execution",
            )
