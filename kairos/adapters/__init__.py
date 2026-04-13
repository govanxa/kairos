"""Kairos adapters — convenience connectors for LLM providers.

Public exports:
- ModelAdapter: Protocol that all adapter classes must satisfy.
- ModelResponse: Normalized response from any LLM provider.
- TokenUsage: Token count and optional cost metadata.
"""

from __future__ import annotations

from kairos.adapters.base import ModelAdapter, ModelResponse, TokenUsage

__all__ = [
    "ModelAdapter",
    "ModelResponse",
    "TokenUsage",
]
