"""Jeu de données synthétique partagé par les tests des étapes 2 et 3."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from comptage_train import quality, silver, warehouse

TRANCHES = silver.TRANCHES
NOW = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)


def _parquet(root: Path, log: warehouse.RunLog, source: str, sql: str) -> None:
    rel = f"bronze/{source}/ingest_date=2026-10-21/20261021T060000Z.parquet"
    out = root / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    duckdb.execute(f"COPY ({sql}) TO '{out.as_posix()}' (FORMAT parquet)")
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    log.add("bronze_files", [source, NOW, sha, out.stat().st_size, "parquet", rel, "https://x", "v1"])


def _rows(*rows) -> str:
    return " UNION ALL ".join(rows)


def comptage(gare, nom, date, type_jour, tranche, montants, ligne="C", axe="X") -> str:
    return (f"SELECT '{nom}' AS nom_gare, '{gare}' AS code_gare, '{type_jour}' AS type_jour, "
            f"DATE '{date}' AS date, {date[:4]}::BIGINT AS annee, '{ligne}' AS ligne, '{axe}' AS axe, "
            f"'{tranche}' AS tranche_horaire, {montants}::BIGINT AS somme_de_montants")


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    log = warehouse.RunLog()
    jours = [
        # Gare A, mardi 04/03/2025 : propre et complète
        *[comptage("87000001", "GARE-A", "2025-03-04", "JOB", t, v)
          for t, v in zip(TRANCHES, [50, 400, 300, 350, 100])],
        # Gare A, samedi 08/03/2025 publié 2025-08-03 (dimanche) : inversion jour/mois
        *[comptage("87000001", "GARE-A", "2025-08-03", "SAM", t, v)
          for t, v in zip(TRANCHES, [20, 150, 200, 180, 60])],
        # Gare B, mardi : zéro en pointe du matin, et une tranche en double
        *[comptage("87000002", "GARE-B", "2025-03-04", "JOB", t, v)
          for t, v in zip(TRANCHES, [10, 0, 80, 90, 20])],
        comptage("87000002", "GARE-B", "2025-03-04", "JOB", "Après 20h", 20),
    ]
    _parquet(tmp_path, log, "sncf_comptage_montants_trains", _rows(*jours))
    _parquet(tmp_path, log, "sncf_liste_gares", _rows(
        "SELECT '87000001' AS code_uic, 'Gare A' AS libelle, 'O' AS voyageurs, 1000.0 AS x_l93, 1000.0 AS y_l93",
        "SELECT '87000002', 'Gare B', 'O', 5000.0, 5000.0"))
    _parquet(tmp_path, log, "sncf_frequentation_gares", _rows(
        "SELECT '87000001' AS code_uic_complet, 1000000::BIGINT AS total_voyageurs_non_voyageurs_2024",
        "SELECT '87000002', 100000::BIGINT"))
    _parquet(tmp_path, log, "idfm_emplacement_gares", _rows(
        "SELECT 100::BIGINT AS id_ref_zdc, 'Gare A' AS nom_zdc, 'TRAIN' AS mode, 1050.0 AS x, 1000.0 AS y",
        "SELECT 200, 'Ailleurs', 'RER', 9000.0, 9000.0"))
    # T1 publie l'ancien nom de colonne `ida` et des nombres en texte (dérive réelle)
    _parquet(tmp_path, log, "idfm_validations_ferre_jour_t1", _rows(
        "SELECT DATE '2025-03-11' AS jour, '800' AS code_stif_trns, '1' AS code_stif_res, '9' AS code_stif_arret, "
        "'100.0' AS ida, 'GARE A' AS libelle_arret, 'Forfait Navigo' AS categorie_titre, '1000.0' AS nb_vald",
        "SELECT DATE '2025-03-11', '800', '1', '9', '100.0', 'GARE A', 'FORFAIT NAVIGO', '200.0'"))
    _parquet(tmp_path, log, "idfm_validations_ferre_profils_horaires_t1", _rows(*[
        f"SELECT '800' AS code_stif_trns, '1' AS code_stif_res, '9' AS code_stif_arret, 100 AS id_zdc, "
        f"'GARE A' AS libelle_arret, 'JOHV' AS cat_jour, '{h}H-{h + 1}H' AS trnc_horr_60, {p} AS pourcentage_validations"
        for h, p in [(5, 4.0), (8, 33.0), (12, 25.0), (17, 30.0), (21, 8.0)]]))
    log.flush(tmp_path, "test", "2026-10-21")
    return tmp_path


@pytest.fixture
def con(data_root: Path):
    c = warehouse.connect(data_root)
    silver.build_silver(c, data_root, "2026-10-21")
    quality.qualify(c, "2026-10-21")
    yield c
    c.close()
