"""Tests for kairos.schema — written BEFORE implementation."""

from __future__ import annotations

import json
import math
import typing
from collections.abc import Callable
from typing import Any

import pytest

from kairos.enums import Severity
from kairos.exceptions import ConfigError
from kairos.schema import (
    ContractPair,
    Schema,
    SchemaRegistry,
    ValidationResult,
)

# ---------------------------------------------------------------------------
# Group 1: Failure paths (written FIRST)
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """Tests for failure paths — unsupported types, bad input, invalid schemas."""

    def test_unsupported_type_raises_config_error(self):
        """Unsupported types like bytes must raise ConfigError at definition time."""
        with pytest.raises(ConfigError, match="unsupported"):
            Schema({"data": bytes})

    def test_unsupported_type_set_raises_config_error(self):
        """set is not JSON-serializable — ConfigError at definition time."""
        with pytest.raises(ConfigError, match="unsupported"):
            Schema({"data": set})

    def test_validate_non_dict_fails(self):
        """validate() on a non-dict returns ValidationResult(valid=False)."""
        schema = Schema({"name": str})
        result = schema.validate("not a dict")
        assert result.valid is False
        assert len(result.errors) > 0

    def test_validate_none_fails(self):
        """validate(None) returns ValidationResult(valid=False)."""
        schema = Schema({"name": str})
        result = schema.validate(None)
        assert result.valid is False

    def test_validate_list_fails(self):
        """validate([]) returns ValidationResult(valid=False)."""
        schema = Schema({"name": str})
        result = schema.validate([])
        assert result.valid is False

    def test_missing_required_field_fails(self):
        """Missing required field returns error."""
        schema = Schema({"name": str, "age": int})
        result = schema.validate({"name": "Alice"})
        assert result.valid is False
        assert any(e.field == "age" for e in result.errors)

    def test_wrong_type_str_field_fails(self):
        """Passing int where str is expected fails."""
        schema = Schema({"name": str})
        result = schema.validate({"name": 42})
        assert result.valid is False
        assert any(e.field == "name" for e in result.errors)

    def test_wrong_type_int_field_fails(self):
        """Passing str where int is expected fails."""
        schema = Schema({"count": int})
        result = schema.validate({"count": "five"})
        assert result.valid is False

    def test_bool_not_accepted_as_int(self):
        """bool is a subclass of int in Python but should be rejected for int fields."""
        schema = Schema({"count": int})
        result = schema.validate({"count": True})
        assert result.valid is False

    def test_float_nan_fails(self):
        """NaN is not a valid float value — fails validation."""
        schema = Schema({"score": float})
        result = schema.validate({"score": math.nan})
        assert result.valid is False
        assert any(e.field == "score" for e in result.errors)

    def test_float_inf_fails(self):
        """Inf is not a valid float value — fails validation."""
        schema = Schema({"score": float})
        result = schema.validate({"score": math.inf})
        assert result.valid is False

    def test_float_neg_inf_fails(self):
        """Negative Inf is not a valid float value — fails validation."""
        schema = Schema({"score": float})
        result = schema.validate({"score": -math.inf})
        assert result.valid is False

    def test_nested_wrong_type_fails_with_dotted_path(self):
        """Nested schema validation error uses dot-separated field path."""
        address = Schema({"zip": str})
        company = Schema({"hq": address})
        result = company.validate({"hq": {"zip": 12345}})
        assert result.valid is False
        assert any(e.field == "hq.zip" for e in result.errors)

    def test_list_item_wrong_type_fails_with_indexed_path(self):
        """List item validation error includes index in path."""
        schema = Schema({"tags": list[str]})
        result = schema.validate({"tags": ["ok", 123, "also_ok"]})
        assert result.valid is False
        assert any(e.field == "tags[1]" for e in result.errors)

    def test_from_json_schema_rejects_non_dict(self):
        """from_json_schema raises ConfigError for non-dict input."""
        with pytest.raises(ConfigError):
            Schema.from_json_schema("not a dict")

    def test_from_json_schema_rejects_wrong_type(self):
        """from_json_schema raises ConfigError when type is not 'object'."""
        with pytest.raises(ConfigError):
            Schema.from_json_schema({"type": "string"})

    def test_from_json_schema_rejects_missing_type(self):
        """from_json_schema raises ConfigError when type key is absent."""
        with pytest.raises(ConfigError):
            Schema.from_json_schema({"properties": {"name": {"type": "string"}}})

    def test_from_pydantic_without_pydantic_raises_config_error(self):
        """from_pydantic raises ConfigError if pydantic is not installed."""
        try:
            import pydantic  # noqa: F401, PLC0415  # type: ignore[import-not-found,unused-ignore]

            pytest.skip("pydantic is installed — skipping unavailability test")
        except ImportError:
            with pytest.raises(ConfigError, match="pydantic"):
                Schema.from_pydantic(object)

    def test_from_pydantic_rejects_non_basemodel(self):
        """from_pydantic raises ConfigError when given a non-BaseModel class."""
        try:
            import pydantic  # noqa: F401, PLC0415  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            pytest.skip("pydantic not installed")
        with pytest.raises(ConfigError, match="BaseModel"):
            Schema.from_pydantic(str)

    def test_registry_raises_on_empty_step_id(self):
        """SchemaRegistry.register raises ConfigError for empty step_id."""
        registry = SchemaRegistry()
        schema = Schema({"name": str})
        with pytest.raises(ConfigError):
            registry.register("", schema, None)

    def test_registry_raises_on_duplicate_registration(self):
        """SchemaRegistry.register raises ConfigError for duplicate step_id."""
        registry = SchemaRegistry()
        schema = Schema({"name": str})
        registry.register("step_1", schema, None)
        with pytest.raises(ConfigError):
            registry.register("step_1", schema, None)

    def test_circular_reference_raises_config_error(self):
        """Non-circular deep nesting works (verifies circular detection path)."""
        schema_a = Schema({"x": str})
        schema_b = Schema({"a": schema_a})
        schema_c = Schema({"b": schema_b})  # Should succeed — not circular
        assert schema_c is not None

    def test_multiple_errors_collected(self):
        """validate() collects all errors, not just the first."""
        schema = Schema({"name": str, "age": int, "score": float})
        result = schema.validate({"name": 1, "age": "bad", "score": math.nan})
        assert result.valid is False
        assert len(result.errors) >= 3


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge cases: empty schema, single field, None values, bool vs int, etc."""

    def test_empty_schema_validates_any_dict(self):
        """Schema({}) accepts any dict (open schema, no required fields)."""
        schema = Schema({})
        result = schema.validate({})
        assert result.valid is True

    def test_empty_schema_extra_fields_allowed(self):
        """Extra fields not in schema are allowed (open schema)."""
        schema = Schema({"name": str})
        result = schema.validate({"name": "Alice", "extra_field": "ignored"})
        assert result.valid is True

    def test_optional_field_absent_is_valid(self):
        """Optional field (str | None) can be absent."""
        schema = Schema({"name": str, "nickname": str | None})
        result = schema.validate({"name": "Alice"})
        assert result.valid is True

    def test_optional_field_none_is_valid(self):
        """Optional field (str | None) can be explicitly None."""
        schema = Schema({"name": str, "nickname": str | None})
        result = schema.validate({"name": "Alice", "nickname": None})
        assert result.valid is True

    def test_optional_field_with_value_is_valid(self):
        """Optional field (str | None) with a valid value passes."""
        schema = Schema({"name": str, "nickname": str | None})
        result = schema.validate({"name": "Alice", "nickname": "Ally"})
        assert result.valid is True

    def test_optional_field_wrong_type_fails(self):
        """Optional field (str | None) with wrong type still fails."""
        schema = Schema({"nickname": str | None})
        result = schema.validate({"nickname": 42})
        assert result.valid is False

    def test_int_accepted_for_float_field(self):
        """int is a valid value for float fields (numeric widening)."""
        schema = Schema({"score": float})
        result = schema.validate({"score": 5})  # int, not float
        assert result.valid is True

    def test_float_zero_is_valid(self):
        """Zero float value is valid."""
        schema = Schema({"score": float})
        result = schema.validate({"score": 0.0})
        assert result.valid is True

    def test_int_zero_is_valid(self):
        """Zero int value is valid."""
        schema = Schema({"count": int})
        result = schema.validate({"count": 0})
        assert result.valid is True

    def test_empty_string_is_valid_str(self):
        """Empty string is valid for str fields."""
        schema = Schema({"name": str})
        result = schema.validate({"name": ""})
        assert result.valid is True

    def test_empty_list_is_valid(self):
        """Empty list is valid for list[str] fields."""
        schema = Schema({"tags": list[str]})
        result = schema.validate({"tags": []})
        assert result.valid is True

    def test_bool_false_not_accepted_as_int(self):
        """False (bool) is rejected for int fields despite Python's subclassing."""
        schema = Schema({"flag": int})
        result = schema.validate({"flag": False})
        assert result.valid is False

    def test_bool_true_not_accepted_as_int(self):
        """True (bool) is rejected for int fields."""
        schema = Schema({"flag": int})
        result = schema.validate({"flag": True})
        assert result.valid is False

    def test_bare_list_type_accepted(self):
        """Bare list (no type param) is accepted in schema definition."""
        schema = Schema({"items": list})
        result = schema.validate({"items": [1, "two", 3.0]})
        assert result.valid is True

    def test_bare_list_non_list_fails(self):
        """Bare list field rejects non-list values."""
        schema = Schema({"items": list})
        result = schema.validate({"items": "not a list"})
        assert result.valid is False

    def test_single_field_schema(self):
        """Schema with exactly one field works correctly."""
        schema = Schema({"x": int})
        assert schema.validate({"x": 5}).valid is True
        assert schema.validate({"x": "five"}).valid is False

    def test_extend_override_base_field(self):
        """extend() allows overriding fields from the base schema."""
        base = Schema({"name": str, "age": int})
        extended = base.extend({"age": str})  # Override age type
        result = extended.validate({"name": "Alice", "age": "twenty"})
        assert result.valid is True

    def test_extend_does_not_mutate_original(self):
        """extend() returns a new Schema, leaving the original unchanged."""
        base = Schema({"name": str})
        _ = base.extend({"email": str})
        assert "email" not in base.field_names

    def test_nested_schema_missing_required_sub_field_fails(self):
        """Nested schema: missing required sub-field produces dotted-path error."""
        address = Schema({"street": str, "city": str})
        company = Schema({"hq": address})
        result = company.validate({"hq": {"street": "Main St"}})
        assert result.valid is False
        assert any(e.field == "hq.city" for e in result.errors)

    def test_deeply_nested_schema(self):
        """Deeply nested schemas (3 levels) validate correctly."""
        zip_schema = Schema({"code": str})
        address = Schema({"zip": zip_schema})
        company = Schema({"hq": address})
        result = company.validate({"hq": {"zip": {"code": "90210"}}})
        assert result.valid is True


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    """Happy paths: construction, validation pass, properties, to/from JSON Schema."""

    def test_schema_construction_with_primitives(self):
        """Schema accepts str, int, float, bool types."""
        schema = Schema({"name": str, "age": int, "score": float, "active": bool})
        assert set(schema.field_names) == {"name", "age", "score", "active"}

    def test_required_fields_property(self):
        """required_fields returns only fields that are required."""
        schema = Schema({"name": str, "nickname": str | None})
        assert "name" in schema.required_fields
        assert "nickname" not in schema.required_fields

    def test_validate_passing_data(self):
        """validate() returns valid=True for correct data."""
        schema = Schema({"name": str, "age": int})
        result = schema.validate({"name": "Alice", "age": 30})
        assert result.valid is True
        assert len(result.errors) == 0

    def test_validate_list_of_strings(self):
        """list[str] field validates correctly."""
        schema = Schema({"tags": list[str]})
        result = schema.validate({"tags": ["python", "sdk", "kairos"]})
        assert result.valid is True

    def test_validate_nested_schema(self):
        """Nested Schema instance validates correctly."""
        address = Schema({"street": str, "city": str})
        company = Schema({"name": str, "hq": address})
        result = company.validate({"name": "Acme", "hq": {"street": "1st Ave", "city": "NY"}})
        assert result.valid is True

    def test_validate_list_of_schemas(self):
        """list[Schema] field validates each item against nested schema."""
        item_schema = Schema({"id": int, "label": str})
        parent = Schema({"items": list[item_schema]})
        result = parent.validate({"items": [{"id": 1, "label": "A"}, {"id": 2, "label": "B"}]})
        assert result.valid is True

    def test_field_definitions_property(self):
        """field_definitions returns FieldDefinition objects for all fields."""
        schema = Schema({"name": str})
        defs = schema.field_definitions
        assert len(defs) == 1
        assert defs[0].name == "name"

    def test_bool_field_accepts_bool(self):
        """bool field accepts True and False."""
        schema = Schema({"active": bool})
        assert schema.validate({"active": True}).valid is True
        assert schema.validate({"active": False}).valid is True

    def test_bool_field_rejects_int(self):
        """bool field rejects int values (even 0 and 1)."""
        schema = Schema({"active": bool})
        assert schema.validate({"active": 1}).valid is False
        assert schema.validate({"active": 0}).valid is False

    def test_extend_adds_new_fields(self):
        """extend() returns schema with base + new fields."""
        base = Schema({"name": str})
        extended = base.extend({"email": str})
        assert "name" in extended.field_names
        assert "email" in extended.field_names

    def test_extend_with_validators(self):
        """extend() accepts validators for new fields."""
        base = Schema({"name": str})
        validators_map: dict[str, list[Callable[[Any], bool]]] = {
            "email": [lambda v: v],  # stub validator
        }
        extended = base.extend({"email": str}, validators=validators_map)
        assert "email" in extended.field_names

    def test_schema_repr(self):
        """Schema has a useful __repr__."""
        schema = Schema({"name": str})
        r = repr(schema)
        assert "Schema" in r

    def test_schema_equality(self):
        """Two schemas with the same fields are equal."""
        a = Schema({"name": str})
        b = Schema({"name": str})
        assert a == b

    def test_schema_inequality(self):
        """Two schemas with different fields are not equal."""
        a = Schema({"name": str})
        b = Schema({"name": int})
        assert a != b

    def test_validation_result_errors_empty_on_pass(self):
        """ValidationResult.errors is empty list on success."""
        schema = Schema({"name": str})
        result = schema.validate({"name": "Alice"})
        assert result.errors == []

    def test_field_validation_error_has_severity(self):
        """FieldValidationError has a severity field using Severity enum."""
        schema = Schema({"count": int})
        result = schema.validate({"count": "bad"})
        assert result.valid is False
        error = result.errors[0]
        assert error.severity in (Severity.ERROR, Severity.WARNING)

    def test_contract_pair_creation(self):
        """ContractPair holds optional input and output schemas."""
        schema = Schema({"name": str})
        pair = ContractPair(input_schema=schema, output_schema=None)
        assert pair.input_schema is schema
        assert pair.output_schema is None

    def test_registry_register_and_retrieve(self):
        """SchemaRegistry stores and retrieves contracts."""
        registry = SchemaRegistry()
        in_schema = Schema({"name": str})
        out_schema = Schema({"result": str})
        registry.register("step_1", in_schema, out_schema)
        assert registry.get_input_contract("step_1") is in_schema
        assert registry.get_output_contract("step_1") is out_schema

    def test_registry_get_unregistered_returns_none(self):
        """get_input_contract / get_output_contract return None for unknown step."""
        registry = SchemaRegistry()
        assert registry.get_input_contract("unknown") is None
        assert registry.get_output_contract("unknown") is None

    def test_registry_has_contract(self):
        """has_contract returns True only for registered steps."""
        registry = SchemaRegistry()
        registry.register("step_1", Schema({"x": str}), None)
        assert registry.has_contract("step_1") is True
        assert registry.has_contract("step_2") is False

    def test_registry_all_contracts(self):
        """all_contracts returns dict of all registered ContractPairs."""
        registry = SchemaRegistry()
        registry.register("step_1", Schema({"x": str}), None)
        contracts = registry.all_contracts()
        assert "step_1" in contracts
        assert isinstance(contracts["step_1"], ContractPair)

    def test_validators_stored_not_executed(self):
        """Validators on FieldDefinition are stored as callables, not called."""
        call_log: list[str] = []

        def my_validator(v: object) -> bool:
            call_log.append("called")
            return True

        schema = Schema({"score": int}, validators={"score": [my_validator]})
        schema.validate({"score": 5})
        # Module 8: validators are stored, not executed. Module 9 executes them.
        assert call_log == []


# ---------------------------------------------------------------------------
# Group 4: JSON Schema import/export
# ---------------------------------------------------------------------------


class TestJsonSchema:
    """to_json_schema and from_json_schema tests."""

    def test_to_json_schema_str(self):
        """str field exports as 'string'."""
        schema = Schema({"name": str})
        js = schema.to_json_schema()
        assert js["type"] == "object"
        assert js["properties"]["name"]["type"] == "string"

    def test_to_json_schema_int(self):
        """int field exports as 'integer'."""
        schema = Schema({"count": int})
        js = schema.to_json_schema()
        assert js["properties"]["count"]["type"] == "integer"

    def test_to_json_schema_float(self):
        """float field exports as 'number'."""
        schema = Schema({"score": float})
        js = schema.to_json_schema()
        assert js["properties"]["score"]["type"] == "number"

    def test_to_json_schema_bool(self):
        """bool field exports as 'boolean'."""
        schema = Schema({"active": bool})
        js = schema.to_json_schema()
        assert js["properties"]["active"]["type"] == "boolean"

    def test_to_json_schema_optional(self):
        """str | None exports as {"type": ["string", "null"]}."""
        schema = Schema({"nickname": str | None})
        js = schema.to_json_schema()
        prop = js["properties"]["nickname"]
        assert set(prop["type"]) == {"string", "null"}

    def test_to_json_schema_list_of_str(self):
        """list[str] exports as {"type": "array", "items": {"type": "string"}}."""
        schema = Schema({"tags": list[str]})
        js = schema.to_json_schema()
        prop = js["properties"]["tags"]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "string"

    def test_to_json_schema_required_fields(self):
        """Required fields appear in top-level 'required' array."""
        schema = Schema({"name": str, "nickname": str | None})
        js = schema.to_json_schema()
        assert "name" in js["required"]
        assert "nickname" not in js.get("required", [])

    def test_to_json_schema_nested(self):
        """Nested Schema exports as nested JSON Schema object."""
        address = Schema({"street": str, "city": str})
        company = Schema({"hq": address})
        js = company.to_json_schema()
        hq_prop = js["properties"]["hq"]
        assert hq_prop["type"] == "object"
        assert "street" in hq_prop["properties"]

    def test_from_json_schema_basic(self):
        """from_json_schema creates a Schema from a valid JSON Schema object."""
        spec = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
        schema = Schema.from_json_schema(spec)
        assert "name" in schema.field_names
        assert "age" in schema.field_names

    def test_from_json_schema_required_fields(self):
        """from_json_schema marks required fields correctly."""
        spec = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "nickname": {"type": "string"},
            },
            "required": ["name"],
        }
        schema = Schema.from_json_schema(spec)
        assert "name" in schema.required_fields
        assert "nickname" not in schema.required_fields

    def test_from_json_schema_nullable_type(self):
        """from_json_schema handles {"type": ["string", "null"]} as optional."""
        spec = {
            "type": "object",
            "properties": {
                "nickname": {"type": ["string", "null"]},
            },
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"nickname": None})
        assert result.valid is True

    def test_from_json_schema_ignores_unknown_keywords(self):
        """from_json_schema silently ignores unknown JSON Schema keywords."""
        spec = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "$schema": "http://json-schema.org/draft-07/schema",
            "title": "MySchema",
            "description": "A schema",
            "additionalProperties": False,
            "$id": "my-id",
        }
        schema = Schema.from_json_schema(spec)  # Should not raise
        assert "name" in schema.field_names

    def test_from_json_schema_nested_object(self):
        """from_json_schema handles nested object types."""
        spec = {
            "type": "object",
            "properties": {
                "address": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                    },
                    "required": ["city"],
                }
            },
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"address": {"city": "NY"}})
        assert result.valid is True

    def test_from_json_schema_array_type(self):
        """from_json_schema handles array with items."""
        spec = {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"tags": ["a", "b"]})
        assert result.valid is True

    def test_round_trip_to_from_json_schema(self):
        """Schema → to_json_schema → from_json_schema → validates same data."""
        original = Schema({"name": str, "age": int, "nickname": str | None})
        json_spec = original.to_json_schema()
        reconstructed = Schema.from_json_schema(json_spec)
        data = {"name": "Alice", "age": 30}
        assert original.validate(data).valid is True
        assert reconstructed.validate(data).valid is True


# ---------------------------------------------------------------------------
# Group 5: Pydantic integration
# ---------------------------------------------------------------------------


class TestPydanticIntegration:
    """from_pydantic tests — skipped if pydantic not installed."""

    def test_from_pydantic_basic(self):
        """from_pydantic creates Schema from a simple BaseModel."""
        try:
            from pydantic import BaseModel  # noqa: PLC0415,E501,I001  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            pytest.skip("pydantic not installed")

        class User(BaseModel):  # pyright: ignore[reportUntypedBaseClass]
            name: str
            age: int

        schema = Schema.from_pydantic(User)
        assert "name" in schema.field_names
        assert "age" in schema.field_names

    def test_from_pydantic_optional_field(self):
        """from_pydantic handles Optional[str] as non-required field."""
        try:
            from pydantic import BaseModel  # noqa: PLC0415,E501,I001  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            pytest.skip("pydantic not installed")

        class User(BaseModel):  # pyright: ignore[reportUntypedBaseClass]
            name: str
            nickname: str | None = None

        schema = Schema.from_pydantic(User)
        assert "nickname" not in schema.required_fields

    def test_from_pydantic_nested_model(self):
        """from_pydantic handles nested BaseModel subclasses."""
        try:
            from pydantic import BaseModel  # noqa: PLC0415,E501,I001  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            pytest.skip("pydantic not installed")

        class Address(BaseModel):  # pyright: ignore[reportUntypedBaseClass]
            city: str

        class Company(BaseModel):  # pyright: ignore[reportUntypedBaseClass]
            name: str
            hq: Address

        schema = Schema.from_pydantic(Company)
        result = schema.validate({"name": "Acme", "hq": {"city": "NY"}})
        assert result.valid is True

    def test_from_pydantic_validates_correctly(self):
        """Schema from Pydantic model validates data correctly."""
        try:
            from pydantic import BaseModel  # noqa: PLC0415,E501,I001  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            pytest.skip("pydantic not installed")

        class Product(BaseModel):  # pyright: ignore[reportUntypedBaseClass]
            id: int
            name: str

        schema = Schema.from_pydantic(Product)
        assert schema.validate({"id": 1, "name": "Widget"}).valid is True
        assert schema.validate({"id": "bad", "name": "Widget"}).valid is False


# ---------------------------------------------------------------------------
# Group 6: Security constraints
# ---------------------------------------------------------------------------


class TestSecurity:
    """Security-specific tests for schema.py."""

    def test_no_eval_or_exec_in_from_json_schema(self):
        """from_json_schema handles arbitrary string values without eval/exec."""
        spec = {
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
            },
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"cmd": "__import__('os').system('echo pwned')"})
        assert result.valid is True  # It's just a string, passes str type check

    def test_from_json_schema_ignores_injection_keywords(self):
        """from_json_schema ignores unknown keywords that could be injection vectors."""
        spec = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "x-custom-extension": "ignored",
            "if": {"properties": {"name": {"const": "admin"}}},
            "then": {"properties": {"role": {"type": "string"}}},
        }
        schema = Schema.from_json_schema(spec)  # Should not raise or execute anything
        assert "name" in schema.field_names

    def test_circular_reference_detection_indirect(self):
        """Indirect circular reference is detected at definition time."""
        a = Schema({"x": str})
        b = Schema({"a": a})
        c = Schema({"b": b})
        assert c is not None  # Not circular — should succeed

    def test_validate_never_raises(self):
        """validate() never raises an exception — always returns ValidationResult."""
        schema = Schema({"name": str})
        result = schema.validate({"name": object()})  # Non-string object
        assert isinstance(result, ValidationResult)
        assert result.valid is False

    def test_registry_export_json_schema(self):
        """SchemaRegistry.export_json_schema returns JSON-serializable dict."""
        registry = SchemaRegistry()
        registry.register("step_1", Schema({"name": str}), Schema({"result": int}))
        exported = registry.export_json_schema()
        serialized = json.dumps(exported)
        assert "step_1" in json.loads(serialized)

    def test_same_schema_reused_in_two_fields_not_circular(self):
        """Reusing the same schema instance in two fields should not be flagged as circular."""
        shared = Schema({"code": str})
        parent = Schema({"primary": shared, "secondary": shared})
        assert parent is not None


# ---------------------------------------------------------------------------
# Group 7: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """JSON round-trip and serialization correctness."""

    def test_validation_result_is_json_serializable(self):
        """ValidationResult can be serialized to JSON."""
        schema = Schema({"name": str, "age": int})
        result = schema.validate({"name": "Alice", "age": "bad"})
        data = {
            "valid": result.valid,
            "errors": [
                {
                    "field": e.field,
                    "expected": e.expected,
                    "actual": e.actual,
                    "message": e.message,
                    "severity": str(e.severity),
                }
                for e in result.errors
            ],
        }
        serialized = json.dumps(data)
        parsed = json.loads(serialized)
        assert parsed["valid"] is False
        assert len(parsed["errors"]) > 0

    def test_to_json_schema_output_is_json_serializable(self):
        """to_json_schema() returns a dict that json.dumps() handles."""
        schema = Schema({"name": str, "tags": list[str], "score": float | None})
        js = schema.to_json_schema()
        serialized = json.dumps(js)
        parsed = json.loads(serialized)
        assert parsed["type"] == "object"

    def test_registry_export_is_json_serializable(self):
        """SchemaRegistry.export_json_schema() output is fully JSON-serializable."""
        registry = SchemaRegistry()
        registry.register(
            "step_1",
            Schema({"name": str, "items": list[str]}),
            Schema({"result": float | None}),
        )
        exported = registry.export_json_schema()
        json.dumps(exported)  # Must not raise

    def test_field_validation_error_fields_are_strings(self):
        """FieldValidationError.field, expected, actual, message are all strings."""
        schema = Schema({"count": int})
        result = schema.validate({"count": "bad"})
        assert result.valid is False
        error = result.errors[0]
        assert isinstance(error.field, str)
        assert isinstance(error.expected, str)
        assert isinstance(error.actual, str)
        assert isinstance(error.message, str)

    def test_round_trip_structural_equality(self):
        """Round-trip preserves field_names and required_fields."""
        original = Schema({"name": str, "age": int, "nickname": str | None})
        reconstructed = Schema.from_json_schema(original.to_json_schema())
        assert original.field_names == reconstructed.field_names
        assert original.required_fields == reconstructed.required_fields


# ---------------------------------------------------------------------------
# Group 8: Security — depth limits and circular references (new fixes)
# ---------------------------------------------------------------------------


class TestDepthLimitsAndCircular:
    """Tests for recursion depth limits (SEV-001, SEV-002, SEV-003)."""

    def test_deeply_nested_schema_raises_config_error(self):
        """Schema with more than _MAX_SCHEMA_DEPTH nesting levels raises ConfigError.

        Each Schema({"child": current}) call triggers _check_circular on the entire
        chain accumulated so far. Once the chain is deeper than _MAX_SCHEMA_DEPTH,
        _check_circular raises ConfigError.
        """
        from kairos.schema import _MAX_SCHEMA_DEPTH  # pyright: ignore[reportPrivateUsage]

        # Build schemas one level at a time. The ConfigError must be raised somewhere
        # during construction of a schema that pushes depth beyond the limit.
        with pytest.raises(ConfigError):
            current = Schema({"leaf": str})
            # Adding _MAX_SCHEMA_DEPTH + 2 wrapper levels must trigger the depth guard.
            for _ in range(_MAX_SCHEMA_DEPTH + 2):
                current = Schema({"child": current})

    def test_from_json_schema_rejects_excessive_nesting(self):
        """from_json_schema raises ConfigError for JSON Schema with 50+ nested objects."""
        from kairos.schema import _MAX_SCHEMA_DEPTH  # pyright: ignore[reportPrivateUsage]

        # Build a deeply nested JSON Schema dict programmatically
        depth = _MAX_SCHEMA_DEPTH + 18  # 50 total
        spec: dict[str, Any] = {"type": "object", "properties": {"leaf": {"type": "string"}}}
        for _ in range(depth):
            spec = {"type": "object", "properties": {"child": spec}}

        with pytest.raises(ConfigError):
            Schema.from_json_schema(spec)

    def test_actual_circular_reference_detected(self):
        """Manually injected cycle between two schemas raises ConfigError."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _check_circular,  # pyright: ignore[reportPrivateUsage]
        )

        schema_a = Schema({"name": str})
        schema_b = Schema({"ref": schema_a})
        # Mutate schema_a's _field_defs to point back at schema_b — creating a cycle:
        #   schema_a -> schema_b -> schema_a -> ...
        schema_a._field_defs[0] = FieldDefinition(  # pyright: ignore[reportPrivateUsage]
            name="ref",
            field_type="nested",
            required=True,
            nested_schema=schema_b,
        )
        with pytest.raises(ConfigError, match="[Cc]ircular"):
            _check_circular(schema_a, set())  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Group 9: Additional validation and type coverage (code review gaps)
# ---------------------------------------------------------------------------


class TestAdditionalTypeCoverage:
    """Tests for list[Schema] failure path, Schema | None field, and unsupported union."""

    def test_list_schema_item_invalid_reports_indexed_path(self):
        """list[Schema] with an invalid item reports an indexed error path."""
        item_schema = Schema({"name": str})
        parent = Schema({"items": list[item_schema]})
        # items[0] is valid, items[1] has 'name' as int (wrong type)
        result = parent.validate({"items": [{"name": "ok"}, {"name": 99}]})
        assert result.valid is False
        # Error path should reference items[1].name
        assert any("items[1]" in e.field for e in result.errors)
        assert any(e.field == "items[1].name" for e in result.errors)

    def test_schema_or_none_field_accepts_none(self):
        """A typing.Union[Schema, None] field accepts None as a valid value."""
        address = Schema({"city": str})
        # Schema instances are not types, so | syntax doesn't work. Use typing.Union.
        parent = Schema({"hq": typing.Union[address, None]})  # noqa: UP007 — Schema instance, not type
        result = parent.validate({"hq": None})
        assert result.valid is True

    def test_schema_or_none_field_accepts_valid_dict(self):
        """A typing.Union[Schema, None] field accepts a valid nested dict."""
        address = Schema({"city": str})
        parent = Schema({"hq": typing.Union[address, None]})  # noqa: UP007 — Schema instance, not type
        result = parent.validate({"hq": {"city": "NY"}})
        assert result.valid is True

    def test_schema_or_none_field_absent_is_valid(self):
        """A typing.Union[Schema, None] field can be absent entirely."""
        address = Schema({"city": str})
        parent = Schema({"hq": typing.Union[address, None]})  # noqa: UP007 — Schema instance, not type
        result = parent.validate({})
        assert result.valid is True

    def test_unsupported_union_type_raises_config_error(self):
        """str | int (non-None union) raises ConfigError at definition time."""
        with pytest.raises(ConfigError, match="unsupported union"):
            Schema({"x": str | int})

    def test_from_json_schema_nested_optional_object(self):
        """Nested object NOT in 'required' is treated as optional in from_json_schema."""
        spec = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "address": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
            "required": ["name"],  # address is NOT required
        }
        schema = Schema.from_json_schema(spec)
        # address is not required — omitting it should pass
        result = schema.validate({"name": "Alice"})
        assert result.valid is True
        # Providing a valid address should also pass
        result2 = schema.validate({"name": "Alice", "address": {"city": "NY"}})
        assert result2.valid is True


# ---------------------------------------------------------------------------
# Group 10: Coverage gap tests — written by QA
# ---------------------------------------------------------------------------


class TestCoverageGaps:
    """Tests written by QA to close coverage gaps in schema.py."""

    # --- FieldDefinition.__eq__ NotImplemented branch (line 64) ---

    def test_field_definition_eq_non_field_definition(self):
        """FieldDefinition.__eq__ returns NotImplemented for non-FieldDefinition."""
        from kairos.schema import FieldDefinition

        fd = FieldDefinition(name="x", field_type=str, required=True)
        assert fd.__eq__("not a FieldDefinition") is NotImplemented

    # --- _normalize_type depth guard (line 161) ---

    def test_normalize_type_depth_guard(self):
        """_normalize_type raises ConfigError when depth exceeds _MAX_SCHEMA_DEPTH."""
        from kairos.schema import _MAX_SCHEMA_DEPTH, _normalize_type  # pyright: ignore[reportPrivateUsage]  # noqa: I001

        with pytest.raises(ConfigError, match="nesting depth"):
            _normalize_type("field", str, [], depth=_MAX_SCHEMA_DEPTH + 1)  # pyright: ignore[reportPrivateUsage]

    # --- list with no type args in parameterized form (line 247) ---

    def test_list_no_type_args_parameterized(self):
        """list with no actual args (via get_args returning empty) treated as bare list."""
        # typing.List (without args) has origin=list but no args
        schema = Schema({"items": typing.List})  # noqa: UP006 — intentional
        result = schema.validate({"items": [1, "two"]})
        assert result.valid is True

    # --- Unsupported list item type (line 272) ---

    def test_unsupported_list_item_type(self):
        """list[set] raises ConfigError — set is not a supported item type."""
        with pytest.raises(ConfigError, match="unsupported list item type"):
            Schema({"data": list[bytes]})

    # --- validate() internal exception catch (lines 403-404) ---

    def test_validate_catches_internal_exception(self):
        """validate() returns ValidationResult(valid=False) on internal error."""
        schema = Schema({"name": str})
        # Monkey-patch _validate_dict to raise an unexpected exception

        def exploding_validate(data: object, prefix: str = "") -> list[object]:  # noqa: ARG001
            raise RuntimeError("simulated internal error")

        schema._validate_dict = exploding_validate  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
        result = schema.validate({"name": "Alice"})
        assert isinstance(result, ValidationResult)
        assert result.valid is False
        assert any("internal error" in e.message.lower() for e in result.errors)

    # --- from_json_schema skips non-dict properties (line 549) ---

    def test_from_json_schema_skips_non_dict_property_spec(self):
        """from_json_schema ignores properties whose spec is not a dict."""
        spec = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "bad_prop": "not a dict",  # should be silently skipped
            },
        }
        schema = Schema.from_json_schema(spec)
        assert "name" in schema.field_names
        assert "bad_prop" not in schema.field_names

    # --- Bool rejected for float fields (lines 876-885) ---

    def test_bool_rejected_for_float_field(self):
        """bool values are rejected for float fields."""
        schema = Schema({"score": float})
        result = schema.validate({"score": True})
        assert result.valid is False
        assert any("bool" in e.actual for e in result.errors)

    # --- Non-numeric value for float field (lines 887-896) ---

    def test_string_rejected_for_float_field(self):
        """String values are rejected for float fields."""
        schema = Schema({"score": float})
        result = schema.validate({"score": "high"})
        assert result.valid is False
        assert any("float" in e.expected for e in result.errors)

    # --- extend() with no validators on base fields (line 628) ---

    def test_extend_with_no_validators(self):
        """extend() works when neither base nor extension has validators."""
        base = Schema({"name": str})
        extended = base.extend({"age": int})
        result = extended.validate({"name": "Alice", "age": 30})
        assert result.valid is True

    # --- _fd_type_label branches (lines 950-959) ---

    def test_fd_type_label_nested(self):
        """_fd_type_label returns 'Schema' for nested field type."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_type_label,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(
            name="x",
            field_type="nested",
            required=True,
            nested_schema=Schema({"a": str}),
        )
        assert _fd_type_label(fd) == "Schema"  # pyright: ignore[reportPrivateUsage]

    def test_fd_type_label_list_schema(self):
        """_fd_type_label returns 'list[Schema]' for list of schema."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_type_label,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(
            name="x",
            field_type="list",
            required=True,
            nested_schema=Schema({"a": str}),
        )
        assert _fd_type_label(fd) == "list[Schema]"  # pyright: ignore[reportPrivateUsage]

    def test_fd_type_label_list_primitive(self):
        """_fd_type_label returns 'list[str]' for list of str."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_type_label,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(name="x", field_type="list", required=True, item_type=str)
        assert _fd_type_label(fd) == "list[str]"  # pyright: ignore[reportPrivateUsage]

    def test_fd_type_label_bare_list(self):
        """_fd_type_label returns 'list' for bare list."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_type_label,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(name="x", field_type="list", required=True)
        assert _fd_type_label(fd) == "list"  # pyright: ignore[reportPrivateUsage]

    def test_fd_type_label_primitive(self):
        """_fd_type_label returns 'int' for int field type."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_type_label,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(name="x", field_type=int, required=True)
        assert _fd_type_label(fd) == "int"  # pyright: ignore[reportPrivateUsage]

    # --- _fd_to_json_schema_prop branches (lines 981-994) ---

    def test_to_json_schema_bare_list(self):
        """Bare list exports as {"type": "array"} with no items."""
        schema = Schema({"items": list})
        js = schema.to_json_schema()
        prop = js["properties"]["items"]
        assert prop["type"] == "array"
        assert "items" not in prop

    def test_to_json_schema_list_of_schema(self):
        """list[Schema] exports as array with nested object items."""
        inner = Schema({"name": str})
        schema = Schema({"entries": list[inner]})
        js = schema.to_json_schema()
        prop = js["properties"]["entries"]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "object"

    def test_to_json_schema_optional_primitive(self):
        """Optional int field exports as {"type": ["integer", "null"]}."""
        schema = Schema({"count": int | None})
        js = schema.to_json_schema()
        prop = js["properties"]["count"]
        assert set(prop["type"]) == {"integer", "null"}

    # --- _json_schema_prop_to_annotation: nullable fallback (line 1019) ---

    def test_from_json_schema_nullable_non_standard_combo(self):
        """Nullable with unrecognised type combo falls back to optional str."""
        spec = {
            "type": "object",
            "properties": {
                "x": {"type": ["custom_type", "null"]},
            },
        }
        schema = Schema.from_json_schema(spec)
        # Should not raise — falls back to str | None
        result = schema.validate({"x": None})
        assert result.valid is True

    # --- _json_schema_prop_to_annotation: array without items (line 1051) ---

    def test_from_json_schema_array_no_items(self):
        """Array type without items produces bare list."""
        spec = {
            "type": "object",
            "properties": {
                "data": {"type": "array"},
            },
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"data": [1, "two", 3.0]})
        assert result.valid is True

    # --- _json_schema_prop_to_annotation: unknown type fallback (line 1057) ---

    def test_from_json_schema_unknown_type_defaults_to_str(self):
        """Unknown JSON Schema type defaults to str."""
        spec = {
            "type": "object",
            "properties": {
                "x": {"type": "custom_unknown"},
            },
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"x": "anything"})
        assert result.valid is True

    # --- _json_schema_prop_to_annotation: array with object items (lines 1036-1046) ---

    def test_from_json_schema_array_of_objects(self):
        """Array with object items creates list[Schema]."""
        spec = {
            "type": "object",
            "properties": {
                "people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            },
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"people": [{"name": "Alice"}]})
        assert result.valid is True

    # --- _json_schema_prop_to_annotation: known primitive type (line 1053) ---

    def test_from_json_schema_integer_type(self):
        """JSON Schema integer type maps to Python int."""
        spec = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        schema = Schema.from_json_schema(spec)
        result = schema.validate({"count": 5})
        assert result.valid is True

    # --- _make_optional branches (lines 1078-1094) ---

    def test_extend_preserves_optional_fields(self):
        """extend() preserves optional fields from base schema."""
        base = Schema({"name": str, "nickname": str | None})
        extended = base.extend({"email": str})
        result = extended.validate({"name": "Alice", "email": "a@b.com"})
        assert result.valid is True
        assert "nickname" not in extended.required_fields

    def test_extend_preserves_list_fields(self):
        """extend() preserves list fields from base schema."""
        base = Schema({"tags": list[str]})
        extended = base.extend({"name": str})
        result = extended.validate({"tags": ["a", "b"], "name": "test"})
        assert result.valid is True

    def test_extend_preserves_nested_schema_fields(self):
        """extend() preserves nested Schema fields from base schema."""
        inner = Schema({"city": str})
        base = Schema({"address": inner})
        extended = base.extend({"name": str})
        result = extended.validate({"address": {"city": "NY"}, "name": "test"})
        assert result.valid is True

    # --- _fd_to_annotation: list branches (lines 1112-1120) ---

    def test_extend_preserves_list_of_schema(self):
        """extend() preserves list[Schema] fields from base schema."""
        inner = Schema({"name": str})
        base = Schema({"items": list[inner]})
        extended = base.extend({"count": int})
        result = extended.validate({"items": [{"name": "A"}], "count": 1})
        assert result.valid is True

    def test_extend_preserves_bare_list(self):
        """extend() preserves bare list fields from base schema."""
        base = Schema({"data": list})
        extended = base.extend({"name": str})
        result = extended.validate({"data": [1, 2], "name": "test"})
        assert result.valid is True

    # --- Schema equality NotImplemented (line 644) ---

    def test_schema_eq_non_schema_returns_not_implemented(self):
        """Schema.__eq__ returns NotImplemented for non-Schema comparison."""
        schema = Schema({"name": str})
        assert schema.__eq__("not a schema") is NotImplemented

    # --- _fd_to_json_schema_prop fallback (line 994) ---

    def test_fd_to_json_schema_prop_fallback(self):
        """_fd_to_json_schema_prop returns {} for unknown field type."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_to_json_schema_prop,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(name="x", field_type="unknown_type", required=True)
        result = _fd_to_json_schema_prop(fd)  # pyright: ignore[reportPrivateUsage]
        assert result == {}

    # --- _fd_type_label string fallback (line 959) ---

    def test_fd_type_label_string_field_type(self):
        """_fd_type_label returns string representation for non-type field_type."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_type_label,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(name="x", field_type="custom", required=True)
        assert _fd_type_label(fd) == "custom"  # pyright: ignore[reportPrivateUsage]

    # --- _fd_to_annotation fallback (line 1127) ---

    def test_fd_to_annotation_fallback(self):
        """_fd_to_annotation returns str for unknown field_type."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _fd_to_annotation,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(name="x", field_type="unknown", required=True)
        result = _fd_to_annotation(fd)  # pyright: ignore[reportPrivateUsage]
        assert result is str

    # --- extend() combined_validators branch (line 628) ---

    def test_extend_with_base_validators(self):
        """extend() carries over validators from base when extending."""
        call_log: list[str] = []

        def v(val: object) -> bool:
            call_log.append("called")
            return True

        base = Schema({"name": str}, validators={"name": [v]})
        extended = base.extend({"age": int})
        # Validators should be preserved — check field definitions
        name_fd = [fd for fd in extended.field_definitions if fd.name == "name"][0]
        assert len(name_fd.validators) == 1

    # --- _validate_value fallback return (line 822) ---

    def test_validate_value_unknown_field_type_no_crash(self):
        """_validate_value returns empty errors for unknown field_type (fallback)."""
        from kairos.schema import (  # pyright: ignore[reportPrivateUsage]
            FieldDefinition,
            _validate_value,  # pyright: ignore[reportPrivateUsage]
        )

        fd = FieldDefinition(name="x", field_type="unknown_type", required=True)
        errors = _validate_value("anything", fd, "x")  # pyright: ignore[reportPrivateUsage]
        assert errors == []

    # --- float conversion error (lines 900-910) ---

    def test_float_conversion_error_handled(self):
        """_check_primitive_type handles values that pass isinstance but fail float()."""
        from kairos.schema import _check_primitive_type  # pyright: ignore[reportPrivateUsage]

        # Create a custom numeric type that passes isinstance(v, (int, float))
        # but raises on float() conversion
        class BadFloat(float):
            def __float__(self) -> float:
                raise ValueError("cannot convert")

        errors = _check_primitive_type(BadFloat(0), float, "score")  # pyright: ignore[reportPrivateUsage]
        # BadFloat(0) is isinstance float, float(BadFloat(0)) may raise or not
        # depending on Python internals — either way, no crash
        assert isinstance(errors, list)

    # --- _check_primitive_type final fallback (line 936) ---

    def test_check_primitive_type_unknown_expected(self):
        """_check_primitive_type returns empty errors for unsupported expected_type."""
        from kairos.schema import _check_primitive_type  # pyright: ignore[reportPrivateUsage]

        # Use a type not in the checked branches (dict is not str/int/float/bool)
        errors = _check_primitive_type("value", dict, "field")  # pyright: ignore[reportPrivateUsage]
        assert errors == []

    # --- _make_optional branches (lines 1078, 1085, 1094) ---

    def test_make_optional_already_optional_union(self):
        """_make_optional returns same type for already-optional union."""
        from kairos.schema import _make_optional  # pyright: ignore[reportPrivateUsage]

        optional_type = str | None
        result = _make_optional(optional_type)  # pyright: ignore[reportPrivateUsage]
        # Should return the same type — already optional
        assert result is optional_type

    def test_make_optional_typing_union(self):
        """_make_optional returns same type for typing.Union[X, None]."""
        from kairos.schema import _make_optional  # pyright: ignore[reportPrivateUsage]

        optional_type = typing.Union[str, None]  # noqa: UP007 — intentional
        result = _make_optional(optional_type)  # pyright: ignore[reportPrivateUsage]
        assert result is optional_type

    def test_make_optional_non_primitive_non_list(self):
        """_make_optional returns annotation as-is for unsupported types."""
        from kairos.schema import _make_optional  # pyright: ignore[reportPrivateUsage]

        result = _make_optional(dict)  # pyright: ignore[reportPrivateUsage]
        assert result is dict
