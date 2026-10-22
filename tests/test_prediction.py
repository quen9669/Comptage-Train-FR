"""Tests de l'étape 4 (prévision + API) sur un compte officiel synthétique.

Trois gares, 200 jours : un niveau propre à chaque gare, un profil horaire, et des
week-ends à 60 % d'un jour ouvré. Le modèle doit retrouver l'effet week-end, et
chaque prévision doit se décomposer exactement en niveau habituel + effets.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from comptage_train import prediction, silver, warehouse

REFERENCE = "2026-03-20"
NIVEAUX = {"87000001": 1000, "87000002": 400, "87000003": 150}
PARTS = [0.05, 0.35, 0.25, 0.28, 0.07]


@pytest.fixture(scope="module")
def data_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("data")
    rng = np.random.default_rng(0)
    jours = [date(2025, 9, 1) + timedelta(days=i) for i in range(200)]
    lignes = []
    for gare, niveau in NIVEAUX.items():
        for j in jours:
            facteur = 0.6 if j.isoweekday() >= 6 else 1.0
            for tranche, part in zip(silver.TRANCHES, PARTS):
                v = round(niveau * 5 * part * facteur * rng.normal(1, 0.05))
                lignes.append((gare, f"GARE-{gare[-1]}", "C", j, tranche, v, 78, "RECONCILIE_IDFM"))
    con = duckdb.connect()
    con.execute("""CREATE TABLE co (code_gare VARCHAR, nom_gare VARCHAR, ligne VARCHAR, date DATE,
                   tranche_horaire VARCHAR, voyageurs BIGINT, score_confiance INTEGER, methode VARCHAR)""")
    con.executemany("INSERT INTO co VALUES (?, ?, ?, ?, ?, ?, ?, ?)", lignes)
    (root / "gold" / "compte_officiel").mkdir(parents=True)
    (root / "silver").mkdir()
    con.execute(f"COPY co TO '{(root / 'gold/compte_officiel/mois=tout.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"""COPY (SELECT DISTINCT code_gare, ligne, 'ETALONNE' AS statut FROM co)
                    TO '{(root / 'gold/etalonnage.parquet').as_posix()}' (FORMAT parquet)""")
    con.execute(f"""COPY (
        SELECT d::DATE AS jour, isodow(d) AS jour_semaine, false AS est_ferie, false AS vacances_zone_c,
               CASE WHEN isodow(d) = 7 THEN 'DIM' WHEN isodow(d) = 6 THEN 'SAM' ELSE 'JOB' END AS type_jour_sncf,
               CASE WHEN isodow(d) = 7 THEN 'DIJFP' WHEN isodow(d) = 6 THEN 'SAHV' ELSE 'JOHV' END AS cat_jour_idfm
        FROM range(DATE '2025-01-01', DATE '2027-01-01', INTERVAL 1 DAY) t(d))
        TO '{(root / 'silver/calendrier.parquet').as_posix()}' (FORMAT parquet)""")
    return root


@pytest.fixture(scope="module")
def config():
    cfg = prediction.load_config()
    return dataclasses.replace(cfg, test_jours=30, lightgbm={**cfg.lightgbm, "num_boost_round": 60,
                                                               "min_data_in_leaf": 20})


@pytest.fixture(scope="module")
def resultat(data_root, config):
    evaluation = prediction.run_prediction(data_root, REFERENCE, config)
    return evaluation, pd.read_parquet(data_root / "gold" / "previsions.parquet")


def test_grille_complete_et_intervalle_ordonne(resultat, config):
    _, prev = resultat
    assert len(prev) == len(NIVEAUX) * config.horizon_jours * 5
    assert (prev.p10 <= prev.p50).all() and (prev.p50 <= prev.p90).all()
    assert set(prev.niveau) <= set(prediction.NIVEAUX)


def test_effet_week_end_retrouve(resultat):
    _, prev = resultat
    prev["we"] = pd.to_datetime(prev.date).dt.dayofweek >= 5
    g = prev[prev.code_gare == "87000001"].groupby("we").p50.mean()
    assert g[True] / g[False] == pytest.approx(0.6, abs=0.1)


def test_explication_additive(resultat):
    _, prev = resultat
    for _, r in prev.head(20).iterrows():
        somme = r.niveau_habituel + r.effet_modele + sum(e["effet_voyageurs"] for e in r.contributions)
        assert somme == pytest.approx(r.p50, abs=2)


def test_evaluation_et_recalibrage(resultat, config):
    evaluation, _ = resultat
    ev = evaluation.set_index("metrique")["valeur"]
    assert ev["wape_modele_p50"] < 0.15
    # le recalibrage élargit ou resserre l'intervalle jusqu'à la couverture visée
    assert ev["couverture_recalibree_p10_p90"] == pytest.approx(config.couverture_cible, abs=0.05)


def test_modele_sauve_puis_recharge(data_root, resultat, config):
    _, prev = resultat
    modele = prediction.charger_modele(data_root / "models", config)
    x = prev.drop(columns=["niveau_habituel", "p10", "p50", "p90", "niveau", "effet_modele", "contributions"])
    again = modele.predire(x)
    assert again.p50.tolist() == prev.p50.tolist()
    assert "models/" in (data_root / ".gitignore").read_text()


def test_api(data_root, resultat, monkeypatch):
    from fastapi.testclient import TestClient

    from comptage_train import api

    monkeypatch.setenv("COMPTAGE_DATA_DIR", str(data_root))
    client = TestClient(api.app)
    jour = (date.fromisoformat(REFERENCE) + timedelta(days=1)).isoformat()
    r = client.get("/api/v1/affluence", params={"gare": "87000001", "ligne": "C", "date": jour,
                                               "tranche": "De 6h à 10h"})
    assert r.status_code == 200
    corps = r.json()
    assert corps["badge"] in ("VERT", "ORANGE", "ROUGE")
    assert corps["intervalle_p10_p90"][0] <= corps["voyageurs_p50"] <= corps["intervalle_p10_p90"][1]
    assert corps["explication"].endswith(f"= {corps['voyageurs_p50']} voyageurs prévus")
    assert len(client.get("/api/v1/affluence/journee", params={"gare": "87000001", "ligne": "C",
                                                               "date": jour}).json()) == 5
    assert client.get("/api/v1/affluence", params={"gare": "0", "ligne": "C", "date": jour}).status_code == 404


def _historique_feries() -> pd.DataFrame:
    """Un an pour quatre gares : les fériés hors vacances (des lundis) font 1,2 dimanche,
    le seul jeudi férié (Noël, en vacances) n'en fait que 0,5."""
    rng = np.random.default_rng(1)
    noel = date(2025, 12, 25)
    lundis_feries = {date(2025, 4, 21), date(2025, 6, 9), date(2025, 11, 10), date(2026, 4, 6),
                     date(2025, 5, 5), date(2025, 9, 8), date(2026, 2, 2), date(2026, 3, 2)}
    lignes = []
    for gare in ("1", "2", "3", "4"):
        for i in range(365):
            j = date(2025, 4, 1) + timedelta(days=i)
            ferie = j == noel or j in lundis_feries
            dow = j.isoweekday()
            cat = "DIJFP" if dow == 7 or ferie else "SAHV" if dow == 6 else "JOHV"
            niveau = {"DIJFP": 300, "SAHV": 500, "JOHV": 1000}[cat]
            if ferie:
                niveau = 300 * (0.5 if j == noel else 1.2)
            for tranche, part in zip(silver.TRANCHES, PARTS):
                lignes.append(dict(code_gare=gare, ligne="C", date=j, tranche_horaire=tranche,
                                   voyageurs=round(niveau * 5 * part * rng.normal(1, 0.03)), score_confiance=78,
                                   jour_semaine=dow, cat_jour_idfm=cat, ferie=int(ferie),
                                   vacances=int(j == noel), pont=0))
    return pd.DataFrame(lignes)


@pytest.mark.parametrize("comme_dimanche", [True, False])
def test_jeudi_ferie_hors_vacances(config, comme_dimanche):
    """L'Ascension (jeudi férié hors vacances) ne doit pas hériter de l'effet de Noël."""
    # sans fin_annee : on isole l'effet du jour de la semaine porté par le seul jeudi férié connu
    cfg = dataclasses.replace(config, ferie_comme_dimanche=comme_dimanche,
                              variables=[v for v in config.variables if v != "fin_annee"])
    hist = _historique_feries()
    modalites = {c: sorted(hist[c].unique().tolist()) for c in prediction.CATEGORIELLES}
    modele = prediction._apprendre(hist, cfg, modalites)
    ascension = hist[hist.date == date(2025, 12, 25)].assign(date=date(2026, 5, 14), vacances=0)
    attendu = ascension.voyageurs.sum() / 0.5 * 1.2          # 1,2 dimanche au lieu de 0,5
    ratio = modele.predire(ascension).p50.sum() / attendu
    if comme_dimanche:
        assert ratio == pytest.approx(1.0, abs=0.15)
    else:
        assert ratio < 0.8   # le défaut corrigé : le jeudi férié est tiré vers Noël


def test_calendrier_fin_annee(data_root):
    con = warehouse.connect_derived(data_root)
    cal = con.execute(f"SELECT jour::VARCHAR, fin_annee FROM ({prediction._CALENDRIER}) "
                      "WHERE jour IN ('2025-12-23', '2025-12-24', '2025-12-31', '2026-01-01', '2026-01-02')").fetchall()
    assert dict(cal) == {"2025-12-23": 0, "2025-12-24": 1, "2025-12-31": 1, "2026-01-01": 1, "2026-01-02": 0}
