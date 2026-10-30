"""Runs cases against a target, scores them and saves everything a run needs to be
re-scored, compared and gated later:

    runs/<name>/meta.json          what was tested: dataset version, target settings
    runs/<name>/responses.jsonl    what the system answered or called (recorded)
    runs/<name>/judgments.jsonl    the judge's verdicts (cached, so CI needs no model)
    runs/<name>/results.jsonl      every check for every case
    runs/<name>/summary.json       the metrics
"""

import json
import os
import subprocess
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import anyio

from evalgate import dataset, metrics, tracing
from evalgate.judge import Judge
from evalgate.models import Case, CaseResult, Check, Response
from evalgate.scorers import score
from evalgate.store import History
from evalgate.targets import AgentTarget, RagTarget, Target

ROOT = dataset.ROOT
HISTORY = ROOT / "data" / "history.db"


def load_config(path: Path) -> dict:
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    config.setdefault("label", path.stem)
    return config


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env support for the Project 1 test account (never committed)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _git_sha(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip()
    except OSError:
        return ""


def make_target(suite: str, config: dict) -> Target:
    if suite == "rag":
        rag = config["rag"]
        load_env()
        return RagTarget(
            url=os.environ.get("EVAL_RAG_URL", rag.get("url", "http://localhost:8000")),
            username=os.environ.get("EVAL_RAG_USER", ""),
            password=os.environ.get("EVAL_RAG_PASSWORD", ""),
            fixtures=ROOT / "datasets" / config["dataset"] / "fixtures",
            mode=rag.get("mode") or None,
            top_k=rag.get("top_k") or None,
        )
    agent = config["agent"]
    return AgentTarget(
        project2=(ROOT / agent["project2"]).resolve(),
        model=agent["model"],
        system_prompt=(ROOT / agent["prompt"]).read_text(encoding="utf-8"),
        max_steps=agent.get("max_steps", 3),
    )


def score_case(case: Case, response: Response, judge: Judge | None) -> CaseResult:
    """Rule checks, plus the judge's checks for answerable RAG questions (skipped when
    judge is None, e.g. for live progress lines before the judge runs)."""
    checks = score(case, response)
    if judge and case.category == "answerable" and not response.error:
        if not all(c.passed for c in checks if c.name == "answered"):
            # "I don't know" to an answerable question is wrong; no judge needed.
            checks.append(Check(name="correct", passed=False, detail="said it does not know"))
        else:
            reference = case.expect.get("reference", "")
            correct = judge.correctness(case.input, reference, response.output)
            checks.append(Check(name="correct", passed=correct.passed, detail=correct.reason))
            faithful = judge.faithfulness(response.output, [c.text for c in response.citations])
            checks.append(Check(name="faithful", passed=faithful.passed, detail=faithful.reason))
    return CaseResult(
        case_id=case.id,
        suite=case.suite,
        category=case.category,
        checks=checks,
        latency_ms=response.latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


def _read_jsonl(path: Path, model):
    if not path.is_file():
        return []
    return [
        model.model_validate_json(line) for line in path.read_text("utf-8").splitlines() if line
    ]


def _write_jsonl(path: Path, items) -> None:
    path.write_text("".join(i.model_dump_json() + "\n" for i in items), "utf-8", newline="\n")


async def _collect(cases: list[Case], target: Target, on_done) -> list[Response]:
    responses = []
    async with target:
        for case in cases:
            try:
                response = await target.respond(case)
            except Exception as exc:  # one broken case must not lose an hour-long run
                response = Response(case_id=case.id, error=f"{type(exc).__name__}: {exc}")
            responses.append(response)
            on_done(case, response)
    return responses


def run(
    config: dict,
    suite: str,
    out: Path,
    only: list[str] | None = None,
    progress=print,
) -> dict:
    """Runs one suite live and merges its responses into the run folder `out`. The judge
    runs once at the end rather than between answers, so Ollama doesn't swap models on a
    small machine for every question."""
    cases = dataset.load(config["dataset"], suites=(suite,))
    if only:
        cases = [c for c in cases if c.id in only]
    # Cases that share fixture documents run together, so each is uploaded once.
    cases.sort(key=lambda c: (tuple(c.setup), c.id))
    out.mkdir(parents=True, exist_ok=True)

    def done(case: Case, response: Response) -> None:
        result = score_case(case, response, None)
        mark = "pass" if result.passed else "FAIL"
        failed = ", ".join(c.name for c in result.checks if not c.passed)
        progress(f"{case.id:<10} {mark} {response.latency_ms / 1000:6.1f}s  {failed}")

    new = anyio.run(_collect, cases, make_target(suite, config), done)

    previous = {r.case_id: r for r in _read_jsonl(out / "responses.jsonl", Response)}
    previous.update({r.case_id: r for r in new})
    _write_jsonl(out / "responses.jsonl", sorted(previous.values(), key=lambda r: r.case_id))
    meta = _meta(config, out)
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", "utf-8", newline="\n")
    return rescore(out, offline=False)


def _meta(config: dict, out: Path) -> dict:
    meta_file = out / "meta.json"
    meta = json.loads(meta_file.read_text("utf-8")) if meta_file.is_file() else {}
    manifest = dataset.verify(config["dataset"])
    project1 = ROOT / config.get("rag", {}).get("project1", "../grounded-ai-knowledge-assistant")
    project2 = ROOT / config.get("agent", {}).get("project2", "../github-issue-coding-agent")
    return {
        "run_id": meta.get("run_id", uuid.uuid4().hex[:12]),
        "created_at": meta.get("created_at", datetime.now(UTC).isoformat(timespec="seconds")),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "label": config["label"],
        "dataset": config["dataset"],
        "fingerprint": manifest["fingerprint"],
        "config": config,
        "versions": {
            "project1": _git_sha(project1),
            "project2": _git_sha(project2),
            "evalgate": _git_sha(ROOT),
        },
    }


def rescore(out: Path, offline: bool = True, history: Path | None = HISTORY) -> dict:
    """Re-scores recorded responses with the current checks. Offline (the CI mode) it uses
    only cached judge verdicts, so no model is needed."""
    meta = json.loads((out / "meta.json").read_text("utf-8"))
    config = meta["config"]
    responses = {r.case_id: r for r in _read_jsonl(out / "responses.jsonl", Response)}
    cases = [c for c in dataset.load(config["dataset"]) if c.id in responses]
    judge = Judge(
        config["judge"]["correctness_model"],
        out / "judgments.jsonl",
        offline=offline,
        faithfulness_model=config["judge"]["faithfulness_model"],
    )
    results = [score_case(c, responses[c.id], judge) for c in cases]
    _write_jsonl(out / "results.jsonl", results)
    pricing = config.get("pricing", {})
    summary = {
        "label": meta["label"],
        "dataset": meta["dataset"],
        "fingerprint": meta["fingerprint"],
        **metrics.summarise(results, pricing),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8", newline="\n")
    if history is not None:
        History(history).record(meta, summary, results)
    return summary


def trace_run(out: Path, endpoint: str) -> int:
    """Sends a recorded run to Phoenix as one trace per case, with no model needed. Used
    to inspect a run after the fact, e.g. to diagnose a regression the gate caught."""
    meta = json.loads((out / "meta.json").read_text("utf-8"))
    responses = {r.case_id: r for r in _read_jsonl(out / "responses.jsonl", Response)}
    results = {r.case_id: r for r in load_results(out)}
    cases = [c for c in dataset.load(meta["dataset"]) if c.id in results]
    tracer = tracing.setup(endpoint, project=f"evalgate-{meta['label']}")
    # Cases are laid end to end with their recorded latencies, finishing now.
    start = time.time_ns() - sum(int(responses[c.id].latency_ms * 1_000_000) for c in cases)
    for case in cases:
        start = tracing.record_case(tracer, case, responses[case.id], results[case.id], start)
    tracing.shutdown()
    return len(cases)


def load_results(out: Path) -> list[CaseResult]:
    return _read_jsonl(out / "results.jsonl", CaseResult)
