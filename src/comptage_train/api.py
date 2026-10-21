"""API d'affluence pour écrans de quai et application mobile (étape 4).

Lancer :  uvicorn comptage_train.api:app --reload      (documentation interactive : /docs)

Les réponses viennent de gold/previsions.parquet, écrit chaque jour par `comptage prevoir`.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import prediction
from .settings import data_dir, load_dotenv

load_dotenv()

app = FastAPI(
    title="Affluence Transilien : prévisions Open Data",
    version=prediction.load_config().version,
    description="Voyageurs montants prévus par gare, ligne et tranche horaire (J+1 à J+7), "
                "avec intervalle P10-P90, niveau d'affluence et explication de chaque prévision. "
                "Calculé uniquement à partir de données publiques (SNCF Open Data, IDFM, Open-Meteo).",
)

BADGES = {"FAIBLE": "VERT", "MOYENNE": "ORANGE", "FORTE": "ROUGE"}
CONSEILS = {
    "FAIBLE": "Affluence faible : des places assises devraient être disponibles.",
    "MOYENNE": "Affluence moyenne : privilégiez les voitures de tête et de queue.",
    "FORTE": "Forte affluence prévue : répartissez-vous sur toute la longueur du quai, "
             "ou décalez votre trajet d'une tranche horaire si vous le pouvez.",
}
LIBELLES = {"tranche_horaire": "tranche horaire", "ligne": "ligne", "jour_semaine": "jour de la semaine",
            "cat_jour_idfm": "type de jour", "ferie": "jour férié", "vacances": "vacances scolaires",
            "pont": "pont", "pluie_mm": "pluie", "neige_cm": "neige"}


class Effet(BaseModel):
    variable: str
    valeur: str
    effet_voyageurs: float


class Prevision(BaseModel):
    code_gare: str
    gare: str
    ligne: str
    date: date
    tranche_horaire: str
    voyageurs_p50: int
    intervalle_p10_p90: tuple[int, int]
    niveau: str
    badge: str
    conseil: str
    explication: str
    niveau_habituel: float
    effets: list[Effet]
    source_meteo: str
    version_modele: str


@lru_cache(maxsize=1)
def _previsions(mtime: float) -> pd.DataFrame:
    return pd.read_parquet(data_dir() / "gold" / "previsions.parquet")


def previsions() -> pd.DataFrame:
    path = data_dir() / "gold" / "previsions.parquet"
    if not path.exists():
        raise HTTPException(503, "Aucune prévision : lancer `comptage prevoir`.")
    return _previsions(path.stat().st_mtime)


def explication(row) -> str:
    """Phrase lisible : niveau habituel puis les trois effets les plus forts."""
    effets = sorted(row["contributions"], key=lambda e: -abs(e["effet_voyageurs"]))[:3]
    morceaux = [f"niveau habituel {row['niveau_habituel']:.0f} voy."]
    morceaux += [f"{LIBELLES.get(e['variable'], e['variable'])} ({e['valeur']}) {e['effet_voyageurs']:+.0f}"
                 for e in effets if abs(e["effet_voyageurs"]) >= 1]
    reste = row["p50"] - row["niveau_habituel"] - sum(e["effet_voyageurs"] for e in effets)
    if abs(reste) >= 1:
        morceaux.append(f"autres effets {reste:+.0f}")
    return " | ".join(morceaux) + f" = {row['p50']} voyageurs prévus"


def _reponse(row) -> Prevision:
    return Prevision(
        code_gare=row["code_gare"], gare=row["nom_gare"], ligne=row["ligne"], date=row["date"],
        tranche_horaire=row["tranche_horaire"], voyageurs_p50=int(row["p50"]),
        intervalle_p10_p90=(int(row["p10"]), int(row["p90"])), niveau=row["niveau"],
        badge=BADGES[row["niveau"]], conseil=CONSEILS[row["niveau"]], explication=explication(row),
        niveau_habituel=float(row["niveau_habituel"]), effets=[Effet(**e) for e in row["contributions"]],
        source_meteo=row["source_meteo"], version_modele=row["version_modele"])


def _selection(gare: str, ligne: str, jour: date, tranche: str | None = None) -> pd.DataFrame:
    df = previsions()
    sel = df[(df["code_gare"] == gare) & (df["ligne"] == ligne) & (pd.to_datetime(df["date"]).dt.date == jour)]
    if tranche:
        sel = sel[sel["tranche_horaire"] == tranche]
    if sel.empty:
        raise HTTPException(404, "Pas de prévision pour cette gare, cette ligne et cette date "
                                 "(horizon J+1 à J+7, gares comptées par la SNCF).")
    return sel


@app.get("/api/v1/gares", summary="Gares et lignes couvertes")
def gares() -> list[dict]:
    df = previsions()
    g = df.groupby(["code_gare", "nom_gare"])["ligne"].unique().reset_index()
    return [{"code_gare": r.code_gare, "gare": r.nom_gare, "lignes": sorted(r.ligne)} for r in g.itertuples()]


@app.get("/api/v1/affluence", response_model=Prevision, summary="Affluence prévue pour une tranche")
def affluence(gare: str = Query(..., description="code UIC, ex. 87271007"), ligne: str = Query(...),
              jour: date = Query(..., alias="date"), tranche: str = Query("De 6h à 10h")) -> Prevision:
    return _reponse(_selection(gare, ligne, jour, tranche).iloc[0])


@app.get("/api/v1/affluence/journee", response_model=list[Prevision], summary="Les 5 tranches d'une journée")
def affluence_journee(gare: str, ligne: str, jour: date = Query(..., alias="date")) -> list[Prevision]:
    sel = _selection(gare, ligne, jour)
    ordre = {t: i for i, t in enumerate(prediction.silver.TRANCHES)}
    return [_reponse(r) for _, r in sel.sort_values("tranche_horaire", key=lambda s: s.map(ordre)).iterrows()]
