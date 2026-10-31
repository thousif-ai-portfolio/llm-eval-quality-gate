import shutil
from collections import Counter

import pytest

from evalgate import dataset, runner
from evalgate.cli import main


def test_v1_loads_with_the_planned_mix_of_cases():
    cases = dataset.load("v1")
    counts = Counter((c.suite, c.category) for c in cases)
    assert len(cases) == 60
    assert counts == {
        ("rag", "answerable"): 20,
        ("rag", "unanswerable"): 10,
        ("rag", "injection"): 6,
        ("agent", "tool_use"): 12,
        ("agent", "forbidden"): 8,
        ("agent", "injection"): 4,
    }


def test_every_case_has_what_its_checks_need():
    for case in dataset.load("v1"):
        e = case.expect
        if case.category == "answerable":
            assert e["facts"] and e["source"] and e["reference"], case.id
        if case.suite == "rag" and case.category == "injection":
            assert e["forbidden"], case.id
        if case.category == "tool_use":
            assert e["tools"], case.id
        for name in case.setup:
            assert (dataset.DATASETS / "v1" / "fixtures" / name).is_file(), name


def test_editing_a_frozen_dataset_is_refused(tmp_path):
    shutil.copytree(dataset.DATASETS / "v1", tmp_path / "v1")
    assert len(dataset.load("v1", base=tmp_path)) == 60
    rag = tmp_path / "v1" / "rag.jsonl"
    rag.write_text(rag.read_text(encoding="utf-8").replace("2 GB", "3 GB"), encoding="utf-8")
    with pytest.raises(dataset.DatasetError, match="changed since it was frozen"):
        dataset.load("v1", base=tmp_path)


def test_a_new_version_is_frozen_with_its_own_fingerprint(tmp_path):
    shutil.copytree(dataset.DATASETS / "v1", tmp_path / "v2")
    (tmp_path / "v2" / "rag.jsonl").write_text("", encoding="utf-8")
    manifest = dataset.freeze("v2", base=tmp_path)
    assert manifest["fingerprint"] != dataset.verify("v1")["fingerprint"]
    assert len(dataset.load("v2", base=tmp_path)) == 24


def test_a_frozen_version_cannot_be_frozen_again(tmp_path):
    shutil.copytree(dataset.DATASETS / "v1", tmp_path / "v1")
    with pytest.raises(dataset.DatasetError, match="already frozen"):
        dataset.freeze("v1", base=tmp_path)


def test_cli_reports_a_missing_dataset_without_a_traceback(capsys):
    assert main(["dataset", "verify", "v0"]) == 2
    assert "error: Dataset v0 has no manifest" in capsys.readouterr().err


def test_cli_reports_a_missing_run_folder_without_a_traceback(capsys, tmp_path):
    assert main(["gate", str(tmp_path / "no-such-run")]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_reports_a_broken_gate_file_without_a_traceback(capsys, tmp_path):
    run = tmp_path / "run"
    shutil.copytree(dataset.ROOT / "runs" / "current", run)
    (tmp_path / "gate.toml").write_text("minimums = [", encoding="utf-8")
    assert main(["gate", str(run), "--gate", str(tmp_path / "gate.toml")]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_a_suite_with_no_selected_cases_is_skipped_without_its_target(tmp_path):
    assert runner.run({"dataset": "v1", "label": "x"}, "rag", tmp_path, only=["agent-01"]) is None
