"""Dashboard 4 : simulateur d'écran de quai et d'application mobile (prévision + explication).

Lancer : streamlit run dashboards/app.py   (ou ce fichier seul)
Données : gold/previsions.parquet et gold/modele_evaluation.parquet (`comptage prevoir`).
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from comptage_train import api, prediction
from comptage_train.settings import data_dir, load_dotenv

load_dotenv()
DATA = data_dir()

st.set_page_config(page_title="D4 · Affluence prévue", layout="wide")
st.title("Dashboard 4 : affluence prévue (J+1 à J+7)")

COULEURS = {"FAIBLE": "#2ca02c", "MOYENNE": "#ff7f0e", "FORTE": "#d62728"}
PASTILLES = {"FAIBLE": "🟢", "MOYENNE": "🟠", "FORTE": "🔴"}
ORDRE = prediction.silver.TRANCHES


@st.cache_data(ttl=300)
def charger(path: str, mtime: float) -> pd.DataFrame:
    return pd.read_parquet(path)


prev_path = DATA / "gold" / "previsions.parquet"
if not prev_path.exists():
    st.info(f"Aucune prévision dans {DATA}. Lancer `comptage prevoir`.")
    st.stop()
prev = charger(str(prev_path), prev_path.stat().st_mtime)
prev["date"] = pd.to_datetime(prev["date"]).dt.date

gares = prev.groupby("code_gare")["nom_gare"].first().sort_values()
c1, c2, c3 = st.columns([3, 1, 2])
code = c1.selectbox("Gare", gares.index, format_func=gares.get,
                    index=list(gares.index).index("87271031") if "87271031" in gares.index else 0)
ligne = c2.selectbox("Ligne", sorted(prev.loc[prev.code_gare == code, "ligne"].unique()))
jours = sorted(prev["date"].unique())
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
libelle_jour = lambda d: f"{JOURS[d.weekday()]} {d:%d/%m}"
jour = c3.select_slider("Jour", jours, format_func=libelle_jour)
sel = prev[(prev.code_gare == code) & (prev.ligne == ligne) & (prev.date == jour)].copy()
sel["ordre"] = sel["tranche_horaire"].map({t: i for i, t in enumerate(ORDRE)})
sel = sel.sort_values("ordre")

# ------------------------------------------------------------------ écran de quai
st.subheader("Écran de quai")
tranche = st.radio("Tranche horaire", ORDRE, index=1, horizontal=True)
r = sel[sel.tranche_horaire == tranche].iloc[0]
couleur = COULEURS[r["niveau"]]
st.markdown(f"""
<div style="background:#0b1f3a;color:#fff;border-radius:10px;padding:18px 24px;font-family:sans-serif">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div><span style="background:#fff;color:#0b1f3a;border-radius:6px;padding:2px 10px;font-weight:700">{ligne}</span>
      &nbsp;<b style="font-size:1.3em">{r['nom_gare'].replace('-', ' ').title()}</b></div>
    <div style="opacity:.8">{libelle_jour(jour)} · {tranche}</div>
  </div>
  <div style="margin-top:14px;display:flex;align-items:center;gap:16px">
    <div style="background:{couleur};border-radius:8px;padding:8px 16px;font-weight:700;font-size:1.2em">
      {PASTILLES[r['niveau']]} Affluence {r['niveau'].lower()}</div>
    <div>{api.CONSEILS[r['niveau']]}</div>
  </div>
  <div style="margin-top:10px;opacity:.8">≈ {r['p50']:,} voyageurs montants prévus (entre {r['p10']:,} et {r['p90']:,})</div>
</div>""".replace(",", " "), unsafe_allow_html=True)
st.caption(f"Niveau calculé par rapport à l'historique de cette gare et de cette ligne (voyageurs par heure) : "
           f"la capacité des trains n'est pas publiée en Open Data. Météo {r['source_meteo'].lower()} : "
           f"{r['pluie_mm']:.1f} mm de pluie (affichée, non utilisée par le modèle : voir notebook 03).")

# ------------------------------------------------------------------ application mobile
col1, col2 = st.columns(2)
with col1:
    st.subheader("Application mobile : la journée")
    base = alt.Chart(sel).encode(x=alt.X("tranche_horaire:N", sort=ORDRE, title=None))
    cone = base.mark_area(opacity=.25, color="#1f77b4").encode(y=alt.Y("p10:Q", title="voyageurs"), y2="p90:Q")
    ligne_p50 = base.mark_line(point=True, color="#1f77b4").encode(
        y="p50:Q", tooltip=["tranche_horaire", "p10", "p50", "p90", "niveau"])
    pastilles = base.mark_circle(size=220).encode(
        y="p50:Q", color=alt.Color("niveau:N", scale=alt.Scale(domain=list(COULEURS), range=list(COULEURS.values())),
                                   legend=alt.Legend(title="Affluence")))
    st.altair_chart(cone + ligne_p50 + pastilles, use_container_width=True)
    st.caption("Zone bleue : intervalle P10-P90 (8 cas sur 10 attendus dedans, recalibré sur la période de test).")

# ------------------------------------------------------------------ explication
with col2:
    st.subheader("Pourquoi cette prévision ?")
    etapes = [("Niveau habituel", r["niveau_habituel"])]
    if abs(r["effet_modele"]) >= 1:
        etapes.append(("Écart moyen du modèle", r["effet_modele"]))
    etapes += [(f"{api.LIBELLES.get(e['variable'], e['variable'])} = {e['valeur']}", e["effet_voyageurs"])
               for e in sorted(r["contributions"], key=lambda e: -abs(e["effet_voyageurs"]))
               if abs(e["effet_voyageurs"]) >= 1]
    wf = pd.DataFrame(etapes, columns=["etape", "effet"])
    wf["fin"] = wf["effet"].cumsum()
    wf["debut"] = wf["fin"] - wf["effet"]
    wf["sens"] = ["base"] + ["hausse" if e > 0 else "baisse" for e in wf["effet"][1:]]
    total = pd.DataFrame([{"etape": "Prévision P50", "debut": 0, "fin": r["p50"], "effet": r["p50"], "sens": "total"}])
    wf = pd.concat([wf, total], ignore_index=True)
    chart = alt.Chart(wf).mark_bar().encode(
        y=alt.Y("etape:N", sort=None, title=None), x=alt.X("debut:Q", title="voyageurs"), x2="fin:Q",
        color=alt.Color("sens:N", scale=alt.Scale(domain=["base", "hausse", "baisse", "total"],
                                                  range=["#7f7f7f", "#d62728", "#2ca02c", "#1f77b4"]), legend=None),
        tooltip=["etape", alt.Tooltip("effet:Q", format="+.0f")])
    st.altair_chart(chart, use_container_width=True)
    st.caption("Contributions TreeSHAP du modèle P50, converties en voyageurs : leur somme donne la prévision.")

# ------------------------------------------------------------------ fiabilité
st.subheader("Fiabilité du modèle")
ev_path = DATA / "gold" / "modele_evaluation.parquet"
if ev_path.exists():
    ev = charger(str(ev_path), ev_path.stat().st_mtime).set_index("metrique")["valeur"]
    m1, m2, m3 = st.columns(3)
    m1.metric("Erreur du modèle (P50)", f"{ev['wape_modele_p50']:.1%}")
    m2.metric("Erreur de la référence « niveau habituel »", f"{ev['wape_reference_niveau_habituel']:.1%}")
    m3.metric("Couverture P10-P90", f"{ev['couverture_recalibree_p10_p90']:.0%}",
              help=f"Avant recalibrage : {ev['couverture_brute_p10_p90']:.0%}")
    st.caption("Évaluation sur les 60 derniers jours connus du compte officiel, jamais vus à l'apprentissage. "
               "Erreur pondérée par le volume (WAPE).")
