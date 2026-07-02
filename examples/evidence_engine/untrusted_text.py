"""Evidence Engine untrusted-text primitives — promoted to kairos.security (B1).

Thin re-export shim. The implementation now lives in kairos.security; this
module preserves the A1 spike import surface so the spike suite is unchanged.
"""

from __future__ import annotations

from kairos.security import (
    FLAG_IMPERATIVE,
    FLAG_ROLE_MARKER,
    FLAG_TEMPLATE_TOKEN,
    FLAG_TOOL_CALL,
    SanitizedText,
    is_predominantly_instructional,
    neutralize,
    normalize,
    sanitize_untrusted_text,
    scrub_credentials,
)
from kairos.security import (
    INJECTION_FLAGS as _ALL_FLAGS,
)

__all__ = [
    "_ALL_FLAGS",
    "FLAG_IMPERATIVE",
    "FLAG_ROLE_MARKER",
    "FLAG_TEMPLATE_TOKEN",
    "FLAG_TOOL_CALL",
    "SanitizedText",
    "is_predominantly_instructional",
    "neutralize",
    "normalize",
    "sanitize_untrusted_text",
    "scrub_credentials",
]
