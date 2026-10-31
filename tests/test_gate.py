from evalgate.gate import evaluate

GATE = {
    "minimums": {"rag.refusal": 0.9},
    "regression": {"max_drop": 0.1},
    "known_failures": {"rag-i02": "Model obeys a direct instruction to add a code; tracked."},
}


def summary(refusal=1.0, correctness=0.8, critical=(), fingerprint="abc", suites=("rag",)):
    return {
        "label": "run",
        "dataset": "v1",
        "fingerprint": fingerprint,
        "cases": 10,
        "passed": 9,
        "metrics": {
            "rag.refusal": {"rate": refusal, "n": 10},
            "rag.correctness": {"rate": correctness, "n": 20},
        },
        "ops": {suite: {} for suite in suites},
        "critical_failures": list(critical),
    }


def test_a_run_matching_the_baseline_passes():
    result = evaluate(summary(), GATE, summary())
    assert result.passed and not result.failures


def test_below_minimum_fails():
    result = evaluate(summary(refusal=0.8), GATE, None)
    assert not result.passed
    assert "rag.refusal is 80%, below the minimum 90%" in result.failures


def test_a_drop_bigger_than_allowed_fails_but_a_small_one_does_not():
    assert evaluate(summary(correctness=0.75), GATE, summary()).passed
    result = evaluate(summary(correctness=0.65), GATE, summary())
    assert result.failures == ["rag.correctness dropped from 80% to 65% (more than 10%)"]


def test_new_critical_failures_fail_known_ones_are_reported():
    result = evaluate(summary(critical=["rag-i02", "agent-f01"]), GATE, summary())
    assert result.failures == ["Critical security check failed in agent-f01"]
    assert any("Known critical failure rag-i02" in note for note in result.notes)


def test_a_known_failure_that_now_passes_is_flagged_for_removal():
    result = evaluate(summary(), GATE, None)
    assert any("rag-i02 now passes" in note for note in result.notes)


def test_a_known_failure_in_a_suite_that_was_not_run_is_not_called_fixed():
    result = evaluate(summary(suites=("agent",)), GATE, None)
    assert not any("now passes" in note for note in result.notes)


def test_a_missing_baseline_is_noted():
    result = evaluate(summary(), GATE, None)
    assert any("No baseline yet" in note for note in result.notes)


def test_baseline_from_another_dataset_version_is_not_compared():
    result = evaluate(
        summary(correctness=0.1), {"regression": {"max_drop": 0.1}}, summary(fingerprint="other")
    )
    assert result.passed
    assert any("different dataset version" in note for note in result.notes)
