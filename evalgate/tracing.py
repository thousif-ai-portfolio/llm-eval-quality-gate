"""OpenTelemetry traces in the OpenInference format that Arize Phoenix understands.

One trace per case: the case span holds the question, the answer and every check result,
with an LLM, retriever or tool span under it. Failed cases get an ERROR status, so in
Phoenix they can be filtered and opened to see what the system retrieved or called.
Traces go to an OTLP endpoint (e.g. http://127.0.0.1:6006/v1/traces).
Each run goes to its own Phoenix project, and spans get the latency recorded in the run,
so Phoenix shows how long each case really took.
"""

import json
import time
from contextlib import contextmanager

import httpx
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import Status, StatusCode

from evalgate.models import Case, CaseResult, Response

KIND = "openinference.span.kind"
_provider: TracerProvider | None = None


class TracingError(RuntimeError):
    pass


def setup(endpoint: str, project: str) -> trace.Tracer:
    global _provider
    try:
        httpx.get(endpoint, timeout=5)  # any HTTP reply means the collector is there
    except httpx.HTTPError as exc:
        raise TracingError(f"No trace collector at {endpoint}; is Phoenix running?") from exc
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    _provider = TracerProvider(resource=Resource({"openinference.project.name": project}))
    _provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return _provider.get_tracer("evalgate")


def shutdown() -> None:
    if _provider is not None:
        _provider.force_flush()
        _provider.shutdown()


@contextmanager
def _span(tracer: trace.Tracer, name: str, start: int, end: int):
    span = tracer.start_span(name, start_time=start)
    with trace.use_span(span, end_on_exit=False):
        yield span
    span.end(end_time=end)


def record_case(
    tracer: trace.Tracer,
    case: Case,
    response: Response,
    result: CaseResult,
    start: int | None = None,
) -> int:
    """Records one case starting at `start` (ns, default now) and returns its end time."""
    start = time.time_ns() if start is None else start
    end = start + int(response.latency_ms * 1_000_000)
    with _span(tracer, f"{case.id} [{case.category}]", start, end) as span:
        span.set_attribute(KIND, "CHAIN")
        span.set_attribute("input.value", case.input)
        span.set_attribute("output.value", response.output or response.error)
        span.set_attribute("metadata", json.dumps({"suite": case.suite, "category": case.category}))
        for check in result.checks:
            span.set_attribute(f"eval.{check.name}.passed", check.passed)
            if check.detail:
                span.set_attribute(f"eval.{check.name}.detail", check.detail)
        if case.suite == "rag":
            _rag_spans(tracer, case, response, start, end)
        else:
            _agent_spans(tracer, case, response, start, end)
        failed = [c.name for c in result.checks if not c.passed]
        if failed:
            span.set_status(Status(StatusCode.ERROR, f"failed: {', '.join(failed)}"))
        else:
            span.set_status(Status(StatusCode.OK))
    return end


def _rag_spans(tracer: trace.Tracer, case: Case, response: Response, start: int, end: int):
    with _span(tracer, "retrieve (cited sources)", start, start) as span:
        span.set_attribute(KIND, "RETRIEVER")
        span.set_attribute("input.value", case.input)
        for i, citation in enumerate(response.citations):
            prefix = f"retrieval.documents.{i}.document"
            span.set_attribute(f"{prefix}.content", citation.text)
            span.set_attribute(f"{prefix}.id", f"{citation.filename}#p{citation.page}")
            span.set_attribute(
                f"{prefix}.metadata", json.dumps({"file": citation.filename, "page": citation.page})
            )
    with _span(tracer, "generate answer", start, end) as span:
        _llm_attributes(span, case, response)


def _agent_spans(tracer: trace.Tracer, case: Case, response: Response, start: int, end: int):
    # Tool calls are recorded without their own timings, so they are shown at the start.
    with _span(tracer, "agent", start, end) as span:
        _llm_attributes(span, case, response)
        for call in response.tool_calls:
            with _span(tracer, f"tool: {call.name}", start, start) as tool:
                tool.set_attribute(KIND, "TOOL")
                tool.set_attribute("tool.name", call.name)
                tool.set_attribute("tool.parameters", json.dumps(call.arguments))
                tool.set_attribute("input.value", json.dumps(call.arguments))
                tool.set_attribute("output.value", call.result)
                tool.set_attribute("metadata", json.dumps({"format": call.format}))
                if call.ok is False:
                    tool.set_status(Status(StatusCode.ERROR, call.result[:200]))


def _llm_attributes(span, case: Case, response: Response) -> None:
    span.set_attribute(KIND, "LLM")
    span.set_attribute("llm.model_name", response.model)
    span.set_attribute("input.value", case.input)
    span.set_attribute("output.value", response.output)
    span.set_attribute("llm.token_count.prompt", response.input_tokens)
    span.set_attribute("llm.token_count.completion", response.output_tokens)
    span.set_attribute("llm.token_count.total", response.input_tokens + response.output_tokens)
    span.set_attribute("metadata.latency_ms", response.latency_ms)
