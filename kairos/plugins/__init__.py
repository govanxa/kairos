"""Kairos plugin system — public surface.

Public names
------------
PluginManifest, StepPluginSpec    — manifest model and step spec dataclasses.
build_manifest                    — assemble a manifest from decorated callables.
step_plugin, validator_plugin     — decorators for plugin authors.
load_plugin                       — load a single named distribution.
discover_plugins                  — discover and load from an allowlist.
SecurityWarning                   — emitted by discover_plugins(allow_all=True).

Module-level utility (not a Step attribute)
-------------------------------------------
qualified_step_name               — returns ``"<plugin>.<step>"`` as a logging /
                                    registry display label only.  The dot is
                                    intentionally disallowed in Step.name; this
                                    function is a module-level helper and is not
                                    asymmetrically placed on any class.
"""

from __future__ import annotations

from kairos.plugins.registry import (
    PluginManifest,
    SecurityWarning,
    StepPluginSpec,
    build_manifest,
    discover_plugins,
    load_plugin,
    qualified_step_name,
    step_plugin,
    validator_plugin,
)

__all__ = [
    "PluginManifest",
    "SecurityWarning",
    "StepPluginSpec",
    "build_manifest",
    "discover_plugins",
    "load_plugin",
    "qualified_step_name",
    "step_plugin",
    "validator_plugin",
]
