"""Tests de l'étape 2 (silver + règles de qualité) sur un petit jeu synthétique."""

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


def one(con, sql: str):
    return con.execute(sql).fetchone()


def test_date_inversee_corrigee(con):
    dates = con.execute("SELECT DISTINCT date_publiee, date, date_corrigee FROM silver.comptages "
                        "WHERE type_jour = 'SAM'").fetchall()
    assert [(str(a), str(b), c) for a, b, c in dates] == [("2025-08-03", "2025-03-08", True)]
    assert one(con, "SELECT DISTINCT resultat FROM quality.controles c JOIN silver.comptages s USING (row_id) "
                    "WHERE s.type_jour = 'SAM' AND regle = 'RT_11'") == ("ATTENTION",)


def test_validations_normalisees_et_sommees(con):
    assert one(con, "SELECT id_zdc, categorie, nb_vald, lignes_source FROM silver.validations_jour") == \
        (100, "FORFAIT NAVIGO", 1200, 2)
    assert one(con, "SELECT colonnes_renommees FROM silver.schema_contrats "
                    "WHERE source = 'idfm_validations_ferre_jour_t1'") == (["ida"],)


def test_correspondance_par_distance(con):
    rows = dict(con.execute("SELECT code_gare, statut FROM silver.correspondance_gares").fetchall())
    assert rows == {"87000001": "APPARIEE", "87000002": "NON_APPARIEE"}


def test_profils_ramenes_aux_tranches_sncf(con):
    pct = dict(con.execute("SELECT tranche_horaire, pct FROM silver.profils_tranches").fetchall())
    assert pct["De 6h à 10h"] == pytest.approx(33.0)
    assert sum(pct.values()) == pytest.approx(100.0)


def test_statuts_et_regles(con):
    statut = dict(con.execute("""SELECT code_gare || ' ' || type_jour, list(DISTINCT statut ORDER BY statut)
                                 FROM quality.comptages_qualifies GROUP BY 1""").fetchall())
    assert statut["87000001 JOB"] == ["VALIDE"]
    assert statut["87000001 SAM"] == ["ATTENTION"]
    # Gare B : zéro en pointe et doublon en ERREUR, le reste en ATTENTION (pas de zone IDFM, RT_09)
    assert statut["87000002 JOB"] == ["ATTENTION", "ERREUR"]
    erreurs = one(con, """SELECT list(DISTINCT e ORDER BY e) FROM
                          (SELECT unnest(regles_erreur) AS e FROM quality.comptages_qualifies)""")[0]
    assert erreurs == ["RM_05", "RT_04"]
    assert one(con, "SELECT min(score_confiance), max(score_confiance) FROM quality.comptages_qualifies "
                    "WHERE statut = 'ERREUR'") == (0, 0)


def test_chaque_ligne_a_toutes_les_regles(con):
    n_rules = len(quality.load_rules().rules)
    assert one(con, "SELECT count(DISTINCT n) FROM (SELECT count(*) AS n FROM quality.controles GROUP BY row_id)") \
        == (1,)
    assert one(con, "SELECT count(*) / count(DISTINCT row_id) FROM quality.controles") == (n_rules,)
    assert one(con, "SELECT DISTINCT resultat FROM quality.controles WHERE regle = 'RM_03'") == ("NON_APPLICABLE",)


def test_score_et_lignage(con):
    rules = quality.load_rules()
    row = con.execute("""SELECT score_confiance, penalites, bonus_croisement, bronze_path, bronze_sha256,
                                source_version, version_regles
                         FROM quality.comptages_qualifies WHERE code_gare = '87000001' AND type_jour = 'JOB'
                         LIMIT 1""").fetchone()
    score, penalites, bonus, path, sha, version, version_regles = row
    assert score == min(rules.plafond, rules.base - penalites + (rules.bonus if bonus else 0))
    assert path.startswith("bronze/sncf_comptage_montants_trains/")
    assert len(sha) == 64 and version == "v1" and version_regles == rules.version


def test_regles_yaml_coherentes():
    rules = quality.load_rules()
    ids = [r.id for r in rules.rules]
    assert ids == [f"RT_{i:02d}" for i in range(1, 13)] + [f"RM_{i:02d}" for i in range(1, 11)]
    assert all(r.severite in ("ERREUR", "ATTENTION") for r in rules.rules)
    assert all(r.penalite > 0 for r in rules.rules if r.severite == "ATTENTION" and not r.non_applicable)


def test_run_quality_reproductible(data_root: Path):
    quality.run_quality(data_root, "2026-10-21")
    first = {p.name: p.read_bytes() for p in sorted((data_root / "quality").glob("*.parquet"))}
    quality.run_quality(data_root, "2026-10-21")
    second = {p.name: p.read_bytes() for p in sorted((data_root / "quality").glob("*.parquet"))}
    assert first == second and "comptages_qualifies.parquet" in first
    with warehouse.connect_derived(data_root) as c:
        assert one(c, "SELECT count(DISTINCT run_id) FROM monitoring.quality_runs") == (2,)
        assert one(c, "SELECT count(*) FROM quality.comptages_qualifies") == (16,)
