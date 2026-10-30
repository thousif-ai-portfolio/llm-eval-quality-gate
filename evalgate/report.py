"""Markdown reports: one run against its baseline, or several runs side by side."""

from pathlib import Path

from evalgate.gate import GateResult
from evalgate.models import CaseResult

LABELS = {
    "rag.correctness": "RAG: answer correct (judge)",
    "rag.faithfulness": "RAG: no unsupported claims (judge)",
    "rag.facts": "RAG: expected facts present",
    "rag.citations": "RAG: cites the right document and page",
    "rag.refusal": 'RAG: says "I don\'t know" when it should',
    "agent.tool_choice": "Agent: right tool",
    "agent.tool_args": "Agent: right tool inputs",
    "agent.native_format": "Agent: proper tool-call format",
    "agent.tool_ran": "Agent: tool call ran without error",
    "agent.answer": "Agent: final answer right",
    "security.rag_injection": "Security: RAG ignores injected instructions",
    "security.agent_no_unsafe_effect": "Security: no unsafe tool action succeeded",
    "security.agent_no_unsafe_attempt": "Security: agent never tried an unsafe action",
    "security.agent_refusal": "Security: agent refuses forbidden requests",
}


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def gate_report(summary: dict, gate: GateResult, results: list[CaseResult]) -> str:
    verdict = "✅ **PASSED**" if gate.passed else "❌ **FAILED**"
    lines = [
        f"## Quality gate: {verdict}",
        "",
        f"Run **{summary['label']}** on dataset **{summary['dataset']}** "
        f"(`{summary['fingerprint']}`): {summary['passed']}/{summary['cases']} cases passed.",
        "",
    ]
    if gate.failures:
        lines += ["**Why it failed**", ""] + [f"- {f}" for f in gate.failures] + [""]
    if gate.notes:
        lines += ["**Notes**", ""] + [f"- {n}" for n in gate.notes] + [""]
    lines += ["| Metric | Result | Baseline | Minimum | Status |", "|---|---:|---:|---:|---|"]
    for row in gate.rows:
        status = {
            "ok": "✅",
            "below minimum": "❌ below minimum",
            "regressed": "❌ regressed",
            "missing": "❌ missing",
        }[row["status"]]
        lines.append(
            f"| {LABELS.get(row['metric'], row['metric'])} | {_pct(row['value'])} | "
            f"{_pct(row['baseline'])} | {_pct(row['min'])} | {status} |"
        )
    lines += ["", _ops_table(summary), "", _failed_cases(results)]
    return "\n".join(lines) + "\n"


def _ops_table(summary: dict) -> str:
    lines = [
        "| Suite | Latency p50 | Latency p95 | Tokens in / out (avg) | Est. cost per 1,000 |",
        "|---|---:|---:|---:|---:|",
    ]
    for suite, ops in summary["ops"].items():
        lines.append(
            f"| {suite} | {ops['latency_p50_ms'] / 1000:.1f} s"
            f" | {ops['latency_p95_ms'] / 1000:.1f} s"
            f" | {ops['tokens_in_avg']:.0f} / {ops['tokens_out_avg']:.0f}"
            f" | ${ops['est_cost_per_1k_usd']:.2f} |"
        )
    return "\n".join(lines)


def _failed_cases(results: list[CaseResult]) -> str:
    failed = [r for r in results if not r.passed]
    if not failed:
        return "All cases passed."
    lines = [
        f"<details><summary>{len(failed)} failing cases</summary>",
        "",
        "| Case | Failed checks |",
        "|---|---|",
    ]
    for r in failed:
        bad = "; ".join(
            f"{'**' if c.critical else ''}{c.name}{'**' if c.critical else ''}"
            + (f": {c.detail[:90]}" if c.detail else "")
            for c in r.checks
            if not c.passed
        )
        lines.append(f"| {r.case_id} | {bad.replace('|', '/').replace(chr(10), ' ')} |")
    return "\n".join(lines + ["", "</details>"])


def compare_report(summaries: list[dict]) -> str:
    labels = [s["label"] for s in summaries]
    names = sorted({m for s in summaries for m in s["metrics"]})
    lines = ["| Metric | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)]
    for name in names:
        values = [s["metrics"].get(name, {}).get("rate") for s in summaries]
        lines.append(f"| {LABELS.get(name, name)} | " + " | ".join(map(_pct, values)) + " |")
    lines.append(
        "| Cases passed | " + " | ".join(f"{s['passed']}/{s['cases']}" for s in summaries) + " |"
    )
    for suite in sorted({suite for s in summaries for suite in s["ops"]}):
        p50 = [s["ops"].get(suite, {}).get("latency_p50_ms") for s in summaries]
        lines.append(
            f"| {suite} latency p50 | "
            + " | ".join("–" if v is None else f"{v / 1000:.1f} s" for v in p50)
            + " |"
        )
    return "\n".join(lines) + "\n"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
