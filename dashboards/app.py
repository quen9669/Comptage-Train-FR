"""Point d'entrée des dashboards : streamlit run dashboards/app.py"""

from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent
st.navigation([
    st.Page(HERE / "d0_monitoring.py", title="D0 · Monitoring ingestion"),
    st.Page(HERE / "d1_qualite.py", title="D1 · Qualité des données"),
    st.Page(HERE / "d2_lignage.py", title="D2 · Lignage et confiance"),
]).run()
