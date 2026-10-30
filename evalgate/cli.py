"""Command line: `python -m evalgate <command>`.

run        run a suite live against Project 1 or 2 and record it
rescore    re-score a recorded run with the current checks (no model needed)
gate       pass or fail a run against the baseline and gate.toml
compare    several runs side by side (prompt or model comparisons)
baseline   accept a run as the new baseline
calibrate  measure the judge against hand-labelled examples
history    scores over time, from the SQLite history
dataset    freeze or verify a dataset version
setup-rag-user   create the Project 1 test account the RAG suite signs in with
"""

import argparse
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

import httpx

from evalgate import dataset, report, runner
from evalgate.gate import evaluate, load_gate
from evalgate.judge import Judge, JudgeCacheMiss, Label, calibrate
from evalgate.store import History
from evalgate.targets import TargetError

ROOT = dataset.ROOT


def _summary(run_dir: Path) -> dict:
    return json.loads((run_dir / "summary.json").read_text("utf-8"))


def cmd_run(args) -> int:
    config = runner.load_config(args.config)
    out = args.out or ROOT / "runs" / config["label"]
    suites = ["rag", "agent"] if args.suite == "all" else [args.suite]
    for suite in suites:
        print(f"== {suite} suite: {config['label']} ==")
        summary = runner.run(config, suite, out, args.only)
    print(f"\n{summary['passed']}/{summary['cases']} cases passed. Saved to {out}")
    return 0


def cmd_trace(args) -> int:
    count = runner.trace_run(args.run, args.endpoint)
    print(f"Sent {count} case traces to {args.endpoint}.")
    return 0


def cmd_rescore(args) -> int:
    summary = runner.rescore(args.run, offline=not args.online)
    print(f"{summary['passed']}/{summary['cases']} cases passed.")
    return 0


def cmd_gate(args) -> int:
    summary = _summary(args.run)
    baseline = json.loads(args.baseline.read_text("utf-8")) if args.baseline.is_file() else None
    result = evaluate(summary, load_gate(args.gate), baseline)
    text = report.gate_report(summary, result, runner.load_results(args.run))
    report.write(args.run / "report.md", text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0 if result.passed else 1


def cmd_compare(args) -> int:
    print(report.compare_report([_summary(run) for run in args.runs]))
    return 0


def cmd_baseline(args) -> int:
    target = ROOT / "baselines" / "main.json"
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(args.run / "summary.json", target)
    print(f"Baseline is now {args.run.name} ({target}).")
    return 0


def cmd_calibrate(args) -> int:
    lines = args.labels.read_text("utf-8").splitlines()
    labels = [Label.model_validate_json(line) for line in lines if line.strip()]
    cache = ROOT / "calibration" / f"judgments-{args.model.replace(':', '-')}.jsonl"
    result = calibrate(Judge(args.model, cache), labels)
    out = ROOT / "calibration" / f"{args.model.replace(':', '-')}.json"
    out.write_text(json.dumps(result, indent=2) + "\n", "utf-8", newline="\n")
    for kind, stats in result["kinds"].items():
        print(
            f"{kind:<13} n={stats['n']:<3} agreement={stats['agreement']:.0%} "
            f"kappa={stats['kappa']:.2f} false passes={stats['false_passes']} "
            f"false fails={stats['false_fails']}"
        )
    return 0


def cmd_history(args) -> int:
    history = History(runner.HISTORY)
    if args.case:
        for row in history.case_history(args.case):
            print(f"{row['created_at']}  {row['label']:<24} {'PASS' if row['passed'] else 'FAIL'}")
        return 0
    for row in history.runs(args.limit):
        s = row["summary"]
        print(
            f"{row['created_at']}  {row['label']:<24} {s['passed']:>3}/{s['cases']:<3} "
            f"critical failures: {len(s['critical_failures'])}"
        )
    return 0


def cmd_dataset(args) -> int:
    if args.action == "freeze":
        manifest = dataset.freeze(args.version)
    else:
        manifest = dataset.verify(args.version)
    cases = dataset.load(args.version)
    print(f"Dataset {args.version}: {len(cases)} cases, fingerprint {manifest['fingerprint']}")
    return 0


def cmd_setup_rag_user(args) -> int:
    """Creates a dedicated local test account on Project 1 and stores it in .env."""
    env = ROOT / ".env"
    runner.load_env(env)
    if os.environ.get("EVAL_RAG_USER"):
        print("A test account is already configured in .env.")
        return 0
    username, password = "evalgate_bot", secrets.token_urlsafe(18)
    reply = httpx.post(
        f"{args.url}/api/auth/register",
        json={"username": username, "password": password},
        timeout=30,
    )
    if reply.status_code not in (200, 201):
        print(f"Could not create the account: {reply.status_code} {reply.text}")
        return 1
    with env.open("a", encoding="utf-8") as f:
        f.write(
            f"EVAL_RAG_URL={args.url}\nEVAL_RAG_USER={username}\nEVAL_RAG_PASSWORD={password}\n"
        )
    print(f"Created test account {username}; credentials saved to .env (not committed).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evalgate", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run")
    p.add_argument("--config", type=Path, default=ROOT / "configs" / "baseline.toml")
    p.add_argument("--suite", choices=["rag", "agent", "all"], default="all")
    p.add_argument("--only", nargs="*", help="Only these case ids")
    p.add_argument("--out", type=Path, help="Run folder (default runs/<label>)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("trace", help="Send a recorded run to Phoenix")
    p.add_argument("run", type=Path)
    p.add_argument("--endpoint", default="http://127.0.0.1:6006/v1/traces")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("rescore")
    p.add_argument("run", type=Path)
    p.add_argument("--online", action="store_true", help="Allow new judge calls")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("gate")
    p.add_argument("run", type=Path)
    p.add_argument("--baseline", type=Path, default=ROOT / "baselines" / "main.json")
    p.add_argument("--gate", type=Path, default=ROOT / "gate.toml")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("compare")
    p.add_argument("runs", type=Path, nargs="+")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("baseline")
    p.add_argument("run", type=Path)
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("calibrate")
    p.add_argument("--model", required=True)
    p.add_argument("--labels", type=Path, default=ROOT / "datasets" / "judge_labels.jsonl")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("history")
    p.add_argument("--case")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("dataset")
    p.add_argument("action", choices=["freeze", "verify"])
    p.add_argument("version")
    p.set_defaults(func=cmd_dataset)

    p = sub.add_parser("setup-rag-user")
    p.add_argument("--url", default="http://localhost:8000")
    p.set_defaults(func=cmd_setup_rag_user)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (dataset.DatasetError, JudgeCacheMiss, TargetError) as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
