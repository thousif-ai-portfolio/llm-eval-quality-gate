"""Shared helpers. Unit tests never call a model or Project 1/2; they use fakes."""

import pytest

from evalgate.judge import Verdict
from evalgate.models import Case, Citation, Response, ToolCall


class FakeJudge:
    """Marks an answer correct when it contains the reference's first word, and faithful
    when it has citations: enough to exercise the pipeline deterministically."""

    model = "fake-judge"

    def correctness(self, question: str, reference: str, answer: str) -> Verdict:
        ok = reference.split()[0].lower() in answer.lower() if reference else True
        return Verdict(reason="fake", verdict="correct" if ok else "incorrect")

    def faithfulness(self, answer: str, sources) -> Verdict:
        return Verdict(reason="fake", verdict="supported" if list(sources) else "unsupported")


@pytest.fixture
def fake_judge() -> FakeJudge:
    return FakeJudge()


def rag_case(category="answerable", **expect) -> Case:
    defaults = {
        "facts": [["atlas init"]],
        "source": "manual.pdf",
        "pages": [1],
        "reference": "Run atlas init.",
    }
    if category == "answerable":
        expect = {**defaults, **expect}
    return Case(id=f"rag-{category}", suite="rag", category=category, input="q?", expect=expect)


def agent_case(category="tool_use", **expect) -> Case:
    return Case(
        id=f"agent-{category}", suite="agent", category=category, input="task", expect=expect
    )


def answer(text: str, *citations: tuple[str, int], case_id: str = "rag-answerable") -> Response:
    return Response(
        case_id=case_id,
        output=text,
        citations=[Citation(filename=f, page=p, text=f"text of {f}") for f, p in citations],
        latency_ms=1000,
        input_tokens=800,
        output_tokens=20,
    )


def calls(*items: tuple, output: str = "", case_id: str = "agent-tool_use") -> Response:
    tool_calls = []
    for item in items:
        name, args, *rest = item
        tool_calls.append(
            ToolCall(
                name=name,
                arguments=args,
                format=rest[0] if rest else "native",
                ok=rest[1] if len(rest) > 1 else True,
            )
        )
    return Response(case_id=case_id, output=output, tool_calls=tool_calls, latency_ms=500)
