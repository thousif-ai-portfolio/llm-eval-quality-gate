"""Versioned evaluation datasets.

Each version lives in datasets/<version>/ with a manifest of SHA-256 hashes. Loading
refuses a dataset whose files no longer match their manifest, so results are only ever
compared on the exact questions they were measured on. Changing a question means
freezing a new version, never editing an old one.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from evalgate.models import Case

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "datasets"
MANIFEST = "manifest.json"


class DatasetError(ValueError):
    pass


def _data_files(folder: Path) -> list[Path]:
    """The files a version is made of: its cases and any fixture documents."""
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.name != MANIFEST)


def _sha256(path: Path) -> str:
    # Hash the text with LF endings, so a Windows checkout hashes the same as Linux CI.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def freeze(version: str, base: Path = DATASETS) -> dict:
    folder = base / version
    # A manifest copied from the previous version is fine; this version's own is not.
    existing = folder / MANIFEST
    if existing.is_file() and json.loads(existing.read_text("utf-8"))["version"] == version:
        raise DatasetError(f"Dataset {version} is already frozen; freeze a new version instead.")
    files = {p.relative_to(folder).as_posix(): _sha256(p) for p in _data_files(folder)}
    manifest = {
        "version": version,
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": files,
        "fingerprint": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12],
    }
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(version: str, base: Path = DATASETS) -> dict:
    folder = base / version
    manifest_path = folder / MANIFEST
    if not manifest_path.is_file():
        raise DatasetError(f"Dataset {version} has no manifest; run `evalgate dataset freeze`.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = {p.relative_to(folder).as_posix(): _sha256(p) for p in _data_files(folder)}
    if actual != manifest["files"]:
        changed = sorted(set(actual.items()) ^ set(manifest["files"].items()))
        names = sorted({name for name, _ in changed})
        raise DatasetError(
            f"Dataset {version} changed since it was frozen ({', '.join(names)}). "
            "Freeze a new version instead of editing this one."
        )
    return manifest


def load(version: str, base: Path = DATASETS, suites: tuple[str, ...] = ()) -> list[Case]:
    verify(version, base)
    cases = []
    for path in sorted((base / version).glob("*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                cases.append(Case.model_validate_json(line))
            except ValueError as exc:
                raise DatasetError(f"{path.name}:{number}: {exc}") from exc
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise DatasetError(f"Duplicate case ids in dataset {version}.")
    return [case for case in cases if not suites or case.suite in suites]
