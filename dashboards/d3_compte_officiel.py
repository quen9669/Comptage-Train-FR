"""Dashboard 3 : le compte officiel de voyageurs (montants) par ligne, gare et tranche.

Lancer : streamlit run dashboards/app.py   (ou ce fichier seul)
Données : tables écrites par `comptage compte-officiel` dans data/gold.
Chaque total affiche sa composition : mesuré, réconcilié via IDFM, extrapolé.
"""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from comptage_train import warehouse
from comptage_train.settings import data_dir, load_dotenv

load_dotenv()
DATA = data_dir()

st.set_page_config(page_title="D3 · Compte officiel", layout="wide")
st.title("Dashboard 3 : compte officiel des voyageurs")

METHODES = ["MESURE", "RECONCILIE_IDFM", "EXTRAPOLE"]
COULEURS = ["#1f77b4", "#ff7f0e", "#bab0ac"]


@st.cache_data(ttl=300)
def query(sql: str, params: tuple = ()):
    with warehouse.connect_derived(DATA) as con:
        return con.execute(sql, list(params)).df()


if not (DATA / "gold" / "etalonnage.parquet").exists():
    st.info(f"Aucun compte officiel dans {DATA}. Lancer `comptage qualify` puis `comptage compte-officiel`.")
    st.stop()

bornes = query("""SELECT min(date) AS debut, max(date) AS fin, any_value(version_moteur) AS version
                  FROM gold.compte_officiel WHERE methode <> 'MESURE' OR date >= DATE '2025-01-01'""").iloc[0]
lignes_dispo = query("SELECT DISTINCT ligne FROM gold.compte_officiel ORDER BY 1")["ligne"].tolist()

c1, c2 = st.columns([2, 3])
periode = c1.date_input("Période", (max(bornes["debut"], bornes["fin"] - timedelta(days=364)), bornes["fin"]),
                        min_value=bornes["debut"], max_value=bornes["fin"], format="DD/MM/YYYY")
choix_lignes = c2.multiselect("Lignes", lignes_dispo, default=lignes_dispo)
if len(periode) != 2 or not choix_lignes:
    st.stop()
debut, fin = periode
filtre = (debut, fin, choix_lignes)
WHERE = "date BETWEEN ? AND ? AND list_contains(?, ligne)"

compo = query(f"""SELECT methode, sum(voyageurs) AS voyageurs, round(avg(score_confiance), 1) AS score
                  FROM gold.compte_officiel WHERE {WHERE} GROUP BY 1""", filtre)
total = int(compo["voyageurs"].sum())
part = dict(zip(compo["methode"], compo["voyageurs"] / max(total, 1)))
backtest = query("SELECT median(erreur_relative) AS mediane, count(*) AS n FROM gold.backtest").iloc[0]

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Voyageurs montants", f"{total / 1e6:,.1f} M".replace(".", ","), help=f"{total:,} voyageurs".replace(",", " "))
m2.metric("Mesuré (SNCF)", f"{part.get('MESURE', 0):.1%}")
m3.metric("Réconcilié (IDFM)", f"{part.get('RECONCILIE_IDFM', 0):.1%}")
m4.metric("Extrapolé", f"{part.get('EXTRAPOLE', 0):.1%}")
m5.metric("Erreur médiane", f"{backtest['mediane']:.0%}",
          help=f"Écart entre chiffre reconstruit et mesure réelle, en validation croisée sur "
               f"{int(backtest['n'])} journées mesurées")
st.caption(f"Moteur {bornes['version']} · {len(choix_lignes)} ligne(s), du {debut:%d/%m/%Y} au {fin:%d/%m/%Y}. "
           "Gares et lignes couvertes : celles que la SNCF a comptées au moins une fois.")

st.subheader("Fréquentation journalière, par nature de donnée")
jours = query(f"""SELECT date, methode, sum(voyageurs) AS voyageurs FROM gold.compte_officiel
                  WHERE {WHERE} GROUP BY ALL ORDER BY date""", filtre)
large = jours.pivot_table(index="date", columns="methode", values="voyageurs", fill_value=0)
large = large.reindex(columns=[m for m in METHODES if m in large.columns])
st.area_chart(large, color=COULEURS[:len(large.columns)], y_label="voyageurs")

col1, col2 = st.columns(2)
with col1:
    st.subheader("Par ligne")
    par_ligne = query(f"""
        SELECT ligne, sum(voyageurs) AS voyageurs,
               round(sum(voyageurs) / count(DISTINCT date)) AS moyenne_par_jour,
               round(100 * coalesce(sum(voyageurs) FILTER (WHERE methode = 'MESURE'), 0) / sum(voyageurs), 1) AS pct_mesure,
               round(100 * coalesce(sum(voyageurs) FILTER (WHERE methode = 'EXTRAPOLE'), 0) / sum(voyageurs), 1) AS pct_extrapole,
               round(avg(score_confiance), 1) AS score
        FROM gold.compte_officiel WHERE {WHERE} GROUP BY 1 ORDER BY voyageurs DESC""", filtre)
    st.dataframe(par_ligne, width="stretch", hide_index=True)
with col2:
    st.subheader("Profil horaire (moyenne par jour)")
    profil = query(f"""
        SELECT tranche_horaire, type_jour, sum(voyageurs) / count(DISTINCT date) AS voyageurs
        FROM gold.compte_officiel WHERE {WHERE} GROUP BY ALL""", filtre)
    ordre = ["Avant 6h", "De 6h à 10h", "De 10h à 16h", "De 16h à 20h", "Après 20h"]
    profil = profil.pivot_table(index="tranche_horaire", columns="type_jour", values="voyageurs").reindex(ordre)
    st.bar_chart(profil, stack=False, y_label="voyageurs / jour")

st.subheader("Par gare")
gares = query(f"""
    SELECT code_gare, any_value(nom_gare) AS gare, list(DISTINCT ligne ORDER BY ligne) AS lignes,
           round(sum(voyageurs) / count(DISTINCT date)) AS moyenne_par_jour,
           round(avg(score_confiance), 1) AS score,
           count(*) FILTER (WHERE methode = 'MESURE') AS tranches_mesurees
    FROM gold.compte_officiel WHERE {WHERE} GROUP BY 1 ORDER BY moyenne_par_jour DESC""", filtre)
st.dataframe(gares, width="stretch", hide_index=True, height=300)

code = st.selectbox("Détail d'une gare", gares["code_gare"].tolist(),
                    format_func=lambda c: gares.set_index("code_gare").loc[c, "gare"])
if code:
    detail = query(f"""
        SELECT date, ligne, tranche_horaire, voyageurs, methode, score_confiance, alpha, alpha_n_journees,
               validations_jour, validations_attendues, part_tranche, source_profil, montants_mesures
        FROM gold.compte_officiel WHERE {WHERE} AND code_gare = ?
        ORDER BY date DESC, ligne, tranche_horaire""", (*filtre, code))
    st.line_chart(detail.groupby(["date", "ligne"], as_index=False)["voyageurs"].sum(),
                  x="date", y="voyageurs", color="ligne")
    with st.expander("Composants de chaque chiffre"):
        st.markdown("Réconcilié : `alpha × validations_jour × part_tranche / 100` · "
                    "Extrapolé : `alpha × validations_attendues × part_tranche / 100` · "
                    "Mesuré : somme des comptages SNCF de la tranche.")
        st.dataframe(detail, width="stretch", hide_index=True)

st.subheader("Étalonnage et validation croisée")
e1, e2 = st.columns(2)
with e1:
    st.markdown("**Facteurs alpha (gare × ligne)**")
    st.dataframe(query("""SELECT statut, count(*) AS paires, round(median(alpha), 3) AS alpha_median,
                                 round(median(n_journees), 1) AS journees_medianes
                          FROM gold.etalonnage GROUP BY 1 ORDER BY 2 DESC"""), width="stretch", hide_index=True)
with e2:
    st.markdown("**Journées mesurées : prédiction sans la journée vs mesure**")
    bt = query("SELECT mesure, prediction, ligne FROM gold.backtest")
    st.scatter_chart(bt, x="mesure", y="prediction", color="ligne")
