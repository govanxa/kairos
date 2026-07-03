# kairos-ai-evidence

Evidence Engine plugin for the Kairos SDK. Provides contract-validated evidence evaluation
with deterministic verdict and confidence derivation — no model-emitted scores.

## Installation

```bash
pip install kairos-ai-evidence
```

This pulls in the core SDK automatically — the plugin declares `kairos-ai>=0.5,<0.6` as a
dependency.

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

## MCP server

Run the Evidence Engine as an MCP server so a calling model must pass retrieved
content through the trust boundary before it can answer. Install the optional
`mcp` extra:

```bash
pip install "kairos-ai-evidence[mcp]"
# or, for an ephemeral run:
uvx --from "kairos-ai-evidence[mcp]" kairos-evidence-mcp
pipx install "kairos-ai-evidence[mcp]"
```

Then run the console script (stdio transport, matching the standard local MCP
pattern):

```bash
kairos-evidence-mcp
```

The server exposes two tools:

- **`evaluate_evidence(documents, claims, query, as_of=None)`** — retrieval-agnostic.
  Works on a bare install with no retriever configured. You supply the documents
  (any shape with a `url` and body text); they are sanitized by the content gate
  before any claim is evaluated.
- **`verified_answer(query, claims=None, max_results=None)`** — the stronger
  firewall. Retrieval happens **server-side, behind the gate**, so the calling
  model cannot substitute its own (possibly stale or hostile) documents for
  evidence — it must consume the gated response. `as_of` is machine-stamped on
  every call; it is never accepted from the wire. Requires a retriever to be
  configured (see below) — without one, this tool returns a structured
  `RetrieverNotConfigured` error and directs the caller to `evaluate_evidence`.

Neither tool runs an LLM inside the server. Both return the same deterministic
JSON evidence bundle (`working_context`, `citations`, `overall_verdict`,
`confidence`, per-claim verdicts, source counts, etc.) — the calling model
composes the natural-language answer from it.

### Configuring a retriever

`verified_answer` needs a retriever: a plain Python callable
`(query: str, *, max_results: int) -> RetrieverResult`. Configuration is
**programmatic only** — there is no environment-variable or import-by-string
retriever resolution, which would be a code-execution vector. Write a small
launcher script that imports your own search function and passes it in:

```python
# my_launcher.py
from kairos_ai_evidence.mcp.server import create_server

from my_project.search import web_search  # your own retrieval function

if __name__ == "__main__":
    create_server(retriever=web_search).run()
```

```bash
python my_launcher.py
```

See `examples/mcp_server_launcher.py` for a runnable version with an offline
stub retriever (no network calls) — swap the stub for your own retriever
following the pattern shown there.

### Security notes

- **stdio transport only** — no HTTP/SSE, no listening socket.
- Logs go to **stderr only**, structural metadata only (counts, verdict,
  confidence, timing) — never query text or document content.
- Every error crossing the wire is either a fixed structural message or has
  passed through `sanitize_exception` — no raw content, credentials, stack
  traces, or file paths ever reach the caller.
- `trust_policy` and other pipeline configuration are `create_server(...)`
  keyword arguments only — never tool arguments on the wire.
- The MCP response is built only from gated/derived pipeline output; raw
  retriever or document text never reaches the response un-gated.

## Packet version compatibility

| `kairos-ai-evidence` | `packet_version` | `kairos-ai` |
|--------------------------|------------------|-------------|
| 0.1.x                    | 1.0              | >=0.5,<0.6  |

## License

Apache 2.0 — Copyright 2026 Vanxa
