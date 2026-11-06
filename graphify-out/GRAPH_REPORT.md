# Graph Report - comptage-train-fr  (2026-11-06)

## Corpus Check
- 36 files · ~20,461 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 9 file(s) not represented in the graph (top: .ipynb 3, .service 2, .timer 2)

## Summary
- 470 nodes · 837 edges · 21 communities (18 shown, 3 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 25 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `96742390`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- ingest.py
- gold.py
- prediction.py
- steps
- test_prediction.py
- seuils
- pathlib
- comptage_train/__init__.py
- api.py
- prediction.yaml
- test_ingest.py
- compte_officiel.yaml
- params
- silver.py
- Comptage Train FR
- quality.py
- steps
- test_quality.py
- Historique de collecte (branche `data`)
- CLAUDE.md
- comptage-train

## God Nodes (most connected - your core abstractions)
1. `run_ingestion()` - 18 edges
2. `fetch()` - 15 edges
3. `_ingest_one()` - 15 edges
4. `main()` - 13 edges
5. `run_gold()` - 13 edges
6. `run_quality()` - 13 edges
7. `build_silver()` - 13 edges
8. `seuils` - 11 edges
9. `params` - 11 edges
10. `build_gold()` - 11 edges

## Surprising Connections (you probably didn't know these)
- `Sources et licences` --references--> `main()`  [INFERRED]
  README.md → src/comptage_train/cli.py
- `Collecte automatique (GitHub Actions)` --references--> `main()`  [INFERRED]
  README.md → src/comptage_train/cli.py
- `1. Préparer le serveur` --references--> `comptage()`  [INFERRED]
  docs/deploiement_vps.md → tests/conftest.py
- `test_no_retry_on_404()` --uses--> `FetchError`  [INFERRED]
  tests/test_ingest.py → src/comptage_train/http_client.py
- `test_no_retry_on_404()` --calls--> `fetch()`  [EXTRACTED]
  tests/test_ingest.py → src/comptage_train/http_client.py

## Import Cycles
- None detected.

## Communities (21 total, 3 thin omitted)

### Community 0 - "ingest.py"
Cohesion: 0.08
Nodes (20): _check_sources(), main(), build_request(), catalog_url(), parse_version(), RequestSpec, resolve_params(), SourceNotReady (+12 more)

### Community 1 - "gold.py"
Cohesion: 0.09
Nodes (18): _attendu(), _backtest(), build_gold(), _compte(), Config, _etalonnage(), load_config(), _mesures() (+10 more)

### Community 2 - "prediction.py"
Cohesion: 0.13
Nodes (15): query(), texte(), _apprendre(), charger_modele(), Config, entrainer(), grille(), historique() (+7 more)

### Community 3 - "steps"
Cohesion: 0.06
Nodes (34): env, runs-on, steps, timeout-minutes, concurrency, cancel-in-progress, group, COMPTAGE_DATA_DIR (+26 more)

### Community 4 - "test_prediction.py"
Cohesion: 0.08
Nodes (12): compte(), gcon(), test_hierarchie_des_methodes(), test_run_gold_reproductible(), config(), data_root(), _historique_feries(), resultat() (+4 more)

### Community 5 - "seuils"
Cohesion: 0.08
Nodes (24): regles, calibration, description, id, nom, non_applicable, penalite, seuils (+16 more)

### Community 6 - "pathlib"
Cohesion: 0.13
Nodes (9): bronze_path(), encode(), sha256(), write_immutable(), comptage(), con(), data_root(), _parquet() (+1 more)

### Community 7 - "comptage_train/__init__.py"
Cohesion: 0.10
Nodes (4): query(), query(), query(), charger()

### Community 8 - "api.py"
Cohesion: 0.18
Nodes (11): affluence(), affluence_journee(), Effet, explication(), gares(), Prevision, previsions(), _previsions() (+3 more)

### Community 9 - "prediction.yaml"
Cohesion: 0.09
Nodes (21): couverture_cible, duree_tranches_h, Après 20h, Avant 6h, De 10h à 16h, De 16h à 20h, De 6h à 10h, ferie_comme_dimanche (+13 more)

### Community 10 - "test_ingest.py"
Cohesion: 0.19
Nodes (12): run_ingestion(), client_for(), no_sleep(), test_error_is_logged_not_raised(), test_ingestion_writes_bronze_and_skips_unchanged(), handler(), test_json_is_stored_gzipped(), test_logs_are_append_only_parquet_files() (+4 more)

### Community 11 - "compte_officiel.yaml"
Cohesion: 0.10
Nodes (20): etalonnage, calibration, date_min, extrapolation, meteo, methode, penalite_dispersion, penalite_une_seule_mesure (+12 more)

### Community 12 - "params"
Cohesion: 0.10
Nodes (20): daily, end_date, forecast_days, hourly, latitude, LineRef, longitude, past_days (+12 more)

### Community 13 - "silver.py"
Cohesion: 0.19
Nodes (13): BronzeFile, build_silver(), _calendar(), columns_of(), _comptages(), _gares(), latest_files(), _nom_sql() (+5 more)

### Community 14 - "Comptage Train FR"
Cohesion: 0.11
Nodes (17): 1. Préparer le serveur, 2. Vérifier les sources, 3. Planifier la collecte, 4. Dashboard, 5. Sauvegarde, Déploiement sur un VPS, Architecture de l'étape 1, Collecte automatique (GitHub Actions) (+9 more)

### Community 15 - "quality.py"
Cohesion: 0.21
Nodes (8): _features(), load_rules(), _q(), qualify(), Rule, _rule_sql(), RuleSet, summary()

### Community 16 - "steps"
Cohesion: 0.12
Nodes (15): jobs, pytest, name, on, pull_request, push, branches, runs-on (+7 more)

### Community 17 - "test_quality.py"
Cohesion: 0.22
Nodes (7): one(), test_chaque_ligne_a_toutes_les_regles(), test_date_inversee_corrigee(), test_profils_ramenes_aux_tranches_sncf(), test_run_quality_reproductible(), test_statuts_et_regles(), test_validations_normalisees_et_sommees()

## Knowledge Gaps
- **1 isolated node(s):** `comptage-train`
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 225 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `ingest.py` to `gold.py`, `prediction.py`, `api.py`, `test_ingest.py`, `Comptage Train FR`?**
  _High betweenness centrality (0.041) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `run_ingestion()` (e.g. with `Source` and `.flush()`) actually correct?**
  _`run_ingestion()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `comptage-train` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `ingest.py` be split into smaller, more focused modules?**
  _Cohesion score 0.07686932215234102 - nodes in this community are weakly interconnected._
- **Why does `run_ingestion()` connect `test_ingest.py` to `ingest.py`, `gold.py`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `_ingest_one()` (e.g. with `CallRecord` and `FetchError`) actually correct?**
  _`_ingest_one()` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Should `gold.py` be split into smaller, more focused modules?**
  _Cohesion score 0.0859465737514518 - nodes in this community are weakly interconnected._