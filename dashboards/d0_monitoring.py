"""Dashboard 0 : santé de l'ingestion et fraîcheur des données.

Lancer : streamlit run dashboards/d0_monitoring.py
"""

from __future__ import annotations

import streamlit as st

from comptage_train import warehouse
from comptage_train.settings import data_dir, load_dotenv

load_dotenv()
DATA = data_dir()

st.set_page_config(page_title="D0 · Monitoring ingestion", layout="wide")
st.title("Dashboard 0 : monitoring de l'ingestion")


@st.cache_data(ttl=60)
def query(sql: str):
    # Les journaux sont des fichiers Parquet : lecture possible pendant une collecte.
    with warehouse.connect(DATA) as con:
        return con.execute(sql).df()


if not (DATA / "logs").exists():
    st.info(f"Aucun journal trouvé dans {DATA}. Lancer `comptage ingest`, "
            "ou récupérer l'historique de la branche `data` (voir README).")
    st.stop()

freshness = query("""
    SELECT source,
           max(finished_at) FILTER (WHERE outcome IN ('new', 'unchanged')) AS dernier_succes,
           max(finished_at) FILTER (WHERE outcome = 'new')                 AS dernier_changement,
           arg_max(outcome, finished_at)                                     AS dernier_statut,
           arg_max(message, finished_at)                                     AS dernier_message
    FROM monitoring.ingestion_runs GROUP BY source ORDER BY source
""")
calls = query("""
    SELECT date_trunc('day', started_at) AS jour, source,
           count(*) AS appels,
           count(*) FILTER (WHERE http_status BETWEEN 200 AND 299) AS ok,
           count(*) FILTER (WHERE http_status = 429)               AS quota_429,
           count(*) FILTER (WHERE http_status >= 500)              AS erreurs_5xx,
           count(*) FILTER (WHERE http_status IS NULL)             AS erreurs_reseau,
           median(duration_ms)                                     AS latence_mediane_ms,
           sum(bytes) / 1e6                                        AS volume_mo
    FROM monitoring.http_calls GROUP BY ALL ORDER BY jour DESC, source
""")
bronze = query("""
    SELECT source, count(*) AS fichiers, sum(bytes) / 1e6 AS volume_mo,
           min(ingested_at) AS premier_depot, max(ingested_at) AS dernier_depot
    FROM bronze_raw.files GROUP BY source ORDER BY source
""")

total = len(freshness)
en_erreur = int((freshness["dernier_statut"] == "error").sum())
c1, c2, c3 = st.columns(3)
c1.metric("Sources suivies", total)
c2.metric("Sources en erreur", en_erreur)
c3.metric("Fichiers bronze", int(bronze["fichiers"].sum()) if len(bronze) else 0)

st.subheader("Fraîcheur par source")
st.dataframe(freshness, width="stretch", hide_index=True)

st.subheader("Appels HTTP par jour")
st.dataframe(calls, width="stretch", hide_index=True)
if len(calls):
    st.line_chart(calls, x="jour", y="latence_mediane_ms", color="source")

st.subheader("Historique bronze")
st.dataframe(bronze, width="stretch", hide_index=True)
