"""Journaux de collecte en Parquet (ajout seul) et lecture via DuckDB.

Chaque exécution écrit ses propres fichiers, sans jamais modifier les
précédents :

    data/logs/http_calls/ingest_date=AAAA-MM-JJ/<run_id>.parquet
    data/logs/ingestion_runs/ingest_date=AAAA-MM-JJ/<run_id>.parquet
    data/logs/bronze_files/ingest_date=AAAA-MM-JJ/<run_id>.parquet

Pourquoi des fichiers plutôt qu'une base DuckDB persistante ? Un historique
en ajout seul se versionne proprement dans Git (GitHub Actions), se copie
tel quel sur un VPS, et on peut lire pendant qu'une collecte écrit.
DuckDB sert de moteur de requête : `connect()` expose des vues sur ces fichiers.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

# Nom de table -> (schéma de vue DuckDB, colonnes)
TABLES: dict[str, tuple[str, str]] = {
    "http_calls": ("monitoring", """
        run_id VARCHAR, source VARCHAR, url VARCHAR, attempt INTEGER,
        started_at TIMESTAMPTZ, duration_ms DOUBLE,
        http_status INTEGER,          -- NULL si erreur réseau (timeout, DNS...)
        bytes BIGINT, error VARCHAR"""),
    "ingestion_runs": ("monitoring", """
        run_id VARCHAR, source VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
        outcome VARCHAR,              -- new | unchanged | skipped | error
        http_status INTEGER, attempts INTEGER, bytes BIGINT, sha256 VARCHAR,
        bronze_path VARCHAR, message VARCHAR"""),
    "bronze_files": ("bronze_raw", """
        source VARCHAR, ingested_at TIMESTAMPTZ, sha256 VARCHAR, bytes BIGINT,
        format VARCHAR, path VARCHAR, origin_url VARCHAR"""),
}


class RunLog:
    """Accumule les lignes d'une exécution, puis les écrit en Parquet."""

    def __init__(self) -> None:
        self.rows: dict[str, list[list]] = {name: [] for name in TABLES}

    def add(self, table: str, row: list) -> None:
        self.rows[table].append(row)

    def flush(self, data_root: Path, run_id: str, ingest_date: str) -> None:
        con = duckdb.connect()  # base en mémoire, juste pour typer et écrire
        try:
            con.execute("SET TimeZone = 'UTC'")
            for table, (_, columns) in TABLES.items():
                rows = self.rows[table]
                if not rows:
                    continue
                con.execute(f"CREATE TABLE {table} ({columns})")
                placeholders = ", ".join("?" * len(rows[0]))
                con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
                out = data_root / "logs" / table / f"ingest_date={ingest_date}" / f"{run_id}.parquet"
                out.parent.mkdir(parents=True, exist_ok=True)
                con.execute(f"COPY {table} TO '{out.as_posix()}' (FORMAT parquet)")
        finally:
            con.close()


def connect(data_root: Path) -> duckdb.DuckDBPyConnection:
    """Connexion DuckDB en mémoire avec une vue par journal.

    Vues : monitoring.http_calls, monitoring.ingestion_runs, bronze_raw.files.
    """
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    for table, (schema, columns) in TABLES.items():
        view = "files" if table == "bronze_files" else table
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        pattern = data_root / "logs" / table / "*" / "*.parquet"
        if any(pattern.parent.parent.glob("*/*.parquet")):
            con.execute(f"CREATE VIEW {schema}.{view} AS SELECT * EXCLUDE (ingest_date) "
                        f"FROM read_parquet('{pattern.as_posix()}', hive_partitioning = true, "
                        f"union_by_name = true)")
        else:
            con.execute(f"CREATE TABLE {schema}.{view} ({columns})")
    return con


def last_sha256_by_source(data_root: Path) -> dict[str, str]:
    con = connect(data_root)
    try:
        rows = con.execute(
            "SELECT source, arg_max(sha256, ingested_at) FROM bronze_raw.files GROUP BY source"
        ).fetchall()
    finally:
        con.close()
    return dict(rows)
