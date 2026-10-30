"""The shapes everything else passes around: test cases, recorded responses and results."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Suite = Literal["rag", "agent"]
Category = Literal["answerable", "unanswerable", "injection", "tool_use", "forbidden"]


class Case(BaseModel):
    """One exam question and what a good response looks like."""

    id: str
    suite: Suite
    category: Category
    input: str
    expect: dict[str, Any] = Field(default_factory=dict)
    # Documents to add to the knowledge base before this case (indirect prompt injection).
    setup: list[str] = Field(default_factory=list)
    notes: str = ""


class Citation(BaseModel):
    filename: str
    page: int
    text: str = ""


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # "native" when the model used the API's tool-call format, "text" when it wrote the
    # call as JSON in its message instead.
    format: Literal["native", "text"] = "native"
    ok: bool | None = None  # None when the call was not executed
    result: str = ""


class Response(BaseModel):
    """What the system under test did for one case. Recorded, so it can be re-scored
    later (and in CI) without running any model."""

    case_id: str
    output: str = ""
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    model: str = ""


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    # Critical checks are the security ones: no leaked canary, no unsafe tool effect.
    critical: bool = False


class CaseResult(BaseModel):
    case_id: str
    suite: Suite
    category: Category
    checks: list[Check]
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)
