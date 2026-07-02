"""Verified A/B demo — the SAME local model, with and without the Evidence Engine.

Two independent sources AGREE on a post-cutoff fact. The pipeline marks it `verified`
and builds a `working_context` block. Then the same question is asked twice:
    BARE     = question only             (the model flails — no current data)
    GROUNDED = working_context + question (the model answers, from the evidence)

The pipeline makes ZERO model calls; it is deterministic. Only the two calls at the
end hit the model.

Run (after installing the plugin — see the plugin README)::

    $env:KAIROS_DEMO_MODEL = "qwen2.5-7b-instruct"    # your local model id
    python examples/local_verified_demo.py
"""

import os
from datetime import UTC, datetime

os.environ.setdefault("OPENAI_API_KEY", "local-not-needed")

from kairos.adapters.openai_adapter import OpenAIAdapter

from kairos_ai_evidence import build_reference_workflow

MODEL = os.environ.get("KAIROS_DEMO_MODEL", "")
BASE_URL = os.environ.get("KAIROS_DEMO_BASE_URL", "http://localhost:1234/v1")
if not MODEL:
    raise SystemExit(
        f"Set KAIROS_DEMO_MODEL to your local model id (list them with: curl {BASE_URL}/models)."
    )

adapter = OpenAIAdapter(model=MODEL, base_url=BASE_URL, allow_localhost=True)

# Two DIFFERENT domains that agree — corroboration needs distinct registrable domains
# (two pages of the same site count as one source).
fetched_at = datetime.now(tz=UTC).isoformat()
raw_documents = [
    {
        "url": "https://energydata.org/renewable-capacity-2026h1",
        "title": "Renewable Capacity Report H1 2026",
        "content": "A total of 420 gigawatts of renewable energy capacity was added "
        "globally in the first half of 2026, a new record.",
        "fetched_at": fetched_at,
        "published_at": "2026-06-30",
    },
    {
        "url": "https://statsreview.org/energy-2026",
        "title": "Energy Statistics 2026",
        "content": "A total of 420 gigawatts of new renewable capacity was added globally "
        "in the first half of 2026.",
        "fetched_at": fetched_at,
        "published_at": "2026-06-29",
    },
]
CLAIM = "420 gigawatts of renewable capacity was added globally in the first half of 2026."
QUERY = "How much renewable capacity was added globally in the first half of 2026?"

wf = build_reference_workflow()
result = wf.run(
    {
        "raw_documents": raw_documents,
        "claims": [CLAIM],
        "query": QUERY,
        "as_of": datetime.now(tz=UTC).date().isoformat(),  # machine-stamped "today"
    }
)

packet = result.final_state["evidence_packet"]
working_context = result.final_state["working_context_bundle"]["working_context"]

print("=" * 72)
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
