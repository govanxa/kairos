"""Competitive analysis example — the canonical Kairos demo workflow.

Demonstrates:
- Diamond dependency pattern (fetch → research + analyze → report)
- foreach fan-out over a list of competitors
- Output contracts with Schema validation
- Scoped state access (read_keys / write_keys)
- Failure policies with retry and abort
- SKIP sentinel for optional steps
- The full Kairos value proposition in one workflow

This is the workflow from the Kairos architecture mockup:
  1. Fetch competitor list from state
  2. Research each competitor (foreach fan-out)
  3. Analyze market positioning (runs in parallel with research conceptually)
  4. Generate the final report from research + analysis
"""

from typing import Any, cast

from kairos import (
    SKIP,
    FailureAction,
    FailurePolicy,
    Schema,
    Step,
    StepContext,
    Workflow,
    WorkflowStatus,
)

# ---------------------------------------------------------------------------
# Schemas — contracts for what each step produces
# ---------------------------------------------------------------------------

competitor_research_schema = Schema(
    {
        "name": str,
        "products": list[str],
        "strengths": str,
    }
)

analysis_schema = Schema(
    {
        "market_size": str,
        "trends": list[str],
        "opportunities": list[str],
    }
)

report_schema = Schema(
    {
        "title": str,
        "competitor_count": int,
        "key_findings": list[str],
        "recommendation": str,
    }
)


# ---------------------------------------------------------------------------
# Step actions
# ---------------------------------------------------------------------------


def fetch_competitors(ctx: StepContext) -> dict[str, Any]:
    """Fetch the list of competitors to analyze."""
    # In a real workflow, this might call an API or database.
    competitors = cast(list[str], ctx.state.get("competitors"))
    return {"competitors": competitors, "count": len(competitors)}


def research_competitor(ctx: StepContext) -> dict[str, Any]:
    """Research a single competitor (runs once per item via foreach)."""
    competitor = cast(str, ctx.item)
    # In a real workflow, this would call an LLM or scrape data.
    # Here we simulate with deterministic output.
    research = {
        "Globex": {
            "name": "Globex",
            "products": ["Widget Pro", "Gadget Max"],
            "strengths": "Strong brand recognition and global distribution",
        },
        "Initech": {
            "name": "Initech",
            "products": ["TPS Reports", "Cover Sheets"],
            "strengths": "Enterprise market penetration and compliance tools",
        },
        "Umbrella": {
            "name": "Umbrella",
            "products": ["BioShield", "PharmaCare"],
            "strengths": "R&D investment and patent portfolio",
        },
    }
    return research.get(
        competitor,
        {
            "name": competitor,
            "products": ["Unknown"],
            "strengths": "No data available",
        },
    )


def analyze_market(ctx: StepContext) -> dict[str, Any]:
    """Analyze the overall market positioning."""
    fetch_data = cast(dict[str, Any], ctx.inputs["fetch_competitors"])
    count = fetch_data["count"]
    return {
        "market_size": "$4.2B",
        "trends": [
            "AI-powered automation",
            "Security-first design",
            "Open-source adoption",
        ],
        "opportunities": [
            f"Market has {count} major players — room for differentiation",
            "No competitor offers sanitized retry context",
            "Compliance-focused buyers underserved",
        ],
    }


def check_compliance(ctx: StepContext) -> object:
    """Optional compliance check — skips if not required."""
    requires_compliance = ctx.state.get("requires_compliance", False)
    if not requires_compliance:
        return SKIP
    return {"compliant": True, "standard": "SOC2"}


def generate_report(ctx: StepContext) -> dict[str, Any]:
    """Generate the final competitive analysis report."""
    research_results = cast(list[dict[str, Any]], ctx.inputs["research"])
    analysis = cast(dict[str, Any], ctx.inputs["analyze"])

    competitor_names: list[str] = [r["name"] for r in research_results]
    all_products: list[str] = []
    for r in research_results:
        all_products.extend(cast(list[str], r["products"]))

    return {
        "title": "Competitive Analysis Report",
        "competitor_count": len(research_results),
        "key_findings": [
            f"Analyzed {len(research_results)} competitors: " + ", ".join(competitor_names),
            f"Market size: {analysis['market_size']}",
            f"Total products tracked: {len(all_products)}",
            f"Top trend: {analysis['trends'][0]}",
            f"Key opportunity: {analysis['opportunities'][1]}",
        ],
        "recommendation": (
            "Focus on security-first differentiation. No competitor "
            "offers sanitized retry context or scoped state access. "
            "Target compliance-conscious enterprise buyers."
        ),
    }


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="competitive-analysis",
    steps=[
        Step(
            name="fetch_competitors",
            action=fetch_competitors,
            read_keys=["competitors"],
        ),
        Step(
            name="research",
            action=research_competitor,
            depends_on=["fetch_competitors"],
            foreach="competitors",
            output_contract=competitor_research_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=3,
            ),
        ),
        Step(
            name="analyze",
            action=analyze_market,
            depends_on=["fetch_competitors"],
            output_contract=analysis_schema,
        ),
        Step(
            name="compliance_check",
            action=check_compliance,
            depends_on=["fetch_competitors"],
        ),
        Step(
            name="report",
            action=generate_report,
            depends_on=["research", "analyze"],
            output_contract=report_schema,
            failure_policy=FailurePolicy(
                on_validation_fail=FailureAction.ABORT,
            ),
        ),
    ],
    failure_policy=FailurePolicy(
        on_execution_fail=FailureAction.RETRY,
        max_retries=2,
    ),
    sensitive_keys=["*api_key*", "*credentials*"],
    metadata={
        "author": "Kairos SDK",
        "version": "1.0",
        "description": "Competitive analysis with foreach fan-out and validation",
    },
)


# ---------------------------------------------------------------------------
# Run it
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Kairos Competitive Analysis Workflow")
    print("=" * 60)
    print()

    result = workflow.run(
        {
            "competitors": ["Globex", "Initech", "Umbrella"],
            "requires_compliance": False,
        }
    )

    print(f"Status: {result.status.value}")
    print(f"Duration: {result.duration_ms:.1f}ms")
    print(f"LLM calls: {result.llm_calls}")
    print()

    # Step-by-step results
    print("Step Results:")
    print("-" * 40)
    for name, sr in result.step_results.items():
        status_icon = {
            "completed": "+",
            "skipped": "~",
            "failed_final": "X",
        }.get(sr.status.value, "?")
        attempts = len(sr.attempts)
        print(f"  [{status_icon}] {name} ({sr.status.value}, {attempts} attempt(s))")

    print()

    # The report
    report = cast(dict[str, Any], result.step_results["report"].output)
    print("REPORT: " + report["title"])
    print("-" * 40)
    print(f"Competitors analyzed: {report['competitor_count']}")
    print()
    print("Key findings:")
    for i, finding in enumerate(report["key_findings"], 1):
        print(f"  {i}. {finding}")
    print()
    print(f"Recommendation: {report['recommendation']}")
    print()

    # Compliance check was skipped
    compliance = result.step_results["compliance_check"]
    print(f"Compliance check: {compliance.status.value}")
    print()

    # Final state shows sensitive keys are redacted
    print("Final state keys:", list(result.final_state.keys()))

    assert result.status == WorkflowStatus.COMPLETE
    print()
    print("Done.")
