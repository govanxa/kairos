"""Tests for kairos.plugins — written after implementation, before review."""

from __future__ import annotations

import sys
import types as _types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kairos.exceptions import ConfigError, SecurityError
from kairos.plugins.registry import (
    PluginManifest,
    SecurityWarning,
    StepPluginSpec,
    _assert_within_distribution,
    _check_version,
    _parse_specifier,
    build_manifest,
    discover_plugins,
    load_plugin,
    qualified_step_name,
    step_plugin,
    validator_plugin,
)
from kairos.schema import Schema
from kairos.step import Step, StepContext

# ---------------------------------------------------------------------------
# Fake entry-point infrastructure
# ---------------------------------------------------------------------------


@dataclass
class _FakeDist:
    name: str
    version: str
    _root: Path
    # SEV-005: list of relative paths representing dist.files (the installed RECORD).
    # None means "no RECORD" → _assert_within_distribution fails closed.
    files: list[str] | None = None

    def locate_file(self, rel: str) -> Path:
        return self._root / rel if rel else self._root


class _FakeEP:
    """Duck-types importlib.metadata.EntryPoint (Python 3.11+)."""

    def __init__(
        self,
        *,
        name: str = "default",
        value: str = "fake_plugin:MANIFEST",
        group: str = "kairos.plugins",
        manifest: PluginManifest | None = None,
        load_error: Exception | None = None,
        module_file: Path | None = None,
        dist: _FakeDist | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self.group = group
        self._manifest = manifest
        self._load_error = load_error
        self._module_file = module_file
        self.dist = dist
        parts = value.split(":", 1)
        self.module = parts[0]
        self.attr = parts[1] if len(parts) > 1 else ""
        # Spy counter — incremented each time load() is invoked.
        self.load_call_count: int = 0

    def load(self) -> object:
        self.load_call_count += 1
        if self._load_error is not None:
            raise self._load_error
        # Note: fake_env pre-populates sys.modules via monkeypatch before load() is called,
        # so the containment check (which now runs BEFORE load) finds __file__ there.
        # This branch is retained as a fallback for _FakeEP instances created outside fake_env.
        if self._module_file is not None and self.module not in sys.modules:
            fake_mod = _types.ModuleType(self.module)
            fake_mod.__file__ = str(self._module_file)
            sys.modules[self.module] = fake_mod
        return self._manifest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_action() -> Any:
    def _action(ctx: StepContext) -> object:
        return {}

    return _action


def _make_manifest(
    name: str = "demo",
    version: str = "0.1.0",
    requires_kairos: str = ">=0.4",
    steps: dict[str, StepPluginSpec] | None = None,
) -> PluginManifest:
    action = _make_action()
    default_steps = steps or {
        "run": StepPluginSpec(
            action=action,
            input_contract=None,
            output_contract=None,
            description="demo step",
        )
    }
    return PluginManifest(
        name=name,
        version=version,
        description="A demo plugin",
        requires_kairos=requires_kairos,
        steps=default_steps,
        validators={},
    )


@pytest.fixture()
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Patch registry.entry_points and registry.distributions with fakes.

    Returns a register() helper that adds a fake distribution + entry point.
    """
    registered_eps: list[_FakeEP] = []
    registered_dists: list[_FakeDist] = []
    added_modules: list[str] = []

    def register(
        dist_name: str,
        manifest: PluginManifest | None = None,
        *,
        module_file: Path | None = None,
        dist_root: Path | None = None,
        load_error: Exception | None = None,
        ep_name: str = "default",
        add_ep: bool = True,
    ) -> _FakeDist:
        root = dist_root or (tmp_path / dist_name.replace("-", "_"))
        root.mkdir(parents=True, exist_ok=True)
        ver = manifest.version if manifest is not None else "0.0.1"
        # SEV-005: populate files with the canonical module filename so the RECORD
        # check passes for well-behaved plugins.  Breach tests pass module_file=<outside>
        # which is rejected by the fast-path prefix check before RECORD is consulted.
        dist = _FakeDist(name=dist_name, version=ver, _root=root, files=["plugin.py"])
        registered_dists.append(dist)

        if add_ep:
            mod_name = f"fake_{dist_name.replace('-', '_')}"
            mf = module_file or (root / "plugin.py")
            # Pre-populate sys.modules so _assert_within_distribution finds __file__
            fake_mod = _types.ModuleType(mod_name)
            fake_mod.__file__ = str(mf)
            monkeypatch.setitem(sys.modules, mod_name, fake_mod)
            added_modules.append(mod_name)

            ep = _FakeEP(
                name=ep_name,
                value=f"{mod_name}:MANIFEST",
                dist=dist,
                manifest=manifest,
                load_error=load_error,
                module_file=mf,
            )
            registered_eps.append(ep)

        return dist

    def fake_entry_points(group: str | None = None, **_kw: Any) -> list[_FakeEP]:
        if group is None:
            return registered_eps
        return [ep for ep in registered_eps if ep.group == group]

    monkeypatch.setattr("kairos.plugins.registry.entry_points", fake_entry_points)
    monkeypatch.setattr("kairos.plugins.registry.distributions", lambda: iter(registered_dists))

    return register


# ---------------------------------------------------------------------------
# TestLoadFailurePaths
# ---------------------------------------------------------------------------


class TestLoadFailurePaths:
    def test_not_installed(self, fake_env: Any) -> None:
        with pytest.raises(ConfigError, match="not installed"):
            load_plugin("kairos-plugin-missing")

    def test_no_entry_point(self, fake_env: Any) -> None:
        fake_env("kairos-plugin-noep", add_ep=False)
        with pytest.raises(ConfigError, match="entry point"):
            load_plugin("kairos-plugin-noep")

    def test_multiple_entry_points(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        manifest = _make_manifest()
        root = tmp_path / "multi"
        root.mkdir()
        dist = _FakeDist(name="kairos-plugin-multi", version="0.1.0", _root=root)

        mod_name = "fake_kairos_plugin_multi"
        fake_mod = _types.ModuleType(mod_name)
        fake_mod.__file__ = str(root / "plugin.py")
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        ep1 = _FakeEP(name="ep1", value=f"{mod_name}:MANIFEST", dist=dist, manifest=manifest)
        ep2 = _FakeEP(name="ep2", value=f"{mod_name}:MANIFEST2", dist=dist, manifest=manifest)

        monkeypatch.setattr(
            "kairos.plugins.registry.entry_points",
            lambda group=None, **_kw: [ep1, ep2] if group == "kairos.plugins" else [],
        )
        monkeypatch.setattr(
            "kairos.plugins.registry.distributions",
            lambda: iter([dist]),
        )

        with pytest.raises(ConfigError, match="ambiguous"):
            load_plugin("kairos-plugin-multi")

    def test_load_import_error_wrapped(self, fake_env: Any) -> None:
        fake_env("kairos-plugin-bad", load_error=ImportError("oops"))
        with pytest.raises(ConfigError):
            load_plugin("kairos-plugin-bad")

    def test_load_error_not_bare_importerror(self, fake_env: Any) -> None:
        fake_env("kairos-plugin-bad2", load_error=ImportError("oops"))
        try:
            load_plugin("kairos-plugin-bad2")
        except ConfigError:
            pass
        except ImportError:
            pytest.fail("bare ImportError escaped load_plugin")

    def test_load_attribute_error_wrapped(self, fake_env: Any) -> None:
        fake_env("kairos-plugin-attr", load_error=AttributeError("no attr"))
        with pytest.raises(ConfigError):
            load_plugin("kairos-plugin-attr")

    def test_not_plugin_manifest(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # load() returns a plain dict — not a PluginManifest
        root = tmp_path / "wrong"
        root.mkdir()
        dist = _FakeDist(
            name="kairos-plugin-wrong", version="0.1.0", _root=root, files=["plugin.py"]
        )
        mod_name = "fake_kairos_plugin_wrong"
        fake_mod = _types.ModuleType(mod_name)
        fake_mod.__file__ = str(root / "plugin.py")
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        ep = _FakeEP(name="d", value=f"{mod_name}:MANIFEST", dist=dist, manifest=None)
        # Override _manifest to return a dict (not a PluginManifest).
        ep._manifest = {"not": "a manifest"}  # type: ignore[assignment]

        monkeypatch.setattr(
            "kairos.plugins.registry.entry_points",
            lambda group=None, **_kw: [ep] if group == "kairos.plugins" else [],
        )
        monkeypatch.setattr(
            "kairos.plugins.registry.distributions",
            lambda: iter([dist]),
        )

        with pytest.raises(ConfigError, match="PluginManifest"):
            load_plugin("kairos-plugin-wrong")

    def test_version_incompatible(self, fake_env: Any) -> None:
        manifest = _make_manifest(requires_kairos=">=99.0")
        fake_env("kairos-plugin-old", manifest)
        with pytest.raises(ConfigError, match="99.0"):
            load_plugin("kairos-plugin-old")

    def test_unparseable_specifier(self) -> None:
        # PluginManifest.__post_init__ validates the specifier format, so an
        # unparseable specifier raises ConfigError at manifest construction.
        # _check_version also raises ConfigError when called directly.
        with pytest.raises(ConfigError):
            _check_version("1!0.4", "0.4.6")
        with pytest.raises(ConfigError):
            _parse_specifier(">=0.4.*")


# ---------------------------------------------------------------------------
# TestManifestValidation
# ---------------------------------------------------------------------------


class TestManifestValidation:
    def test_bad_name_char(self) -> None:
        with pytest.raises(ConfigError, match="invalid"):
            PluginManifest(
                name="bad.name",
                version="0.1.0",
                description="",
                requires_kairos=">=0.4",
                steps={},
                validators={},
            )

    def test_empty_version(self) -> None:
        with pytest.raises(ConfigError, match="non-empty"):
            PluginManifest(
                name="demo",
                version="",
                description="",
                requires_kairos=">=0.4",
                steps={},
                validators={},
            )

    def test_non_callable_action(self) -> None:
        with pytest.raises(ConfigError, match="callable"):
            StepPluginSpec(
                action=42,  # type: ignore[arg-type]
                input_contract=None,
                output_contract=None,
                description="",
            )

    def test_bad_step_key_char(self) -> None:
        action = _make_action()
        spec = StepPluginSpec(
            action=action, input_contract=None, output_contract=None, description=""
        )
        with pytest.raises(ConfigError, match="invalid"):
            PluginManifest(
                name="demo",
                version="0.1.0",
                description="",
                requires_kairos=">=0.4",
                steps={"bad.key": spec},
                validators={},
            )

    def test_non_schema_contract(self) -> None:
        action = _make_action()
        with pytest.raises(ConfigError, match="Schema"):
            StepPluginSpec(
                action=action,
                input_contract={"not": "schema"},  # type: ignore[arg-type]
                output_contract=None,
                description="",
            )

    def test_duplicate_step_name_in_build_manifest(self) -> None:
        @step_plugin(name="run", description="step a")
        def step_a(ctx: StepContext) -> object:
            return {}

        @step_plugin(name="run", description="step b")
        def step_b(ctx: StepContext) -> object:
            return {}

        with pytest.raises(ConfigError, match="Duplicate"):
            build_manifest(
                name="demo",
                version="0.1.0",
                requires_kairos=">=0.4",
                steps=[step_a, step_b],
            )


# ---------------------------------------------------------------------------
# TestDiscoveryFailurePaths
# ---------------------------------------------------------------------------


class TestDiscoveryFailurePaths:
    def test_discover_without_allowlist_raises(self) -> None:
        with pytest.raises(SecurityError):
            discover_plugins(allowlist=None, allow_all=False)

    def test_containment_breach(self, fake_env: Any, tmp_path: Path) -> None:
        root = tmp_path / "legit"
        root.mkdir()
        outside = tmp_path / "outside" / "plugin.py"
        outside.parent.mkdir()
        outside.touch()
        manifest = _make_manifest()
        fake_env("kairos-plugin-breach", manifest, dist_root=root, module_file=outside)
        with pytest.raises(SecurityError, match="outside"):
            load_plugin("kairos-plugin-breach")

    def test_duplicate_plugin_name(self, fake_env: Any) -> None:
        m1 = _make_manifest(name="shared")
        m2 = _make_manifest(name="shared", version="0.2.0")
        fake_env("kairos-plugin-a", m1)
        fake_env("kairos-plugin-b", m2)
        with pytest.raises(ConfigError, match="Duplicate"):
            discover_plugins(allowlist=["kairos-plugin-a", "kairos-plugin-b"])


# ---------------------------------------------------------------------------
# TestBoundaryConditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_empty_allowlist(self, fake_env: Any) -> None:
        result = discover_plugins(allowlist=[])
        assert result == {}

    def test_single_plugin(self, fake_env: Any) -> None:
        manifest = _make_manifest()
        fake_env("kairos-plugin-solo", manifest)
        result = discover_plugins(allowlist=["kairos-plugin-solo"])
        assert "demo" in result

    def test_env_var_empty_treated_as_unset(
        self, fake_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KAIROS_PLUGIN_ALLOWLIST", "")
        with pytest.raises(SecurityError):
            discover_plugins()

    def test_env_var_whitespace_normalized(
        self, fake_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _make_manifest()
        fake_env("kairos-plugin-ws", manifest)
        monkeypatch.setenv("KAIROS_PLUGIN_ALLOWLIST", " kairos-plugin-ws ")
        result = discover_plugins()
        assert "demo" in result

    def test_allowlist_arg_wins_over_env(
        self, fake_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KAIROS_PLUGIN_ALLOWLIST", "some-other-pkg")
        result = discover_plugins(allowlist=[])
        assert result == {}


# ---------------------------------------------------------------------------
# TestBasicBehavior
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    def test_load_plugin_returns_manifest(self, fake_env: Any) -> None:
        manifest = _make_manifest()
        fake_env("kairos-plugin-demo", manifest)
        result = load_plugin("kairos-plugin-demo")
        assert isinstance(result, PluginManifest)
        assert result.name == "demo"

    def test_manifest_steps_access(self, fake_env: Any) -> None:
        manifest = _make_manifest()
        fake_env("kairos-plugin-demo", manifest)
        result = load_plugin("kairos-plugin-demo")
        assert "run" in result.steps
        assert isinstance(result.steps["run"], StepPluginSpec)

    def test_build_step_creates_step(self, fake_env: Any) -> None:
        schema = Schema({"value": str})
        action = _make_action()
        spec = StepPluginSpec(
            action=action, input_contract=None, output_contract=schema, description=""
        )
        manifest = _make_manifest(steps={"gate": spec})
        fake_env("kairos-plugin-step", manifest)
        loaded = load_plugin("kairos-plugin-step")
        step = loaded.build_step("gate")
        assert isinstance(step, Step)
        assert step.output_contract is schema

    def test_build_step_name_default_no_dot(self, fake_env: Any) -> None:
        manifest = _make_manifest()
        fake_env("kairos-plugin-nd", manifest)
        loaded = load_plugin("kairos-plugin-nd")
        step = loaded.build_step("run")
        assert step.name == "run"
        assert "." not in step.name

    def test_discover_loads_subset(self, fake_env: Any) -> None:
        m1 = _make_manifest(name="plug1")
        m2 = _make_manifest(name="plug2")
        fake_env("kairos-plugin-p1", m1)
        fake_env("kairos-plugin-p2", m2)
        result = discover_plugins(allowlist=["kairos-plugin-p1"])
        assert "plug1" in result
        assert "plug2" not in result

    def test_env_var_allowlist(self, fake_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _make_manifest(name="envplug")
        fake_env("kairos-plugin-env", manifest)
        monkeypatch.setenv("KAIROS_PLUGIN_ALLOWLIST", "kairos-plugin-env")
        result = discover_plugins()
        assert "envplug" in result

    def test_decorators_leave_function_callable(self) -> None:
        @step_plugin(name="my_step", description="desc")
        def my_step(ctx: StepContext) -> object:
            return {"ok": True}

        assert callable(my_step)
        # __kairos_plugin_step__ is attached
        assert hasattr(my_step, "__kairos_plugin_step__")

    def test_build_manifest_assembles(self) -> None:
        @step_plugin(name="fetch", description="fetches data")
        def fetch_step(ctx: StepContext) -> object:
            return {}

        @validator_plugin(name="check_url")
        def check_url(value: object) -> bool:
            return True

        manifest = build_manifest(
            name="myplugin",
            version="1.0.0",
            requires_kairos=">=0.4",
            steps=[fetch_step],
            validators=[check_url],
        )
        assert manifest.name == "myplugin"
        assert "fetch" in manifest.steps
        assert "check_url" in manifest.validators

    def test_qualified_step_name(self) -> None:
        manifest = _make_manifest(name="myplugin")
        result = qualified_step_name(manifest, "run")
        assert result == "myplugin.run"


# ---------------------------------------------------------------------------
# TestVersionCompat
# ---------------------------------------------------------------------------


class TestVersionCompat:
    """Tests for _parse_specifier and _check_version against stubbed current="0.4.6"."""

    CURRENT = "0.4.6"

    def _ok(self, spec: str) -> None:
        _check_version(spec, self.CURRENT)

    def _fail(self, spec: str) -> None:
        with pytest.raises(ConfigError):
            _check_version(spec, self.CURRENT)

    def test_equal_match(self) -> None:
        self._ok("==0.4.6")

    def test_equal_mismatch(self) -> None:
        self._fail("==0.4.7")

    def test_not_equal_pass(self) -> None:
        self._ok("!=0.4.7")

    def test_not_equal_fail(self) -> None:
        self._fail("!=0.4.6")

    def test_ge_pass(self) -> None:
        self._ok(">=0.4")

    def test_ge_fail(self) -> None:
        self._fail(">=0.5")

    def test_gt_pass(self) -> None:
        self._ok(">0.4.5")

    def test_gt_fail(self) -> None:
        self._fail(">0.4.6")

    def test_le_pass(self) -> None:
        self._ok("<=0.4.6")

    def test_le_fail(self) -> None:
        self._fail("<=0.4.5")

    def test_lt_pass(self) -> None:
        self._ok("<0.5")

    def test_lt_fail(self) -> None:
        self._fail("<0.4.6")

    def test_tilde_eq_minor(self) -> None:
        # ~=0.4 → >=0.4,<1
        self._ok("~=0.4")
        _check_version("~=0.4", "0.4.0")

    def test_tilde_eq_patch(self) -> None:
        # ~=0.4.5 → >=0.4.5,<0.5
        self._ok("~=0.4.5")
        self._fail("~=0.4.7")  # 0.4.6 < 0.4.7

    def test_tilde_eq_upper_bound(self) -> None:
        # ~=0.4 should fail for 0.5 (outside <1 but ... wait 0.4.6 < 1 so passes)
        # Let's check a version that exceeds: use current="1.0.0"
        with pytest.raises(ConfigError):
            _check_version("~=0.4", "1.0.0")

    def test_comma_and(self) -> None:
        self._ok(">=0.4,<0.5")

    def test_comma_and_fail(self) -> None:
        self._fail(">=0.4,<0.4.6")  # 0.4.6 is not < 0.4.6

    def test_epoch_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _parse_specifier("1!0.4")

    def test_wildcard_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _parse_specifier("==0.4.*")

    def test_pre_release_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _parse_specifier(">=0.4.0a1")

    def test_error_message_names_both_versions(self, fake_env: Any) -> None:
        manifest = _make_manifest(requires_kairos=">=99.0")
        fake_env("kairos-plugin-ver", manifest)
        with pytest.raises(ConfigError) as exc_info:
            load_plugin("kairos-plugin-ver")
        msg = str(exc_info.value)
        assert "99.0" in msg
        # current version also in message
        import kairos as _k

        assert _k.__version__ in msg

    def test_tilde_single_component_rejected(self) -> None:
        with pytest.raises(ConfigError, match="two version"):
            _parse_specifier("~=0")


# ---------------------------------------------------------------------------
# TestPluginSecurity  (CLAUDE.md req #16 — verbatim names required)
# ---------------------------------------------------------------------------


class TestPluginSecurity:
    def test_discover_without_allowlist_raises(self) -> None:
        with pytest.raises(SecurityError):
            discover_plugins(allowlist=None, allow_all=False)

    def test_discover_with_allow_all_logs_warning(self, fake_env: Any) -> None:
        manifest = _make_manifest(name="warned")
        fake_env("kairos-plugin-warn", manifest)
        with pytest.warns(SecurityWarning):
            discover_plugins(allow_all=True)

    def test_load_plugin_logs_source_path(
        self, fake_env: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        manifest = _make_manifest()
        fake_env("kairos-plugin-log", manifest)
        with caplog.at_level("INFO", logger="kairos.plugins.registry"):
            load_plugin("kairos-plugin-log")
        assert any("Loading plugin" in r.message for r in caplog.records)

    def test_entry_point_outside_package_raises_security_error(
        self, fake_env: Any, tmp_path: Path
    ) -> None:
        root = tmp_path / "contained"
        root.mkdir()
        outside = tmp_path / "elsewhere" / "bad.py"
        outside.parent.mkdir()
        outside.touch()
        manifest = _make_manifest(name="breached")
        fake_env(
            "kairos-plugin-contain",
            manifest,
            dist_root=root,
            module_file=outside,
        )
        with pytest.raises(SecurityError):
            load_plugin("kairos-plugin-contain")

    def test_load_error_wrapped_not_bare_importerror(self, fake_env: Any) -> None:
        fake_env("kairos-plugin-wrap", load_error=ImportError("no module"))
        with pytest.raises(ConfigError):
            load_plugin("kairos-plugin-wrap")
        # Verify it doesn't leak as ImportError
        try:
            fake_env("kairos-plugin-wrap2", load_error=ImportError("again"))
            load_plugin("kairos-plugin-wrap2")
        except ConfigError:
            pass
        except ImportError:
            pytest.fail("ImportError escaped — not wrapped in ConfigError")

    def test_allowlist_from_env_var_respected(
        self, fake_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _make_manifest(name="envtest")
        fake_env("kairos-plugin-envtest", manifest)
        monkeypatch.setenv("KAIROS_PLUGIN_ALLOWLIST", "kairos-plugin-envtest")
        result = discover_plugins()
        assert "envtest" in result

    def test_allow_all_warning_lists_packages(self, fake_env: Any) -> None:
        manifest = _make_manifest(name="listed")
        fake_env("kairos-plugin-listed", manifest)
        with pytest.warns(SecurityWarning, match="kairos-plugin-listed"):
            discover_plugins(allow_all=True)

    def test_plugin_list_does_not_execute_code(
        self, fake_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kairos.plugins.registry as _reg
        from kairos.plugins.registry import _list_plugin_entries

        manifest = _make_manifest()
        fake_env("kairos-plugin-noexec", manifest)

        # Capture the fake EPs so we can spy on their load() call counts.
        eps_before = _reg.entry_points(group="kairos.plugins")

        # _list_plugin_entries reads metadata only — no load() must be invoked.
        entries = _list_plugin_entries()
        for entry in entries:
            assert isinstance(entry, dict)
            assert "dist_name" in entry
            assert "version" in entry

        # SEV-001 / L5: assert that .load() was never called on any registered EP.
        for ep in eps_before:
            assert ep.load_call_count == 0, (
                f"_list_plugin_entries() invoked ep.load() for {ep.name!r} — "
                "metadata-only listing must never execute plugin code."
            )

    def test_containment_breach_load_never_invoked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """SEV-001: ep.load() must NEVER be called when containment check fails."""
        import kairos.plugins.registry as _reg

        root = tmp_path / "legit_spy"
        root.mkdir()
        outside = tmp_path / "outside_spy" / "plugin.py"
        outside.parent.mkdir()
        outside.touch()
        manifest = _make_manifest(name="spytest")
        dist = _FakeDist(name="kairos-plugin-spy-breach", version="0.1.0", _root=root)

        mod_name = "fake_spy_breach"
        fake_mod = _types.ModuleType(mod_name)
        # __file__ points OUTSIDE the dist root — containment will fail.
        fake_mod.__file__ = str(outside)
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        ep = _FakeEP(
            name="default",
            value=f"{mod_name}:MANIFEST",
            dist=dist,
            manifest=manifest,
        )

        monkeypatch.setattr(
            _reg,
            "entry_points",
            lambda group=None, **_kw: [ep] if group == "kairos.plugins" else [],
        )
        monkeypatch.setattr(_reg, "distributions", lambda: iter([dist]))

        with pytest.raises(SecurityError):
            load_plugin("kairos-plugin-spy-breach")

        # The critical assertion: load() was never invoked.
        assert ep.load_call_count == 0, (
            "ep.load() was called despite containment check failure — "
            "third-party code must not execute before containment is verified."
        )

    def test_audit_log_line_has_no_control_chars(
        self, fake_env: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """SEV-003: audit log lines must not contain control characters."""
        import re

        manifest = _make_manifest()
        fake_env("kairos-plugin-logclean", manifest)
        with caplog.at_level("INFO", logger="kairos.plugins.registry"):
            load_plugin("kairos-plugin-logclean")
        for record in caplog.records:
            if "Loading plugin" in record.message:
                assert not re.search(r"[\x00-\x1f\x7f]", record.message), (
                    f"Control char found in audit log: {record.message!r}"
                )


# ---------------------------------------------------------------------------
# TestAdversarialSpecifiers  (SEV-002)
# ---------------------------------------------------------------------------


class TestAdversarialSpecifiers:
    """SEV-002: adversarial requires_kairos values must raise ConfigError, never ValueError."""

    def test_huge_version_segment_raises_config_error(self) -> None:
        """'>=9' * 5000 must be rejected before int conversion."""
        with pytest.raises(ConfigError):
            _parse_specifier(">=" + "9" * 5000)

    def test_manifest_with_huge_specifier_raises_config_error(self) -> None:
        """PluginManifest constructor must also reject overlong specifiers."""
        with pytest.raises(ConfigError):
            PluginManifest(
                name="demo",
                version="0.1.0",
                description="",
                requires_kairos=">=" + "9" * 5000,
                steps={},
                validators={},
            )

    def test_too_many_comma_clauses_raises_config_error(self) -> None:
        """Comma-list longer than _MAX_SPECIFIER_CLAUSES must be rejected."""
        huge_spec = ",".join(">=0.1" for _ in range(25))
        with pytest.raises(ConfigError, match="Too many"):
            _parse_specifier(huge_spec)

    def test_empty_segment_rejected(self) -> None:
        """Version string with empty segment (double dot) must be rejected."""
        with pytest.raises(ConfigError):
            _parse_specifier(">=1..2")

    def test_nine_digit_segment_rejected(self) -> None:
        """Single segment with 9 digits exceeds the 8-digit cap."""
        with pytest.raises(ConfigError):
            _parse_specifier(">=123456789")

    def test_eight_digit_segment_accepted(self) -> None:
        """Exactly 8 digits is within the cap and must be accepted."""
        result = _parse_specifier(">=12345678")
        assert len(result) == 1

    def test_bare_value_error_never_escapes(self) -> None:
        """No adversarial specifier must escape as a bare ValueError."""
        adversarial = [
            ">=" + "9" * 5000,
            "," * 30 + ">=1",
            ">=1..0",
        ]
        for spec in adversarial:
            try:
                _parse_specifier(spec)
            except ConfigError:
                pass  # expected
            except ValueError:
                pytest.fail(f"Bare ValueError escaped for specifier {spec[:60]!r}")


# ---------------------------------------------------------------------------
# TestSEV003MetadataSanitization
# ---------------------------------------------------------------------------


class TestSEV003MetadataSanitization:
    """SEV-003: version/description validation and metadata sanitization."""

    def test_version_with_newline_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="invalid characters"):
            PluginManifest(
                name="demo",
                version="1.0\n[CRITICAL] injected",
                description="",
                requires_kairos=">=0.4",
                steps={},
                validators={},
            )

    def test_version_with_ansi_escape_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="invalid characters"):
            PluginManifest(
                name="demo",
                version="1.0\x1b[31mred",
                description="",
                requires_kairos=">=0.4",
                steps={},
                validators={},
            )

    def test_version_too_long_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="invalid characters"):
            PluginManifest(
                name="demo",
                version="0." * 40,  # 80 chars — exceeds 64-char cap
                description="",
                requires_kairos=">=0.4",
                steps={},
                validators={},
            )

    def test_description_control_chars_stripped(self) -> None:
        """Control chars in description are stripped (not rejected) at construction."""
        m = PluginManifest(
            name="demo",
            version="0.1.0",
            description="good\x1b[31m injection\x00attempt",
            requires_kairos=">=0.4",
            steps={},
            validators={},
        )
        import re

        assert not re.search(r"[\x00-\x1f\x7f]", m.description)
        assert "good" in m.description

    def test_description_capped_at_500_chars(self) -> None:
        long_desc = "A" * 600
        m = PluginManifest(
            name="demo",
            version="0.1.0",
            description=long_desc,
            requires_kairos=">=0.4",
            steps={},
            validators={},
        )
        assert len(m.description) <= 500

    def test_cli_list_strips_control_chars_from_dist_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SEV-003: CLI list must not echo control characters from dist metadata."""
        import re

        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        malicious_version = "1.0\n[INJECTED] fake log line"
        dist = _FakeDist(name="evil-dist", version=malicious_version, _root=Path("."))
        ep = _FakeEP(name="default", dist=dist)

        monkeypatch.setattr(
            "kairos.plugins.registry.entry_points",
            lambda group=None, **_kw: [ep] if group == "kairos.plugins" else [],
        )

        runner = CliRunner()
        result = runner.invoke(app, ["plugin", "list"])
        assert result.exit_code == 0
        # Split on legitimate line endings first; each line must be free of control chars.
        for line in result.output.splitlines():
            assert not re.search(r"[\x00-\x1f\x7f]", line), (
                f"Control characters found in CLI plugin list output line: {line!r}"
            )

    def test_allow_all_warning_sanitizes_metadata_control_chars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SEV-003: control chars in dist metadata must not appear in SecurityWarning."""
        import re
        import warnings

        malicious_version = "1.0\n[CRITICAL] injected"
        malicious_name = "evil\x1b[31mred"
        dist = _FakeDist(name=malicious_name, version=malicious_version, _root=Path("."))
        ep = _FakeEP(name="default", dist=dist)

        monkeypatch.setattr(
            "kairos.plugins.registry.entry_points",
            lambda group=None, **_kw: [ep] if group == "kairos.plugins" else [],
        )
        monkeypatch.setattr("kairos.plugins.registry.distributions", lambda: iter([]))

        import contextlib

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with contextlib.suppress(Exception):
                discover_plugins(allow_all=True)

        sec_warnings = [w for w in caught if issubclass(w.category, SecurityWarning)]
        assert sec_warnings, "Expected a SecurityWarning to be emitted"
        for w in sec_warnings:
            msg = str(w.message)
            assert not re.search(r"[\x00-\x1f\x7f]", msg), (
                f"Control chars found in SecurityWarning: {msg!r}"
            )


# ---------------------------------------------------------------------------
# TestSerialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_describe_json_safe(self) -> None:
        import json

        manifest = _make_manifest()
        d = manifest.describe()
        # Should not raise
        json.dumps(d)

    def test_describe_round_trip(self) -> None:
        import json

        manifest = _make_manifest()
        d = manifest.describe()
        assert d == json.loads(json.dumps(d))

    def test_manifest_not_json_serializable(self) -> None:
        import json

        manifest = _make_manifest()
        with pytest.raises(TypeError):
            json.dumps(manifest)  # type: ignore[arg-type]

    def test_describe_contains_step_names(self) -> None:
        schema = Schema({"result": str})
        action = _make_action()
        spec = StepPluginSpec(
            action=action, input_contract=None, output_contract=schema, description="runs stuff"
        )
        manifest = _make_manifest(steps={"do_thing": spec})
        d = manifest.describe()
        steps_d = d["steps"]
        assert isinstance(steps_d, dict)
        assert "do_thing" in steps_d
        step_info = steps_d["do_thing"]
        assert isinstance(step_info, dict)
        assert step_info["description"] == "runs stuff"
        assert step_info["output_contract"] == ["result"]

    def test_describe_validator_names(self) -> None:
        def my_validator(v: object) -> bool:
            return True

        manifest = PluginManifest(
            name="demo",
            version="0.1.0",
            description="",
            requires_kairos=">=0.4",
            steps={},
            validators={"my_validator": my_validator},
        )
        d = manifest.describe()
        assert d["validators"] == ["my_validator"]


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_plugin_list_shows_name_version(self, fake_env: Any) -> None:
        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        manifest = _make_manifest()
        fake_env("kairos-plugin-cli", manifest)
        runner = CliRunner()
        result = runner.invoke(app, ["plugin", "list"])
        assert result.exit_code == 0
        assert "kairos-plugin-cli" in result.output

    def test_plugin_list_no_load_called(
        self, fake_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        manifest = _make_manifest()
        fake_env("kairos-plugin-spy", manifest)

        load_called = []

        def fake_load(name: str) -> PluginManifest:
            load_called.append(name)
            return manifest

        monkeypatch.setattr("kairos.plugins.registry.load_plugin", fake_load)
        runner = CliRunner()
        result = runner.invoke(app, ["plugin", "list"])
        assert result.exit_code == 0
        assert load_called == [], "plugin list without --describe must not call load_plugin"

    def test_plugin_list_describe_loads_manifests(self, fake_env: Any) -> None:
        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        manifest = _make_manifest(name="described")
        fake_env("kairos-plugin-desc", manifest)
        runner = CliRunner()
        result = runner.invoke(app, ["plugin", "list", "--describe"])
        # Should emit the code-execution note (written to stderr via typer.echo err=True,
        # which CliRunner merges into output by default)
        assert "executes plugin code" in result.output

    def test_plugin_list_empty(self, fake_env: Any) -> None:
        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["plugin", "list"])
        assert result.exit_code == 0
        assert "No kairos.plugins" in result.output


# ---------------------------------------------------------------------------
# TestSEV003RStepSpecSanitization
# ---------------------------------------------------------------------------


class TestSEV003RStepSpecSanitization:
    """SEV-003-R: StepPluginSpec.description must be sanitized at construction."""

    def test_step_spec_description_control_chars_stripped(self) -> None:
        """ANSI escape + newline in StepPluginSpec.description → stored sanitized."""
        import re

        spec = StepPluginSpec(
            action=_make_action(),
            input_contract=None,
            output_contract=None,
            description="innocent\x1b[2Jcleared\nfake\x00payload",
        )
        assert not re.search(r"[\x00-\x1f\x7f]", spec.description), (
            f"Control chars survived StepPluginSpec construction: {spec.description!r}"
        )
        assert "innocent" in spec.description
        assert "cleared" in spec.description

    def test_step_spec_description_capped_at_500(self) -> None:
        """Descriptions longer than 500 chars are truncated after stripping."""
        spec = StepPluginSpec(
            action=_make_action(),
            input_contract=None,
            output_contract=None,
            description="X" * 600,
        )
        assert len(spec.description) <= 500

    def test_cli_describe_no_control_chars_in_step_description(self, fake_env: Any) -> None:
        """CLI plugin list --describe output contains no control chars from step descriptions."""
        import re

        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        # StepPluginSpec sanitizes at construction; verify end-to-end via CLI.
        hostile_spec = StepPluginSpec(
            action=_make_action(),
            input_contract=None,
            output_contract=None,
            description="legit\x1b[2Jcleared\n[INJECTED]",
        )
        manifest = _make_manifest(steps={"hostile_step": hostile_spec})
        fake_env("kairos-plugin-hostile-desc", manifest)

        runner = CliRunner()
        result = runner.invoke(app, ["plugin", "list", "--describe"])
        assert result.exit_code == 0
        for line in result.output.splitlines():
            assert not re.search(r"[\x00-\x1f\x7f]", line), (
                f"Control char in CLI --describe output line: {line!r}"
            )

    def test_validator_plugin_description_not_surfaced(self) -> None:
        """validator_plugin discards description — no surface, no sanitization needed."""

        # The description param to @validator_plugin is accepted but never stored;
        # PluginManifest.describe() shows only validator *names*.  This test documents
        # that no additional sanitization is required for validator descriptions.
        @validator_plugin(name="chk", description="evil\x1b[2J")
        def chk(v: object) -> bool:
            return True

        manifest = PluginManifest(
            name="demo",
            version="0.1.0",
            description="",
            requires_kairos=">=0.4",
            steps={},
            validators={"chk": chk},
        )
        d = manifest.describe()
        assert d["validators"] == ["chk"]
        # description never appears in describe() output
        assert "evil" not in str(d)


# ---------------------------------------------------------------------------
# TestSEV004DottedMissingParent
# ---------------------------------------------------------------------------


class TestSEV004DottedMissingParent:
    """SEV-004: ModuleNotFoundError from find_spec must escape as SecurityError."""

    def test_dotted_missing_parent_raises_security_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Dotted ep.value with missing parent → SecurityError, not ModuleNotFoundError."""
        import importlib.util as _iutil

        root = tmp_path / "dotted_pkg"
        root.mkdir()
        dist = _FakeDist(
            name="kairos-plugin-dotted",
            version="0.1.0",
            _root=root,
            files=["plugin.py"],
        )
        manifest = _make_manifest()

        ep = _FakeEP(
            name="default",
            value="missing_parent_xyz.sub.deep:MANIFEST",
            dist=dist,
            manifest=manifest,
        )

        monkeypatch.setattr(
            "kairos.plugins.registry.entry_points",
            lambda group=None, **_kw: [ep] if group == "kairos.plugins" else [],
        )
        monkeypatch.setattr(
            "kairos.plugins.registry.distributions",
            lambda: iter([dist]),
        )

        # Ensure the module is NOT in sys.modules so find_spec is invoked.
        monkeypatch.delitem(sys.modules, "missing_parent_xyz.sub.deep", raising=False)
        monkeypatch.delitem(sys.modules, "missing_parent_xyz", raising=False)

        # Patch find_spec to simulate the dotted-parent-missing condition reliably.
        _original_find_spec = _iutil.find_spec

        def _raising_find_spec(name: str, package: Any = None) -> Any:
            if "missing_parent_xyz" in name:
                raise ModuleNotFoundError(f"No module named {name!r}")
            return _original_find_spec(name, package)

        monkeypatch.setattr(_iutil, "find_spec", _raising_find_spec)

        with pytest.raises(SecurityError):
            load_plugin("kairos-plugin-dotted")

        # ep.load() must never be invoked when containment cannot be verified.
        assert ep.load_call_count == 0

    def test_dotted_missing_parent_not_module_not_found_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verify the exception is SecurityError, not ModuleNotFoundError (regression guard)."""
        import importlib.util as _iutil

        root = tmp_path / "dotted_pkg2"
        root.mkdir()
        dist = _FakeDist(
            name="kairos-plugin-dotted2",
            version="0.1.0",
            _root=root,
            files=["plugin.py"],
        )
        ep = _FakeEP(
            name="default",
            value="missing_parent_zzz.leaf:MANIFEST",
            dist=dist,
        )
        monkeypatch.setattr(
            "kairos.plugins.registry.entry_points",
            lambda group=None, **_kw: [ep] if group == "kairos.plugins" else [],
        )
        monkeypatch.setattr(
            "kairos.plugins.registry.distributions",
            lambda: iter([dist]),
        )
        monkeypatch.delitem(sys.modules, "missing_parent_zzz.leaf", raising=False)
        monkeypatch.delitem(sys.modules, "missing_parent_zzz", raising=False)

        _orig = _iutil.find_spec

        def _raise(name: str, package: Any = None) -> Any:
            if "missing_parent_zzz" in name:
                raise ModuleNotFoundError(f"No module named {name!r}")
            return _orig(name, package)

        monkeypatch.setattr(_iutil, "find_spec", _raise)

        raised: BaseException | None = None
        try:
            load_plugin("kairos-plugin-dotted2")
        except SecurityError as exc:
            raised = exc
        except ModuleNotFoundError:
            pytest.fail("Bare ModuleNotFoundError escaped load_plugin — SEV-004 regression")

        assert raised is not None, "Expected SecurityError, got no exception"


# ---------------------------------------------------------------------------
# TestSEV005ContainmentRecord
# ---------------------------------------------------------------------------


class TestSEV005ContainmentRecord:
    """SEV-005: containment boundary is the distribution RECORD, not site-packages prefix."""

    def test_file_in_distribution_record_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Target in dist.files → _assert_within_distribution does not raise."""
        root = tmp_path / "goodpkg"
        root.mkdir()
        module_file = root / "plugin.py"
        module_file.touch()

        dist = _FakeDist(name="good-pkg", version="0.1.0", _root=root, files=["plugin.py"])
        mod_name = "goodpkg_sev005_pos"
        fake_mod = _types.ModuleType(mod_name)
        fake_mod.__file__ = str(module_file)
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        ep = _FakeEP(name="default", value=f"{mod_name}:MANIFEST", dist=dist)

        # Must not raise — file is in the RECORD.
        _assert_within_distribution(ep, "good-pkg")

    def test_file_not_in_distribution_record_raises_security_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Target under same root but absent from dist.files → SecurityError (victim_pkg)."""
        root = tmp_path / "victim"
        root.mkdir()
        # module_file is physically INSIDE root but not listed in files.
        module_file = root / "plugin.py"
        module_file.touch()

        dist = _FakeDist(
            name="victim-pkg",
            version="0.1.0",
            _root=root,
            files=["other_module.py"],  # plugin.py is NOT listed
        )
        mod_name = "victim_sev005_neg"
        fake_mod = _types.ModuleType(mod_name)
        fake_mod.__file__ = str(module_file)
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        ep = _FakeEP(name="default", value=f"{mod_name}:MANIFEST", dist=dist)

        with pytest.raises(SecurityError, match="RECORD"):
            _assert_within_distribution(ep, "victim-pkg")

    def test_no_record_raises_security_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """dist.files is None → SecurityError (fail closed, no RECORD to verify)."""
        root = tmp_path / "norecord"
        root.mkdir()
        module_file = root / "plugin.py"
        module_file.touch()

        dist = _FakeDist(name="norecord-pkg", version="0.1.0", _root=root, files=None)
        mod_name = "norecord_sev005"
        fake_mod = _types.ModuleType(mod_name)
        fake_mod.__file__ = str(module_file)
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        ep = _FakeEP(name="default", value=f"{mod_name}:MANIFEST", dist=dist)

        with pytest.raises(SecurityError, match="RECORD"):
            _assert_within_distribution(ep, "norecord-pkg")


# ---------------------------------------------------------------------------
# TestContainmentFailClosed  (SEV-001 fail-closed guards + real find_spec path)
# ---------------------------------------------------------------------------


class TestContainmentFailClosed:
    """Fail-closed containment guards and the genuine find_spec resolution path.

    The rest of the suite injects modules into ``sys.modules`` so containment
    reads ``mod.__file__`` directly.  These tests exercise the branches that the
    injection strategy skips: the two fail-closed ``SecurityError`` guards and
    the real ``importlib.util.find_spec`` path that underpins the SEV-001
    "containment-before-load" guarantee.
    """

    def test_unknown_module_file_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cached module with no __file__ → SecurityError (raw_file is None guard)."""
        root = tmp_path / "nofile"
        root.mkdir()
        dist = _FakeDist(name="nofile-pkg", version="0.1.0", _root=root, files=["plugin.py"])

        mod_name = "nofile_containment_mod"
        fake_mod = _types.ModuleType(mod_name)  # ModuleType has no __file__ attribute
        assert getattr(fake_mod, "__file__", None) is None
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        ep = _FakeEP(name="default", value=f"{mod_name}:MANIFEST", dist=dist)

        with pytest.raises(SecurityError, match="__file__ is unknown"):
            _assert_within_distribution(ep, "nofile-pkg")

    def test_missing_distribution_reference_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Entry point with dist=None → SecurityError (no distribution reference guard)."""
        root = tmp_path / "nodist"
        root.mkdir()
        module_file = root / "plugin.py"
        module_file.touch()

        mod_name = "nodist_containment_mod"
        fake_mod = _types.ModuleType(mod_name)
        fake_mod.__file__ = str(module_file)
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

        # dist is None — the entry point cannot be tied to a distribution.
        ep = _FakeEP(name="default", value=f"{mod_name}:MANIFEST", dist=None)

        with pytest.raises(SecurityError, match="no distribution reference"):
            _assert_within_distribution(ep, "nodist-pkg")

    def test_find_spec_resolution_does_not_execute_module(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Real find_spec path: uncached, contained module resolves without executing.

        SEV-001: containment is verified via importlib.util.find_spec, which
        locates the module file WITHOUT running its top-level code.  The module
        writes a sentinel on execution; the sentinel must never appear.
        """
        import importlib

        root = tmp_path / "realpkg"
        root.mkdir()
        mod_name = "kairos_findspec_probe_mod"
        sentinel = tmp_path / "SENTINEL_EXECUTED"
        module_file = root / f"{mod_name}.py"
        module_file.write_text(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed')\n"
            "MANIFEST = None\n"
        )

        # Make the module importable but NOT yet imported, forcing the find_spec branch.
        monkeypatch.syspath_prepend(str(root))
        importlib.invalidate_caches()
        monkeypatch.delitem(sys.modules, mod_name, raising=False)
        assert mod_name not in sys.modules

        dist = _FakeDist(name="real-pkg", version="0.1.0", _root=root, files=[f"{mod_name}.py"])
        ep = _FakeEP(name="default", value=f"{mod_name}:MANIFEST", dist=dist)

        # Contained + in RECORD → must not raise.
        _assert_within_distribution(ep, "real-pkg")

        # The core SEV-001 guarantee: find_spec located the module without running it.
        assert not sentinel.exists(), (
            "Module top-level code executed during containment check — "
            "find_spec must locate without importing the target module."
        )
        assert mod_name not in sys.modules, (
            "find_spec left the target module in sys.modules — it must not import it."
        )
