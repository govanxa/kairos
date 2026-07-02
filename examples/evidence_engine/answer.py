"""Evidence Engine answer step + model seam (→ C4 quickstart).

Provides:
- ScriptedModel: deterministic offline mock for CI and the G2 baseline.
- live_model_fn: wraps OpenAIAdapter for local LM Studio/Ollama (offline-capable).
- make_answer_step: factory producing the answer step action.

The seam is a single Callable[[str], str] — identical for scripted and live
paths, so CI, before/after comparison, and live LM Studio runs use the same
code path with a swapped model_fn.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# ScriptedModel — deterministic offline model (CI + G2 baseline)
# ---------------------------------------------------------------------------


class ScriptedModel:
    """Deterministic offline model for CI and the G2 before/after comparison.

    Maps prompt substrings to canned responses. Two modes:
    - 'grounded': heeds the working_context block (pipeline path, G2 after).
    - 'refusal': ignores the block, simulates cutoff fixation (G2 before / baseline).

    The mode distinction is implemented by the caller using separate
    ScriptedModel instances with different response dicts.

    Args:
        responses: Dict mapping substring key → response string.
            The first key found (in insertion order) in the prompt wins.
        mode: Descriptive label ('grounded' or 'refusal'). Unused in logic;
            included for test assertions.
    """

    def __init__(self, responses: dict[str, str], *, mode: str = "grounded") -> None:
        self._responses = responses
        self.mode = mode

    def __call__(self, prompt: str) -> str:
        """Return the first matching canned response or a generic fallback.

        Args:
            prompt: Full prompt string (may include working_context + question).

        Returns:
            Canned response string.
        """
        for key, response in self._responses.items():
            if key in prompt:
                return response
        return "I cannot determine the answer from the available information."


# ---------------------------------------------------------------------------
# live_model_fn — wraps OpenAIAdapter (allow_localhost=True for LM Studio)
# ---------------------------------------------------------------------------


def live_model_fn(
    *,
    base_url: str,
    model: str,
    allow_localhost: bool = True,
) -> Callable[[str], str]:
    """Return a callable that calls a local OpenAI-compatible model.

    Reads OPENAI_API_KEY from environment (adapters never accept inline keys —
    S14). For LM Studio a dummy key is acceptable (it ignores auth).

    Args:
        base_url: Base URL of the local model server (e.g. http://localhost:1234/v1).
        model: Model identifier string.
        allow_localhost: Passed to enforce_https; True permits http://localhost.

    Returns:
        Callable[[str], str] — takes a prompt, returns the model's text.

    Raises:
        ConfigError: If the adapter cannot be constructed.
        SecurityError: If base_url is not HTTPS and allow_localhost is False.
    """
    from kairos.adapters.openai_adapter import OpenAIAdapter  # noqa: PLC0415

    adapter = OpenAIAdapter(
        model=model,
        base_url=base_url,
        allow_localhost=allow_localhost,
    )

    def _call(prompt: str) -> str:
        response = adapter.call(prompt)
        return response.text

    return _call


# ---------------------------------------------------------------------------
# make_answer_step — factory
# ---------------------------------------------------------------------------


def make_answer_step(
    model_fn: Callable[[str], str],
    *,
    with_context: bool,
) -> Callable[[StepContext], dict[str, Any]]:
    """Return an answer step action closed over model_fn and with_context.

    with_context=True (pipeline path / G2 after):
        Reads 'working_context_bundle' and 'query'. Prompt = working_context
        + '\n\nQUESTION: ' + query. THE FIREWALL PATH.

    with_context=False (baseline path / G2 before):
        Reads ONLY 'query'. Prompt = query. Structurally CANNOT reach any
        web-derived state key (read_keys=['query'] in build_baseline).

    Args:
        model_fn: Callable[[str], str] — scripted or live.
        with_context: Whether to include the belief-revision working_context.

    Returns:
        A step action Callable[[StepContext], dict].
    """

    def answer_step(ctx: StepContext) -> dict[str, Any]:
        query_obj = ctx.state.get("query")
        query: str = str(query_obj) if query_obj is not None else ""

        if with_context:
            bundle_obj = ctx.state.get("working_context_bundle")
            bundle: dict[str, Any] = bundle_obj if isinstance(bundle_obj, dict) else {}
            working_context: str = bundle.get("working_context", "")
            prompt = f"{working_context}\n\nQUESTION: {query}"
        else:
            prompt = query

        ctx.increment_llm_calls()
        answer = model_fn(prompt)

        ctx.state.set("answer", answer)
        return {"answer": answer}

    return answer_step


# ---------------------------------------------------------------------------
# Default scripted response sets (used by harness and tests)
# ---------------------------------------------------------------------------

# Grounded responses: heeds the working_context block.
# Keys are substrings that appear in the working_context or query.
GROUNDED_RESPONSES: dict[str, str] = {
    "ratified": (
        "Yes, based on the verified evidence, the Global Climate Accord was "
        "ratified on June 28, 2026, with all 45 participating nations signing the document."
    ),
    "adopted": (
        "Yes, based on the verified evidence, the international technology framework "
        "was adopted at the June 2026 summit."
    ),
    "420": (
        "Based on the verified evidence, 420 gigawatts of renewable energy capacity "
        "was added globally in the first half of 2026."
    ),
    "policy review": (
        "Based on the verified evidence, the June 2026 policy review produced "
        "actionable recommendations for further action."
    ),
    "conflict": (
        "The sources conflict on this question. Some sources report the "
        "infrastructure bill passed while others report it failed — "
        "the evidence is conflicting and I cannot confirm either outcome."
    ),
    "infrastructure bill": (
        "The sources conflict on this question. Some sources report the "
        "infrastructure bill passed while others report it failed — "
        "the evidence is conflicting and I cannot confirm either outcome."
    ),
}

# Refusal responses: simulates cutoff fixation (baseline, no working_context).
REFUSAL_RESPONSES: dict[str, str] = {
    "ratified": (
        "I don't have reliable information about this from my training data. "
        "A climate accord ratification in June 2026 is beyond my knowledge cutoff."
    ),
    "adopted": (
        "I cannot confirm whether a technology framework was adopted at the June 2026 "
        "summit from my training data."
    ),
    "420": (
        "I don't have specific renewable energy capacity figures for H1 2026 in my training data."
    ),
    "renewable": (
        "I don't have specific renewable energy capacity figures for H1 2026 in my training data."
    ),
    "policy review": (
        "I cannot confirm details about the June 2026 policy review from my training data."
    ),
    "infrastructure": (
        "I don't have information about this infrastructure bill vote from my training data."
    ),
}


def make_grounded_model() -> ScriptedModel:
    """Return a ScriptedModel that heeds the working_context block."""
    return ScriptedModel(GROUNDED_RESPONSES, mode="grounded")


def make_refusal_model() -> ScriptedModel:
    """Return a ScriptedModel that simulates cutoff fixation (baseline)."""
    return ScriptedModel(REFUSAL_RESPONSES, mode="refusal")
