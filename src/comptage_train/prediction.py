"""Prévision de l'affluence J+1 à J+7 (étape 4), paramétrée par config/prediction.yaml.

    voyageurs = niveau habituel x indice du jour

Le niveau habituel est la médiane du compte officiel (gare, ligne, tranche, catégorie
de jour IDFM). L'indice du jour est prédit par trois modèles LightGBM quantile
(P10, P50, P90) à partir du calendrier et de la météo, pondérés par le score de
confiance du compte officiel. Les contributions TreeSHAP du modèle P50 (natives
LightGBM, sans dépendance `shap`) expliquent chaque prévision en voyageurs.

Sorties :
  * gold/previsions.parquet        prévisions de la dernière exécution, avec explication
  * gold/modele_evaluation.parquet erreur sur la période de test, comparée à la référence
  * logs/previsions/...            historique des prévisions (ajout seul), pour les juger plus tard
  * models/                        modèles et paramètres pour l'API (hors Git, recalculables)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from . import silver
from .settings import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "prediction.yaml"
QUANTILES = (0.1, 0.5, 0.9)
CATEGORIELLES = ("tranche_horaire", "ligne", "cat_jour_idfm")
NIVEAUX = ("FAIBLE", "MOYENNE", "FORTE")


@dataclass(frozen=True)
class Config:
    version: str
    historique_jours: int
    horizon_jours: int
    test_jours: int
    variables: list[str]
    ferie_comme_dimanche: bool
    lightgbm: dict
    couverture_cible: float
    niveaux: dict
    duree_tranches_h: dict


def load_config(path: Path = CONFIG_PATH) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["version"] = str(raw["version"])
    return Config(**raw)


# --------------------------------------------------------------------------- données
def meteo_journaliere(con: duckdb.DuckDBPyConnection, data_root: Path) -> None:
    """Table _meteo (jour, pluie_mm, neige_cm, source) : l'historique observé quand il
    existe, sinon la dernière prévision Open-Meteo agrégée par jour (heure de Paris)."""
    files = silver.latest_files(con, data_root)
    rows: dict[str, tuple] = {}
    if "openmeteo_prevision_paris" in files:
        h = silver.read_json(files["openmeteo_prevision_paris"].path).get("hourly", {})
        df = pd.DataFrame({"jour": [t[:10] for t in h.get("time", [])],
                           "pluie": h.get("precipitation", []), "neige": h.get("snowfall", [])})
        for jour, g in df.groupby("jour"):
            rows[jour] = (float(g.pluie.sum()), float(g.neige.sum()), "PREVISION")
    if "openmeteo_historique_paris" in files:
        d = silver.read_json(files["openmeteo_historique_paris"].path).get("daily", {})
        for jour, pluie, neige in zip(d.get("time", []), d.get("precipitation_sum", []), d.get("snowfall_sum", [])):
            if pluie is not None:
                rows[jour] = (float(pluie), float(neige or 0), "OBSERVEE")
    con.execute("CREATE OR REPLACE TEMP TABLE _meteo (jour DATE, pluie_mm DOUBLE, neige_cm DOUBLE, source VARCHAR)")
    if rows:
        con.executemany("INSERT INTO _meteo VALUES (?, ?, ?, ?)", [(j, *v) for j, v in sorted(rows.items())])


_CALENDRIER = """
    SELECT jour, jour_semaine, cat_jour_idfm, est_ferie::INT AS ferie, vacances_zone_c::INT AS vacances,
           -- pont : jour ouvré entre un férié et un week-end (vendredi de l'Ascension...)
           (type_jour_sncf = 'JOB' AND (
              (lag(est_ferie) OVER w AND lead(type_jour_sncf) OVER w <> 'JOB')
              OR (lead(est_ferie) OVER w AND lag(type_jour_sncf) OVER w <> 'JOB')))::INT AS pont,
           -- fêtes de fin d'année : du 24 décembre au 1er janvier
           ((month(jour) = 12 AND day(jour) >= 24) OR (month(jour) = 1 AND day(jour) = 1))::INT AS fin_annee
    FROM silver.calendrier WINDOW w AS (ORDER BY jour)"""


def historique(con, config: Config) -> pd.DataFrame:
    """Lignes d'apprentissage : compte officiel MESURE ou RECONCILIE_IDFM (jamais l'extrapolé,
    qui est lui-même une prévision), sur les `historique_jours` derniers jours connus."""
    return con.execute(f"""
        WITH cal AS ({_CALENDRIER}),
        h AS (SELECT * FROM gold.compte_officiel WHERE methode IN ('MESURE', 'RECONCILIE_IDFM')),
        fin AS (SELECT max(date) AS fin FROM h WHERE methode = 'RECONCILIE_IDFM')
        SELECT h.code_gare, h.nom_gare, h.ligne, h.date, h.tranche_horaire, h.voyageurs, h.score_confiance,
               cal.* EXCLUDE (jour), coalesce(m.pluie_mm, 0) AS pluie_mm, coalesce(m.neige_cm, 0) AS neige_cm
        FROM h CROSS JOIN fin JOIN cal ON cal.jour = h.date LEFT JOIN _meteo m ON m.jour = h.date
        WHERE h.date > fin.fin - INTERVAL {config.historique_jours} DAY AND h.date <= fin.fin
        ORDER BY h.code_gare, h.ligne, h.date, h.tranche_horaire""").df()


def grille(con, date_reference: str, config: Config) -> pd.DataFrame:
    """Lignes à prévoir : chaque gare x ligne étalonnée, de J+1 à J+horizon, 5 tranches."""
    return con.execute(f"""
        WITH cal AS ({_CALENDRIER}),
        paires AS (
          SELECT DISTINCT e.code_gare, c.nom_gare, e.ligne FROM gold.etalonnage e
          JOIN (SELECT code_gare, min(nom_gare) AS nom_gare FROM gold.compte_officiel GROUP BY 1) c USING (code_gare)
          WHERE e.statut = 'ETALONNE'),
        jours AS (SELECT unnest(range(DATE '{date_reference}' + 1, DATE '{date_reference}' + {config.horizon_jours + 1},
                                      INTERVAL 1 DAY))::DATE AS date)
        SELECT p.code_gare, p.nom_gare, p.ligne, j.date, t.tranche_horaire, cal.* EXCLUDE (jour),
               coalesce(m.pluie_mm, 0) AS pluie_mm, coalesce(m.neige_cm, 0) AS neige_cm,
               coalesce(m.source, 'ABSENTE') AS source_meteo
        FROM paires p CROSS JOIN jours j CROSS JOIN (SELECT unnest(?) AS tranche_horaire) t
        JOIN cal ON cal.jour = j.date LEFT JOIN _meteo m ON m.jour = j.date
        ORDER BY p.code_gare, p.ligne, j.date, t.tranche_horaire""", [silver.TRANCHES]).df()


# --------------------------------------------------------------------------- modèle
@dataclass
class Modele:
    """Trois boosters quantile + ce qu'il faut pour les réutiliser (API, D4)."""
    boosters: dict
    niveau_habituel: pd.DataFrame     # code_gare, ligne, tranche_horaire, cat_jour_idfm, niveau
    seuils: pd.DataFrame              # code_gare, ligne, seuil_moyen, seuil_fort (voyageurs / heure)
    modalites: dict                   # variable catégorielle -> liste ordonnée des valeurs
    recalibrage: dict                 # {"bas": k, "haut": k}
    config: Config

    def variables(self, df: pd.DataFrame) -> pd.DataFrame:
        """Valeurs vues par le modèle. Un férié y est un dimanche, comme dans la catégorie
        de jour IDFM : sans cela, le modèle applique au jeudi de l'Ascension l'effet des seuls
        jeudis fériés de l'historique (Noël, Jour de l'an), qui sont bien plus creux."""
        x = df[self.config.variables].copy()
        if self.config.ferie_comme_dimanche and {"ferie", "jour_semaine"} <= set(x.columns):
            x.loc[df["ferie"] == 1, "jour_semaine"] = 7
        return x

    def matrice(self, df: pd.DataFrame) -> pd.DataFrame:
        x = self.variables(df)
        for col in CATEGORIELLES:
            if col in x:
                x[col] = pd.Categorical(x[col], categories=self.modalites[col]).codes.astype("int32")
        return x.astype("float64")

    def predire(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ajoute niveau_habituel, p10, p50, p90, niveau et contributions (P50, en voyageurs)."""
        cles = ["code_gare", "ligne", "tranche_horaire", "cat_jour_idfm"]
        out = df.merge(self.niveau_habituel, on=cles, how="inner")
        x = self.matrice(out)
        indice = {q: self.boosters[q].predict(x) for q in QUANTILES}
        base = out["niveau_habituel"].to_numpy()
        p50 = indice[0.5] * base
        bas = np.minimum(indice[0.1] * base, p50)
        haut = np.maximum(indice[0.9] * base, p50)
        out["p50"] = np.round(np.maximum(p50, 0)).astype("int64")
        out["p10"] = np.round(np.maximum(p50 - self.recalibrage["bas"] * (p50 - bas), 0)).astype("int64")
        out["p90"] = np.round(p50 + self.recalibrage["haut"] * (haut - p50)).astype("int64")
        contrib = self.boosters[0.5].predict(x, pred_contrib=True)
        variables = self.config.variables
        out["effet_modele"] = np.round((contrib[:, -1] - 1) * base, 1)   # écart moyen du modèle à l'habituel
        out["contributions"] = [
            [{"variable": v, "valeur": str(r[v]), "effet_voyageurs": round(float(c[i] * b), 1)}
             for i, v in enumerate(variables)]
            for r, c, b in zip(self.variables(out).to_dict("records"), contrib, base)]
        duree = out["tranche_horaire"].map(self.config.duree_tranches_h).astype(float)
        out = out.merge(self.seuils, on=["code_gare", "ligne"], how="left")
        intensite = out["p50"] / duree
        out["niveau"] = np.select([intensite > out["seuil_fort"], intensite > out["seuil_moyen"]],
                                  ["FORTE", "MOYENNE"], "FAIBLE")
        return out


def _apprendre(df: pd.DataFrame, config: Config, modalites: dict):
    import lightgbm as lgb

    cles = ["code_gare", "ligne", "tranche_horaire", "cat_jour_idfm"]
    niveau = df.groupby(cles, as_index=False)["voyageurs"].median().rename(columns={"voyageurs": "niveau_habituel"})
    d = df.merge(niveau, on=cles)
    d = d[d["niveau_habituel"] > 0]
    m = Modele({}, niveau, pd.DataFrame(), modalites, {"bas": 1.0, "haut": 1.0}, config)
    x = m.matrice(d)
    # Cible = indice du jour ; poids = confiance x niveau (une erreur relative compte plus
    # sur une grande gare, comme dans l'erreur pondérée WAPE).
    y = d["voyageurs"] / d["niveau_habituel"]
    w = d["score_confiance"] / 100 * d["niveau_habituel"]
    params = {k: v for k, v in config.lightgbm.items() if k != "num_boost_round"}
    for q in QUANTILES:
        ds = lgb.Dataset(x, y, weight=w, categorical_feature=[c for c in CATEGORIELLES if c in config.variables],
                         free_raw_data=False)
        m.boosters[q] = lgb.train({**params, "objective": "quantile", "alpha": q, "verbose": -1,
                                   "num_threads": 4, "deterministic": True, "seed": 1},
                                  ds, config.lightgbm["num_boost_round"])
    duree = df["tranche_horaire"].map(config.duree_tranches_h).astype(float)
    intensite = df.assign(i=df["voyageurs"] / duree)
    m.seuils = intensite.groupby(["code_gare", "ligne"])["i"].quantile(
        [config.niveaux["moyen"], config.niveaux["fort"]]).unstack().set_axis(
        ["seuil_moyen", "seuil_fort"], axis=1).reset_index()
    return m


def entrainer(con, config: Config | None = None) -> tuple[Modele, pd.DataFrame]:
    """Apprend sur l'historique sauf la période de test, évalue et recalibre l'intervalle,
    puis réapprend sur tout l'historique. Renvoie (modèle final, évaluation)."""
    config = config or load_config()
    hist = historique(con, config)
    modalites = {c: sorted(hist[c].dropna().unique().tolist()) for c in CATEGORIELLES}
    debut_test = hist["date"].max() - pd.Timedelta(days=config.test_jours - 1)
    app, test = hist[hist["date"] < debut_test], hist[hist["date"] >= debut_test]

    m = _apprendre(app, config, modalites)
    p = m.predire(test)
    y = p["voyageurs"].to_numpy()
    # Recalibrage : facteurs qui élargissent (ou resserrent) chaque côté de l'intervalle
    # pour que la vérité tombe dessous/dessus dans la bonne proportion sur la période de test.
    queue = (1 - config.couverture_cible) / 2
    bas = np.clip((p["p50"] - y) / (p["p50"] - p["p10"]).replace(0, np.nan), 0, None)
    haut = np.clip((y - p["p50"]) / (p["p90"] - p["p50"]).replace(0, np.nan), 0, None)
    recal = {"bas": float(np.nanquantile(bas, 1 - queue)) if bas.notna().any() else 1.0,
             "haut": float(np.nanquantile(haut, 1 - queue)) if haut.notna().any() else 1.0}
    ref = p["niveau_habituel"].to_numpy()
    lo = p["p50"] - recal["bas"] * (p["p50"] - p["p10"])
    hi = p["p50"] + recal["haut"] * (p["p90"] - p["p50"])
    somme = max(float(y.sum()), 1.0)
    evaluation = pd.DataFrame([
        ("lignes_test", float(len(p))),
        ("debut_test", float(debut_test.strftime("%Y%m%d")) if len(test) else np.nan),
        ("wape_modele_p50", float(np.abs(p["p50"] - y).sum() / somme)),
        ("wape_reference_niveau_habituel", float(np.abs(ref - y).sum() / somme)),
        ("couverture_brute_p10_p90", float(((y >= p["p10"]) & (y <= p["p90"])).mean()) if len(p) else np.nan),
        ("couverture_recalibree_p10_p90", float(((y >= lo) & (y <= hi)).mean()) if len(p) else np.nan),
        ("recalibrage_bas", recal["bas"]), ("recalibrage_haut", recal["haut"]),
    ], columns=["metrique", "valeur"])

    final = _apprendre(hist, config, modalites)
    final.recalibrage = recal
    return final, evaluation


def prevoir(con, data_root: Path, date_reference: str, config: Config | None = None) -> tuple[Modele, pd.DataFrame, pd.DataFrame]:
    config = config or load_config()
    meteo_journaliere(con, data_root)
    modele, evaluation = entrainer(con, config)
    previsions = modele.predire(grille(con, date_reference, config))
    previsions["date_reference"] = pd.Timestamp(date_reference).date()
    previsions["version_modele"] = config.version
    return modele, previsions, evaluation


# --------------------------------------------------------------------------- persistance
COLONNES = ["date_reference", "date", "code_gare", "nom_gare", "ligne", "tranche_horaire", "cat_jour_idfm",
            "jour_semaine", "ferie", "vacances", "pont", "fin_annee", "pluie_mm", "neige_cm", "source_meteo",
            "niveau_habituel", "p10", "p50", "p90", "niveau", "effet_modele", "contributions", "version_modele"]


def sauver_modele(modele: Modele, dossier: Path) -> None:
    dossier.mkdir(parents=True, exist_ok=True)
    for q, b in modele.boosters.items():
        b.save_model(str(dossier / f"p{int(q * 100)}.txt"))
    modele.niveau_habituel.to_parquet(dossier / "niveau_habituel.parquet", index=False)
    modele.seuils.to_parquet(dossier / "seuils.parquet", index=False)
    (dossier / "meta.json").write_text(json.dumps(
        {"version": modele.config.version, "modalites": modele.modalites, "recalibrage": modele.recalibrage},
        ensure_ascii=False, indent=1), encoding="utf-8")


def charger_modele(dossier: Path, config: Config | None = None) -> Modele:
    import lightgbm as lgb

    config = config or load_config()
    meta = json.loads((dossier / "meta.json").read_text(encoding="utf-8"))
    boosters = {q: lgb.Booster(model_file=str(dossier / f"p{int(q * 100)}.txt")) for q in QUANTILES}
    return Modele(boosters, pd.read_parquet(dossier / "niveau_habituel.parquet"),
                  pd.read_parquet(dossier / "seuils.parquet"), meta["modalites"], meta["recalibrage"], config)


def run_prediction(data_root: Path, date_reference: str | None = None, config: Config | None = None) -> pd.DataFrame:
    """`comptage prevoir` : apprend, prévoit J+1..J+7, écrit gold/ + journaux + models/."""
    from datetime import datetime, timezone

    from . import warehouse
    from .ingest import new_run_id

    date_reference = date_reference or date.today().isoformat()
    con = warehouse.connect_derived(data_root)
    try:
        con.execute("SET enable_progress_bar = false")
        modele, previsions, evaluation = prevoir(con, data_root, date_reference, config)
        previsions = previsions[COLONNES].sort_values(["date", "code_gare", "ligne", "tranche_horaire"])
        gold = data_root / "gold"
        gold.mkdir(parents=True, exist_ok=True)
        con.register("_prev", previsions)
        con.execute(f"COPY (SELECT * FROM _prev) TO '{(gold / 'previsions.parquet').as_posix()}' "
                    "(FORMAT parquet, COMPRESSION zstd)")
        evaluation = evaluation.assign(version_modele=modele.config.version, date_reference=date_reference)
        con.register("_eval", evaluation)
        con.execute(f"COPY (SELECT * FROM _eval) TO '{(gold / 'modele_evaluation.parquet').as_posix()}' "
                    "(FORMAT parquet)")
        # Historique compact des prévisions : de quoi les comparer au compte officiel plus tard.
        run_at = datetime.now(timezone.utc)
        run_id = new_run_id(run_at)
        histo = data_root / "logs" / "previsions" / f"ingest_date={run_at.date().isoformat()}"
        histo.mkdir(parents=True, exist_ok=True)
        con.execute(f"""COPY (SELECT '{run_id}' AS run_id, date_reference, date, code_gare, ligne, tranche_horaire,
                               p10, p50, p90, niveau, version_modele FROM _prev)
                        TO '{(histo / f'{run_id}.parquet').as_posix()}' (FORMAT parquet, COMPRESSION zstd)""")
        # Les modèles se recalculent en deux minutes : hors de l'historique Git.
        sauver_modele(modele, data_root / "models")
        ignore = data_root / ".gitignore"
        if "models/" not in (ignore.read_text() if ignore.exists() else ""):
            with ignore.open("a") as fh:
                fh.write("models/\n")
        return evaluation
    finally:
        con.close()

