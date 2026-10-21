"""Orchestration de la collecte : une exécution = un run_id, toutes sources."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import bronze, warehouse
from .connectors import SourceNotReady, build_request, catalog_url, parse_version
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
    run_started = datetime.now(timezone.utc)
    run_id = new_run_id(run_started)
    last_deposits = warehouse.last_deposit_by_source(data_root)
    log = warehouse.RunLog()
    results = []
    try:
        for source in sources:
            if not source.enabled or (schedule and source.schedule != schedule):
                continue
            results.append(_ingest_one(log, client, run_id, source, data_root,
                                       last_deposits.get(source.name, (None, None)), sleep))
    finally:
        # Même si une erreur inattendue interrompt la boucle, on garde la trace
        log.flush(data_root, run_id, f"{run_started:%Y-%m-%d}")
    return results


def _ingest_one(log, client, run_id, source, data_root, previous, sleep) -> SourceOutcome:
    previous_sha, previous_version = previous
    started = datetime.now(timezone.utc)
    run = dict(run_id=run_id, source=source.name, started_at=started, http_status=None,
               attempts=0, bytes=None, sha256=None, bronze_path=None, source_version=None)

    def record_call(call: CallRecord) -> None:
        log.add("http_calls", [run_id, source.name, call.url, call.attempt, call.started_at,
                               call.duration_ms, call.http_status, call.bytes, call.error])

    try:
        spec = build_request(source)
    except SourceNotReady as exc:
        return _finish(log, run, "skipped", str(exc))

    kwargs = {"sleep": sleep} if sleep else {}
    meta_url = catalog_url(source)
    if meta_url:
        # Petite requête de métadonnées d'abord : si le producteur n'a pas
        # retraité le jeu, on évite de retélécharger plusieurs Mo pour rien.
        try:
            meta = fetch(client, meta_url, on_call=record_call, **kwargs)
        except FetchError as exc:
            run.update(http_status=exc.status, attempts=exc.attempts)
            return _finish(log, run, "error", f"métadonnées : {exc}")
        run["source_version"] = parse_version(meta.content)
        if run["source_version"] and run["source_version"] == previous_version:
            run.update(http_status=meta.status, attempts=meta.attempts)
            return _finish(log, run, "unchanged",
                           f"version producteur inchangée ({previous_version})")

    try:
        result = fetch(client, spec.url, params=spec.params, headers=spec.headers,
                       on_call=record_call, **kwargs)
    except FetchError as exc:
        run.update(http_status=exc.status, attempts=exc.attempts)
        return _finish(log, run, "error", str(exc))

    digest = bronze.sha256(result.content)
    run.update(http_status=result.status, attempts=result.attempts,
               bytes=len(result.content), sha256=digest)
    if digest == previous_sha:
        return _finish(log, run, "unchanged", "contenu identique au dernier dépôt")

    stored, extension = bronze.encode(result.content, spec.file_format)
    path = bronze.bronze_path(data_root, source.name, started, digest, extension)
    bronze.write_immutable(path, stored)
    relative = path.relative_to(data_root).as_posix()
    log.add("bronze_files", [source.name, started, digest, len(result.content),
                             extension, relative, spec.url, run["source_version"]])
    run["bronze_path"] = relative
    return _finish(log, run, "new", "")


def _finish(log: warehouse.RunLog, run: dict, outcome: str, message: str) -> SourceOutcome:
    log.add("ingestion_runs", [run["run_id"], run["source"], run["started_at"],
                               datetime.now(timezone.utc), outcome, run["http_status"],
                               run["attempts"], run["bytes"], run["sha256"],
                               run["bronze_path"], message, run["source_version"]])
    return SourceOutcome(run["source"], outcome, message)


def new_run_id(started: datetime) -> str:
    return started.strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:6]
