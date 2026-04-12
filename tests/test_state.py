"""Tests for kairos.state — written BEFORE implementation.

Priority order:
1. Failure paths (missing key, non-serializable, size exceeded, unauthorized proxy, delete missing)
2. Boundary conditions (default=None, default=0, overwrite accounting, empty snapshot, exact limits)
3. Happy paths (get/set all types, has/keys, snapshot/restore, to_dict/to_safe_dict, merge, proxy)
4. Security (JSON-serializable enforcement, sensitive redaction, scoped proxy blocks, round-trip)
5. Serialization (snapshot JSON round-trip, frozen dataclass, to_dict JSON-safe)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest

from kairos.exceptions import StateError
from kairos.state import ScopedStateProxy, StateStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> StateStore:
    """Empty StateStore with default configuration."""
    return StateStore()


@pytest.fixture
def populated_store() -> StateStore:
    """StateStore pre-loaded with varied JSON-safe values."""
    s = StateStore()
    s.set("name", "alice")
    s.set("count", 42)
    s.set("scores", [1.0, 2.0, 3.0])
    s.set("config", {"timeout": 30, "retries": 3})
    return s


@pytest.fixture
def sensitive_store() -> StateStore:
    """StateStore with both normal and sensitive keys."""
    s = StateStore()
    s.set("name", "alice")
    s.set("api_key", "sk-secret-123")
    s.set("password", "hunter2")
    s.set("output", "safe result")
    return s


# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """Error conditions that must be caught and raised correctly."""

    def test_get_missing_key_raises_state_error(self, store: StateStore):
        """get() on a key that was never set raises StateError."""
        with pytest.raises(StateError):
            store.get("nonexistent_key")

    def test_get_missing_key_error_mentions_key(self, store: StateStore):
        """The StateError message includes the missing key name."""
        with pytest.raises(StateError, match="missing_key"):
            store.get("missing_key")

    def test_set_non_serializable_raises_state_error(self, store: StateStore):
        """set() with a non-JSON-serializable value raises StateError by default."""

        class NotSerializable:
            pass

        with pytest.raises(StateError):
            store.set("bad", NotSerializable())

    def test_set_lambda_raises_state_error(self, store: StateStore):
        """Lambda functions are not JSON-serializable and must be rejected."""
        with pytest.raises(StateError):
            store.set("fn", lambda x: x)

    def test_set_exceeds_max_total_size_raises_state_error(self):
        """set() raises StateError when total state size exceeds max_total_size."""
        small_store = StateStore(max_total_size=50)
        # A string that definitely exceeds 50 bytes when JSON-encoded
        with pytest.raises(StateError, match="size"):
            small_store.set("big", "x" * 100)

    def test_delete_missing_key_raises_state_error(self, store: StateStore):
        """delete() on a nonexistent key raises StateError."""
        with pytest.raises(StateError):
            store.delete("not_here")

    def test_scoped_proxy_get_unauthorized_key_raises_state_error(
        self, populated_store: StateStore
    ):
        """ScopedStateProxy.get() raises StateError for keys outside read_keys."""
        proxy = ScopedStateProxy(populated_store, read_keys=["name"], write_keys=None)
        with pytest.raises(StateError):
            proxy.get("count")

    def test_scoped_proxy_set_unauthorized_key_raises_state_error(
        self, populated_store: StateStore
    ):
        """ScopedStateProxy.set() raises StateError for keys outside write_keys."""
        proxy = ScopedStateProxy(populated_store, read_keys=None, write_keys=["output"])
        with pytest.raises(StateError):
            proxy.set("name", "eve")

    def test_scoped_proxy_get_without_read_keys_and_key_missing_raises(self, store: StateStore):
        """ScopedStateProxy with write_keys only: get() of absent key still raises StateError."""
        proxy = ScopedStateProxy(store, read_keys=None, write_keys=["output"])
        with pytest.raises(StateError):
            proxy.get("nonexistent")

    def test_set_object_with_custom_deepcopy_raises(self, store: StateStore):
        """An object with a custom __deepcopy__ method is not JSON-safe and must be rejected."""

        class TrickyObj:
            def __deepcopy__(self, memo: dict) -> TrickyObj:  # pragma: no cover
                return TrickyObj()

        with pytest.raises(StateError):
            store.set("tricky", TrickyObj())


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge cases: defaults, size accounting, empty collections, exact limits."""

    def test_get_with_default_none_returns_none_not_error(self, store: StateStore):
        """get(key, default=None) returns None when key is missing — no exception."""
        result = store.get("missing", None)
        assert result is None

    def test_get_with_default_zero_returns_zero(self, store: StateStore):
        """get(key, default=0) returns 0 when key is missing — 0 is falsy but valid."""
        result = store.get("missing", 0)
        assert result == 0

    def test_get_with_default_false_returns_false(self, store: StateStore):
        """get(key, default=False) returns False — falsy default must not be ignored."""
        result = store.get("missing", False)
        assert result is False

    def test_get_with_default_empty_string_returns_empty_string(self, store: StateStore):
        """get(key, default='') returns '' when key is missing."""
        result = store.get("missing", "")
        assert result == ""

    def test_overwrite_key_updates_total_size_correctly(self, store: StateStore):
        """Overwriting a key subtracts old size and adds new size — no double counting."""
        store.set("key", "x" * 10)
        size_after_first = store._total_size
        store.set("key", "y" * 20)
        size_after_second = store._total_size
        # The size should have grown by roughly the difference between the two values
        assert size_after_second > size_after_first

    def test_overwrite_key_with_smaller_value_reduces_total_size(self, store: StateStore):
        """Overwriting with a smaller value reduces the tracked total size."""
        store.set("key", "x" * 100)
        size_large = store._total_size
        store.set("key", "y")
        size_small = store._total_size
        assert size_small < size_large

    def test_empty_store_snapshot_has_empty_data(self, store: StateStore):
        """Snapshot of an empty store has an empty data dict."""
        snap = store.snapshot()
        assert snap.data == {}

    def test_empty_merge_does_not_change_store(self, store: StateStore):
        """merge({}) leaves the store unchanged."""
        store.set("key", "value")
        store.merge({})
        assert store.get("key") == "value"
        assert len(store.keys()) == 1

    def test_set_value_exactly_at_max_value_size_warns_not_raises(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A value exactly at max_value_size emits a warning but does NOT raise."""
        import logging

        max_size = 100
        # Build a string whose JSON encoding is exactly max_size bytes
        # json.dumps adds quotes, so we need a string of length max_size - 2
        target_value = "x" * (max_size - 2)
        s = StateStore(max_value_size=max_size)
        with caplog.at_level(logging.WARNING):
            s.set("key", target_value)
        # No exception raised — value is stored
        assert s.get("key") == target_value

    def test_set_value_over_max_value_size_warns(self, caplog: pytest.LogCaptureFixture):
        """A value exceeding max_value_size emits a warning log message."""
        import logging

        s = StateStore(max_value_size=10)
        with caplog.at_level(logging.WARNING):
            s.set("big_key", "x" * 100)
        # Value is still stored (soft limit — just a warning)
        assert s.get("big_key") == "x" * 100
        assert len(caplog.records) > 0

    def test_scoped_proxy_with_none_read_keys_allows_all_reads(self, populated_store: StateStore):
        """ScopedStateProxy with read_keys=None allows reading any key from the store."""
        proxy = ScopedStateProxy(populated_store, read_keys=None, write_keys=None)
        assert proxy.get("name") == "alice"
        assert proxy.get("count") == 42

    def test_scoped_proxy_with_none_write_keys_allows_all_writes(self, store: StateStore):
        """ScopedStateProxy with write_keys=None allows writing any key."""
        proxy = ScopedStateProxy(store, read_keys=None, write_keys=None)
        proxy.set("anything", "value")
        assert store.get("anything") == "value"

    def test_scoped_proxy_with_empty_list_read_keys_blocks_all_reads(
        self, populated_store: StateStore
    ):
        """ScopedStateProxy with read_keys=[] blocks all read access."""
        proxy = ScopedStateProxy(populated_store, read_keys=[], write_keys=None)
        with pytest.raises(StateError):
            proxy.get("name")

    def test_scoped_proxy_with_empty_list_write_keys_blocks_all_writes(self, store: StateStore):
        """ScopedStateProxy with write_keys=[] blocks all write access."""
        proxy = ScopedStateProxy(store, read_keys=None, write_keys=[])
        with pytest.raises(StateError):
            proxy.set("anything", "value")

    def test_delete_last_key_leaves_empty_store(self, store: StateStore):
        """Deleting the only key leaves an empty store with size 0."""
        store.set("only_key", "only_value")
        store.delete("only_key")
        assert store.keys() == []
        assert store._total_size == 0

    def test_has_returns_false_for_nonexistent_key(self, store: StateStore):
        """has() returns False for a key that was never set."""
        assert store.has("nonexistent") is False

    def test_keys_empty_store_returns_empty_list(self, store: StateStore):
        """keys() on an empty store returns an empty list."""
        assert store.keys() == []


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    """Normal usage producing correct outputs."""

    def test_set_and_get_string(self, store: StateStore):
        """set/get round-trip for a plain string value."""
        store.set("greeting", "hello")
        assert store.get("greeting") == "hello"

    def test_set_and_get_integer(self, store: StateStore):
        """set/get round-trip for an integer value."""
        store.set("count", 99)
        assert store.get("count") == 99

    def test_set_and_get_float(self, store: StateStore):
        """set/get round-trip for a float value."""
        store.set("score", 3.14)
        assert store.get("score") == 3.14

    def test_set_and_get_bool(self, store: StateStore):
        """set/get round-trip for a boolean value."""
        store.set("active", True)
        assert store.get("active") is True

    def test_set_and_get_none(self, store: StateStore):
        """set/get round-trip for None (JSON null)."""
        store.set("empty", None)
        assert store.get("empty") is None

    def test_set_and_get_list(self, store: StateStore):
        """set/get round-trip for a list value."""
        store.set("items", [1, 2, 3])
        assert store.get("items") == [1, 2, 3]

    def test_set_and_get_nested_dict(self, store: StateStore):
        """set/get round-trip for a nested dict."""
        store.set("cfg", {"a": 1, "b": {"c": 2}})
        assert store.get("cfg") == {"a": 1, "b": {"c": 2}}

    def test_has_returns_true_for_existing_key(self, store: StateStore):
        """has() returns True after a key is set."""
        store.set("x", 1)
        assert store.has("x") is True

    def test_keys_returns_all_set_keys(self, populated_store: StateStore):
        """keys() returns a list containing all keys that were set."""
        k = populated_store.keys()
        assert "name" in k
        assert "count" in k
        assert "scores" in k
        assert "config" in k

    def test_delete_removes_key(self, store: StateStore):
        """delete() removes a key so that has() returns False afterward."""
        store.set("temp", "gone")
        store.delete("temp")
        assert store.has("temp") is False

    def test_snapshot_captures_current_state(self, populated_store: StateStore):
        """snapshot() returns a StateSnapshot whose .data matches the current store."""
        snap = populated_store.snapshot()
        assert snap.data["name"] == "alice"
        assert snap.data["count"] == 42

    def test_snapshot_step_id_stored(self, store: StateStore):
        """snapshot(step_id='my_step') stores the step_id in the snapshot."""
        store.set("k", "v")
        snap = store.snapshot(step_id="my_step")
        assert snap.step_id == "my_step"

    def test_snapshot_default_step_id_is_empty_string(self, store: StateStore):
        """snapshot() with no step_id defaults to empty string."""
        store.set("k", "v")
        snap = store.snapshot()
        assert snap.step_id == ""

    def test_snapshot_has_utc_timestamp(self, store: StateStore):
        """snapshot() sets a timestamp (datetime instance)."""
        store.set("k", "v")
        snap = store.snapshot()
        assert isinstance(snap.timestamp, datetime)

    def test_snapshot_independence_from_live_state(self, store: StateStore):
        """Mutations after snapshot() do not affect the snapshot's data."""
        store.set("key", "original")
        snap = store.snapshot()
        store.set("key", "mutated")
        assert snap.data["key"] == "original"
        assert store.get("key") == "mutated"

    def test_restore_reverts_to_snapshot_state(self, store: StateStore):
        """restore() replaces live state with snapshot data."""
        store.set("key", "before")
        snap = store.snapshot()
        store.set("key", "after")
        store.restore(snap)
        assert store.get("key") == "before"

    def test_restore_removes_keys_added_after_snapshot(self, store: StateStore):
        """restore() removes keys that were added after the snapshot was taken."""
        store.set("original", 1)
        snap = store.snapshot()
        store.set("added_later", 2)
        store.restore(snap)
        assert not store.has("added_later")

    def test_restore_recalculates_total_size(self, store: StateStore):
        """restore() recalculates _total_size to match the restored data."""
        store.set("a", "x" * 100)
        snap = store.snapshot()
        store.set("b", "y" * 200)
        store.restore(snap)
        # After restore, "b" is gone; size should reflect only "a"
        expected_size = len(json.dumps("x" * 100).encode("utf-8"))
        assert store._total_size == expected_size

    def test_to_dict_returns_all_key_value_pairs(self, populated_store: StateStore):
        """to_dict() returns a dict with all stored keys and their values."""
        d = populated_store.to_dict()
        assert d["name"] == "alice"
        assert d["count"] == 42

    def test_to_dict_returns_deep_copy(self, store: StateStore):
        """to_dict() returns an independent copy — mutating it does not affect the store."""
        store.set("cfg", {"nested": "value"})
        d = store.to_dict()
        d["cfg"]["nested"] = "mutated"  # type: ignore[index]
        assert store.get("cfg") == {"nested": "value"}

    def test_to_safe_dict_redacts_sensitive_keys(self, sensitive_store: StateStore):
        """to_safe_dict() replaces sensitive key values with '[REDACTED]'."""
        safe = sensitive_store.to_safe_dict()
        assert safe["api_key"] == "[REDACTED]"
        assert safe["password"] == "[REDACTED]"  # noqa: S105

    def test_to_safe_dict_keeps_non_sensitive_values(self, sensitive_store: StateStore):
        """to_safe_dict() leaves non-sensitive values intact."""
        safe = sensitive_store.to_safe_dict()
        assert safe["name"] == "alice"
        assert safe["output"] == "safe result"

    def test_get_still_returns_sensitive_value_after_to_safe_dict(
        self, sensitive_store: StateStore
    ):
        """get() always returns the raw value regardless of to_safe_dict() redaction."""
        _ = sensitive_store.to_safe_dict()
        assert sensitive_store.get("api_key") == "sk-secret-123"

    def test_merge_adds_all_keys(self, store: StateStore):
        """merge() adds all keys from the provided dict."""
        store.merge({"a": 1, "b": "two", "c": [3]})
        assert store.get("a") == 1
        assert store.get("b") == "two"
        assert store.get("c") == [3]

    def test_merge_overwrites_existing_keys(self, store: StateStore):
        """merge() overwrites values for keys that already exist."""
        store.set("key", "old")
        store.merge({"key": "new"})
        assert store.get("key") == "new"

    def test_scoped_proxy_allows_permitted_read(self, populated_store: StateStore):
        """ScopedStateProxy.get() returns value for a key in read_keys."""
        proxy = ScopedStateProxy(populated_store, read_keys=["name", "count"], write_keys=[])
        assert proxy.get("name") == "alice"

    def test_scoped_proxy_allows_permitted_write(self, store: StateStore):
        """ScopedStateProxy.set() writes through to the underlying store."""
        proxy = ScopedStateProxy(store, read_keys=None, write_keys=["output"])
        proxy.set("output", "result")
        assert store.get("output") == "result"

    def test_scoped_proxy_has_returns_true_for_readable_key(self, populated_store: StateStore):
        """ScopedStateProxy.has() returns True for an existing key in read_keys."""
        proxy = ScopedStateProxy(populated_store, read_keys=["name"], write_keys=None)
        assert proxy.has("name") is True

    def test_scoped_proxy_has_returns_false_for_key_outside_read_scope(
        self, populated_store: StateStore
    ):
        """ScopedStateProxy.has() returns False for a key not in read_keys (never raises)."""
        proxy = ScopedStateProxy(populated_store, read_keys=["name"], write_keys=None)
        assert proxy.has("count") is False

    def test_scoped_proxy_keys_returns_intersection_of_read_and_store(
        self, populated_store: StateStore
    ):
        """ScopedStateProxy.keys() returns only keys in both read_keys and the store."""
        proxy = ScopedStateProxy(
            populated_store, read_keys=["name", "count", "absent"], write_keys=None
        )
        result = proxy.keys()
        assert "name" in result
        assert "count" in result
        assert "absent" not in result
        assert "scores" not in result

    def test_scoped_proxy_keys_with_none_read_keys_returns_all_store_keys(
        self, populated_store: StateStore
    ):
        """ScopedStateProxy.keys() with read_keys=None returns all keys from the store."""
        proxy = ScopedStateProxy(populated_store, read_keys=None, write_keys=None)
        assert set(proxy.keys()) == set(populated_store.keys())

    def test_store_scoped_method_returns_proxy(self, store: StateStore):
        """StateStore.scoped() returns a ScopedStateProxy instance."""
        proxy = store.scoped(read_keys=["a"], write_keys=["b"])
        assert isinstance(proxy, ScopedStateProxy)

    def test_allow_non_serializable_stores_value(self, store: StateStore):
        """allow_non_serializable=True permits storing non-JSON-safe objects."""

        class Obj:
            pass

        s = StateStore(allow_non_serializable=True)
        obj = Obj()
        s.set("thing", obj)
        assert s.get("thing") is obj

    def test_allow_non_serializable_uses_getsizeof_for_size(self):
        """allow_non_serializable=True uses sys.getsizeof() for size estimation."""

        class Obj:
            pass

        s = StateStore(allow_non_serializable=True)
        obj = Obj()
        s.set("thing", obj)
        assert s._total_size == sys.getsizeof(obj)


# ---------------------------------------------------------------------------
# Group 4: Security tests
# ---------------------------------------------------------------------------


class TestSecurity:
    """Security constraints — each maps to a CLAUDE.md security requirement."""

    # Security req 3 — state values must be JSON-serializable
    def test_non_serializable_value_rejected_by_default(self, store: StateStore):
        """StateStore rejects non-JSON-serializable values by default (req 3)."""

        class Secret:
            pass

        with pytest.raises(StateError):
            store.set("bad", Secret())

    def test_json_serializable_values_accepted(self, store: StateStore):
        """All JSON-native types are accepted without error."""
        store.set("str_val", "text")
        store.set("int_val", 1)
        store.set("float_val", 1.5)
        store.set("bool_val", True)
        store.set("null_val", None)
        store.set("list_val", [1, "a"])
        store.set("dict_val", {"k": "v"})

    # Security req 4 — sensitive key redaction
    def test_sensitive_key_redacted_in_to_safe_dict(self, sensitive_store: StateStore):
        """to_safe_dict() redacts keys matching DEFAULT_SENSITIVE_PATTERNS (req 4)."""
        safe = sensitive_store.to_safe_dict()
        assert safe["api_key"] == "[REDACTED]"
        assert safe["password"] == "[REDACTED]"  # noqa: S105

    def test_sensitive_key_accessible_via_get(self, sensitive_store: StateStore):
        """get() returns raw sensitive value even after to_safe_dict() call (req 4)."""
        _ = sensitive_store.to_safe_dict()
        assert sensitive_store.get("api_key") == "sk-secret-123"

    def test_custom_sensitive_patterns_redacted_in_safe_dict(self):
        """User-specified sensitive_keys are also redacted in to_safe_dict() (req 4)."""
        s = StateStore(sensitive_keys=["*my_custom_field*"])
        s.set("my_custom_field", "hidden")
        s.set("safe_field", "visible")
        safe = s.to_safe_dict()
        assert safe["my_custom_field"] == "[REDACTED]"
        assert safe["safe_field"] == "visible"

    # Security req 5 — scoped state proxy
    def test_scoped_proxy_blocks_unauthorized_read(self, populated_store: StateStore):
        """Unauthorized read on ScopedStateProxy raises StateError (req 5)."""
        proxy = ScopedStateProxy(populated_store, read_keys=["name"], write_keys=None)
        with pytest.raises(StateError):
            proxy.get("config")

    def test_scoped_proxy_blocks_unauthorized_write(self, populated_store: StateStore):
        """Unauthorized write on ScopedStateProxy raises StateError (req 5)."""
        proxy = ScopedStateProxy(populated_store, read_keys=None, write_keys=["output"])
        with pytest.raises(StateError):
            proxy.set("name", "hacker")

    # Security req 6 — state size limits
    def test_max_total_size_enforced_on_set(self):
        """set() raises StateError when total state exceeds max_total_size (req 6)."""
        s = StateStore(max_total_size=20)
        with pytest.raises(StateError):
            s.set("k", "x" * 100)

    def test_max_value_size_warning_emitted(self, caplog: pytest.LogCaptureFixture):
        """Exceeding max_value_size emits a warning log (req 6 — soft limit)."""
        import logging

        s = StateStore(max_value_size=5)
        with caplog.at_level(logging.WARNING):
            s.set("big", "exceeds the soft limit for sure")
        assert len(caplog.records) > 0

    # Security req 7 — JSON round-trip for deep copy
    def test_snapshot_uses_json_round_trip_not_deepcopy(self, store: StateStore):
        """snapshot() produces an independent copy via JSON — not copy.deepcopy() (req 7)."""
        store.set("data", {"nested": [1, 2, 3]})
        snap = store.snapshot()
        # Mutate the live store's nested structure by replacing the value entirely
        store.set("data", {"nested": [99]})
        # Snapshot must be unaffected
        assert snap.data["data"] == {"nested": [1, 2, 3]}

    def test_no_copy_import_in_state_module(self):
        """The state module must not import the 'copy' standard library module (req 7).

        This ensures the JSON round-trip contract cannot accidentally be broken
        by someone switching back to copy.deepcopy().
        """
        import importlib
        import importlib.util

        spec = importlib.util.find_spec("kairos.state")
        assert spec is not None
        assert spec.origin is not None
        with open(spec.origin) as f:
            source = f.read()
        assert "import copy" not in source
        assert "from copy import" not in source

    def test_to_dict_returns_independent_copy_not_reference(self, store: StateStore):
        """to_dict() must return a deep copy — caller cannot corrupt internal state."""
        store.set("cfg", {"key": "original"})
        d = store.to_dict()
        d["cfg"]["key"] = "corrupted"  # type: ignore[index]
        assert store.get("cfg") == {"key": "original"}

    def test_restore_uses_json_round_trip(self, store: StateStore):
        """restore() uses JSON round-trip so the live state is independent of the snapshot."""
        store.set("v", [1, 2])
        snap = store.snapshot()
        store.restore(snap)
        # Mutating the snapshot data should NOT affect restored live state
        snap.data["v"].append(99)  # type: ignore[index]
        assert store.get("v") == [1, 2]


# ---------------------------------------------------------------------------
# Group 5: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """JSON round-trip correctness for all serializable data structures."""

    def test_snapshot_data_is_json_serializable(self, populated_store: StateStore):
        """StateSnapshot.data can be serialized to JSON without error."""
        snap = populated_store.snapshot()
        serialized = json.dumps(snap.data)
        restored = json.loads(serialized)
        assert restored["name"] == "alice"
        assert restored["count"] == 42

    def test_snapshot_is_frozen_dataclass(self, store: StateStore):
        """StateSnapshot is a frozen dataclass — modifying its fields raises FrozenInstanceError."""
        store.set("k", "v")
        snap = store.snapshot()
        with pytest.raises((AttributeError, TypeError)):
            snap.data = {}  # type: ignore[misc]

    def test_snapshot_step_id_is_string(self, store: StateStore):
        """StateSnapshot.step_id is always a str."""
        store.set("k", "v")
        snap = store.snapshot(step_id="step_x")
        assert isinstance(snap.step_id, str)

    def test_snapshot_timestamp_is_datetime(self, store: StateStore):
        """StateSnapshot.timestamp is a datetime instance."""
        store.set("k", "v")
        snap = store.snapshot()
        assert isinstance(snap.timestamp, datetime)

    def test_to_dict_output_is_json_serializable(self, populated_store: StateStore):
        """to_dict() returns a dict that round-trips through JSON cleanly."""
        d = populated_store.to_dict()
        serialized = json.dumps(d)
        restored = json.loads(serialized)
        assert restored["name"] == "alice"

    def test_to_safe_dict_output_is_json_serializable(self, sensitive_store: StateStore):
        """to_safe_dict() output (with [REDACTED] strings) is JSON-serializable."""
        safe = sensitive_store.to_safe_dict()
        serialized = json.dumps(safe)
        restored = json.loads(serialized)
        assert restored["api_key"] == "[REDACTED]"

    def test_snapshot_data_is_deep_copy_of_original(self, store: StateStore):
        """StateSnapshot.data is a deep copy — not the same object as internal _data."""
        store.set("key", [1, 2, 3])
        snap = store.snapshot()
        # The snapshot data list object must not be the same as what the store holds
        assert snap.data["key"] is not store._data["key"]

    def test_multiple_snapshots_are_independent(self, store: StateStore):
        """Multiple snapshots at different points in time are independent of each other."""
        store.set("v", 1)
        snap1 = store.snapshot()
        store.set("v", 2)
        snap2 = store.snapshot()
        assert snap1.data["v"] == 1
        assert snap2.data["v"] == 2


# ---------------------------------------------------------------------------
# Group 6: Regression fixes from code review
# ---------------------------------------------------------------------------


class TestNonSerializableSnapshotEdgeCases:
    """snapshot()/to_dict() must raise StateError when store contains non-serializable values."""

    def test_snapshot_with_non_serializable_value_raises_state_error(self):
        """snapshot() on a store with allow_non_serializable=True and non-JSON-safe data raises."""

        class Custom:
            pass

        s = StateStore(allow_non_serializable=True)
        s.set("obj", Custom())
        with pytest.raises(StateError, match="snapshot"):
            s.snapshot()

    def test_to_dict_with_non_serializable_value_raises_state_error(self):
        """to_dict() on a store with non-serializable values raises StateError."""

        class Custom:
            pass

        s = StateStore(allow_non_serializable=True)
        s.set("obj", Custom())
        with pytest.raises(StateError, match="to_dict"):
            s.to_dict()


class TestMergePartialFailure:
    """merge() applies keys sequentially — partial merge on failure."""

    def test_merge_partial_failure_commits_earlier_keys(self, store: StateStore):
        """If merge fails on key N, keys 0..N-1 are already committed."""
        with pytest.raises(StateError):
            store.merge({"a": 1, "b": 2, "bad": object(), "c": 3})
        # "a" and "b" were committed before "bad" failed
        assert store.get("a") == 1
        assert store.get("b") == 2
        assert not store.has("bad")
        assert not store.has("c")


class TestScopedProxyDeleteNotExposed:
    """ScopedStateProxy intentionally does not expose delete()."""

    def test_scoped_proxy_has_no_delete_method(self, populated_store: StateStore):
        """ScopedStateProxy does not have a delete() method — AttributeError on call."""
        proxy = ScopedStateProxy(populated_store, read_keys=["name"], write_keys=["name"])
        assert not hasattr(proxy, "delete")
