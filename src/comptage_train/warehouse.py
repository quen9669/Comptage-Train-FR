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
        bronze_path VARCHAR, message VARCHAR,
        source_version VARCHAR        -- version annoncée par le producteur (si connue)
        """),
    "bronze_files": ("bronze_raw", """
        source VARCHAR, ingested_at TIMESTAMPTZ, sha256 VARCHAR, bytes BIGINT,
        format VARCHAR, path VARCHAR, origin_url VARCHAR,
        source_version VARCHAR"""),
    # Une ligne par (exécution de `comptage qualify`, règle, résultat) : l'historique
    # de la qualité. regle = 'STATUT' pour le décompte des statuts de lignes.
    "quality_runs": ("monitoring", """
        run_id VARCHAR, run_at TIMESTAMPTZ, version_regles VARCHAR, date_reference DATE,
        source_sha256 VARCHAR, regle VARCHAR, resultat VARCHAR, lignes BIGINT"""),
    # Une ligne par (exécution de `comptage compte-officiel`, méthode) : l'historique du compte.
    # methode = 'BACKTEST' : voyageurs = journées testées, score_moyen = erreur médiane en %.
    "gold_runs": ("monitoring", """
        run_id VARCHAR, run_at TIMESTAMPTZ, version_moteur VARCHAR, date_reference DATE,
        methode VARCHAR, lignes BIGINT, voyageurs BIGINT, score_moyen DOUBLE"""),
}

# Couches dérivées (recalculables depuis le bronze), écrites par `comptage qualify`
DERIVED = {
    "silver": ["calendrier", "comptages", "correspondance_gares", "gares_sncf", "profils_tranches",
               "schema_contrats", "validations_zone_jour", "zones_idfm"],
    "quality": ["comptages_qualifies", "controles", "regles"],
    "gold": ["etalonnage", "backtest", "compte_officiel"],
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

    Vues : monitoring.http_calls, monitoring.ingestion_runs, monitoring.quality_runs, monitoring.gold_runs,
    bronze_raw.files.

    Chaque vue part d'une table vide au schéma courant, unie par nom aux
    fichiers : une colonne ajoutée plus tard existe toujours (NULL dans les
    anciens journaux), ce qui permet de faire évoluer le schéma sans migration.
    """
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA _empty")
    for table, (schema, columns) in TABLES.items():
        view = "files" if table == "bronze_files" else table
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        con.execute(f"CREATE TABLE _empty.{table} ({columns})")
        select = f"SELECT * FROM _empty.{table}"
        pattern = data_root / "logs" / table / "*" / "*.parquet"
        if any(pattern.parent.parent.glob("*/*.parquet")):
            select += (f" UNION ALL BY NAME SELECT * EXCLUDE (ingest_date) "
                       f"FROM read_parquet('{pattern.as_posix()}', hive_partitioning = true, "
                       f"union_by_name = true)")
        con.execute(f"CREATE VIEW {schema}.{view} AS {select}")
    return con


def connect_derived(data_root: Path) -> duckdb.DuckDBPyConnection:
    """Comme `connect`, plus une vue par table silver/quality déjà calculée."""
    con = connect(data_root)
    for schema, tables in DERIVED.items():
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for table in tables:
            path = data_root / schema / f"{table}.parquet"
            parts = data_root / schema / table  # table partitionnée (un fichier par mois)
            if path.exists():
                con.execute(f"CREATE VIEW {schema}.{table} AS SELECT * FROM '{path.as_posix()}'")
            elif any(parts.glob("*.parquet")):
                con.execute(f"CREATE VIEW {schema}.{table} AS SELECT * FROM read_parquet("
                            f"'{(parts / '*.parquet').as_posix()}', hive_partitioning = false)")
    return con


def last_deposit_by_source(data_root: Path) -> dict[str, tuple[str, str | None]]:
    """Pour chaque source : (sha256, version producteur) du dernier dépôt bronze."""
    con = connect(data_root)
    try:
        rows = con.execute(
            "SELECT source, arg_max(sha256, ingested_at), arg_max(source_version, ingested_at) "
            "FROM bronze_raw.files GROUP BY source"
        ).fetchall()
    finally:
        con.close()
    return {source: (sha, version) for source, sha, version in rows}
