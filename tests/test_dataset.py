import shutil
from collections import Counter

import pytest

from evalgate import dataset


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
            assert dataset.fixture("v1", name).is_file(), name


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
