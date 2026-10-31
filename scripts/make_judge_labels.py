"""Builds datasets/judge_labels.jsonl, the hand-labelled set the judge is calibrated on.

Real answers come from the recorded baseline run (runs/current) with their real cited
sources; each was checked by hand against the full source text. Synthetic variants add a
realistic mistake (an invented detail, a wrong number, a near-miss name), because a judge
must also catch bad answers, not just agree with good ones.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "runs" / "current"
OUT = ROOT / "datasets" / "judge_labels.jsonl"

responses = {
    r["case_id"]: r
    for r in map(json.loads, (RUN / "responses.jsonl").read_text("utf-8").strip().splitlines())
}
cases = {
    c["id"]: c
    for c in map(
        json.loads, (ROOT / "datasets/v1/rag.jsonl").read_text("utf-8").strip().splitlines()
    )
}


def sources(case_id):
    return [c["text"] for c in responses[case_id]["citations"]]


labels = []


def faithful(id, case_id, human, answer=None, note=""):
    labels.append({
        "id": id, "kind": "faithfulness", "sources": sources(case_id),
        "answer": answer or responses[case_id]["output"], "human": human, "note": note,
    })  # fmt: skip


def correct(id, case_id, human, answer=None, note=""):
    case = cases[case_id]
    labels.append({
        "id": id, "kind": "correctness", "question": case["input"],
        "reference": case["expect"]["reference"],
        "answer": answer or responses[case_id]["output"], "human": human, "note": note,
    })  # fmt: skip


# Faithfulness: 10 real answers, all supported by their cited text.
for case_id in ["rag-03", "rag-04", "rag-05", "rag-08", "rag-09", "rag-16", "rag-17", "rag-18",
                "rag-20"]:  # fmt: skip
    faithful(f"f-{case_id}", case_id, True, note="real answer, checked against its sources")
faithful("f-rag-14", "rag-14", True, note="real; 'to maintain efficiency' is a fair paraphrase")
# Faithfulness: 6 answers with one invented detail each.
faithful("f-s1", "rag-03", False, "Files larger than max_upload_mb are skipped with a warning "
         "and retried later [1].", "invented: retried later")  # fmt: skip
faithful("f-s2", "rag-04", False, "Atlas retries a failed transfer 5 times, waiting 10 seconds "
         "between attempts [1].", "invented: 10 seconds")  # fmt: skip
faithful("f-s3", "rag-16", False, "One token is roughly four characters, and a page of text is "
         "about 500 tokens [1].", "invented: 500 tokens per page")  # fmt: skip
faithful("f-s4", "rag-20", False, "Recall@5 is the most common version, and most benchmarks "
         "also require MRR@10 [1].", "invented: MRR@10 requirement")  # fmt: skip
faithful("f-s5", "rag-08", False, "E1007 means the access token expired; run atlas login again "
         "and the new token lasts 24 hours [1].", "invented: 24 hours")  # fmt: skip
faithful("f-s6", "rag-18", False, "The ops guide recommends hashing passwords with Argon2 or "
         "bcrypt [1].", "invented: bcrypt")  # fmt: skip

# Correctness: 8 real correct answers, 2 correct paraphrases, 6 wrong answers.
for case_id in ["rag-01", "rag-02", "rag-07", "rag-10", "rag-12", "rag-13", "rag-15", "rag-19"]:
    correct(f"c-{case_id}", case_id, True, note="real answer, matches the reference")
correct("c-p1", "rag-17", True, "Setting the temperature to zero makes the output repeatable.",
        "paraphrase")  # fmt: skip
correct("c-p2", "rag-05", True, "A value of 0 removes the cap on upload speed.", "paraphrase")
correct("c-w1", "rag-01", False, "Atlas needs at least 512 MB of free disk space [1].",
        "wrong: confuses the memory figure with disk space")  # fmt: skip
correct("c-w2", "rag-04", False, "It retries 3 times by default [1].", "wrong number")
correct("c-w3", "rag-12", False, "HNSW stands for Hierarchical Network Search Workflow [1].",
        "near-miss expansion")  # fmt: skip
correct("c-w4", "rag-13", False, "k is usually set to 10 [1].", "wrong number")
correct("c-w5", "rag-11", False, "I don't know.", "refusal of an answerable question")
correct("c-w6", "rag-06", False, "atlas status shows the files whose checksums don't match [1].",
        "wrong command")  # fmt: skip

OUT.write_text("".join(json.dumps(label) + "\n" for label in labels), "utf-8", newline="\n")
print(len(labels), "labels:", sum(not label["human"] for label in labels), "negative")
