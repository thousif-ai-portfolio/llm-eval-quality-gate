"""The systems under test. Each target turns a Case into a recorded Response.

- RagTarget: Project 1 (grounded-ai-knowledge-assistant) through its HTTP API.
- AgentTarget: a tool-calling model driving Project 2's real MCP tool server.

CI needs neither: it re-scores the recorded responses (runner.rescore).
"""

import json
import os
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol

import anyio
import httpx
import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from evalgate.models import Case, Citation, Response, ToolCall


class Target(Protocol):
    async def __aenter__(self) -> "Target": ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def respond(self, case: Case) -> Response: ...


class TargetError(RuntimeError):
    pass


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


# --- Project 1: RAG over HTTP ------------------------------------------------------------


class RagTarget:
    """Asks Project 1's /api/ask endpoint, as a signed-in user. Fixture documents for
    indirect-injection cases are uploaded just before their case and deleted afterwards,
    so the knowledge base is left as it was found."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        fixtures: Path,
        mode: str | None = None,
        timeout_s: float = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url, self._username, self._password = url.rstrip("/"), username, password
        self._fixtures, self._mode = fixtures, mode
        self._client = httpx.AsyncClient(base_url=self._url, timeout=timeout_s, transport=transport)
        self._uploaded: dict[str, str] = {}  # fixture name -> doc_id

    async def __aenter__(self) -> "RagTarget":
        try:
            login = await self._client.post(
                "/api/auth/login", data={"username": self._username, "password": self._password}
            )
        except httpx.HTTPError as exc:
            raise TargetError(f"Cannot reach Project 1 at {self._url}: {exc}") from exc
        if login.status_code != 200:
            raise TargetError(
                f"Login to Project 1 failed ({login.status_code}). Run `evalgate setup-rag-user`."
            )
        self._client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._sync_setup([])
        await self._client.aclose()

    async def _sync_setup(self, wanted: list[str]) -> None:
        for name in [n for n in self._uploaded if n not in wanted]:
            await self._client.delete(f"/api/documents/{self._uploaded.pop(name)}")
        for name in [n for n in wanted if n not in self._uploaded]:
            path = self._fixtures / name
            reply = await self._client.post(
                "/api/documents", files={"file": (name, path.read_bytes(), "text/markdown")}
            )
            reply.raise_for_status()
            self._uploaded[name] = reply.json()["doc_id"]

    async def respond(self, case: Case) -> Response:
        await self._sync_setup(case.setup)
        body: dict[str, Any] = {"question": case.input}
        if self._mode:
            body["mode"] = self._mode
        start = time.perf_counter()
        try:
            reply = await self._client.post("/api/ask", json=body)
            reply.raise_for_status()
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            return Response(case_id=case.id, error=error, latency_ms=_elapsed_ms(start))
        data = reply.json()
        return Response(
            case_id=case.id,
            output=data["answer"],
            citations=[
                Citation(filename=c["filename"], page=c["page"], text=c["text"])
                for c in data["citations"]
            ],
            latency_ms=_elapsed_ms(start),
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            model=f"project1/{data['mode']}",
        )


# --- Project 2: tool-calling model + MCP tool server -------------------------------------


def _text_tool_calls(content: str) -> list[ToolCall]:
    """Small models often write the call as JSON in their reply instead of using the API's
    tool-call format. Those are still parsed (and executed) but marked format="text"."""
    calls, decoder, end = [], json.JSONDecoder(), 0
    # Every JSON object or list in the reply, so two calls in one message are both found.
    for match in re.finditer(r"[{\[]", content):
        if match.start() < end:
            continue
        try:
            data, end = decoder.raw_decode(content, match.start())
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                args = item.get("arguments") or item.get("parameters") or {}
                calls.append(
                    ToolCall(
                        name=item["name"],
                        arguments=args if isinstance(args, dict) else {},
                        format="text",
                    )
                )
    return calls


class AgentTarget:
    """Gives a local model Project 2's MCP tools and a task, runs up to `max_steps` rounds of
    tool calls through the real server, and records every call and its result."""

    def __init__(
        self,
        project2: Path,
        model: str,
        system_prompt: str,
        max_steps: int = 3,
        timeout_s: float = 300,
        session: Any = None,
        client: Any = None,
    ) -> None:
        """`session` and `client` stand in for the MCP server and Ollama in tests."""
        self._project2, self._model, self._system = project2, model, system_prompt
        self._max_steps, self._timeout_s = max_steps, timeout_s
        # A per-request HTTP timeout as well as the per-case limit below, so a stalled
        # model connection cannot hold up a run.
        self._ollama = client or ollama.AsyncClient(host=os.environ.get("OLLAMA_HOST"), timeout=180)
        self._stack = AsyncExitStack()
        self._session = session
        self._tools: list[dict] = []

    async def __aenter__(self) -> "AgentTarget":
        if self._session is None:
            python = self._project2 / ".venv" / "Scripts" / "python.exe"
            if not python.is_file():
                python = self._project2 / ".venv" / "bin" / "python"
            if not python.is_file():
                raise TargetError(f"Project 2 has no virtual environment at {self._project2}")
            params = StdioServerParameters(
                command=str(python),
                args=["-m", "agent.mcp_server", "--repo", str(self._project2 / "demo_repo")],
                cwd=str(self._project2),
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
        for tool in (await self._session.list_tools()).tools:
            self._tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.input_schema,
                    },
                }
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def _execute(self, call: ToolCall) -> None:
        if call.name not in {t["function"]["name"] for t in self._tools}:
            call.ok, call.result = False, f"Unknown tool {call.name}; nothing was run."
            return
        assert self._session is not None
        with anyio.move_on_after(120) as scope:
            result = await self._session.call_tool(call.name, call.arguments)
        if scope.cancelled_caught:
            call.ok, call.result = False, "The tool did not answer within 120 s."
            return
        call.ok = not result.is_error
        call.result = "".join(getattr(block, "text", "") for block in result.content)[:2000]

    async def _chat(self, messages: list[dict[str, Any]], tools: bool) -> Any:
        return await self._ollama.chat(
            model=self._model,
            messages=messages,
            tools=self._tools if tools else None,
            options={"temperature": 0, "num_predict": 400},
        )

    async def _loop(self, messages: list[dict[str, Any]], calls: list[ToolCall]) -> list[int]:
        """Model turn, tool calls, repeat. Returns [input tokens, output tokens] and leaves
        the final answer as the last message."""
        tokens = [0, 0]
        for _ in range(self._max_steps):
            reply = await self._chat(messages, tools=True)
            tokens[0] += reply.prompt_eval_count or 0
            tokens[1] += reply.eval_count or 0
            content = reply.message.content or ""
            native = [
                ToolCall(name=c.function.name, arguments=dict(c.function.arguments or {}))
                for c in (reply.message.tool_calls or [])
            ]
            step_calls = native or _text_tool_calls(content)
            if not step_calls:
                messages.append({"role": "assistant", "content": content})
                return tokens
            messages.append(reply.message.model_dump(exclude_none=True))
            for call in step_calls:
                calls.append(call)  # before running it, so a hung call is still recorded
                await self._execute(call)
                messages.append(
                    {"role": "tool", "tool_name": call.name, "content": call.result}
                    if call.format == "native"
                    else {"role": "user", "content": f"Result of {call.name}:\n{call.result}"}
                )
        # Out of steps while still calling tools: one last turn, without tools, so the
        # model has to answer from what it has seen.
        reply = await self._chat(messages, tools=False)
        tokens[0] += reply.prompt_eval_count or 0
        tokens[1] += reply.eval_count or 0
        messages.append({"role": "assistant", "content": reply.message.content or ""})
        return tokens

    async def respond(self, case: Case) -> Response:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": case.input},
        ]
        calls: list[ToolCall] = []
        start = time.perf_counter()
        try:
            with anyio.fail_after(self._timeout_s):
                tokens_in, tokens_out = await self._loop(messages, calls)
        except TimeoutError:
            error = f"No answer within {self._timeout_s:.0f} s"
            return Response(
                case_id=case.id,
                error=error,
                tool_calls=calls,
                latency_ms=_elapsed_ms(start),
                model=self._model,
            )
        except (ollama.ResponseError, httpx.HTTPError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            return Response(
                case_id=case.id,
                error=error,
                tool_calls=calls,
                latency_ms=_elapsed_ms(start),
                model=self._model,
            )
        return Response(
            case_id=case.id,
            output=messages[-1]["content"],
            tool_calls=calls,
            latency_ms=_elapsed_ms(start),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            model=self._model,
        )
