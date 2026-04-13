"""Tests for kairos.enums — written BEFORE implementation."""

import json


class TestWorkflowStatus:
    def test_values_exist(self):
        from kairos.enums import WorkflowStatus

        assert WorkflowStatus.COMPLETE == "complete"
        assert WorkflowStatus.FAILED == "failed"
        assert WorkflowStatus.PARTIAL == "partial"

    def test_str_mixin(self):
        from kairos.enums import WorkflowStatus

        # str mixin ensures value equality with plain strings
        assert WorkflowStatus.COMPLETE == "complete"
        assert isinstance(WorkflowStatus.COMPLETE, str)
        # .value always returns the raw string
        assert WorkflowStatus.FAILED.value == "failed"

    def test_json_serializable(self):
        from kairos.enums import WorkflowStatus

        dumped = json.dumps({"status": WorkflowStatus.COMPLETE})
        loaded = json.loads(dumped)
        assert loaded["status"] == "complete"


class TestStepStatus:
    def test_all_values(self):
        from kairos.enums import StepStatus

        expected = {
            "PENDING": "pending",
            "RUNNING": "running",
            "VALIDATING": "validating",
            "COMPLETED": "completed",
            "FAILED": "failed",
            "RETRYING": "retrying",
            "FAILED_FINAL": "failed_final",
            "ROUTING": "routing",
            "SKIPPED": "skipped",
        }
        for name, value in expected.items():
            assert getattr(StepStatus, name) == value

    def test_json_serializable(self):
        from kairos.enums import StepStatus

        dumped = json.dumps({"status": StepStatus.RUNNING})
        loaded = json.loads(dumped)
        assert loaded["status"] == "running"


class TestFailureAction:
    def test_all_values(self):
        from kairos.enums import FailureAction

        assert FailureAction.RETRY == "retry"
        assert FailureAction.REPLAN == "replan"
        assert FailureAction.SKIP == "skip"
        assert FailureAction.ABORT == "abort"
        assert FailureAction.CUSTOM == "custom"


class TestFailureType:
    def test_all_values(self):
        from kairos.enums import FailureType

        assert FailureType.EXECUTION == "execution"
        assert FailureType.VALIDATION == "validation"


class TestForeachPolicy:
    def test_all_values(self):
        from kairos.enums import ForeachPolicy

        assert ForeachPolicy.REQUIRE_ALL == "require_all"
        assert ForeachPolicy.ALLOW_PARTIAL == "allow_partial"


class TestAttemptStatus:
    def test_all_values(self):
        from kairos.enums import AttemptStatus

        assert AttemptStatus.SUCCESS == "success"
        assert AttemptStatus.FAILURE == "failure"


class TestValidationLayer:
    def test_all_values(self):
        from kairos.enums import ValidationLayer

        assert ValidationLayer.STRUCTURAL == "structural"
        assert ValidationLayer.SEMANTIC == "semantic"
        assert ValidationLayer.BOTH == "both"


class TestSeverity:
    def test_all_values(self):
        from kairos.enums import Severity

        assert Severity.ERROR == "error"
        assert Severity.WARNING == "warning"


class TestLogLevel:
    def test_all_values(self):
        from kairos.enums import LogLevel

        assert LogLevel.INFO == "info"
        assert LogLevel.WARN == "warn"
        assert LogLevel.ERROR == "error"


class TestLogVerbosity:
    def test_all_values(self):
        from kairos.enums import LogVerbosity

        assert LogVerbosity.MINIMAL == "minimal"
        assert LogVerbosity.NORMAL == "normal"
        assert LogVerbosity.VERBOSE == "verbose"


class TestPlanStrategy:
    def test_all_values(self):
        from kairos.enums import PlanStrategy

        assert PlanStrategy.MANUAL == "manual"
        assert PlanStrategy.LLM_GENERATED == "llm_generated"
        assert PlanStrategy.HYBRID == "hybrid"


class TestEnumCount:
    def test_total_enum_count(self):
        """Verify all 11 enums are defined."""
        from enum import Enum, StrEnum

        from kairos import enums

        enum_classes = [
            v
            for v in vars(enums).values()
            if isinstance(v, type) and issubclass(v, Enum) and v not in (Enum, StrEnum)
        ]
        assert len(enum_classes) == 11
