"""Dashboard 1 : qualité des comptages (règles RT/RM, statuts, scores).

Lancer : streamlit run dashboards/app.py   (ou ce fichier seul)
Données : tables écrites par `comptage qualify` dans data/silver et data/quality.
"""

from __future__ import annotations

import streamlit as st

from comptage_train import warehouse
from comptage_train.settings import data_dir, load_dotenv

load_dotenv()
DATA = data_dir()

st.set_page_config(page_title="D1 · Qualité des données", layout="wide")
st.title("Dashboard 1 : qualité des comptages")


@st.cache_data(ttl=300)
def query(sql: str):
    with warehouse.connect_derived(DATA) as con:
        return con.execute(sql).df()


if not (DATA / "quality" / "comptages_qualifies.parquet").exists():
    st.info(f"Aucun résultat de qualité dans {DATA}. Lancer `comptage qualify`.")
    st.stop()

statuts = query("""
    SELECT statut, count(*) AS lignes, round(avg(score_confiance), 1) AS score_moyen
    FROM quality.comptages_qualifies GROUP BY statut ORDER BY statut""")
meta = query("""SELECT any_value(version_regles) AS version, any_value(date_reference) AS date_ref,
                       count(*) AS lignes, round(avg(score_confiance), 1) AS score
                FROM quality.comptages_qualifies""").iloc[0]

total = int(meta["lignes"])
par_statut = dict(zip(statuts["statut"], statuts["lignes"]))
c1, c2, c3, c4 = st.columns(4)
c1.metric("Lignes de comptage", f"{total:,}".replace(",", " "))
c2.metric("Valides", f"{par_statut.get('VALIDE', 0) / total:.0%}")
c3.metric("En attention", f"{par_statut.get('ATTENTION', 0) / total:.0%}")
c4.metric("En erreur", f"{par_statut.get('ERREUR', 0) / total:.0%}")
st.caption(f"Règles version {meta['version']} · date de référence {meta['date_ref']:%d/%m/%Y} · "
           f"score de confiance moyen {meta['score']}")

st.subheader("Résultat de chaque règle")
st.caption("Une ligne par règle : combien de comptages la passent, la violent, ou n'ont pas pu être testés.")
regles = query("""
    SELECT r.regle, r.nom, r.severite,
           count(*) FILTER (WHERE k.resultat = 'OK') AS ok,
           count(*) FILTER (WHERE k.resultat = 'ATTENTION') AS attention,
           count(*) FILTER (WHERE k.resultat = 'ERREUR') AS erreur,
           count(*) FILTER (WHERE k.resultat = 'PAS_TESTE') AS pas_teste,
           count(*) FILTER (WHERE k.resultat = 'NON_APPLICABLE') AS non_applicable,
           any_value(r.seuils) AS seuils, any_value(r.calibration) AS calibration
    FROM quality.regles r JOIN quality.controles k USING (regle)
    GROUP BY r.regle, r.nom, r.severite ORDER BY left(r.regle, 2) DESC, r.regle""")
st.dataframe(regles, width="stretch", hide_index=True)
st.bar_chart(regles, x="regle", y=["attention", "erreur", "pas_teste"], stack=True,
             color=["#f28e2b", "#d62728", "#bab0ac"], x_label="règle", y_label="comptages")

col1, col2 = st.columns(2)
with col1:
    st.subheader("Score par ligne")
    lignes = query("""
        SELECT ligne, count(*) AS comptages, round(avg(score_confiance), 1) AS score_moyen,
               round(100 * avg((statut = 'VALIDE')::INT), 1) AS pct_valides
        FROM quality.comptages_qualifies GROUP BY ligne ORDER BY ligne""")
    st.dataframe(lignes, width="stretch", hide_index=True)
with col2:
    st.subheader("Distribution des scores")
    scores = query("""SELECT score_confiance, count(*) AS lignes FROM quality.comptages_qualifies
                      GROUP BY 1 ORDER BY 1""")
    st.bar_chart(scores, x="score_confiance", y="lignes")

st.subheader("Historique des exécutions")
historique = query("""
    SELECT run_at, version_regles, resultat AS statut, lignes
    FROM monitoring.quality_runs WHERE regle = 'STATUT' ORDER BY run_at""")
if len(historique):
    st.line_chart(historique, x="run_at", y="lignes", color="statut")
else:
    st.caption("Pas encore d'historique.")

col3, col4 = st.columns(2)
with col3:
    st.subheader("Contrats de schéma (RT_10)")
    st.dataframe(query("SELECT * FROM silver.schema_contrats ORDER BY source"), width="stretch", hide_index=True)
with col4:
    st.subheader("Correspondance gares SNCF ↔ zones IDFM")
    st.dataframe(query("""SELECT statut, count(*) AS gares, median(distance_m) AS distance_mediane_m
                          FROM silver.correspondance_gares GROUP BY 1 ORDER BY 2 DESC"""),
                 width="stretch", hide_index=True)
    with st.expander("Gares non appariées automatiquement à moins de 300 m"):
        st.dataframe(query("""SELECT code_gare, nom_gare, nom_zdc, distance_m, similarite_nom,
                                     zone_la_plus_proche, statut
                              FROM silver.correspondance_gares WHERE statut <> 'APPARIEE'"""),
                     width="stretch", hide_index=True)
