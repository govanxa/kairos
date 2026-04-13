"""Kairos state — shared key-value store that carries context between workflow steps.

Provides:
- StateStore: main key-value store with JSON-serializable enforcement, size limits,
  sensitive-key redaction, snapshot/restore, and scoped access proxies.
- StateSnapshot: frozen, immutable checkpoint of the store at a point in time.
- ScopedStateProxy: restricted view of StateStore enforcing read/write boundaries.

Security contracts:
- All values are JSON-serializable by default (prevents arbitrary object storage).
- Snapshots use json.loads(json.dumps()) for deep copy — never copy.deepcopy().
- Sensitive keys are redacted in to_safe_dict() and all log output.
- ScopedStateProxy raises StateError on any access outside declared keys.
- State size is tracked and a hard limit is enforced on total size.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from kairos.exceptions import StateError
from kairos.security import DEFAULT_SENSITIVE_PATTERNS, redact_sensitive

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel — distinguishes "no default provided" from default=None
# ---------------------------------------------------------------------------

_SENTINEL = object()


# ---------------------------------------------------------------------------
# StateSnapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateSnapshot:
    """Immutable checkpoint of the StateStore at a specific point in time.

    Attributes:
        data: Deep copy of the store's key-value pairs at snapshot time.
            Produced via JSON round-trip, not copy.deepcopy().
        step_id: ID of the step that triggered this snapshot (may be empty).
        timestamp: UTC datetime when this snapshot was taken.
    """

    data: dict[str, object]
    step_id: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------


class StateStore:
    """Key-value store for workflow state shared across steps.

    Values are JSON-serializable by default. Sensitive keys are redacted in
    safe-export methods. Size limits are enforced to prevent memory exhaustion.
    Scoped proxies restrict step-level access to declared keys.

    Args:
        max_value_size: Soft size limit per value in bytes. Exceeding this emits
            a warning but does not block the write. Default: 10 MiB.
        max_total_size: Hard size limit for the entire store in bytes. Exceeding
            this raises StateError. Default: 100 MiB.
        sensitive_keys: Additional glob patterns for keys whose values are
            redacted in to_safe_dict() and log output. Combined with
            DEFAULT_SENSITIVE_PATTERNS.
        allow_non_serializable: When True, non-JSON-serializable values are
            accepted and sized via sys.getsizeof(). Default: False.
    """

    def __init__(
        self,
        max_value_size: int = 10_485_760,
        max_total_size: int = 104_857_600,
        sensitive_keys: list[str] | None = None,
        allow_non_serializable: bool = False,
    ) -> None:
        self._data: dict[str, object] = {}
        self._total_size: int = 0
        self._max_value_size = max_value_size
        self._max_total_size = max_total_size
        self._allow_non_serializable = allow_non_serializable
        extra = sensitive_keys or []
        self._sensitive_patterns: list[str] = DEFAULT_SENSITIVE_PATTERNS + extra

    # ------------------------------------------------------------------
    # Core read/write
    # ------------------------------------------------------------------

    def get(self, key: str, default: object = _SENTINEL) -> object:
        """Retrieve a value by key.

        Args:
            key: The key to look up.
            default: Value to return if the key is absent. If not provided,
                a StateError is raised for missing keys.

        Returns:
            The stored value, or *default* if the key is absent.

        Raises:
            StateError: When the key is absent and no default was provided.
        """
        if key in self._data:
            return self._data[key]
        if default is not _SENTINEL:
            return default
        raise StateError(f"Key {key!r} not found in state store.", key=key)

    def set(self, key: str, value: object) -> None:
        """Store a value under the given key.

        Validates JSON-serializability (unless allow_non_serializable=True),
        emits a warning if the value exceeds max_value_size, and raises
        StateError if adding the value would exceed max_total_size.

        Overwrites are supported — the old value's size is subtracted before
        the new value's size is added.

        Args:
            key: The key to store the value under.
            value: The value to store. Must be JSON-serializable by default.

        Raises:
            StateError: When the value is not JSON-serializable (and
                allow_non_serializable is False), or when the total state size
                would exceed max_total_size.
        """
        value_size = self._measure_size(key, value)

        # Subtract the old size when overwriting an existing key
        old_size = 0
        if key in self._data:
            old_size = self._measure_size(key, self._data[key])

        new_total = self._total_size - old_size + value_size
        if new_total > self._max_total_size:
            raise StateError(
                f"Cannot set key {key!r}: total state size would exceed the "
                f"configured limit of {self._max_total_size} bytes.",
                key=key,
            )

        if value_size > self._max_value_size:
            logger.warning(
                "State value for key %r is %d bytes, which exceeds the "
                "soft per-value limit of %d bytes.",
                key,
                value_size,
                self._max_value_size,
            )

        self._data[key] = value
        self._total_size = new_total

    def has(self, key: str) -> bool:
        """Check whether a key exists in the store.

        Args:
            key: The key to check.

        Returns:
            True if the key exists, False otherwise.
        """
        return key in self._data

    def keys(self) -> list[str]:
        """Return a list of all keys currently in the store.

        Returns:
            List of key strings in arbitrary order.
        """
        return list(self._data.keys())

    def delete(self, key: str) -> None:
        """Remove a key from the store and update the tracked size.

        Args:
            key: The key to delete.

        Raises:
            StateError: When the key does not exist.
        """
        if key not in self._data:
            raise StateError(f"Cannot delete key {key!r}: key not found in state store.", key=key)
        old_size = self._measure_size(key, self._data[key])
        del self._data[key]
        self._total_size -= old_size

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self, step_id: str = "") -> StateSnapshot:
        """Create an immutable checkpoint of the current state.

        Uses json.loads(json.dumps(...)) for the deep copy — never
        copy.deepcopy() — to prevent exploitation of custom __deepcopy__
        methods and to guarantee only JSON-safe data crosses the boundary.

        Args:
            step_id: Optional identifier of the step triggering this snapshot.

        Returns:
            A frozen StateSnapshot containing the current data.
        """
        try:
            data_copy: dict[str, object] = json.loads(json.dumps(self._data))
        except (TypeError, ValueError) as exc:
            raise StateError(
                "Cannot snapshot: state contains non-JSON-serializable values. "
                "Snapshot requires all values to be JSON-safe."
            ) from exc
        return StateSnapshot(
            data=data_copy,
            step_id=step_id,
            timestamp=datetime.now(tz=UTC),
        )

    def restore(self, snapshot: StateSnapshot) -> None:
        """Roll back the store to a previously taken snapshot.

        Uses json.loads(json.dumps(...)) to produce an independent copy of
        the snapshot data — changes to the live store after restore will not
        affect the snapshot.

        Args:
            snapshot: A StateSnapshot previously created by snapshot().
        """
        restored: dict[str, object] = json.loads(json.dumps(snapshot.data))
        self._data = restored
        self._total_size = self._calculate_total_size()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """Return a deep copy of the store's data (sensitive keys NOT redacted).

        The returned dict is independent of the internal state — mutations do
        not affect the store.

        Returns:
            A JSON round-trip copy of all stored key-value pairs.
        """
        try:
            return cast(dict[str, object], json.loads(json.dumps(self._data)))
        except (TypeError, ValueError) as exc:
            raise StateError(
                "Cannot call to_dict: state contains non-JSON-serializable values."
            ) from exc

    def to_safe_dict(self) -> dict[str, object]:
        """Return a redacted copy of the store's data for safe export.

        Keys matching any sensitive pattern (DEFAULT_SENSITIVE_PATTERNS plus
        user-configured patterns) have their values replaced with '[REDACTED]'.

        Returns:
            A dict with sensitive values replaced by the string '[REDACTED]'.
        """
        return redact_sensitive(self._data, sensitive_patterns=self._sensitive_patterns)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def merge(self, data: dict[str, object]) -> None:
        """Merge a dict into the store, calling set() for each key-value pair.

        Args:
            data: Dict of key-value pairs to merge. Each value must be
                JSON-serializable (unless allow_non_serializable=True).

        Raises:
            StateError: If any value fails serialization or size checks.
        """
        for key, value in data.items():
            self.set(key, value)

    # ------------------------------------------------------------------
    # Scoped access
    # ------------------------------------------------------------------

    def scoped(
        self,
        read_keys: list[str] | None = None,
        write_keys: list[str] | None = None,
    ) -> ScopedStateProxy:
        """Create a ScopedStateProxy restricted to the given keys.

        Args:
            read_keys: Keys the proxy may read. None means unrestricted.
            write_keys: Keys the proxy may write. None means unrestricted.

        Returns:
            A ScopedStateProxy wrapping this store with the given restrictions.
        """
        return ScopedStateProxy(self, read_keys=read_keys, write_keys=write_keys)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _measure_size(self, key: str, value: object) -> int:
        """Calculate the byte size of *value* for tracking purposes.

        For JSON-serializable values, uses len(json.dumps(value).encode("utf-8")).
        For non-serializable values (only when allow_non_serializable=True),
        falls back to sys.getsizeof().

        Args:
            key: Key name (used only in error messages).
            value: The value to measure.

        Returns:
            Estimated size in bytes.

        Raises:
            StateError: When the value is not JSON-serializable and
                allow_non_serializable is False.
        """
        try:
            return len(json.dumps(value).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            if self._allow_non_serializable:
                return sys.getsizeof(value)
            raise StateError(
                f"Cannot set key {key!r}: value is not JSON-serializable. "
                f"Use allow_non_serializable=True to store arbitrary Python objects.",
                key=key,
            ) from exc

    def _calculate_total_size(self) -> int:
        """Recalculate the total byte size of all stored values.

        Used after restore() to reset the tracked size from scratch.

        Returns:
            Total size in bytes across all current key-value pairs.
        """
        total = 0
        for key, value in self._data.items():
            total += self._measure_size(key, value)
        return total


# ---------------------------------------------------------------------------
# ScopedStateProxy
# ---------------------------------------------------------------------------


class ScopedStateProxy:
    """Restricted view of a StateStore that enforces per-step access boundaries.

    When a step declares read_keys or write_keys, the executor provides a
    ScopedStateProxy rather than the raw StateStore. Any access outside the
    declared keys raises StateError.

    Args:
        store: The underlying StateStore to delegate to.
        read_keys: Keys this proxy is allowed to read. None means all keys
            are readable. An empty list blocks all reads.
        write_keys: Keys this proxy is allowed to write. None means all keys
            are writable. An empty list blocks all writes.
    """

    def __init__(
        self,
        store: StateStore,
        read_keys: list[str] | None = None,
        write_keys: list[str] | None = None,
    ) -> None:
        self._store = store
        # Convert to frozenset for O(1) membership checks, or None for unrestricted
        self._read_keys: frozenset[str] | None = None if read_keys is None else frozenset(read_keys)
        self._write_keys: frozenset[str] | None = (
            None if write_keys is None else frozenset(write_keys)
        )

    def get(self, key: str, default: object = _SENTINEL) -> object:
        """Retrieve a value, enforcing the read scope.

        Args:
            key: The key to look up.
            default: Fallback value when the key is absent. If not provided,
                StateError is raised for missing keys.

        Returns:
            The stored value, or *default* if the key is absent.

        Raises:
            StateError: When *key* is outside the declared read_keys, or when
                the key is absent and no default was provided.
        """
        if self._read_keys is not None and key not in self._read_keys:
            raise StateError(
                f"Unauthorized read: key {key!r} is not in the declared read_keys for this step.",
                key=key,
            )
        return self._store.get(key, default) if default is not _SENTINEL else self._store.get(key)

    def set(self, key: str, value: object) -> None:
        """Write a value, enforcing the write scope.

        Args:
            key: The key to write.
            value: The value to store.

        Raises:
            StateError: When *key* is outside the declared write_keys.
        """
        if self._write_keys is not None and key not in self._write_keys:
            raise StateError(
                f"Unauthorized write: key {key!r} is not in the declared write_keys for this step.",
                key=key,
            )
        self._store.set(key, value)

    def has(self, key: str) -> bool:
        """Check if a key exists, respecting the read scope.

        Returns False — rather than raising — when *key* is outside read_keys,
        because the step genuinely cannot see that key.

        Args:
            key: The key to check.

        Returns:
            True only if the key is within the read scope AND exists in the store.
        """
        if self._read_keys is not None and key not in self._read_keys:
            return False
        return self._store.has(key)

    def keys(self) -> list[str]:
        """Return the intersection of read_keys and the store's current keys.

        Returns:
            List of keys that are both declared readable and currently in the store.
            When read_keys is None (unrestricted), returns all store keys.
        """
        all_keys = self._store.keys()
        if self._read_keys is None:
            return all_keys
        return [k for k in all_keys if k in self._read_keys]
