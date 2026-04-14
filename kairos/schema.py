"""Kairos schema — Schema DSL, validation result types, and SchemaRegistry."""

from __future__ import annotations

import math
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast, get_args, get_origin

from kairos.enums import Severity
from kairos.exceptions import ConfigError

# ---------------------------------------------------------------------------
# Supported primitive types — anything else is a ConfigError at definition time.
# ---------------------------------------------------------------------------

_PRIMITIVE_TYPES: frozenset[type] = frozenset({str, int, float, bool})

# Maximum nesting depth allowed in schema definitions and JSON Schema imports.
# Prevents unhandled RecursionError from deeply nested or crafted schemas.
_MAX_SCHEMA_DEPTH: int = 32

# Mapping from Python type to JSON Schema type string
_TYPE_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

# Mapping from JSON Schema type string to Python type
_JSON_TO_TYPE: dict[str, type] = {v: k for k, v in _TYPE_TO_JSON.items()}


# ---------------------------------------------------------------------------
# FieldDefinition — internal canonical form for a single field
# ---------------------------------------------------------------------------


@dataclass
class FieldDefinition:
    """Canonical representation of one field in a Schema.

    Attributes:
        name: The field name (key).
        field_type: The Python type for this field (str, int, float, bool, list, Schema).
        required: Whether the field must be present.
        validators: Callables stored for later execution by the validators module.
        item_type: For list[T] fields — the type T (None for bare list).
        nested_schema: For nested Schema fields or list[Schema] item type.
    """

    name: str
    field_type: type | str  # 'list' | 'nested' | 'optional_<X>' or a primitive type
    required: bool
    validators: list[Callable[..., Any]] = field(default_factory=lambda: [])
    item_type: type | None = None  # primitive type for list[T]
    nested_schema: Schema | None = None  # Schema for nested / list[Schema]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FieldDefinition):
            return NotImplemented
        # Exclude validators (callables) from equality — compare structure only
        return (
            self.name == other.name
            and self.field_type == other.field_type
            and self.required == other.required
            and self.item_type == other.item_type
            and self.nested_schema == other.nested_schema
        )


# ---------------------------------------------------------------------------
# FieldValidationError — single field-level validation error
# ---------------------------------------------------------------------------


@dataclass
class FieldValidationError:
    """A single field-level validation error.

    Attributes:
        field: Dot-separated path to the field (e.g. "address.zip" or "items[0]").
        expected: Human-readable description of the expected type/value.
        actual: Human-readable description of what was found.
        message: A full human-readable error message.
        severity: ERROR or WARNING from the Severity enum.
    """

    field: str
    expected: str
    actual: str
    message: str
    severity: Severity = Severity.ERROR


# ---------------------------------------------------------------------------
# ValidationResult — outcome of Schema.validate()
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of a schema validation.

    Attributes:
        valid: True if all checks passed.
        errors: List of field-level errors (empty when valid is True).
        metadata: Optional dict for validator-specific data (e.g. LLMValidator
            stores "confidence" and "raw_response" here).
    """

    valid: bool
    errors: list[FieldValidationError] = field(default_factory=lambda: [])
    metadata: dict[str, object] = field(default_factory=lambda: {})


# ---------------------------------------------------------------------------
# ContractPair — input + output schema pair for a step
# ---------------------------------------------------------------------------


@dataclass
class ContractPair:
    """Input/output contract pair for a workflow step.

    Attributes:
        input_schema: Schema for the step's input (may be None).
        output_schema: Schema for the step's output (may be None).
    """

    input_schema: Schema | None
    output_schema: Schema | None


# ---------------------------------------------------------------------------
# Internal helpers — type normalization
# ---------------------------------------------------------------------------


def _normalize_type(
    name: str,
    annotation: Any,
    validators: list[Callable[..., Any]],
    depth: int = 0,
) -> FieldDefinition:
    """Translate a DSL annotation into a FieldDefinition.

    Args:
        name: Field name.
        annotation: The Python type annotation from the Schema DSL dict.
        validators: Pre-collected validators for this field.
        depth: Current recursion depth — raises ConfigError beyond _MAX_SCHEMA_DEPTH.

    Returns:
        A FieldDefinition for the field.

    Raises:
        ConfigError: If the annotation is not a supported type or nesting is too deep.
    """
    if depth > _MAX_SCHEMA_DEPTH:
        raise ConfigError(
            f"Field '{name}': schema nesting depth exceeds the maximum of "
            f"{_MAX_SCHEMA_DEPTH}. Reduce schema nesting."
        )

    # ------------------------------------------------------------------
    # Schema instance → nested required field
    # ------------------------------------------------------------------
    if isinstance(annotation, Schema):
        return FieldDefinition(
            name=name,
            field_type="nested",
            required=True,
            validators=validators,
            nested_schema=annotation,
        )

    # ------------------------------------------------------------------
    # bare list (no type argument)
    # ------------------------------------------------------------------
    if annotation is list:
        return FieldDefinition(
            name=name,
            field_type="list",
            required=True,
            validators=validators,
        )

    # ------------------------------------------------------------------
    # Compute origin early — needed before frozenset membership check
    # to avoid TypeError when annotation is a parameterized generic (e.g.
    # list[Schema(...)] where the Schema instance is unhashable in a set).
    # ------------------------------------------------------------------
    origin = get_origin(annotation)

    # ------------------------------------------------------------------
    # Primitive types: str, int, float, bool
    # (Only check after confirming it's not a generic — generics are not
    # hashable in all Python versions and would raise TypeError in `in`.)
    # ------------------------------------------------------------------
    if origin is None and isinstance(annotation, type) and annotation in _PRIMITIVE_TYPES:
        return FieldDefinition(
            name=name,
            field_type=annotation,
            required=True,
            validators=validators,
        )

    # ------------------------------------------------------------------
    # Python 3.10+ union syntax: X | None  (types.UnionType)
    # Also handles typing.Union[X, None] / typing.Optional[X]
    # ------------------------------------------------------------------

    if isinstance(annotation, types.UnionType) or origin is typing.Union:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        # Check Schema instance FIRST — Schema instances are unhashable so the
        # `in _PRIMITIVE_TYPES` frozenset membership check below would raise
        # TypeError if we don't guard for Schema first.
        if len(non_none) == 1 and isinstance(non_none[0], Schema):
            return FieldDefinition(
                name=name,
                field_type="nested",
                required=False,
                validators=validators,
                nested_schema=non_none[0],
            )
        if len(non_none) == 1 and non_none[0] in _PRIMITIVE_TYPES:
            return FieldDefinition(
                name=name,
                field_type=non_none[0],
                required=False,
                validators=validators,
            )
        raise ConfigError(
            f"Field '{name}': unsupported union type {annotation!r}. "
            "Only 'T | None' unions with primitive or Schema types are supported."
        )

    # ------------------------------------------------------------------
    # list[T] — parameterized list
    # ------------------------------------------------------------------
    if origin is list:
        args = get_args(annotation)
        if not args:
            # list with no args — treated same as bare list
            return FieldDefinition(
                name=name,
                field_type="list",
                required=True,
                validators=validators,
            )
        item_ann = args[0]
        # list[Schema instance]
        if isinstance(item_ann, Schema):
            return FieldDefinition(
                name=name,
                field_type="list",
                required=True,
                validators=validators,
                nested_schema=item_ann,
            )
        # list[primitive]
        if item_ann in _PRIMITIVE_TYPES:
            return FieldDefinition(
                name=name,
                field_type="list",
                required=True,
                validators=validators,
                item_type=item_ann,
            )
        raise ConfigError(
            f"Field '{name}': unsupported list item type {item_ann!r}. "
            "List items must be primitive types or Schema instances."
        )

    raise ConfigError(
        f"Field '{name}': unsupported type {annotation!r}. "
        "Supported types: str, int, float, bool, list, list[T], T | None, Schema."
    )


def _check_circular(schema: Schema, seen: set[int], depth: int = 0) -> None:
    """Traverse nested schemas using id() to detect cycles.

    Args:
        schema: The schema to check.
        seen: Set of schema ids already on the current traversal path.
        depth: Current recursion depth — raises ConfigError beyond _MAX_SCHEMA_DEPTH.

    Raises:
        ConfigError: If a circular reference is detected or nesting is too deep.
    """
    if depth > _MAX_SCHEMA_DEPTH:
        raise ConfigError(
            f"Schema nesting depth exceeds the maximum of {_MAX_SCHEMA_DEPTH}. "
            "Reduce schema nesting."
        )
    schema_id = id(schema)
    if schema_id in seen:
        raise ConfigError(
            "Circular schema reference detected. "
            "Schema cannot reference itself directly or indirectly."
        )
    new_seen = seen | {schema_id}
    for fd in schema.field_definitions:
        if fd.nested_schema is not None:
            _check_circular(fd.nested_schema, new_seen, depth + 1)


# ---------------------------------------------------------------------------
# Schema — main class
# ---------------------------------------------------------------------------


class Schema:
    """A lightweight schema definition for Kairos workflow contracts.

    The Schema DSL accepts Python types and normalizes them into FieldDefinitions.
    Validation is structural only — type checking, required-field enforcement, and
    NaN/Inf rejection. Validator callables are stored but not executed here; that
    is the responsibility of the validators module (Module 9).

    Args:
        fields: Mapping of field name to type annotation (the DSL).
        validators: Optional mapping of field name to list of validator callables.

    Raises:
        ConfigError: If any annotation is unsupported or a circular reference exists.

    Example:
        >>> schema = Schema({"name": str, "score": float | None})
        >>> schema.validate({"name": "Alice"})
        ValidationResult(valid=True, errors=[])
    """

    def __init__(
        self,
        fields: dict[str, Any],
        validators: dict[str, list[Callable[..., Any]]] | None = None,
    ) -> None:
        validators = validators or {}
        self._field_defs: list[FieldDefinition] = []

        for fname, annotation in fields.items():
            field_validators = validators.get(fname, [])
            fd = _normalize_type(fname, annotation, field_validators)
            self._field_defs.append(fd)

        # Circular reference detection — runs after all FieldDefinitions are built
        _check_circular(self, set())

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def field_definitions(self) -> list[FieldDefinition]:
        """Return all FieldDefinition objects for this schema.

        Returns:
            A list of FieldDefinition instances, one per field.
        """
        return list(self._field_defs)

    @property
    def field_names(self) -> list[str]:
        """Return all field names.

        Returns:
            A list of field name strings.
        """
        return [fd.name for fd in self._field_defs]

    @property
    def required_fields(self) -> list[str]:
        """Return names of required fields.

        Returns:
            A list of field names where required=True.
        """
        return [fd.name for fd in self._field_defs if fd.required]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_dict(self, data: Any, prefix: str = "") -> ValidationResult:
        """Validate data against this schema, returning errors with a path prefix.

        This is a public variant of ``_validate_dict`` used when callers need to
        embed nested-schema errors into a parent path (e.g. ``"address.street"``).

        Args:
            data: The value to validate. Must be a dict to be valid.
            prefix: Dot-separated prefix prepended to all error field paths.

        Returns:
            ValidationResult with scoped error paths.
        """
        return self._validate_dict(data, prefix=prefix)

    def validate(self, data: Any) -> ValidationResult:
        """Validate data against this schema.

        Performs structural validation: type checking, required-field enforcement,
        NaN/Inf rejection for floats, and recursive validation for nested schemas.
        Never raises — always returns a ValidationResult.

        Args:
            data: The value to validate. Must be a dict to be valid.

        Returns:
            ValidationResult with valid=True (no errors) or valid=False (with errors).
        """
        try:
            return self._validate_dict(data, prefix="")
        except Exception as exc:  # noqa: BLE001 — validation must never crash
            return ValidationResult(
                valid=False,
                errors=[
                    FieldValidationError(
                        field="<schema>",
                        expected="dict",
                        actual=type(data).__name__,
                        message=(
                            f"Unexpected internal error during validation ({type(exc).__name__})."
                        ),
                        severity=Severity.ERROR,
                    )
                ],
            )

    def _validate_dict(self, data: Any, prefix: str) -> ValidationResult:
        """Internal recursive validation against a dict.

        Args:
            data: The value to validate.
            prefix: Dot-separated field path prefix for nested error reporting.

        Returns:
            ValidationResult.
        """
        if not isinstance(data, dict):
            field_path = prefix.rstrip(".") or "<root>"
            return ValidationResult(
                valid=False,
                errors=[
                    FieldValidationError(
                        field=field_path,
                        expected="dict",
                        actual=type(data).__name__,
                        message=f"Expected a dict at '{field_path}', got {type(data).__name__}.",
                        severity=Severity.ERROR,
                    )
                ],
            )

        errors: list[FieldValidationError] = []
        data_dict: dict[str, Any] = cast(dict[str, Any], data)

        for fd in self._field_defs:
            path = f"{prefix}{fd.name}"
            present = fd.name in data_dict

            # ----------------------------------------------------------
            # Missing required field
            # ----------------------------------------------------------
            if not present:
                if fd.required:
                    errors.append(
                        FieldValidationError(
                            field=path,
                            expected=_fd_type_label(fd),
                            actual="missing",
                            message=f"Required field '{path}' is missing.",
                            severity=Severity.ERROR,
                        )
                    )
                # Optional and absent — fine, skip
                continue

            value: Any = data_dict[fd.name]

            # ----------------------------------------------------------
            # Optional field present as None — valid
            # ----------------------------------------------------------
            if not fd.required and value is None:
                continue

            # ----------------------------------------------------------
            # Validate by field_type
            # ----------------------------------------------------------
            field_errors = _validate_value(value, fd, path)
            errors.extend(field_errors)

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    # ------------------------------------------------------------------
    # JSON Schema export
    # ------------------------------------------------------------------

    def to_json_schema(self) -> dict[str, Any]:
        """Export this schema as a JSON Schema object.

        Returns:
            A JSON Schema dict with "type": "object", "properties", and optionally
            "required".
        """
        properties: dict[str, Any] = {}
        required: list[str] = []

        for fd in self._field_defs:
            properties[fd.name] = _fd_to_json_schema_prop(fd)
            if fd.required:
                required.append(fd.name)

        result: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            result["required"] = required
        return result

    # ------------------------------------------------------------------
    # JSON Schema import
    # ------------------------------------------------------------------

    @classmethod
    def from_json_schema(cls, spec: Any, *, _depth: int = 0) -> Schema:
        """Create a Schema from a JSON Schema dict.

        Only processes known safe keywords: type, properties, required, items.
        Unknown keywords are silently ignored for security.

        Args:
            spec: A JSON Schema dict. Must have {"type": "object"}.
            _depth: Internal recursion depth counter — not part of public API.

        Returns:
            A new Schema instance.

        Raises:
            ConfigError: If spec is not a dict, type is not "object", or nesting
                is too deep.
        """
        if _depth > _MAX_SCHEMA_DEPTH:
            raise ConfigError(
                f"from_json_schema: JSON Schema nesting depth exceeds the maximum of "
                f"{_MAX_SCHEMA_DEPTH}. Reduce schema nesting."
            )
        if not isinstance(spec, dict):
            raise ConfigError(f"from_json_schema requires a dict, got {type(spec).__name__}.")
        spec_typed = cast(dict[str, Any], spec)
        schema_type: Any = spec_typed.get("type")
        if schema_type != "object":
            raise ConfigError(
                f"from_json_schema requires a JSON Schema object (type='object'), "
                f"got type={schema_type!r}."
            )

        properties: dict[str, Any] = cast(dict[str, Any], spec_typed.get("properties") or {})
        required_names: list[str] = cast(list[str], spec_typed.get("required") or [])

        fields_dict: dict[str, Any] = {}
        for prop_name, prop_spec in properties.items():
            if not isinstance(prop_spec, dict):
                continue
            prop_spec_typed = cast(dict[str, Any], prop_spec)
            annotation = _json_schema_prop_to_annotation(prop_spec_typed, prop_name, _depth + 1)
            # Apply optional wrapping if not in required list
            if prop_name not in required_names:
                annotation = _make_optional(annotation, nested_required=False)
            fields_dict[prop_name] = annotation

        return cls(fields_dict)

    # ------------------------------------------------------------------
    # Pydantic integration
    # ------------------------------------------------------------------

    @classmethod
    def from_pydantic(cls, model: Any) -> Schema:
        """Create a Schema from a Pydantic BaseModel class.

        Args:
            model: A Pydantic v2 BaseModel subclass.

        Returns:
            A new Schema instance mirroring the Pydantic model's fields.

        Raises:
            ConfigError: If pydantic is not installed or model is not a BaseModel subclass.
        """
        try:
            import pydantic  # noqa: PLC0415,F401,I001
            from pydantic import BaseModel  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415,E501,I001
        except ImportError as exc:
            raise ConfigError(
                "pydantic is required for Schema.from_pydantic(). "
                "Install it with: pip install pydantic"
            ) from exc

        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise ConfigError(f"from_pydantic requires a BaseModel subclass, got {model!r}.")

        fields_dict: dict[str, Any] = {}
        # model is confirmed to be a BaseModel subclass at this point.
        bm_model: Any = model
        model_fields = bm_model.model_fields  # Pydantic v2

        for fname, field_info in model_fields.items():
            annotation = field_info.annotation
            is_required = field_info.is_required()
            resolved = _pydantic_annotation_to_kairos(annotation, fname)
            if not is_required:
                resolved = _make_optional(resolved)
            fields_dict[fname] = resolved

        return cls(fields_dict)

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def extend(
        self,
        fields: dict[str, Any],
        validators: dict[str, list[Callable[..., Any]]] | None = None,
    ) -> Schema:
        """Return a new Schema extending this one with additional or overriding fields.

        Args:
            fields: New fields (or overrides) to add.
            validators: Optional validators for the new fields.

        Returns:
            A new Schema with this schema's fields merged with the extension fields.
            Extension fields take precedence.
        """
        # Rebuild a combined fields dict: base fields then overrides
        base_fields: dict[str, Any] = {}
        base_validators: dict[str, list[Callable[..., Any]]] = {}

        for fd in self._field_defs:
            base_fields[fd.name] = _fd_to_annotation(fd)
            if fd.validators:
                base_validators[fd.name] = fd.validators

        # Extension fields override base
        combined_fields = {**base_fields, **fields}
        combined_validators = {**base_validators, **(validators or {})}
        return Schema(combined_fields, combined_validators if combined_validators else None)

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        fields_repr = ", ".join(f"{fd.name}: {_fd_type_label(fd)}" for fd in self._field_defs)
        return f"Schema({{{fields_repr}}})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Schema):
            return NotImplemented
        return self._field_defs == other.field_definitions


# ---------------------------------------------------------------------------
# SchemaRegistry — internal lookup table (NOT exported from __init__.py)
# ---------------------------------------------------------------------------


class SchemaRegistry:
    """Internal registry mapping step IDs to input/output ContractPairs.

    Populated automatically by Workflow from step contracts. Not part of the
    public API — developers never interact with this directly.
    """

    def __init__(self) -> None:
        self._contracts: dict[str, ContractPair] = {}

    def register(
        self,
        step_id: str,
        input_schema: Schema | None,
        output_schema: Schema | None,
    ) -> None:
        """Register input/output contracts for a step.

        Args:
            step_id: The unique step identifier.
            input_schema: Input contract (or None).
            output_schema: Output contract (or None).

        Raises:
            ConfigError: If step_id is empty or already registered.
        """
        if not step_id:
            raise ConfigError("step_id must be a non-empty string.")
        if step_id in self._contracts:
            raise ConfigError(
                f"Step '{step_id}' already has a registered contract. "
                "Use a unique step_id for each registration."
            )
        self._contracts[step_id] = ContractPair(
            input_schema=input_schema,
            output_schema=output_schema,
        )

    def get_input_contract(self, step_id: str) -> Schema | None:
        """Return the input Schema for a step, or None if not registered.

        Args:
            step_id: The step identifier to look up.

        Returns:
            The input Schema or None.
        """
        pair = self._contracts.get(step_id)
        return pair.input_schema if pair is not None else None

    def get_output_contract(self, step_id: str) -> Schema | None:
        """Return the output Schema for a step, or None if not registered.

        Args:
            step_id: The step identifier to look up.

        Returns:
            The output Schema or None.
        """
        pair = self._contracts.get(step_id)
        return pair.output_schema if pair is not None else None

    def has_contract(self, step_id: str) -> bool:
        """Check if a step has any registered contracts.

        Args:
            step_id: The step identifier to check.

        Returns:
            True if the step is registered, False otherwise.
        """
        return step_id in self._contracts

    def all_contracts(self) -> dict[str, ContractPair]:
        """Return a copy of all registered contracts.

        Returns:
            A dict mapping step_id to ContractPair.
        """
        return dict(self._contracts)

    def export_json_schema(self) -> dict[str, Any]:
        """Export all registered schemas as JSON Schema for documentation.

        Returns:
            A JSON-serializable dict mapping step_id to their contract JSON Schemas.
        """
        result: dict[str, Any] = {}
        for step_id, pair in self._contracts.items():
            entry: dict[str, Any] = {}
            if pair.input_schema is not None:
                entry["input"] = pair.input_schema.to_json_schema()
            if pair.output_schema is not None:
                entry["output"] = pair.output_schema.to_json_schema()
            result[step_id] = entry
        return result


# ---------------------------------------------------------------------------
# Internal helper functions
# ---------------------------------------------------------------------------


def _validate_value(
    value: Any,
    fd: FieldDefinition,
    path: str,
) -> list[FieldValidationError]:
    """Validate a single value against a FieldDefinition.

    Args:
        value: The data value to check.
        fd: The FieldDefinition describing the expected type.
        path: The dot-separated path for error reporting.

    Returns:
        A list of FieldValidationError (empty if the value is valid).
    """
    errors: list[FieldValidationError] = []

    field_type = fd.field_type

    # ------------------------------------------------------------------
    # Nested schema
    # ------------------------------------------------------------------
    if field_type == "nested" and fd.nested_schema is not None:
        sub_result = fd.nested_schema.validate_dict(value, prefix=f"{path}.")
        errors.extend(sub_result.errors)
        return errors

    # ------------------------------------------------------------------
    # List field
    # ------------------------------------------------------------------
    if field_type == "list":
        if not isinstance(value, list):
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="list",
                    actual=type(value).__name__,
                    message=f"Field '{path}': expected list, got {type(value).__name__}.",
                    severity=Severity.ERROR,
                )
            )
            return errors

        # Validate each item
        for idx, item in enumerate(cast(list[Any], value)):  # type: ignore[redundant-cast]
            item_path = f"{path}[{idx}]"
            if fd.nested_schema is not None:
                # list[Schema]
                sub_result = fd.nested_schema.validate_dict(item, prefix=f"{item_path}.")
                errors.extend(sub_result.errors)
            elif fd.item_type is not None:
                # list[primitive]
                item_errors = _check_primitive_type(item, fd.item_type, item_path)
                errors.extend(item_errors)
            # bare list — no item type check

        return errors

    # ------------------------------------------------------------------
    # Primitive types
    # ------------------------------------------------------------------
    if field_type in _PRIMITIVE_TYPES:
        errors.extend(_check_primitive_type(value, field_type, path))
        return errors

    return errors


def _check_primitive_type(
    value: Any,
    expected_type: type,
    path: str,
) -> list[FieldValidationError]:
    """Validate that value matches expected_type with strict bool/int distinction.

    Args:
        value: The value to check.
        expected_type: The expected Python primitive type.
        path: Field path for error messages.

    Returns:
        A list of FieldValidationError (empty if valid).
    """
    errors: list[FieldValidationError] = []

    # ------------------------------------------------------------------
    # bool vs int: use identity check (type(v) is T) for both bool and int.
    # For float fields, int is allowed as a widening conversion.
    # ------------------------------------------------------------------
    if expected_type is bool:
        if type(value) is not bool:
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="bool",
                    actual=type(value).__name__,
                    message=f"Field '{path}': expected bool, got {type(value).__name__}.",
                    severity=Severity.ERROR,
                )
            )
        return errors

    if expected_type is int:
        # Strict: bool must NOT pass for int fields (bool is a subclass of int)
        if type(value) is not int:
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="int",
                    actual=type(value).__name__,
                    message=f"Field '{path}': expected int, got {type(value).__name__}.",
                    severity=Severity.ERROR,
                )
            )
        return errors

    if expected_type is float:
        # Accept int as float (numeric widening), but reject bool
        if type(value) is bool:
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="float",
                    actual="bool",
                    message=f"Field '{path}': expected float, got bool.",
                    severity=Severity.ERROR,
                )
            )
            return errors
        if not isinstance(value, (int, float)):
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="float",
                    actual=type(value).__name__,
                    message=f"Field '{path}': expected float (or int), got {type(value).__name__}.",
                    severity=Severity.ERROR,
                )
            )
            return errors
        # Reject NaN and Inf
        try:
            fval = float(value)
        except (TypeError, ValueError):
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="finite float",
                    actual=str(value),
                    message=f"Field '{path}': could not interpret value as float.",
                    severity=Severity.ERROR,
                )
            )
            return errors
        if math.isnan(fval) or math.isinf(fval):
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="finite float",
                    actual="NaN" if math.isnan(fval) else "Inf",
                    message=f"Field '{path}': NaN and Inf are not valid float values.",
                    severity=Severity.ERROR,
                )
            )
        return errors

    if expected_type is str:
        if type(value) is not str:
            errors.append(
                FieldValidationError(
                    field=path,
                    expected="str",
                    actual=type(value).__name__,
                    message=f"Field '{path}': expected str, got {type(value).__name__}.",
                    severity=Severity.ERROR,
                )
            )
        return errors

    return errors


def _fd_type_label(fd: FieldDefinition) -> str:
    """Return a human-readable type label for a FieldDefinition.

    Args:
        fd: The FieldDefinition to describe.

    Returns:
        A short string like "str", "list[str]", "Schema", etc.
    """
    ft = fd.field_type
    if ft == "nested":
        return "Schema"
    if ft == "list":
        if fd.nested_schema is not None:
            return "list[Schema]"
        if fd.item_type is not None:
            return f"list[{fd.item_type.__name__}]"
        return "list"
    if isinstance(ft, type):
        return ft.__name__
    return str(ft)


def _fd_to_json_schema_prop(fd: FieldDefinition) -> dict[str, Any]:
    """Convert a FieldDefinition to a JSON Schema property dict.

    Args:
        fd: The FieldDefinition to convert.

    Returns:
        A JSON Schema property dict.
    """
    ft = fd.field_type

    # Nested schema
    if ft == "nested" and fd.nested_schema is not None:
        return fd.nested_schema.to_json_schema()

    # List
    if ft == "list":
        prop: dict[str, Any] = {"type": "array"}
        if fd.nested_schema is not None:
            prop["items"] = fd.nested_schema.to_json_schema()
        elif fd.item_type is not None:
            prop["items"] = {"type": _TYPE_TO_JSON[fd.item_type]}
        return prop

    # Optional primitive (required=False)
    if isinstance(ft, type) and ft in _PRIMITIVE_TYPES and not fd.required:
        return {"type": [_TYPE_TO_JSON[ft], "null"]}

    # Required primitive
    if isinstance(ft, type) and ft in _PRIMITIVE_TYPES:
        return {"type": _TYPE_TO_JSON[ft]}

    return {}


def _json_schema_prop_to_annotation(prop_spec: dict[str, Any], name: str, _depth: int = 0) -> Any:
    """Convert a JSON Schema property dict to a Kairos annotation.

    Only processes known safe keys: type, properties, required, items.
    Unknown keys are silently ignored.

    Args:
        prop_spec: A JSON Schema property object dict.
        name: Field name (for error messages).
        _depth: Current recursion depth — passed through to from_json_schema.

    Returns:
        A Python type annotation suitable for the Schema DSL.
    """
    json_type = prop_spec.get("type")

    # Nullable type: {"type": ["string", "null"]}
    if isinstance(json_type, list):
        non_null: list[Any] = [t for t in cast(list[Any], json_type) if t != "null"]  # type: ignore[redundant-cast]
        if len(non_null) == 1 and non_null[0] in _JSON_TO_TYPE:
            return _make_union_with_none(_JSON_TO_TYPE[non_null[0]])
        # Fallback to str for unrecognised nullable combos
        return _make_union_with_none(str)

    if json_type == "object":
        # Nested object — recurse
        nested_spec: dict[str, Any] = {
            "type": "object",
            "properties": prop_spec.get("properties") or {},
        }
        if "required" in prop_spec:
            nested_spec["required"] = prop_spec["required"]
        return Schema.from_json_schema(nested_spec, _depth=_depth)

    if json_type == "array":
        items_spec = prop_spec.get("items")
        if isinstance(items_spec, dict):
            items_spec_typed: dict[str, Any] = cast(dict[str, Any], items_spec)
            item_json_type: Any = items_spec_typed.get("type")
            if item_json_type == "object":
                nested_item = Schema.from_json_schema(
                    {
                        "type": "object",
                        "properties": items_spec_typed.get("properties") or {},
                        **(
                            {"required": items_spec_typed["required"]}
                            if "required" in items_spec_typed
                            else {}
                        ),
                    },
                    _depth=_depth,
                )
                return list[nested_item]  # type: ignore[valid-type]
            if item_json_type in _JSON_TO_TYPE:
                item_py = _JSON_TO_TYPE[item_json_type]
                # Build list[T] dynamically
                return list[item_py]  # type: ignore[valid-type]
        return list

    if json_type in _JSON_TO_TYPE:
        return _JSON_TO_TYPE[json_type]

    # Unknown type — default to str
    return str


def _make_optional(annotation: Any, nested_required: bool = True) -> Any:
    """Wrap an annotation as optional (T | None).

    Args:
        annotation: The base annotation.
        nested_required: When False and annotation is a Schema instance, wrap it
            as ``Schema | None`` so ``_normalize_type`` sets required=False.
            Defaults to True (legacy behaviour: leave Schema as-is).

    Returns:
        annotation | None for primitives, or the annotation itself for lists /
        already-optional types.  Schema instances are wrapped when
        nested_required=False.
    """
    if isinstance(annotation, Schema):
        if not nested_required:
            # Return a union so _normalize_type picks up required=False
            return _make_union_with_schema_none(annotation)
        return annotation  # required=True — leave as-is

    # Already optional (X | None)
    if isinstance(annotation, types.UnionType):
        return annotation
    origin = get_origin(annotation)
    if origin is typing.Union:
        return annotation
    # list types stay as required (optional lists are just absent, not None)
    if annotation is list or origin is list:
        return annotation

    # Primitive
    if annotation in _PRIMITIVE_TYPES:
        return _make_union_with_none(annotation)

    return annotation


def _fd_to_annotation(fd: FieldDefinition) -> Any:
    """Convert a FieldDefinition back to a DSL annotation (for extend()).

    Args:
        fd: The FieldDefinition to reverse.

    Returns:
        An annotation suitable for use in Schema(fields=...).
    """
    ft = fd.field_type

    if ft == "nested" and fd.nested_schema is not None:
        return fd.nested_schema

    if ft == "list":
        nested = fd.nested_schema
        item_t = fd.item_type
        if nested is not None:
            # Build list[Schema] dynamically — Any return type tells mypy not to
            # try to statically verify the subscript (runtime value as type arg).
            return _make_list_of(nested)
        if item_t is not None:
            return _make_list_of(item_t)
        return list

    if isinstance(ft, type) and ft in _PRIMITIVE_TYPES:
        if not fd.required:
            return _make_union_with_none(ft)
        return ft

    return str


def _make_list_of(item: Any) -> Any:
    """Build list[item] at runtime, returning Any so mypy does not analyse it.

    Args:
        item: The item type (primitive type or Schema instance).

    Returns:
        A parameterized list type.
    """
    return list[item]


def _make_union_with_none(t: type) -> Any:
    """Build t | None at runtime, returning Any so mypy does not analyse it.

    Args:
        t: A primitive Python type.

    Returns:
        A union type t | None.
    """
    return t | None


def _make_union_with_schema_none(schema: Schema) -> Any:
    """Build typing.Union[schema, None] carrying a Schema *instance* as a union member.

    Cannot use the ``X | None`` syntax here because ``schema`` is an instance,
    not a type.  ``typing.Union`` accepts arbitrary values as members, which lets
    ``_normalize_type`` detect it via ``isinstance(non_none[0], Schema)``.

    Args:
        schema: A Schema instance to mark as optional.

    Returns:
        A typing.Union that carries the Schema instance and NoneType.
    """
    return typing.Union[schema, None]  # noqa: UP007 — runtime value as union member


def _pydantic_annotation_to_kairos(annotation: Any, name: str) -> Any:
    """Convert a Pydantic field annotation to a Kairos Schema DSL annotation.

    Args:
        annotation: The raw type annotation from Pydantic model_fields.
        name: Field name for error messages.

    Returns:
        A Kairos-compatible annotation.
    """
    if annotation in _PRIMITIVE_TYPES:
        return annotation

    if annotation is list:
        return list

    # Resolve imports lazily to avoid hard dependency at module load
    try:
        from pydantic import BaseModel  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415,E501,I001
    except ImportError:
        return str

    # Nested BaseModel
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return Schema.from_pydantic(annotation)

    # Handle Union / Optional
    origin = get_origin(annotation)
    if isinstance(annotation, types.UnionType) or origin is typing.Union:
        args = get_args(annotation)
        non_none: list[Any] = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            inner = _pydantic_annotation_to_kairos(non_none[0], name)
            return _make_optional(inner)
        return _make_union_with_none(str)

    # list[T]
    if origin is list:
        args = get_args(annotation)
        if args:
            item_ann = args[0]
            if isinstance(item_ann, type) and issubclass(item_ann, BaseModel):
                nested = Schema.from_pydantic(item_ann)
                return list[nested]  # type: ignore[valid-type]
            if item_ann in _PRIMITIVE_TYPES:
                return list[item_ann]  # type: ignore[valid-type]
        return list

    # Fallback
    return str
