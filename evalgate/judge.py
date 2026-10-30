"""Model-graded checks for answerable RAG questions: correctness against a reference
answer, and faithfulness (no claim beyond what the cited sources say).

A judge is only worth its agreement with a person, so `calibrate` measures it against
hand-labelled examples (datasets/judge_labels.jsonl). Verdicts are cached per run in
judgments.jsonl, which lets CI re-score a recorded run without running any model.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

import ollama
from pydantic import BaseModel, Field, ValidationError

# v1 judged the raw answer: a 3B judge treated "[1]" and "According to source [1]" as
# unsupported claims. v2 cleaned the answer first. v3 makes faithfulness quote-then-verify.
PROMPT_VERSION = "v3"

CORRECTNESS = """You grade answers from a documentation assistant.

Question: {question}
Reference answer (written by a person): {reference}
Assistant's answer: {answer}

Does the assistant's answer give the same key fact as the reference?
- "correct": the key fact is there. Different wording or extra true detail is fine.
- "incorrect": the key fact is missing, different or contradicted, or the assistant says
  it does not know.

Examples:
Reference "Run atlas init." / Answer "Use the command atlas init to create it." -> correct
Reference "384 dimensions." / Answer "It produces 768-dimensional vectors." -> incorrect
Reference "k = 60." / Answer "I don't know." -> incorrect

First write the key fact you found (or "none"), then the verdict."""

# Faithfulness is quote-then-verify: a 3B model asked "is anything unsupported?" almost
# always finds something (25% on correct answers in the first baseline). Copying the
# supporting sentence is a task small models do well, and code then checks the quote
# really is in the sources and that no number in the answer is missing from them.
FAITHFULNESS = """Here are some sources and the numbered sentences of an answer.

Sources:
{sources}

Answer sentences:
{sentences}

For each answer sentence, copy the one sentence from the sources that states the same
fact, word for word. If no sentence in the sources states it, write NONE."""

CITATION = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")
ATTRIBUTION = re.compile(
    r"\b(according to|as stated in|based on) (the )?(sources?|release notes|documents?|manual)"
    r"( \d+)?,?\s*",
    re.IGNORECASE,
)


def clean_answer(answer: str) -> str:
    """Removes citation markers and "According to source [1]," so the judge only sees the
    claims themselves."""
    return ATTRIBUTION.sub("", CITATION.sub("", answer)).strip()


class Verdict(BaseModel):
    reason: str
    verdict: Literal["correct", "incorrect", "supported", "unsupported"]

    @property
    def passed(self) -> bool:
        return self.verdict in ("correct", "supported")


class CorrectnessReply(BaseModel):
    key_fact_found: str = Field(description="The key fact found in the answer, or none")
    verdict: Literal["correct", "incorrect"]


class Support(BaseModel):
    sentence: int
    source_quote: str = Field(description="The source sentence, word for word, or NONE")


class FaithfulnessReply(BaseModel):
    support: list[Support]


STOPWORDS = set(
    "a an the is are was were be been of to in on for and or but it its this that these "
    "those with as by at from which what when where how why if then so than can you your "
    "they their there also only just more most very".split()
)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s./~@-]", " ", text.lower())).strip()


def _content_words(text: str) -> set[str]:
    return {w for w in _normalise(text).split() if len(w) > 2 and w not in STOPWORDS}


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 3]


def verify_support(sentences: list[str], quotes: dict[int, str], sources: str) -> str:
    """Returns "" if every sentence is backed by a real quote, else why not."""
    source_text = _normalise(sources)
    for number, sentence in enumerate(sentences, 1):
        quote = _normalise(quotes.get(number, ""))
        if not quote or quote == "none":
            return f"no source states: {sentence}"
        match = SequenceMatcher(None, quote, source_text, autojunk=False).find_longest_match()
        if match.size < 0.8 * len(quote):
            return f"quoted text is not in the sources: {quotes[number][:80]}"
        figures = re.findall(r"\d+(?:\.\d+)?", sentence)
        for figure in figures:
            if not re.search(rf"(?<![\d.]){re.escape(figure)}(?![\d])", source_text):
                return f"number {figure} is not in the sources: {sentence}"
        # The quote must be about the sentence: a shared word stem or number, which allows
        # paraphrase ("retries 5 times" / "5 attempts") but not an unrelated real sentence.
        stems = {w[:5] for w in _content_words(sentence)}
        if stems and not (stems & {w[:5] for w in _content_words(quote)}):
            if not set(figures) & set(re.findall(r"\d+(?:\.\d+)?", quote)):
                return f"quote does not match the sentence: {sentence}"
    return ""


def decide(reply: BaseModel, **inputs: str) -> Verdict:
    """Turns the model's structured reply into a verdict. For faithfulness the decision is
    made here, in code, from the quotes the model copied."""
    if isinstance(reply, FaithfulnessReply):
        sentences = split_sentences(inputs["answer"]) or [inputs["answer"]]
        quotes = {s.sentence: s.source_quote for s in reply.support}
        problem = verify_support(sentences, quotes, inputs["sources"])
        return Verdict(
            verdict="unsupported" if problem else "supported",
            reason=problem or "every sentence is quoted from the sources",
        )
    assert isinstance(reply, CorrectnessReply)
    return Verdict(verdict=reply.verdict, reason=reply.key_fact_found)


class JudgeCacheMiss(KeyError):
    pass


class Judge:
    def __init__(
        self,
        model: str,
        cache_file: Path,
        offline: bool = False,
        faithfulness_model: str | None = None,
    ) -> None:
        """`model` judges correctness; `faithfulness_model` (default: the same) judges
        faithfulness. Calibration picked a different model for each."""
        self.model, self._cache_file, self._offline = model, cache_file, offline
        self.models = {"correctness": model, "faithfulness": faithfulness_model or model}
        self._cache: dict[str, Verdict] = {}
        if cache_file.is_file():
            for line in cache_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                self._cache[entry["key"]] = Verdict.model_validate(entry["verdict"])
        self._client = None if offline else ollama.Client()

    def _key(self, kind: str, **inputs: str) -> str:
        payload = json.dumps([kind, self.models[kind], PROMPT_VERSION, inputs], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _ask(self, kind: str, prompt: str, schema: type[BaseModel], **inputs: str) -> Verdict:
        key = self._key(kind, **inputs)
        if key in self._cache:
            return self._cache[key]
        if self._offline or self._client is None:
            raise JudgeCacheMiss(f"No recorded {kind} verdict; re-run the evaluation with a model.")
        reply = self._client.chat(
            model=self.models[kind],
            messages=[{"role": "user", "content": prompt}],
            format=schema.model_json_schema(),
            # Quoting sources takes more tokens than a verdict.
            options={"temperature": 0, "num_predict": 800},
        )
        try:
            verdict = decide(schema.model_validate_json(reply.message.content or "{}"), **inputs)
        except ValidationError:
            # A reply cut off or malformed: count it as a failure rather than crash the run.
            negative = "incorrect" if kind == "correctness" else "unsupported"
            verdict = Verdict(verdict=negative, reason="judge reply was not valid JSON")
        self._cache[key] = verdict
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_file.open("a", encoding="utf-8", newline="\n") as f:
            entry = {"key": key, "kind": kind, "verdict": verdict.model_dump()}
            f.write(json.dumps(entry) + "\n")
        return verdict

    def correctness(self, question: str, reference: str, answer: str) -> Verdict:
        answer = clean_answer(answer)
        prompt = CORRECTNESS.format(question=question, reference=reference, answer=answer)
        return self._ask(
            "correctness",
            prompt,
            CorrectnessReply,
            question=question,
            reference=reference,
            answer=answer,
        )

    def faithfulness(self, answer: str, sources: Iterable[str]) -> Verdict:
        answer = clean_answer(answer)
        joined = "\n---\n".join(sources) or "(no sources)"
        numbered = "\n".join(f"{n}. {s}" for n, s in enumerate(split_sentences(answer), 1))
        prompt = FAITHFULNESS.format(sources=joined, sentences=numbered or f"1. {answer}")
        return self._ask("faithfulness", prompt, FaithfulnessReply, answer=answer, sources=joined)


# --- Calibration -------------------------------------------------------------------------


class Label(BaseModel):
    """One hand-labelled example: what a person decided, to compare the judge with."""

    id: str
    kind: Literal["correctness", "faithfulness"]
    question: str = ""
    reference: str = ""
    sources: list[str] = []
    answer: str
    human: bool  # True = correct / supported
    note: str = ""


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float:
    """Agreement beyond chance between two raters; 1 is perfect, 0 is chance."""
    n = len(pairs)
    if not n:
        return 0.0
    observed = sum(a == b for a, b in pairs) / n
    p_a = sum(a for a, _ in pairs) / n
    p_b = sum(b for _, b in pairs) / n
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    return 1.0 if expected == 1 else round((observed - expected) / (1 - expected), 3)


def calibrate(judge: Judge, labels: list[Label]) -> dict:
    rows = []
    for label in labels:
        if label.kind == "correctness":
            verdict = judge.correctness(label.question, label.reference, label.answer)
        else:
            verdict = judge.faithfulness(label.answer, label.sources)
        rows.append(
            {
                "id": label.id,
                "kind": label.kind,
                "human": label.human,
                "judge": verdict.passed,
                "reason": verdict.reason,
            }
        )
    result: dict = {"model": judge.model, "prompt_version": PROMPT_VERSION, "kinds": {}}
    for kind in ("correctness", "faithfulness", "all"):
        subset = [r for r in rows if kind == "all" or r["kind"] == kind]
        pairs = [(r["human"], r["judge"]) for r in subset]
        result["kinds"][kind] = {
            "n": len(pairs),
            "agreement": round(sum(a == b for a, b in pairs) / len(pairs), 3) if pairs else 0.0,
            "kappa": cohen_kappa(pairs),
            # The costly mistake for a quality gate: passing an answer a person failed.
            "false_passes": sum(1 for h, j in pairs if j and not h),
            "false_fails": sum(1 for h, j in pairs if h and not j),
        }
    result["disagreements"] = [r for r in rows if r["human"] != r["judge"]]
    return result
