# kairos-ai-evidence

Evidence Engine plugin for the Kairos SDK. Provides contract-validated evidence evaluation
with deterministic verdict and confidence derivation — no model-emitted scores.

## Installation

```bash
pip install "kairos-ai>=0.5,<0.6"
pip install kairos-ai-evidence
```

## Quick start

```python
from kairos_ai_evidence import (
    EVIDENCE_PACKET,
    make_packet,
    make_source_record,
    make_claim_record,
    derive_support_level,
    derive_verdict,
    derive_overall_verdict,
    derive_confidence,
)
```

## Packet version compatibility

| `kairos-ai-evidence` | `packet_version` | `kairos-ai` |
|--------------------------|------------------|-------------|
| 0.1.x                    | 1.0              | >=0.5,<0.6  |

## License

Apache 2.0 — Copyright 2026 Vanxa
