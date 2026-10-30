"""SQLite history of every run, so quality can be followed over time ("when did the
refusal rate drop?") rather than only compared with the last run."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL,
    dataset TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    config TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    run_id TEXT NOT NULL REFERENCES runs(id),
    case_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    checks TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    PRIMARY KEY (run_id, case_id)
);
"""


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, meta: dict, summary: dict, results: list) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM results WHERE run_id = ?", (meta["run_id"],))
            conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    meta["run_id"],
                    meta["created_at"],
                    meta["label"],
                    meta["dataset"],
                    meta["fingerprint"],
                    json.dumps(meta["config"]),
                    json.dumps(summary),
                ),
            )
            conn.executemany(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        meta["run_id"],
                        r.case_id,
                        int(r.passed),
                        json.dumps([c.model_dump() for c in r.checks]),
                        r.latency_ms,
                    )
                    for r in results
                ],
            )

    def runs(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
            return [{**dict(row), "summary": json.loads(row["summary"])} for row in rows]

    def case_history(self, case_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT runs.created_at, runs.label, results.passed FROM results "
                "JOIN runs ON runs.id = results.run_id WHERE case_id = ? "
                "ORDER BY runs.created_at",
                (case_id,),
            )
            return [dict(row) for row in rows]
