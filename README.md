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
                    ┌─────────────────────────┴──────────────────────┐
                    ▼                                                ▼
   data/bronze/<source>/ingest_date=.../*        data/logs/<journal>/ingest_date=.../<run>.parquet
   fichiers bruts, lecture seule,                http_calls · ingestion_runs · bronze_files
   dédupliqués par SHA-256                       (un fichier par exécution, ajout seul)
                                                                     │
                                          DuckDB (vues en mémoire) ◄─┘
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
- **Journaux en Parquet, ajout seul** : rien n'est jamais réécrit, ce qui permet de
  versionner l'historique dans Git et de lire pendant une collecte.

## Collecte automatique (GitHub Actions)

Le workflow [`collecte.yml`](.github/workflows/collecte.yml) tourne 24h/24 et pousse
l'historique sur la branche orpheline **`data`** (le code reste sur `main`) :

| Déclencheur | Sources |
|---|---|
| toutes les heures (hh:07 UTC) | `hourly` : prévisions Open-Meteo |
| chaque jour 05:37 UTC | `daily` : SNCF, IDFM, calendriers, PRIM + `check-sources` |
| chaque lundi 06:37 UTC | `weekly` |

Le résumé de chaque exécution (onglet **Actions**) affiche le résultat par source.
Une exécution est marquée en échec si une source est en erreur, ce qui déclenche
un e-mail de GitHub.

- **Clé PRIM** : l'ajouter dans *Settings → Secrets and variables → Actions* sous le
  nom `PRIM_API_KEY`. Sans elle, la source PRIM est simplement ignorée (`skipped`).
- **Lancer à la main** : onglet Actions → Collecte → *Run workflow*.
- GitHub met en pause les workflows planifiés d'un dépôt public après 60 jours sans
  activité : il suffit alors de le réactiver dans l'onglet Actions.

### Consulter l'historique en local

```bash
git clone --branch data --single-branch https://github.com/quen9669/Comptage-Train-FR.git data
streamlit run dashboards/d0_monitoring.py      # lit ./data par défaut
```

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

Plus tard, déploiement sur VPS : voir [docs/deploiement_vps.md](docs/deploiement_vps.md).

## Sources et licences

Les données ne sont pas sur `main` (`data/` est ignoré) mais sur la branche `data`.
Elles restent soumises à la licence de chaque producteur (ODbL pour SNCF Open Data,
Licence Ouverte pour les portails publics) ; voir `config/sources.yaml`.
