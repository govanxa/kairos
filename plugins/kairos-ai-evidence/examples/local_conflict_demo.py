"""Conflict A/B demo — the "honest uncertainty" path.

Two independent sources DISAGREE on the same value (4.25% vs 4.50%). The pipeline
can't tell which is right, so it marks the claim `conflicting` / `[DISPUTED]` and
must NOT pick a side. Watch a grounded model present the disagreement instead of
grabbing the tidy answer sitting in front of it.

Run (after installing the plugin)::

    $env:KAIROS_DEMO_MODEL = "qwen2.5-7b-instruct"
    python examples/local_conflict_demo.py
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

# Two independent domains reporting a DIFFERENT value for the same thing.
fetched_at = datetime.now(tz=UTC).isoformat()
raw_documents = [
    {
        "url": "https://centralbank.example.org/rate-decision",
        "title": "Central Bank Rate Decision",
        "content": "Following the July 2026 meeting, the base interest rate is 4.25%. "
        "The rate of 4.25% takes effect immediately.",
        "fetched_at": fetched_at,
        "published_at": "2026-07-01",
    },
    {
        "url": "https://financialpress.example.com/rate-coverage",
        "title": "Rate Decision Coverage",
        "content": "The base interest rate is 4.50% following the July 2026 decision. "
        "The new rate of 4.50% was announced after the vote.",
        "fetched_at": fetched_at,
        "published_at": "2026-07-01",
    },
]
CLAIM = "The base interest rate is 4.25%."
QUERY = "What is the current base interest rate following the July 2026 decision?"

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
bundle = result.final_state["working_context_bundle"]
working_context = bundle["working_context"]

print("=" * 72)
print(f"PIPELINE VERDICT: {packet['overall_verdict']}  (confidence: {packet['confidence']})")
print(f"UNRESOLVED CONFLICTS: {bundle.get('unresolved_conflicts')}")
print("=" * 72)
print(working_context)
print("=" * 72)

print("\nBARE (question only):")
print("-" * 72)
print(adapter.call(QUERY).text)

print("\nGROUNDED (working_context + question):")
print("-" * 72)
print(adapter.call(f"{working_context}\n\nQUESTION: {QUERY}").text)
