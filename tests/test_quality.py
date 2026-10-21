"""Tests de l'étape 2 (silver + règles de qualité) sur un petit jeu synthétique (conftest.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comptage_train import quality, warehouse


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
