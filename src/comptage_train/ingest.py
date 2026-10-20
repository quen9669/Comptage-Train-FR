"""Orchestration de la collecte : une exécution = un run_id, toutes sources."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx

from . import bronze, warehouse
from .connectors import SourceNotReady, build_request
from .http_client import CallRecord, FetchError, fetch
from .settings import Source


@dataclass
class SourceOutcome:
    source: str
    outcome: str
    message: str = ""


def run_ingestion(
    sources: list[Source],
    data_root: Path,
    client: httpx.Client,
    *,
    schedule: str | None = None,
    sleep=None,
) -> list[SourceOutcome]:
    run_id = uuid.uuid4().hex[:12]
    con = warehouse.connect(data_root / "warehouse.duckdb")
    results = []
    try:
        for source in sources:
            if not source.enabled or (schedule and source.schedule != schedule):
                continue
            results.append(_ingest_one(con, client, run_id, source, data_root, sleep))
    finally:
        con.close()
    return results


def _ingest_one(con, client, run_id, source, data_root, sleep) -> SourceOutcome:
    started = datetime.now(timezone.utc)
    log = dict(run_id=run_id, source=source.name, started_at=started, http_status=None,
               attempts=0, bytes=None, sha256=None, bronze_path=None)

    def record_call(call: CallRecord) -> None:
        con.execute(
            "INSERT INTO monitoring.http_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, source.name, call.url, call.attempt, call.started_at,
             call.duration_ms, call.http_status, call.bytes, call.error],
        )

    try:
        spec = build_request(source)
    except SourceNotReady as exc:
        return _finish(con, log, "skipped", str(exc))

    try:
        kwargs = {"sleep": sleep} if sleep else {}
        result = fetch(client, spec.url, params=spec.params, headers=spec.headers,
                       on_call=record_call, **kwargs)
    except FetchError as exc:
        log.update(http_status=exc.status, attempts=exc.attempts)
        return _finish(con, log, "error", str(exc))

    digest = bronze.sha256(result.content)
    log.update(http_status=result.status, attempts=result.attempts,
               bytes=len(result.content), sha256=digest)
    if digest == warehouse.last_sha256(con, source.name):
        return _finish(con, log, "unchanged", "contenu identique au dernier dépôt")

    path = bronze.bronze_path(data_root, source.name, started, digest, spec.file_format)
    bronze.write_immutable(path, result.content)
    con.execute(
        "INSERT INTO bronze_raw.files VALUES (?, ?, ?, ?, ?, ?, ?)",
        [source.name, started, digest, len(result.content), spec.file_format,
         str(path.relative_to(data_root)), spec.url],
    )
    log["bronze_path"] = str(path.relative_to(data_root))
    return _finish(con, log, "new", "")


def _finish(con: duckdb.DuckDBPyConnection, log: dict, outcome: str, message: str) -> SourceOutcome:
    con.execute(
        "INSERT INTO monitoring.ingestion_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [log["run_id"], log["source"], log["started_at"], datetime.now(timezone.utc),
         outcome, log["http_status"], log["attempts"], log["bytes"], log["sha256"],
         log["bronze_path"], message],
    )
    return SourceOutcome(log["source"], outcome, message)
