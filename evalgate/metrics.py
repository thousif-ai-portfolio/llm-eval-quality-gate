"""Turns per-case results into the numbers the gate and the reports use."""

import statistics

from evalgate.models import CaseResult

# metric name -> (suite, categories it is measured on, check name)
METRICS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "rag.correctness": ("rag", ("answerable",), "correct"),
    "rag.faithfulness": ("rag", ("answerable",), "faithful"),
    "rag.facts": ("rag", ("answerable",), "facts"),
    "rag.citations": ("rag", ("answerable",), "cites_source"),
    "rag.refusal": ("rag", ("unanswerable",), "refused"),
    "agent.tool_choice": ("agent", ("tool_use",), "tool_choice"),
    "agent.tool_args": ("agent", ("tool_use",), "tool_args"),
    "agent.native_format": ("agent", ("tool_use",), "native_format"),
    "agent.tool_ran": ("agent", ("tool_use",), "tool_ran"),
    "agent.answer": ("agent", ("tool_use",), "answer"),
    "security.rag_injection": ("rag", ("injection",), "no_injected_output"),
    "security.agent_no_unsafe_effect": ("agent", ("injection", "forbidden"), "no_unsafe_effect"),
    "security.agent_no_unsafe_attempt": ("agent", ("injection", "forbidden"), "no_unsafe_attempt"),
    "security.agent_refusal": ("agent", ("forbidden",), "refused"),
}


def _rate(results: list[CaseResult], suite: str, categories: tuple[str, ...], check: str):
    # A case that errored has only a failed "responded" check; it fails every metric.
    checks = [
        r.check(check) or r.check("responded")
        for r in results
        if r.suite == suite and r.category in categories
    ]
    found = [c for c in checks if c is not None]
    if not found:
        return None
    return {"rate": round(sum(c.passed for c in found) / len(found), 3), "n": len(found)}


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return round(statistics.quantiles(values, n=100, method="inclusive")[q - 1], 1)


def summarise(results: list[CaseResult], pricing: dict[str, float]) -> dict:
    metrics = {}
    for name, (suite, categories, check) in METRICS.items():
        value = _rate(results, suite, categories, check)
        if value is not None:
            metrics[name] = value
    categories: dict[str, dict] = {}
    for r in results:
        entry = categories.setdefault(f"{r.suite}.{r.category}", {"passed": 0, "n": 0})
        entry["n"] += 1
        entry["passed"] += r.passed
    ops = {}
    for suite in sorted({r.suite for r in results}):
        rs = [r for r in results if r.suite == suite]
        latencies = [r.latency_ms for r in rs]
        tokens_in = statistics.fmean(r.input_tokens for r in rs)
        tokens_out = statistics.fmean(r.output_tokens for r in rs)
        cost = (
            tokens_in * pricing.get("input_per_mtok", 0)
            + tokens_out * pricing.get("output_per_mtok", 0)
        ) / 1e6
        ops[suite] = {
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "tokens_in_avg": round(tokens_in, 1),
            "tokens_out_avg": round(tokens_out, 1),
            "est_cost_per_1k_usd": round(cost * 1000, 4),
        }
    critical = sorted(
        r.case_id for r in results if any(c.critical and not c.passed for c in r.checks)
    )
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": round(sum(r.passed for r in results) / len(results), 3) if results else 0.0,
        "metrics": metrics,
        "categories": categories,
        "ops": ops,
        "critical_failures": critical,
        # Cases whose security checks ran and all passed (not ones that errored first).
        "critical_passes": sorted(
            r.case_id
            for r in results
            if (checks := [c for c in r.checks if c.critical]) and all(c.passed for c in checks)
        ),
    }
