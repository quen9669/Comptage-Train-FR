"""Base DuckDB locale : journal de télémétrie et manifeste de la couche bronze.

DuckDB est un fichier unique, gratuit et très rapide en analytique : idéal
pour un VPS à quelques euros. Les schémas silver/gold viendront aux étapes 2 et 3.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS monitoring;
CREATE SCHEMA IF NOT EXISTS bronze_raw;

-- Une ligne par requête HTTP (y compris chaque nouvelle tentative).
CREATE TABLE IF NOT EXISTS monitoring.http_calls (
    run_id        VARCHAR,
    source        VARCHAR,
    url           VARCHAR,
    attempt       INTEGER,
    started_at    TIMESTAMPTZ,
    duration_ms   DOUBLE,
    http_status   INTEGER,      -- NULL si erreur réseau (timeout, DNS...)
    bytes         BIGINT,
    error         VARCHAR
);

-- Une ligne par source et par exécution : le résultat final de la collecte.
CREATE TABLE IF NOT EXISTS monitoring.ingestion_runs (
    run_id        VARCHAR,
    source        VARCHAR,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    outcome       VARCHAR,      -- new | unchanged | skipped | error
    http_status   INTEGER,
    attempts      INTEGER,
    bytes         BIGINT,
    sha256        VARCHAR,
    bronze_path   VARCHAR,
    message       VARCHAR
);

-- Manifeste des fichiers bruts : la couche bronze est en append-only.
CREATE TABLE IF NOT EXISTS bronze_raw.files (
    source        VARCHAR,
    ingested_at   TIMESTAMPTZ,
    sha256        VARCHAR,
    bytes         BIGINT,
    format        VARCHAR,
    path          VARCHAR,
    origin_url    VARCHAR
);
"""


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_SQL)
    return con


def last_sha256(con: duckdb.DuckDBPyConnection, source: str) -> str | None:
    row = con.execute(
        "SELECT sha256 FROM bronze_raw.files WHERE source = ? "
        "ORDER BY ingested_at DESC LIMIT 1",
        [source],
    ).fetchone()
    return row[0] if row else None
