"""Dashboard 0 : santé de l'ingestion et fraîcheur des données.

Lancer : streamlit run dashboards/d0_monitoring.py
"""

from __future__ import annotations

import duckdb
import streamlit as st

from comptage_train.settings import data_dir, load_dotenv

load_dotenv()
DB = data_dir() / "warehouse.duckdb"

st.set_page_config(page_title="D0 · Monitoring ingestion", layout="wide")
st.title("Dashboard 0 : monitoring de l'ingestion")


@st.cache_data(ttl=60)
def query(sql: str):
    # Lecture seule et connexion courte : la collecte garde la main en écriture.
    with duckdb.connect(str(DB), read_only=True) as con:
        return con.execute(sql).df()


if not DB.exists():
    st.info(f"Aucune base trouvée ({DB}). Lancer d'abord `comptage ingest`.")
    st.stop()

try:
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
except duckdb.IOException:
    st.warning("Base occupée par une collecte en cours, réessayer dans un instant.")
    st.stop()

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
