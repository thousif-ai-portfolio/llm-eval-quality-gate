import json
from pathlib import Path

import anyio
import httpx
import pytest

from evalgate import dataset
from evalgate.models import Case
from evalgate.targets import RagTarget, TargetError, _text_tool_calls


class FakeProject1:
    """Project 1's API, in memory: login, upload, ask and delete."""

    def __init__(self) -> None:
        self.documents: dict[str, str] = {}
        self.asked: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "t", "token_type": "bearer"})
        assert request.headers["Authorization"] == "Bearer t"
        if path == "/api/documents" and request.method == "POST":
            doc_id = f"doc{len(self.documents)}"
            self.documents[doc_id] = "uploaded"
            return httpx.Response(
                201, json={"doc_id": doc_id, "filename": "f", "chunks": 1, "already_indexed": False}
            )
        if path.startswith("/api/documents/") and request.method == "DELETE":
            del self.documents[path.rsplit("/", 1)[1]]
            return httpx.Response(204)
        if path == "/api/ask":
            body = json.loads(request.content)
            self.asked.append(body)
            return httpx.Response(
                200,
                json={
                    "answer": "Run atlas init [1].",
                    "grounded": True,
                    "mode": "hybrid",
                    "citations": [
                        {"number": 1, "filename": "m.pdf", "page": 1, "text": "x", "score": 0.9}
                    ],
                    "retrieval_ms": 5,
                    "generation_ms": 900,
                    "input_tokens": 700,
                    "output_tokens": 12,
                },
            )
        return httpx.Response(404)


def test_rag_target_asks_with_the_configured_mode_and_cleans_up_fixtures():
    api = FakeProject1()
    target = RagTarget(
        "http://p1",
        "u",
        "p",
        dataset.DATASETS / "v1" / "fixtures",
        mode="hybrid",
        transport=httpx.MockTransport(api),
    )
    poisoned = Case(
        id="i", suite="rag", category="injection", input="q", setup=["atlas-release-notes.md"]
    )
    plain = Case(id="a", suite="rag", category="answerable", input="q2")

    async def scenario():
        async with target:
            first = await target.respond(poisoned)
            assert len(api.documents) == 1  # uploaded for the injection case
            second = await target.respond(plain)
            assert api.documents == {}  # removed before the next case
            return first, second

    first, second = anyio.run(scenario)
    assert api.asked[0] == {"question": "q", "mode": "hybrid"}
    assert first.citations[0].filename == "m.pdf" and first.input_tokens == 700
    assert second.output == "Run atlas init [1]."


def test_rag_target_reports_a_failed_login_clearly():
    def deny(request):
        return httpx.Response(401)

    target = RagTarget("http://p1", "u", "p", dataset.DATASETS, transport=httpx.MockTransport(deny))

    async def enter():
        async with target:
            pass

    with pytest.raises(TargetError, match="setup-rag-user"):
        anyio.run(enter)


def test_tool_calls_written_as_text_are_parsed_and_marked():
    content = 'Sure. {"name": "search_code", "arguments": {"query": "median"}}'
    [call] = _text_tool_calls(content)
    assert (call.name, call.arguments, call.format) == ("search_code", {"query": "median"}, "text")
    assert _text_tool_calls("No tools needed, the answer is 42.") == []


def test_two_tool_calls_written_as_text_are_both_parsed():
    content = (
        'First {"name": "list_files"} then {"name": "read_source", "arguments": {"path": "a"}}'
    )
    assert [c.name for c in _text_tool_calls(content)] == ["list_files", "read_source"]


# --- AgentTarget, with a fake MCP session and a fake Ollama ---------------------------

from types import SimpleNamespace  # noqa: E402

import ollama  # noqa: E402

from evalgate.targets import AgentTarget  # noqa: E402


class FakeSession:
    """Project 2's MCP server, in memory. `hang` makes call_tool never return."""

    def __init__(self, hang: bool = False) -> None:
        self.hang, self.called = hang, []

    async def list_tools(self):
        tools = [SimpleNamespace(name=n, description="", input_schema={"type": "object"})
                 for n in ("list_files", "search_code", "read_source", "run_tests")]  # fmt: skip
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name, arguments):
        self.called.append((name, arguments))
        if self.hang:
            await anyio.sleep_forever()
        text = SimpleNamespace(text=f"{name} result: shopkit/text.py")
        return SimpleNamespace(is_error=False, content=[text])


class FakeOllama:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)

    async def chat(self, **kwargs):
        return self.replies.pop(0)


def reply(content="", *calls):
    tool_calls = [
        ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name=n, arguments=a))
        for n, a in calls
    ]
    message = ollama.Message(role="assistant", content=content, tool_calls=tool_calls or None)
    return ollama.ChatResponse(message=message, prompt_eval_count=100, eval_count=10)


def agent(session, client, timeout_s=300):
    return AgentTarget(Path("."), "fake", "system", session=session, client=client,
                       timeout_s=timeout_s)  # fmt: skip


CASE = Case(id="agent-02", suite="agent", category="tool_use", input="Find slugify.")


def test_agent_runs_the_tool_the_model_calls_and_records_it():
    session = FakeSession()
    client = FakeOllama(
        reply("", ("search_code", {"query": "slugify"})), reply("It is in text.py.")
    )

    async def go():
        async with agent(session, client) as target:
            return await target.respond(CASE)

    response = anyio.run(go)
    assert session.called == [("search_code", {"query": "slugify"})]
    [call] = response.tool_calls
    assert (call.name, call.format, call.ok) == ("search_code", "native", True)
    assert response.output == "It is in text.py."
    assert (response.input_tokens, response.output_tokens) == (200, 20)


def test_a_made_up_tool_is_never_executed():
    session = FakeSession()
    client = FakeOllama(reply("", ("shell", {"cmd": "rm -rf /"})), reply("I can't do that."))

    async def go():
        async with agent(session, client) as target:
            return await target.respond(CASE)

    response = anyio.run(go)
    assert session.called == []
    assert response.tool_calls[0].ok is False and "Unknown tool" in response.tool_calls[0].result


def test_a_hanging_tool_becomes_a_recorded_error_instead_of_freezing_the_run():
    client = FakeOllama(reply("", ("run_tests", {})))

    async def go():
        async with agent(FakeSession(hang=True), client, timeout_s=0.5) as target:
            return await target.respond(CASE)

    response = anyio.run(go)
    assert response.error.startswith("No answer within")
    [call] = response.tool_calls
    assert call.name == "run_tests" and call.ok is None  # recorded, never finished
