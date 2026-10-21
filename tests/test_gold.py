"""Tests de l'étape 3 (compte officiel) sur le jeu synthétique de conftest.py.

Gare A, ligne C : journée complète le mardi 04/03/2025 (1 200 montants) et le samedi
08/03/2025 ; validations IDFM de la zone 100 le seul mardi 11/03/2025 (1 200).
Donc alpha = 1 200 / 1 200 = 1, appris sur une seule journée.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comptage_train import gold, quality, silver, warehouse

REFERENCE = "2025-03-12"


@pytest.fixture
def gcon(data_root: Path):
    c = warehouse.connect(data_root)
    silver.build_silver(c, data_root, REFERENCE)
    quality.qualify(c, REFERENCE)
    gold.build_gold(c, REFERENCE)
    yield c
    c.close()


def compte(con, date: str, tranche: str = "De 6h à 10h"):
    return con.execute("""SELECT methode, voyageurs, score_confiance FROM gold.compte_officiel
                          WHERE code_gare = '87000001' AND date = ? AND tranche_horaire = ?""",
                       [date, tranche]).fetchone()


def test_etalonnage(gcon):
    rows = dict(gcon.execute("SELECT code_gare, (statut, alpha, n_journees) FROM gold.etalonnage").fetchall())
    assert rows["87000001"] == ("ETALONNE", 1.0, 1)
    assert rows["87000002"][0] == "SANS_ZONE_IDFM"


def test_hierarchie_des_methodes(gcon):
    config = gold.load_config()
    une_mesure = config.score["reconcilie"]["penalite_une_seule_mesure"]
    assert compte(gcon, "2025-03-04") == ("MESURE", 400, pytest.approx(90, abs=10))
    # alpha 1 x 1 200 validations x 33 % de la tranche 6h-10h (profil IDFM)
    assert compte(gcon, "2025-03-11") == ("RECONCILIE_IDFM", 396, config.score["reconcilie"]["plafond"] - une_mesure)
    # pas de validation publiée le 12/03 : médiane du même mois, même catégorie de jour
    assert compte(gcon, "2025-03-12") == ("EXTRAPOLE", 396, config.score["extrapole"]["plafond"] - une_mesure)


def test_composants_permettent_de_refaire_le_calcul(gcon):
    rows = gcon.execute("""SELECT methode, voyageurs, alpha, coalesce(validations_jour, validations_attendues),
                                  part_tranche FROM gold.compte_officiel
                           WHERE methode IN ('RECONCILIE_IDFM', 'EXTRAPOLE')""").fetchall()
    assert rows
    for _, voyageurs, alpha, validations, part in rows:
        assert voyageurs == round(alpha * validations * part / 100)


def test_gares_sans_facteur_absentes(gcon):
    assert gcon.execute("SELECT count(*) FROM gold.compte_officiel WHERE code_gare = '87000002'").fetchone() == (0,)
    assert gcon.execute("SELECT count(DISTINCT date) FROM gold.compte_officiel").fetchone() == (4,)


def test_run_gold_reproductible(data_root: Path):
    quality.run_quality(data_root, REFERENCE)
    gold.run_gold(data_root, REFERENCE)
    first = {p.relative_to(data_root): p.read_bytes() for p in sorted((data_root / "gold").rglob("*.parquet"))}
    gold.run_gold(data_root, REFERENCE)
    second = {p.relative_to(data_root): p.read_bytes() for p in sorted((data_root / "gold").rglob("*.parquet"))}
    assert first == second
    assert Path("gold/compte_officiel/mois=2025-03.parquet") in first
    with warehouse.connect_derived(data_root) as c:
        assert c.execute("SELECT count(*) FROM gold.compte_officiel").fetchone() == (20,)
        assert c.execute("SELECT count(DISTINCT run_id) FROM monitoring.gold_runs").fetchone() == (2,)
