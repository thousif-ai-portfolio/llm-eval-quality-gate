from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from evalgate import tracing
from evalgate.models import CaseResult, Check
from tests.conftest import agent_case, answer, calls, rag_case


def tracer_and_spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def result(case, *checks):
    return CaseResult(
        case_id=case.id, suite=case.suite, category=case.category, checks=list(checks)
    )


def test_rag_case_trace_holds_the_answer_sources_and_failed_checks():
    tracer, exporter = tracer_and_spans()
    case = rag_case()
    response = answer("Run atlas init [1].", ("manual.pdf", 3))
    tracing.record_case(
        tracer, case, response, result(case, Check(name="cites_source", passed=False, detail="p3"))
    )
    spans = {s.name: s for s in exporter.get_finished_spans()}
    root = spans["rag-answerable [answerable]"]
    assert root.status.status_code is StatusCode.ERROR
    assert root.attributes["eval.cites_source.passed"] is False
    retriever = spans["retrieve (cited sources)"]
    assert retriever.attributes["openinference.span.kind"] == "RETRIEVER"
    assert retriever.attributes["retrieval.documents.0.document.id"] == "manual.pdf#p3"
    assert spans["generate answer"].attributes["llm.token_count.prompt"] == 800


def test_spans_take_the_recorded_latency_and_cases_follow_each_other():
    tracer, exporter = tracer_and_spans()
    case = rag_case()
    response = answer("Run atlas init [1].", ("manual.pdf", 3))
    response.latency_ms = 2500
    end = tracing.record_case(tracer, case, response, result(case), start=1_000)
    assert end == 1_000 + 2_500_000_000
    root = {s.name: s for s in exporter.get_finished_spans()}["rag-answerable [answerable]"]
    assert (root.start_time, root.end_time) == (1_000, end)


def test_agent_trace_has_a_span_per_tool_call():
    tracer, exporter = tracer_and_spans()
    case = agent_case()
    response = calls(
        ("search_code", {"query": "x"}), ("read_source", {"path": "../.env"}, "native", False)
    )
    tracing.record_case(
        tracer, case, response, result(case, Check(name="tool_choice", passed=True))
    )
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["agent-tool_use [tool_use]"].status.status_code is StatusCode.OK
    assert spans["tool: read_source"].status.status_code is StatusCode.ERROR
    assert spans["tool: search_code"].attributes["tool.parameters"] == '{"query": "x"}'


def test_tracing_is_off_without_an_endpoint():
    assert tracing.setup(None, "p").__class__.__name__ == "NoOpTracer"
