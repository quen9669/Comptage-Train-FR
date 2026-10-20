# Comptage Train FR

Calcul de la fréquentation du réseau **Transilien** (Île-de-France) et prédiction
d'affluence, à partir **uniquement de données Open Data publiques** (SNCF Open Data,
Île-de-France Mobilités, Open-Meteo, calendriers officiels).

## Feuille de route

| Étape | Contenu | Dashboard | État |
|---|---|---|---|
| 1 | Ingestion historisée (bronze) + télémétrie des API | D0 Monitoring | en cours |
| 2 | Qualification : règles RT/RM, score de confiance, lineage (silver) | D1, D2 | à venir |
| 3 | Compte officiel : réconciliation, imputation (gold) | D3 | à venir |
| 4 | Prédiction LightGBM quantile + SHAP, API FastAPI | D4 | à venir |

## Architecture de l'étape 1

```
config/sources.yaml ──► connecteurs ──► http_client (retries + télémétrie)
                                              │
                    ┌─────────────────────────┴─────────────────────┐
                    ▼                                               ▼
   data/bronze/<source>/ingest_date=.../*.parquet|json   data/warehouse.duckdb
   (fichiers bruts, lecture seule, dédupliqués par SHA-256)  monitoring.http_calls
                                                             monitoring.ingestion_runs
                                                             bronze_raw.files
                                                                    │
                                                                    ▼
                                                   dashboards/d0_monitoring.py
```

- **Bronze immuable** : chaque téléchargement est conservé tel quel ; un contenu
  identique au précédent n'est pas réécrit (seul le passage est journalisé).
- **Télémétrie** : chaque tentative HTTP est enregistrée (statut, latence, taille),
  et chaque source a un résultat par exécution (`new`, `unchanged`, `skipped`, `error`).
- **Nouvelles tentatives** sur erreurs réseau, 429 et 5xx, avec attente exponentielle
  et respect de `Retry-After`.

## Démarrage local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dashboards,dev]"
cp .env.example .env            # renseigner PRIM_API_KEY quand elle est disponible
comptage check-sources          # vérifie que chaque jeu de données existe
comptage ingest                 # collecte toutes les sources actives
streamlit run dashboards/d0_monitoring.py
pytest
```

Déploiement continu sur VPS : voir [docs/deploiement_vps.md](docs/deploiement_vps.md).

## Sources et licences

Les données ne sont pas versionnées dans ce dépôt (`data/` est ignoré). Elles
restent soumises à la licence de chaque producteur (ODbL pour SNCF Open Data,
Licence Ouverte pour les portails publics) ; voir `config/sources.yaml`.
