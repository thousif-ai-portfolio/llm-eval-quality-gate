"""The quality gate: decides whether a run is good enough to merge.

A run fails when
1. a metric is below its minimum in gate.toml,
2. a metric dropped more than `max_drop` below the baseline (the last accepted run),
3. a critical (security) check failed, unless that case is listed as a known failure
   with a reason. Known failures are tracked, not hidden: they appear in every report,
   and the gate says when one starts passing so it can be removed from the list.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)


def load_gate(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def evaluate(summary: dict, gate: dict, baseline: dict | None) -> GateResult:
    result = GateResult(passed=True)
    minimums: dict[str, float] = gate.get("minimums", {})
    max_drop = gate.get("regression", {}).get("max_drop", 0.1)
    metrics = summary["metrics"]
    base_metrics = (baseline or {}).get("metrics", {})

    if baseline and baseline.get("fingerprint") != summary.get("fingerprint"):
        result.notes.append(
            "Baseline was measured on a different dataset version, so it is not compared."
        )
        base_metrics = {}

    for name in sorted(set(metrics) | set(minimums)):
        value = metrics.get(name, {}).get("rate")
        base = base_metrics.get(name, {}).get("rate")
        row = {
            "metric": name,
            "value": value,
            "baseline": base,
            "min": minimums.get(name),
            "status": "ok",
        }
        if value is None:
            if name in minimums:
                row["status"] = "missing"
                result.failures.append(f"{name}: not measured in this run")
            result.rows.append(row)
            continue
        if name in minimums and value < minimums[name]:
            row["status"] = "below minimum"
            result.failures.append(f"{name} is {value:.0%}, below the minimum {minimums[name]:.0%}")
        elif base is not None and base - value > max_drop + 1e-9:
            row["status"] = "regressed"
            result.failures.append(
                f"{name} dropped from {base:.0%} to {value:.0%} (more than {max_drop:.0%})"
            )
        result.rows.append(row)

    known = gate.get("known_failures", {})
    for case_id in summary["critical_failures"]:
        if case_id in known:
            result.notes.append(f"Known critical failure {case_id}: {known[case_id]}")
        else:
            result.failures.append(f"Critical security check failed in {case_id}")
    # Case ids start with their suite ("rag-i02"); a suite that wasn't run proves nothing.
    suites_run = set(summary["ops"])
    for case_id in sorted(set(known) - set(summary["critical_failures"])):
        if case_id.split("-")[0] in suites_run:
            result.notes.append(
                f"{case_id} now passes; remove it from known_failures in gate.toml."
            )

    result.passed = not result.failures
    return result
