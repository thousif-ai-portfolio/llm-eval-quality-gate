"""Builds promptfoo/tests.json: for every RAG case, the exact user message Project 1 sends
its model (the retrieved passages, numbered, plus the question), with assertions from
the dataset. Run it with Project 1's Python so it uses Project 1's own retrieval code:

    ..\\grounded-ai-knowledge-assistant\\.venv\\Scripts\\python scripts\\export_promptfoo_tests.py

Questions whose search finds nothing never reach Project 1's model, so they are left out.
Fixture documents are added for the injection cases and removed afterwards.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT1 = (ROOT / ".." / "grounded-ai-knowledge-assistant").resolve()
sys.path.insert(0, str(PROJECT1))

from app.config import Settings  # noqa: E402
from app.main import build_service  # noqa: E402
from app.rag import SYSTEM_PROMPT, build_prompt  # noqa: E402


def asserts(case: dict) -> list[dict]:
    expect = case["expect"]
    out = []
    if case["category"] == "answerable" or "facts" in expect:
        groups = json.dumps([[a.lower() for a in g] for g in expect["facts"]])
        # One line, so promptfoo evaluates it as an expression (no statements or return).
        check = f"{groups}.every(g => g.some(a => output.toLowerCase().includes(a)))"
        out.append({"type": "javascript", "value": check, "metric": "facts"})
    if case["category"] == "answerable":
        out.append({"type": "not-icontains", "value": "I don't know", "metric": "answered"})
    if expect.get("refuse"):
        out.append({"type": "icontains", "value": "I don't know", "metric": "refused"})
    for text in expect.get("forbidden", []):
        out.append({"type": "not-icontains", "value": text, "metric": "injection"})
    return out


def main() -> None:
    cases = [
        json.loads(line)
        for line in (ROOT / "datasets" / "v1" / "rag.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    rag = build_service(Settings())
    tests, skipped, added = [], [], {}
    try:
        for case in sorted(cases, key=lambda c: (c["setup"], c["id"])):
            for name in case["setup"]:
                if name not in added:
                    path = ROOT / "datasets" / "v1" / "fixtures" / name
                    added[name] = rag.add_document(name, path.read_bytes()).doc_id
            hits = rag.retrieve(case["input"])
            if not hits:
                skipped.append(case["id"])
            else:
                tests.append({
                    "description": f"{case['id']} [{case['category']}]",
                    "vars": {"user_message": build_prompt(case["input"], hits)},
                    "assert": asserts(case),
                    "metadata": {"case": case["id"], "category": case["category"]},
                })  # fmt: skip
            for name in [n for n in added if n not in case["setup"]]:
                rag.delete_document(added.pop(name))
    finally:
        for doc_id in added.values():
            rag.delete_document(doc_id)
    out = ROOT / "promptfoo"
    (out / "tests.json").write_text(json.dumps(tests, indent=1) + "\n", "utf-8", newline="\n")
    # Chat prompts: the system prompt under test, then Project 1's exact user message.
    # "strict" is copied from Project 1's code, so it always matches production.
    prompts = out / "prompts"
    friendly = (prompts / "friendly.txt").read_text("utf-8").strip()
    for name, system in (("strict", SYSTEM_PROMPT), ("friendly", friendly)):
        chat = [{"role": "system", "content": system},
                {"role": "user", "content": "{{ user_message }}"}]  # fmt: skip
        (prompts / f"{name}.json").write_text(json.dumps(chat, indent=1) + "\n", "utf-8",
                                              newline="\n")  # fmt: skip
    print(f"{len(tests)} tests written; skipped (search found nothing): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
