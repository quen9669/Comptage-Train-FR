"""Dashboard 2 : lignage et confiance d'un comptage, règle par règle.

Lancer : streamlit run dashboards/app.py   (ou ce fichier seul)
Pour une gare et une date : chaque comptage, son score, et pour chacun la
preuve de chaque règle (valeur observée, seuil) jusqu'au fichier bronze d'origine.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from comptage_train import quality, warehouse
from comptage_train.settings import data_dir, load_dotenv

load_dotenv()
DATA = data_dir()

st.set_page_config(page_title="D2 · Lignage et confiance", layout="wide")
st.title("Dashboard 2 : lignage et confiance")


@st.cache_data(ttl=300)
def query(sql: str, params: tuple = ()):
    with warehouse.connect_derived(DATA) as con:
        return con.execute(sql, list(params)).df()


if not (DATA / "quality" / "comptages_qualifies.parquet").exists():
    st.info(f"Aucun résultat de qualité dans {DATA}. Lancer `comptage qualify`.")
    st.stop()

gares = query("""SELECT code_gare, any_value(nom_gare) AS nom, round(avg(score_confiance), 1) AS score
                 FROM quality.comptages_qualifies GROUP BY 1 ORDER BY nom""")
libelles = {r.code_gare: f"{r.nom} ({r.code_gare}) · score {r.score}" for r in gares.itertuples()}
col1, col2, col3 = st.columns([2, 1, 1])
code = col1.selectbox("Gare", list(libelles), format_func=libelles.get)
dates = query("SELECT DISTINCT date FROM quality.comptages_qualifies WHERE code_gare = ? ORDER BY date DESC",
              (code,))["date"].tolist()
jour = col2.selectbox("Date", dates, format_func=lambda d: d.strftime("%d/%m/%Y"))
statut = col3.multiselect("Statut", ["VALIDE", "ATTENTION", "ERREUR"], default=["VALIDE", "ATTENTION", "ERREUR"])

lignes = query("""
    SELECT row_id, ligne, axe, type_jour, tranche_horaire, montants, statut, score_confiance,
           regles_attention, regles_erreur, regles_non_testees, date_publiee
    FROM quality.comptages_qualifies WHERE code_gare = ? AND date = ?
    ORDER BY ligne, axe, tranche_horaire""", (code, jour))
lignes = lignes[lignes["statut"].isin(statut)]
st.subheader("Comptages")
choix = st.dataframe(lignes.drop(columns="row_id"), width="stretch", hide_index=True,
                     on_select="rerun", selection_mode="single-row")
if not len(lignes):
    st.stop()
rang = choix.selection.rows[0] if choix.selection.rows else 0
row_id = lignes.iloc[rang]["row_id"]

detail = query("SELECT * FROM quality.comptages_qualifies WHERE row_id = ?", (row_id,)).iloc[0]
st.subheader(f"Ligne {detail['ligne']} · {detail['axe']} · {detail['tranche_horaire']} : "
             f"{detail['montants']} montées, score {detail['score_confiance']}")

c1, c2 = st.columns([3, 2])
with c1:
    st.markdown("**Contrôles appliqués**")
    controles = query("""
        SELECT k.regle, r.nom, k.resultat, k.valeur_observee, r.seuils, r.penalite, r.description,
               r.non_applicable
        FROM quality.controles k JOIN quality.regles r USING (regle)
        WHERE k.row_id = ? ORDER BY k.regle""", (row_id,))
    st.dataframe(controles, width="stretch", hide_index=True)
with c2:
    st.markdown("**Calcul du score**")
    if detail["statut"] == "ERREUR":
        st.write(f"Règle(s) en erreur {', '.join(detail['regles_erreur'])} : score = 0.")
    else:
        rules = quality.load_rules()
        bonus = rules.bonus if detail["bonus_croisement"] else 0
        st.write(f"Base {rules.base} − pénalités {int(detail['penalites'])} + bonus croisement IDFM {bonus} "
                 f"= **{detail['score_confiance']}** (plafond {rules.plafond})")
    st.markdown("**Lignage**")
    def texte(value):
        return None if pd.isna(value) else str(value)

    st.json({
        "source": texte(detail["source"]),
        "fichier_bronze": texte(detail["bronze_path"]),
        "sha256_bronze": texte(detail["bronze_sha256"]),
        "version_producteur": texte(detail["source_version"]),
        "version_regles": texte(detail["version_regles"]),
        "date_publiee": f"{detail['date_publiee']:%d/%m/%Y}",
        "date_retenue": f"{detail['date']:%d/%m/%Y}",
        "valeur_brute": texte(detail["montants_brut"]),
        "zone_idfm": None if pd.isna(detail["id_zdc"]) else int(detail["id_zdc"]),
        "correspondance": texte(detail["statut_correspondance"]),
    })
