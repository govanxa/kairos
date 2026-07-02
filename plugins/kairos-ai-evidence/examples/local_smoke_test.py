"""Smoke test — can Kairos reach your local model?  (No Evidence Engine yet.)

Sends one plain question to a local OpenAI-compatible server (LM Studio, Ollama,
llama.cpp, ...) through Kairos's OpenAIAdapter and prints the reply. If text comes
back, the plumbing works and you can run the other local_* demos.

Configure via environment variables (or edit the constants below)::

    # PowerShell
    $env:KAIROS_DEMO_MODEL = "qwen2.5-7b-instruct"      # exact id from GET /v1/models
    $env:KAIROS_DEMO_BASE_URL = "http://localhost:1234/v1"   # LM Studio default
    python examples/local_smoke_test.py

Endpoints: LM Studio 1234, Ollama 11434, llama.cpp 8080. Local models ignore auth,
but the adapter requires OPENAI_API_KEY to exist (it reads credentials from the
environment only), so a throwaway value is set below.
"""

import os

# Adapter reads the key from the environment only (never inline). Local servers ignore
# it, but the variable must exist. `setdefault` won't clobber a real key if one is set.
os.environ.setdefault("OPENAI_API_KEY", "local-not-needed")

from kairos.adapters.openai_adapter import OpenAIAdapter

MODEL = os.environ.get("KAIROS_DEMO_MODEL", "")
BASE_URL = os.environ.get("KAIROS_DEMO_BASE_URL", "http://localhost:1234/v1")

if not MODEL:
    raise SystemExit(
        "Set KAIROS_DEMO_MODEL to your local model id.\n"
        f"  List available ids with:  curl {BASE_URL}/models\n"
        '  Then e.g. (PowerShell):    $env:KAIROS_DEMO_MODEL = "qwen2.5-7b-instruct"'
    )

adapter = OpenAIAdapter(model=MODEL, base_url=BASE_URL, allow_localhost=True)

question = "In one sentence, what is your knowledge cutoff date?"
print(f"MODEL: {MODEL}  @  {BASE_URL}")
print(f"QUESTION: {question}")
print("-" * 70)

response = adapter.call(question)  # .call(prompt) sends it as a user message
print("MODEL SAYS:")
print(response.text)  # ModelResponse.text is the model's text output
print("-" * 70)
print(f"(latency={response.latency_ms:.0f}ms  tokens={response.usage.total_tokens})")
