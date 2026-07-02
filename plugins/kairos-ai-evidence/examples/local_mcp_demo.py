"""MCP-route demo — feed REAL search results through the Evidence Engine.

The plugin is retrieval-agnostic: it consumes documents you already fetched. This
script shows the shape for a web-search MCP (or any search API). It ships with sample
results so it runs as-is; replace MCP_RESULTS / CLAIM / QUERY with your own.

How to get real results: run your MCP's `web_search` tool (e.g. via the MCP Inspector
or your MCP-enabled chat client), then paste the `results` array — or the whole
`web_search` object — into MCP_RESULTS below. The `ingest_mcp_documents` helper (inlined
here so this file is self-contained) tolerates either shape.

Run (after installing the plugin)::

    $env:KAIROS_DEMO_MODEL = "qwen2.5-7b-instruct"
    python examples/local_mcp_demo.py
"""

import os
from datetime import UTC, datetime

os.environ.setdefault("OPENAI_API_KEY", "local-not-needed")

from kairos.adapters.openai_adapter import OpenAIAdapter

from kairos_ai_evidence import build_reference_workflow


def ingest_mcp_documents(mcp_docs, fetched_at=None):
    """Map raw MCP output -> the pipeline's document shape.

    Handles both common wire shapes and tolerates however you paste them:
      - web_search item -> {title, url, snippet}   (snippet becomes content)
      - fetch_url item  -> {url, title, text}       (text becomes content)
      - a whole web_search object {query, answer, results:[...]}  (results dug out)
      - a list containing such objects              (flattened)
    """

    def _flatten(node, acc):
        if isinstance(node, list):
            for item in node:
                _flatten(item, acc)
        elif isinstance(node, dict):
            if isinstance(node.get("results"), list):
                _flatten(node["results"], acc)  # a web_search wrapper
            elif any(k in node for k in ("url", "snippet", "text", "content")):
                acc.append(node)  # an actual document
        return acc

    stamp = fetched_at or datetime.now(tz=UTC).isoformat()
    out = []
    for doc in _flatten(mcp_docs, []):
        content = doc.get("text") or doc.get("snippet") or doc.get("content") or ""
        item = {
            "url": doc.get("url", ""),
            "title": doc.get("title"),
            "content": content,
            "fetched_at": stamp,
        }
        if "published_at" in doc:
            item["published_at"] = doc["published_at"]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# >>> REPLACE with what your MCP returned. Sample data lets this run as-is. <<<
# ---------------------------------------------------------------------------
MCP_RESULTS = {
    "query": "global renewable capacity added first half 2026",
    "results": [
        {
            "title": "Renewable Capacity Report H1 2026",
            "url": "https://energydata.org/renewable-capacity-2026h1",
            "snippet": "A total of 420 gigawatts of renewable energy capacity was added "
            "globally in the first half of 2026, a new record.",
        },
        {
            "title": "Energy Statistics 2026",
            "url": "https://statsreview.org/energy-2026",
            "snippet": "A total of 420 gigawatts of new renewable capacity was added "
            "globally in the first half of 2026.",
        },
    ],
}
CLAIM = "420 gigawatts of renewable capacity was added globally in the first half of 2026."
QUERY = "How much renewable capacity was added globally in the first half of 2026?"

MODEL = os.environ.get("KAIROS_DEMO_MODEL", "")
BASE_URL = os.environ.get("KAIROS_DEMO_BASE_URL", "http://localhost:1234/v1")
if not MODEL:
    raise SystemExit(
        f"Set KAIROS_DEMO_MODEL to your local model id (list them with: curl {BASE_URL}/models)."
    )

adapter = OpenAIAdapter(model=MODEL, base_url=BASE_URL, allow_localhost=True)

raw_documents = ingest_mcp_documents(MCP_RESULTS)

wf = build_reference_workflow()
result = wf.run(
    {
        "raw_documents": raw_documents,
        "claims": [CLAIM],
        "query": QUERY,
        "as_of": datetime.now(tz=UTC).date().isoformat(),
    }
)

packet = result.final_state["evidence_packet"]
working_context = result.final_state["working_context_bundle"]["working_context"]

print("=" * 72)
print(f"DOCUMENTS ACCEPTED BY THE GATE: {len(packet['sources'])} (of {len(raw_documents)})")
rejected = result.final_state.get("rejected")
if rejected:
    print(f"REJECTED: {rejected}")
print(f"PIPELINE VERDICT: {packet['overall_verdict']}  (confidence: {packet['confidence']})")
print("=" * 72)
print(working_context)
print("=" * 72)

print("\nBARE (question only):")
print("-" * 72)
print(adapter.call(QUERY).text)

print("\nGROUNDED (working_context + question):")
print("-" * 72)
print(adapter.call(f"{working_context}\n\nQUESTION: {QUERY}").text)
