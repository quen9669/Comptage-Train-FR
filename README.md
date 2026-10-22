# Comptage Train FR

Calcul de la fréquentation du réseau **Transilien** (Île-de-France) et prédiction
d'affluence, à partir **uniquement de données Open Data publiques** (SNCF Open Data,
Île-de-France Mobilités, Open-Meteo, calendriers officiels).

## Feuille de route

| Étape | Contenu | Dashboard | État |
|---|---|---|---|
| 1 | Ingestion historisée (bronze) + télémétrie des API | D0 Monitoring | ✅ |
| 2 | Qualification : règles RT/RM, score de confiance, lineage (silver) | D1, D2 | ✅ ([profilage](notebooks/01_profilage_donnees.ipynb), [règles](config/regles_qualite.yaml)) |
| 3 | Compte officiel : réconciliation, imputation (gold) | D3 | ✅ ([notebook](notebooks/02_compte_officiel.ipynb), [paramètres](config/compte_officiel.yaml)) |
| 4 | Prédiction LightGBM quantile + SHAP, API FastAPI | D4 | ✅ ([notebook](notebooks/03_prediction_affluence.ipynb), [paramètres](config/prediction.yaml)) |

## Profilage des données (étape 2A)

Le notebook [`notebooks/01_profilage_donnees.ipynb`](notebooks/01_profilage_donnees.ipynb)
examine chaque source réelle (schéma, couverture, anomalies, croisements possibles) et
justifie, constat chiffré à l'appui, la révision des 20 règles qualité de l'étape 2B.
Il est versionné **avec ses résultats** : il se lit directement sur GitHub.

```bash
git clone --branch data --single-branch https://github.com/quen9669/Comptage-Train-FR.git data
pip install -e ".[notebooks]"
jupyter lab notebooks/01_profilage_donnees.ipynb
```

## Qualification des données (étape 2B)

`comptage qualify` (lancé par la collecte quotidienne) reconstruit deux couches à partir
de la dernière version bronze de chaque source :

- **silver** ([`silver.py`](src/comptage_train/silver.py)) : comptages SNCF typés, dates à
  jour/mois inversés corrigées (et tracées), calendrier fériés / vacances zone C, validations
  IDFM normalisées d'un trimestre à l'autre, profils horaires ramenés aux 5 tranches SNCF,
  correspondance gare SNCF (UIC) ↔ zone IDFM par distance puis par nom, avec sa preuve ;
- **quality** ([`quality.py`](src/comptage_train/quality.py)) : les 22 règles de
  [`config/regles_qualite.yaml`](config/regles_qualite.yaml) appliquées à chaque comptage.

Chaque règle donne pour chaque comptage `OK`, `ATTENTION`, `ERREUR`, `PAS_TESTE` ou
`NON_APPLICABLE` avec la valeur observée (`quality.controles`). Chaque comptage reçoit un
statut, un score de confiance (base 90 − pénalités + bonus de croisement IDFM, plafond 98,
0 en cas d'erreur) et son lignage : fichier bronze, empreinte SHA-256, version du
producteur, version des règles (`quality.comptages_qualifies`). À bronze et date de
référence identiques, les fichiers produits sont identiques à l'octet près.

```bash
comptage qualify                    # écrit data/silver/*.parquet et data/quality/*.parquet
streamlit run dashboards/app.py     # D0 monitoring, D1 qualité, D2 lignage, D3 compte officiel
```

## Compte officiel (étape 3)

`comptage compte-officiel` (lancé après `comptage qualify` par la collecte quotidienne) produit
`gold.compte_officiel` : les voyageurs montants par **gare × ligne × date × tranche horaire**,
chaque jour depuis le début de la fenêtre IDFM et jusqu'à aujourd'hui
([`gold.py`](src/comptage_train/gold.py)). Chaque chiffre porte sa méthode :

| Méthode | Quand | Calcul | Confiance |
|---|---|---|---|
| `MESURE` | la SNCF a compté la tranche, sans erreur qualité | somme des comptages | score qualité (étape 2) |
| `RECONCILIE_IDFM` | validations IDFM publiées pour ce jour | α × validations du jour × part de la tranche | ≤ 80 |
| `EXTRAPOLE` | validations pas encore publiées | α × validations attendues (même mois, même catégorie de jour) × part | ≤ 60 |

α (gare × ligne) est appris sur les journées mesurées (`gold.etalonnage`). En validation croisée,
une journée reconstruite s'écarte de la mesure de 10 % en médiane (`gold.backtest`). Le
[notebook 02](notebooks/02_compte_officiel.ipynb) justifie chaque choix : granularité d'α, profil
horaire, modulation journalière, effet (faible) de la météo. Les composants de chaque chiffre
(α, validations, part de la tranche, comptages source) sont gardés pour refaire le calcul.
`gold/compte_officiel/` est découpé par mois : Git ne stocke chaque jour que le mois qui change.

```bash
comptage qualify && comptage compte-officiel
streamlit run dashboards/app.py     # + D3 compte officiel
```

## Prévision de l'affluence (étape 4)

`comptage prevoir` (lancé après le compte officiel par la collecte quotidienne) prévoit les
voyageurs montants par **gare × ligne × tranche horaire, de J+1 à J+7**
([`prediction.py`](src/comptage_train/prediction.py)) :

* `voyageurs = niveau habituel × indice du jour`. Le niveau habituel est la médiane du compte
  officiel (même gare, ligne, tranche et catégorie de jour IDFM). L'indice du jour vient de trois
  modèles LightGBM quantile (P10, P50, P90) appris sur le calendrier (jour, férié, vacances, pont,
  fêtes de fin d'année) et pondérés par le score de confiance ;
* l'intervalle P10-P90 est recalibré sur les 60 derniers jours connus pour couvrir vraiment 80 % des cas ;
* chaque prévision est expliquée (TreeSHAP) en voyageurs : niveau habituel + effet de chaque variable ;
* le niveau FAIBLE / MOYENNE / FORTE compare l'intensité prévue à l'historique de la gare et de la
  ligne (la capacité des trains n'est pas en Open Data).

Sur la période de test (mai-juin 2026), l'erreur pondérée est de 18,1 % contre 18,8 % pour le
niveau habituel seul ; le gain vient surtout des ponts et des fériés. Un férié est vu par le modèle
comme un dimanche, comme dans la catégorie IDFM : sur les 11 fériés de l'historique, chacun prévu
sans l'avoir appris, l'erreur passe de 28,2 % à 22,9 % (notebook 03 §3 bis). La météo a été
testée puis écartée car elle dégradait l'erreur ([notebook 03](notebooks/03_prediction_affluence.ipynb) §4).
Les métriques de chaque entraînement sont dans `gold/modele_evaluation.parquet`.

```bash
pip install -e ".[api,dashboards]"
comptage prevoir                               # gold/previsions.parquet + modèle dans data/models/
streamlit run dashboards/app.py                # + D4 affluence (écran de quai, intervalle, explication)
uvicorn comptage_train.api:app --reload        # API : http://127.0.0.1:8000/docs
curl "http://127.0.0.1:8000/api/v1/affluence?gare=87271031&ligne=B&date=2026-10-22&tranche=De%206h%20%C3%A0%2010h"
```

Endpoints : `/api/v1/gares`, `/api/v1/affluence` (une tranche), `/api/v1/affluence/journee`
(les 5 tranches). Chaque réponse donne P50, intervalle, niveau, badge couleur, conseil voyageur et
explication en une phrase.

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
| chaque jour 05:37 UTC | `daily` : SNCF, IDFM, calendriers, météo historique, PRIM + `check-sources`, puis qualification, compte officiel et prévisions J+1 à J+7 |
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
streamlit run dashboards/app.py      # lit ./data par défaut
```

## Démarrage local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dashboards,dev]"
cp .env.example .env            # renseigner PRIM_API_KEY quand elle est disponible
comptage check-sources          # vérifie que chaque jeu de données existe
comptage ingest                 # collecte toutes les sources actives
comptage qualify                # silver + règles de qualité
comptage compte-officiel        # compte officiel (gold)
comptage prevoir                # prévisions J+1 à J+7 (gold) ; API : uvicorn comptage_train.api:app
streamlit run dashboards/app.py
pytest
```

Plus tard, déploiement sur VPS : voir [docs/deploiement_vps.md](docs/deploiement_vps.md).

## Sources et licences

Les données ne sont pas sur `main` (`data/` est ignoré) mais sur la branche `data`.
Elles restent soumises à la licence de chaque producteur (ODbL pour SNCF Open Data,
Licence Ouverte pour les portails publics) ; voir `config/sources.yaml`.
