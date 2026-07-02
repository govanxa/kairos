"""Kairos plugin registry — manifest model, decorators, loading, and discovery."""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import (
    distributions,  # module-level so tests can patch via monkeypatch.setattr
    entry_points,  # module-level so tests can patch via monkeypatch.setattr
)
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from kairos.exceptions import ConfigError, SecurityError
from kairos.schema import Schema
from kairos.step import Step

if TYPE_CHECKING:
    from kairos.step import StepContext
    from kairos.workflow import Workflow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KAIROS_PLUGIN_GROUP = "kairos.plugins"
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# Each numeric segment is capped at 8 digits to prevent int-string conversion DoS (SEV-002).
_VERSION_RE = re.compile(r"^\d{1,8}(\.\d{1,8})*$")
_SPEC_OP_RE = re.compile(r"^(~=|==|!=|>=|>|<=|<)\s*(.+)$")
# PluginManifest.version: PEP 440-compatible characters only, capped at 64 (SEV-003).
_VERSION_STR_RE = re.compile(r"^[A-Za-z0-9._+!\-]{1,64}$")
# Control-character pattern — used for sanitizing untrusted metadata strings (SEV-003).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_DESCRIPTION_MAX_LEN = 500
# Cap comma-separated clauses to prevent specifier-parsing DoS (SEV-002).
_MAX_SPECIFIER_CLAUSES = 20


class SecurityWarning(UserWarning):
    """Warning emitted when a security-sensitive operation is performed."""


_F = TypeVar("_F", bound=Callable[..., object])

# ---------------------------------------------------------------------------
# Protocols for duck-typing entry points and distributions
# ---------------------------------------------------------------------------


class _DistLike(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def files(self) -> list[Any] | None: ...  # importlib.metadata.PackagePath list at runtime
    def locate_file(self, rel: str) -> Any: ...  # Path at runtime; Any avoids import cycle


class _EPLike(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def value(self) -> str: ...
    @property
    def group(self) -> str: ...
    @property
    def dist(self) -> _DistLike | None: ...
    def load(self) -> object: ...


# ---------------------------------------------------------------------------
# Version comparator (PEP 440 subset — no packaging dep)
# ---------------------------------------------------------------------------


def _normalize_dist_name(name: str) -> str:
    """Normalize a distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_specifier(spec: str) -> list[tuple[str, tuple[int, ...]]]:
    """Parse a PEP 440 subset specifier into (operator, version_tuple) pairs.

    Supported operators: == != >= > <= < ~=, comma-AND, dotted numeric N(.N)*.
    Epochs, pre/post/dev/local, and wildcards raise ConfigError.

    Args:
        spec: A version specifier string such as ">=0.4,<0.5".

    Returns:
        List of (operator, version_tuple) pairs.

    Raises:
        ConfigError: For unsupported syntax.
    """
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise ConfigError(f"Empty version specifier {spec!r}.")
    if len(parts) > _MAX_SPECIFIER_CLAUSES:
        raise ConfigError(
            f"Too many version specifier clauses ({len(parts)}); max {_MAX_SPECIFIER_CLAUSES}."
        )
    result: list[tuple[str, tuple[int, ...]]] = []
    for part in parts:
        m = _SPEC_OP_RE.match(part)
        if not m:
            raise ConfigError(f"Unsupported version specifier {part[:100]!r}.")
        op, ver_str = m.group(1), m.group(2).strip()
        if "!" in ver_str or "*" in ver_str or re.search(r"[a-zA-Z]", ver_str):
            raise ConfigError(
                f"Unsupported version specifier {part[:100]!r}. "
                "Only numeric N(.N)* segments are supported "
                "(no epochs, pre/post/dev/local, wildcards)."
            )
        if not _VERSION_RE.match(ver_str):
            # Segments > 8 digits are rejected here (SEV-002) before int conversion.
            raise ConfigError(f"Unsupported version specifier {part[:100]!r}.")
        try:
            ver_tuple: tuple[int, ...] = tuple(int(x) for x in ver_str.split("."))
        except ValueError as exc:
            # Defense-in-depth: should not be reachable after _VERSION_RE guards.
            raise ConfigError(
                f"Unsupported version specifier {part[:100]!r}: segment is not a valid integer."
            ) from exc
        if op == "~=":
            if len(ver_tuple) < 2:
                raise ConfigError(f"~= requires at least two version components, got {ver_str!r}.")
            result.append((">=", ver_tuple))
            upper = list(ver_tuple[:-1])
            upper[-1] += 1
            result.append(("<", tuple(upper)))
        else:
            result.append((op, ver_tuple))
    return result


def _cmp_versions(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Compare two version tuples, padding the shorter one with zeros."""
    max_len = max(len(a), len(b))
    ap = a + (0,) * (max_len - len(a))
    bp = b + (0,) * (max_len - len(b))
    if ap < bp:
        return -1
    if ap > bp:
        return 1
    return 0


def _check_version(requires_kairos: str, current: str) -> None:
    """Verify that *current* satisfies the *requires_kairos* specifier.

    Args:
        requires_kairos: PEP 440 subset specifier from the manifest.
        current: The running kairos.__version__.

    Raises:
        ConfigError: When the specifier is unsupported or the version is unsatisfied.
    """
    specs = _parse_specifier(requires_kairos)  # may raise ConfigError
    if not _VERSION_RE.match(current):
        logger.debug(
            "Version check skipped: kairos.__version__=%r is non-numeric; "
            "requires_kairos=%r not evaluated.",
            current,
            requires_kairos,
        )
        return  # non-numeric current (e.g. dev) — skip check
    cur: tuple[int, ...] = tuple(int(x) for x in current.split("."))
    for op, ver in specs:
        cmp = _cmp_versions(cur, ver)
        match op:
            case "==":
                ok = cmp == 0
            case "!=":
                ok = cmp != 0
            case ">=":
                ok = cmp >= 0
            case ">":
                ok = cmp > 0
            case "<=":
                ok = cmp <= 0
            case "<":
                ok = cmp < 0
            case _:  # pragma: no cover
                raise ConfigError(f"Unknown operator {op!r}.")
        if not ok:
            raise ConfigError(
                f"Plugin requires kairos {requires_kairos!r} but current version is {current!r}."
            )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepPluginSpec:
    """Metadata and action for one step exported by a plugin.

    Attributes:
        action: Step action callable matching Callable[[StepContext], object].
        input_contract: Optional Schema for dependency-output validation.
        output_contract: Optional Schema for output validation.
        description: Human-readable description.
    """

    action: Callable[[StepContext], object]
    input_contract: Schema | None
    output_contract: Schema | None
    description: str

    def __post_init__(self) -> None:
        if not callable(self.action):
            raise ConfigError(
                f"StepPluginSpec.action must be callable, got {type(self.action).__name__!r}."
            )
        if self.input_contract is not None and not isinstance(self.input_contract, Schema):
            raise ConfigError(
                f"StepPluginSpec.input_contract must be Schema or None, "
                f"got {type(self.input_contract).__name__!r}."
            )
        if self.output_contract is not None and not isinstance(self.output_contract, Schema):
            raise ConfigError(
                f"StepPluginSpec.output_contract must be Schema or None, "
                f"got {type(self.output_contract).__name__!r}."
            )
        # SEV-003-R: strip control chars from description and cap length.
        # Identical treatment to PluginManifest.description — prevents terminal-escape
        # injection when step descriptions are echoed by `plugin list --describe`.
        sanitized_desc = _CONTROL_CHAR_RE.sub("", self.description)[:_DESCRIPTION_MAX_LEN]
        object.__setattr__(self, "description", sanitized_desc)


@dataclass(frozen=True)
class PluginManifest:
    """Immutable manifest for an installed Kairos plugin.

    Attributes:
        name: Plugin namespace key — [a-zA-Z0-9_-]+.
        version: Plugin version string (non-empty).
        description: Human-readable description.
        requires_kairos: PEP 440 subset specifier for kairos version.
        steps: Mapping of step key to StepPluginSpec.
        validators: Mapping of validator name to callable.
        workflows: Forward-compat slot for workflow factories (shape-validated only).
    """

    name: str
    version: str
    description: str
    requires_kairos: str
    steps: dict[str, StepPluginSpec]
    validators: dict[str, Callable[..., object]]
    workflows: dict[str, Callable[..., Workflow]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _VALID_NAME_RE.match(self.name):
            raise ConfigError(
                f"PluginManifest.name {self.name!r} is invalid. Only [a-zA-Z0-9_-] allowed."
            )
        if not self.version:
            raise ConfigError("PluginManifest.version must be non-empty.")
        # SEV-003: validate version string format to prevent log forging / ANSI injection.
        if not _VERSION_STR_RE.match(self.version):
            raise ConfigError(
                f"PluginManifest.version {self.version[:64]!r} contains invalid characters. "
                "Only [A-Za-z0-9._+!-] are allowed, max 64 characters."
            )
        # SEV-003: strip control chars from description and cap length.
        sanitized_desc = _CONTROL_CHAR_RE.sub("", self.description)[:_DESCRIPTION_MAX_LEN]
        object.__setattr__(self, "description", sanitized_desc)
        for key in self.steps:
            if not _VALID_NAME_RE.match(key):
                raise ConfigError(f"Step key {key!r} is invalid. Only [a-zA-Z0-9_-] allowed.")
        for key, val in self.validators.items():
            if not callable(val):
                raise ConfigError(f"Validator {key!r} must be callable.")
        for key, val in self.workflows.items():
            if not callable(val):
                raise ConfigError(f"Workflow factory {key!r} must be callable.")
        try:
            _parse_specifier(self.requires_kairos)
        except ConfigError as exc:
            raise ConfigError(
                f"PluginManifest.requires_kairos {self.requires_kairos[:100]!r} is invalid: {exc}"
            ) from exc
        except Exception as exc:
            # SEV-002: convert any unexpected parse exception (e.g. ValueError) to ConfigError.
            raise ConfigError(
                f"PluginManifest.requires_kairos {self.requires_kairos[:100]!r} "
                f"caused an unexpected parse error: {type(exc).__name__}."
            ) from exc

    def build_step(
        self,
        step_key: str,
        *,
        name: str | None = None,
        depends_on: list[str] | None = None,
        read_keys: list[str] | None = None,
        write_keys: list[str] | None = None,
        failure_policy: object = None,
        **config_kwargs: object,
    ) -> Step:
        """Construct a Step from a registered spec.

        Args:
            step_key: Key in self.steps.
            name: Override for Step.name; defaults to step_key.
            depends_on: Upstream step names.
            read_keys: Scoped state read access.
            write_keys: Scoped state write access.
            failure_policy: Step-level failure policy.
            **config_kwargs: Forwarded to StepConfig.

        Returns:
            A configured Step instance.

        Raises:
            ConfigError: If step_key is not in self.steps.
        """
        if step_key not in self.steps:
            raise ConfigError(
                f"Plugin {self.name!r} has no step {step_key!r}. Available: {list(self.steps)}."
            )
        spec = self.steps[step_key]
        return Step(
            name=name if name is not None else step_key,
            action=spec.action,
            depends_on=depends_on,
            input_contract=spec.input_contract,
            output_contract=spec.output_contract,
            read_keys=read_keys,
            write_keys=write_keys,
            failure_policy=failure_policy,
            **config_kwargs,  # type: ignore[arg-type]  # dict[str, object] vs StepConfig | None
        )

    def describe(self) -> dict[str, object]:
        """Return a JSON-safe metadata view (no callables).

        Returns:
            Dict with name, version, description, requires_kairos, steps
            (each with description and contract field names), and validator names.
        """
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "requires_kairos": self.requires_kairos,
            "steps": {
                key: {
                    "description": spec.description,
                    "input_contract": (
                        spec.input_contract.field_names if spec.input_contract else None
                    ),
                    "output_contract": (
                        spec.output_contract.field_names if spec.output_contract else None
                    ),
                }
                for key, spec in self.steps.items()
            },
            "validators": list(self.validators.keys()),
        }


# ---------------------------------------------------------------------------
# Decorators + manifest assembly
# ---------------------------------------------------------------------------


def step_plugin(
    *,
    name: str,
    description: str = "",
    input_contract: Schema | None = None,
    output_contract: Schema | None = None,
) -> Callable[[_F], _F]:
    """Attach StepPluginSpec metadata to a callable and return it unchanged.

    The spec is stored as ``func.__kairos_plugin_step__ = (name, spec)``.

    Args:
        name: Key used in PluginManifest.steps.
        description: Human-readable description.
        input_contract: Optional Schema for input validation.
        output_contract: Optional Schema for output validation.

    Returns:
        Decorator that annotates the function and returns it.
    """

    def decorator(func: _F) -> _F:
        spec = StepPluginSpec(
            action=func,  # type: ignore[arg-type]
            input_contract=input_contract,
            output_contract=output_contract,
            description=description,
        )
        func.__kairos_plugin_step__ = (name, spec)  # type: ignore[attr-defined]
        return func

    return decorator


def validator_plugin(*, name: str, description: str = "") -> Callable[[_F], _F]:
    """Attach validator metadata to a callable and return it unchanged.

    The name is stored as ``func.__kairos_plugin_validator__ = name``.

    Args:
        name: Key used in PluginManifest.validators.
        description: Human-readable description (stored for future use).

    Returns:
        Decorator that annotates the function and returns it.
    """

    def decorator(func: _F) -> _F:
        func.__kairos_plugin_validator__ = name  # type: ignore[attr-defined]
        return func

    return decorator


def build_manifest(
    *,
    name: str,
    version: str,
    description: str = "",
    requires_kairos: str,
    steps: Sequence[Callable[..., object]] = (),
    validators: Sequence[Callable[..., object]] = (),
    workflows: dict[str, Callable[..., Workflow]] | None = None,
) -> PluginManifest:
    """Assemble a PluginManifest from decorated callables.

    Args:
        name: Plugin namespace key.
        version: Plugin version string.
        description: Human-readable description.
        requires_kairos: PEP 440 subset specifier.
        steps: Callables decorated with @step_plugin.
        validators: Callables decorated with @validator_plugin.
        workflows: Optional workflow factory mapping.

    Returns:
        A validated PluginManifest.

    Raises:
        ConfigError: If a callable is not decorated, or duplicate step name.
    """
    steps_dict: dict[str, StepPluginSpec] = {}
    for func in steps:
        info = getattr(func, "__kairos_plugin_step__", None)
        if info is None:
            fn_name = getattr(func, "__name__", repr(func))
            raise ConfigError(f"Function {fn_name!r} is not decorated with @step_plugin.")
        step_name, spec = cast(tuple[str, StepPluginSpec], info)
        if step_name in steps_dict:
            raise ConfigError(
                f"Duplicate step name {step_name!r} in build_manifest for plugin {name!r}."
            )
        steps_dict[step_name] = spec

    validators_dict: dict[str, Callable[..., object]] = {}
    for func in validators:
        val_name = getattr(func, "__kairos_plugin_validator__", None)
        if val_name is None:
            fn_name = getattr(func, "__name__", repr(func))
            raise ConfigError(f"Function {fn_name!r} is not decorated with @validator_plugin.")
        validators_dict[cast(str, val_name)] = func

    return PluginManifest(
        name=name,
        version=version,
        description=description,
        requires_kairos=requires_kairos,
        steps=steps_dict,
        validators=validators_dict,
        workflows=workflows or {},
    )


# ---------------------------------------------------------------------------
# Internal loading helpers
# ---------------------------------------------------------------------------


def _entry_points_for(name: str) -> list[_EPLike]:
    """Return all kairos.plugins entry points for the named distribution.

    Args:
        name: Distribution name (unnormalized).

    Returns:
        List of matching entry points (may be empty).
    """
    normalized = _normalize_dist_name(name)
    return [
        ep  # type: ignore[misc]
        for ep in entry_points(group=_KAIROS_PLUGIN_GROUP)
        if ep.dist is not None and _normalize_dist_name(ep.dist.name) == normalized
    ]


def _assert_within_distribution(ep: _EPLike, plugin_name: str) -> None:
    """Raise SecurityError if the entry-point module is outside its distribution.

    This check runs BEFORE ep.load() is called (spec §5.1 step 3), so it must
    not rely on sys.modules being populated by the load.  It uses
    importlib.util.find_spec() to locate the module file without executing it,
    falling back to sys.modules only if the module is already cached from an
    earlier import.  Fails closed when the module's origin cannot be determined.

    Containment is enforced via two sequential gates:

    1. **Fast-path prefix check** — the module file must reside under the
       distribution root (``dist.locate_file("")``).  Rejects obvious supply-chain
       attacks without reading the RECORD.

    2. **Authoritative RECORD check (SEV-005)** — the module file must appear in
       ``dist.files`` (the installed RECORD).  This closes the site-packages
       boundary gap: two packages that share the same root prefix (e.g. both
       installed under site-packages) cannot impersonate each other.  Fails closed
       when ``dist.files`` is ``None`` or empty — a distribution without a RECORD
       cannot be verified.

    Known bounded behavior: for dotted module targets (e.g. ``"pkg.sub.module"``)
    ``importlib.util.find_spec()`` imports each parent package's ``__init__.py``
    in order to locate the child module.  Those parent ``__init__`` files execute
    during the containment step, BEFORE the RECORD gate runs — the gate verifies
    the leaf module only, so a parent package outside this distribution can have
    its ``__init__`` executed as a resolution side effect even when the load is
    ultimately refused.  The leaf module never executes and the manifest is never
    loaded on refusal; the side effect is limited to packages already legitimately
    installed and importable in this environment.

    Known limitation: the RECORD membership set is built from ``.py`` entries
    only, so a source-less plugin (``.pyd``/``.so``/``.pyc``-only distribution)
    is refused with SecurityError (fail-closed by design).

    Args:
        ep: The entry point whose containment is being verified.
        plugin_name: Distribution name string used in error messages.

    Raises:
        SecurityError: If the module cannot be resolved (e.g. dotted path with a
            missing parent package — SEV-004), if the module file is outside the
            distribution root prefix (gate 1), if the distribution has no file
            RECORD or ``dist.files`` is empty (gate 2, fail closed), if the module
            file is not listed in the distribution RECORD (gate 2), if the entry
            point has no distribution reference, or if the module file location
            cannot be determined.
    """
    module_name = ep.value.split(":")[0]
    # Check sys.modules first (module may already be cached from a prior import).
    mod = sys.modules.get(module_name)
    raw_file: str | None
    if mod is not None:
        raw_file = getattr(mod, "__file__", None)
    else:
        # find_spec locates the file without executing the leaf module code.
        # For dotted targets it WILL import parent packages' __init__.py — see docstring.
        # SEV-004: an unresolvable target (missing parent package) is a policy violation;
        # catch the entire ImportError family plus ValueError/AttributeError edge cases
        # and fail closed as SecurityError rather than letting a bare ModuleNotFoundError
        # escape the public API.
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError, AttributeError) as exc:
            raise SecurityError(
                f"Cannot resolve module for plugin {plugin_name!r}: "
                f"{type(exc).__name__} — loading refused (unresolvable module target)."
            ) from exc
        raw_file = spec.origin if spec is not None else None

    if raw_file is None:
        # Fail closed — unknown origin is not acceptable for a security boundary.
        raise SecurityError(
            f"Cannot verify containment for plugin {plugin_name!r}: module __file__ is unknown."
        )

    target = os.path.realpath(raw_file)
    dist = ep.dist
    if dist is None:
        raise SecurityError(
            f"Cannot verify containment for plugin {plugin_name!r}: "
            "entry point has no distribution reference."
        )

    # --- Gate 1: fast-path prefix check ---
    dist_root = os.path.realpath(str(dist.locate_file("")))
    if not (target.startswith(dist_root + os.sep) or target.startswith(dist_root + "/")):
        raise SecurityError(
            f"Plugin {plugin_name!r} entry point resolves outside its declared distribution. "
            "Possible supply chain attack — loading refused."
        )

    # --- Gate 2: authoritative RECORD membership check (SEV-005) ---
    # site-packages is the dist_root for real installs, so the prefix check above is
    # insufficient — two packages installed side-by-side both pass it.  The RECORD
    # (dist.files) lists exactly the files shipped by this distribution; membership
    # in that set is the authoritative boundary.
    dist_files = dist.files
    if not dist_files:
        raise SecurityError(
            f"Plugin {plugin_name!r} has no file RECORD (dist.files is empty/None). "
            "Cannot verify per-distribution containment — loading refused."
        )
    record_paths = {
        os.path.realpath(str(dist.locate_file(str(f))))
        for f in dist_files
        if str(f).endswith(".py")
    }
    if target not in record_paths:
        raise SecurityError(
            f"Plugin {plugin_name!r} entry point module is not in its distribution's "
            "file RECORD. Possible supply chain attack — loading refused."
        )


def _sanitize_metadata_str(s: str, max_len: int = 200) -> str:
    """Strip control characters and length-cap a metadata string.

    Metadata from importlib.metadata (dist.name, dist.version, ep.name) is
    untrusted — it bypasses PluginManifest validation and must be sanitized
    before being echoed to CLI output, log lines, or warning messages (SEV-003).

    Args:
        s: The string to sanitize.
        max_len: Maximum output length after stripping control chars.

    Returns:
        Sanitized string with control characters removed and length-capped.
    """
    return _CONTROL_CHAR_RE.sub("", s)[:max_len]


def _list_plugin_entries() -> list[dict[str, str]]:
    """Return metadata-only info about installed kairos.plugins packages.

    Does NOT call ep.load() — safe to call without executing plugin code.

    Returns:
        List of dicts with keys dist_name, version, ep_name.
    """
    result: list[dict[str, str]] = []
    for ep in entry_points(group=_KAIROS_PLUGIN_GROUP):  # type: ignore[misc]
        dist = ep.dist
        if dist is None:
            continue
        # SEV-003: sanitize metadata strings — they bypass PluginManifest validation.
        result.append(
            {
                "dist_name": _sanitize_metadata_str(dist.name),
                "version": _sanitize_metadata_str(dist.version),
                "ep_name": _sanitize_metadata_str(ep.name),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_plugin(name: str) -> PluginManifest:
    """Load a named Kairos plugin distribution and return its manifest.

    Follows the 7-step security flow from spec §5.1.  Crucially, the
    containment check (step 3) runs BEFORE ep.load() (step 4) so that
    third-party module code is never executed unless it has been verified to
    reside within its declared distribution.

    Steps:
    1. Verify the distribution is installed.
    2. Locate exactly one kairos.plugins entry point.
    3. Containment check — module file within distribution root (via find_spec,
       no import/execution of the target module).
    4. Load and wrap import errors.
    5. Verify the target is a PluginManifest.
    6. Version compatibility check.
    7. Audit log.

    Args:
        name: Distribution name, e.g. "kairos-plugin-evidence".

    Returns:
        The plugin's PluginManifest.

    Raises:
        ConfigError: Distribution not installed, no/ambiguous entry point,
            load failure, wrong type, or version incompatibility.
        SecurityError: Entry point resolves outside its distribution.
    """
    normalized = _normalize_dist_name(name)

    # Step 1 — verify installed
    installed = any(
        _normalize_dist_name(d.name) == normalized  # type: ignore[union-attr]
        for d in distributions()
    )
    if not installed:
        raise ConfigError(f"Plugin {name!r} is not installed.")

    # Step 2 — locate entry point
    eps = _entry_points_for(name)
    if len(eps) == 0:
        raise ConfigError(f"Plugin {name!r} has no {_KAIROS_PLUGIN_GROUP!r} entry point.")
    if len(eps) > 1:
        raise ConfigError(
            f"Plugin {name!r} has {len(eps)} {_KAIROS_PLUGIN_GROUP!r} entry points (ambiguous)."
        )
    ep = eps[0]

    # Step 3 — containment check BEFORE load (SEV-001: spec §5.1 ordering)
    # Uses importlib.util.find_spec — locates module file without executing it.
    _assert_within_distribution(ep, name)

    # Step 4 — load, wrapping bare import errors
    try:
        obj = ep.load()
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"Failed to load plugin {name!r}: {type(exc).__name__}.") from exc

    # Step 5 — verify type
    if not isinstance(obj, PluginManifest):
        raise ConfigError(
            f"Plugin {name!r} entry point returned {type(obj).__name__!r}, expected PluginManifest."
        )
    manifest: PluginManifest = obj

    # Step 6 — version check
    import kairos as _k  # noqa: PLC0415

    _check_version(manifest.requires_kairos, _k.__version__)

    # Step 7 — audit log
    module_name = ep.value.split(":")[0]
    mod = sys.modules.get(module_name)
    source = os.path.realpath(getattr(mod, "__file__", "<unknown>")) if mod else "<unknown>"
    logger.info("Loading plugin %s v%s from %s", manifest.name, manifest.version, source)

    return manifest


def discover_plugins(
    *,
    allowlist: list[str] | None = None,
    allow_all: bool = False,
) -> dict[str, PluginManifest]:
    """Discover and load Kairos plugins.

    Security requirement #16: explicit loading, allowlisted, audited.
    ``allowlist=None`` and ``allow_all=False`` → SecurityError.
    ``allow_all=True`` → SecurityWarning emitted before loading all.

    Args:
        allowlist: Explicit list of distribution names to load.  An explicit
            empty list loads nothing.  When ``None``, the
            ``KAIROS_PLUGIN_ALLOWLIST`` environment variable is consulted.
        allow_all: When True and ``allowlist`` is ``None``, load all installed
            kairos.plugins packages after emitting a SecurityWarning.

    Returns:
        Dict keyed by manifest.name → PluginManifest.

    Raises:
        SecurityError: No allowlist and allow_all not set.
        ConfigError: Duplicate manifest.name across loaded plugins.
    """
    names_to_load: list[str]

    if allowlist is not None:
        names_to_load = [n.strip() for n in allowlist if n.strip()]
    elif allow_all:
        all_eps = list(entry_points(group=_KAIROS_PLUGIN_GROUP))  # type: ignore[misc]
        pkgs = [(ep.dist.name, ep.dist.version) for ep in all_eps if ep.dist is not None]
        # SEV-003: sanitize metadata strings before echoing in the SecurityWarning.
        pkg_list = ", ".join(
            f"{_sanitize_metadata_str(n)} (v{_sanitize_metadata_str(v)})" for n, v in pkgs
        )
        warnings.warn(
            f"Discovering ALL installed {_KAIROS_PLUGIN_GROUP!r} packages. "
            f"This executes code from every declaring package. "
            f"Found: {pkg_list or 'none'}",
            SecurityWarning,
            stacklevel=2,
        )
        names_to_load = [p[0] for p in pkgs]
    else:
        env_val = os.environ.get("KAIROS_PLUGIN_ALLOWLIST", "").strip()
        if env_val:
            names_to_load = [e.strip() for e in env_val.split(",") if e.strip()]
        else:
            raise SecurityError(
                "discover_plugins requires an explicit allowlist or allow_all=True. "
                "Set KAIROS_PLUGIN_ALLOWLIST or pass allowlist=[...] to opt in."
            )

    result: dict[str, PluginManifest] = {}
    for dist_name in names_to_load:
        manifest = load_plugin(dist_name)
        if manifest.name in result:
            raise ConfigError(
                f"Duplicate plugin name {manifest.name!r}: two installed plugins "
                "share the same manifest.name."
            )
        result[manifest.name] = manifest

    return result


def qualified_step_name(manifest: PluginManifest, step_name: str) -> str:
    """Return the qualified step name ``<plugin>.<step>`` for logging/registries.

    This form is NOT used as Step.name (dot is disallowed by the step-name
    regex); it is a display/audit label only.

    Args:
        manifest: The plugin manifest.
        step_name: Unqualified step key.

    Returns:
        ``"<manifest.name>.<step_name>"``.
    """
    return f"{manifest.name}.{step_name}"
