# Historique de collecte (branche `data`)

Cette branche est alimentée automatiquement par le workflow **Collecte**
(GitHub Actions). Ne pas la modifier à la main.

- `bronze/<source>/ingest_date=AAAA-MM-JJ/` : fichiers bruts tels que téléchargés (JSON compressé en `.json.gz`)
- `logs/http_calls/`, `logs/ingestion_runs/`, `logs/bronze_files/`, `logs/quality_runs/`, `logs/gold_runs/`, `logs/previsions/` : journaux Parquet (ajout seul)
- `silver/` : données typées et normalisées, recalculées depuis le bronze par `comptage qualify`
- `quality/` : résultat de chaque règle de qualité, statut, score de confiance et lignage de chaque comptage
- `gold/` : compte officiel (`compte_officiel/mois=AAAA-MM.parquet`), facteurs d'étalonnage et validation croisée ; prévisions J+1 à J+7 (`previsions.parquet`) et
  évaluation du modèle (`modele_evaluation.parquet`)
- `models/` (non versionné) : modèles LightGBM du dernier entraînement, recalculés chaque jour

## Sources et licences

Les données appartiennent à leurs producteurs et restent sous leur licence :

- SNCF Open Data (ressources.data.sncf.com) : ODbL
- Île-de-France Mobilités (data.iledefrance-mobilites.fr, PRIM) : voir chaque jeu
- Ministère de l'Éducation nationale (data.education.gouv.fr) : Licence Ouverte
- calendrier.api.gouv.fr : Licence Ouverte
- Open-Meteo (open-meteo.com) : CC BY 4.0
