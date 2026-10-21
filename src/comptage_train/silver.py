"""Couche silver : données typées, normalisées et reliées entre sources.

Chaque table silver est reconstruite à partir de la DERNIÈRE version bronze de
ses sources (le bronze reste la vérité brute ; la silver est recalculable à
l'identique). Les choix de normalisation viennent du notebook de profilage
(notebooks/01_profilage_donnees.ipynb) :

  * comptages SNCF : clé avec l'axe, dates à jour/mois inversés corrigées
    quand l'inversion est la seule explication cohérente (§2.6) ;
  * validations IDFM : colonnes renommées et typées d'un trimestre à l'autre
    (`ida` -> `id_zdc`, `71370.0` -> 71370), catégories normalisées (§4) ;
  * correspondance gare SNCF (UIC) <-> zone IDFM (ZdC) par distance et nom (§8).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

# Colonnes attendues par source : le « contrat de schéma » (règle RT_10).
# Une colonne listée dans `renames` est acceptée sous son ancien nom.
SCHEMA_CONTRACTS: dict[str, dict] = {
    "sncf_comptage_montants_trains": {
        "columns": ["nom_gare", "code_gare", "type_jour", "date", "annee", "ligne", "axe",
                    "tranche_horaire", "somme_de_montants"]},
    "idfm_validations_ferre_jour": {
        "columns": ["jour", "code_stif_trns", "code_stif_res", "code_stif_arret", "id_zdc",
                    "libelle_arret", "categorie_titre", "nb_vald"],
        "renames": {"ida": "id_zdc"}},
    "idfm_validations_ferre_profils_horaires": {
        "columns": ["code_stif_trns", "code_stif_res", "code_stif_arret", "id_zdc", "libelle_arret",
                    "cat_jour", "trnc_horr_60", "pourcentage_validations"],
        "renames": {"ida": "id_zdc"}},
}

TRANCHES = ["Avant 6h", "De 6h à 10h", "De 10h à 16h", "De 16h à 20h", "Après 20h"]

# Heures IDFM (début de créneau) -> tranche SNCF, pour comparer les profils (RM_07)
TRANCHE_SQL = """CASE WHEN h < 6 THEN 'Avant 6h' WHEN h < 10 THEN 'De 6h à 10h'
                      WHEN h < 16 THEN 'De 10h à 16h' WHEN h < 20 THEN 'De 16h à 20h'
                      ELSE 'Après 20h' END"""

# Seuil d'appariement géographique gare SNCF <-> zone IDFM (constat 8.3)
MATCH_DISTANCE_M = 300
# À défaut, appariement par le nom : similarité minimale et éloignement maximal
NAME_MATCH_MIN_SIM = 0.9
NAME_MATCH_MAX_M = 2000


@dataclass(frozen=True)
class BronzeFile:
    source: str
    path: str          # chemin absolu
    relative: str      # chemin relatif à data/ (pour le lineage)
    sha256: str
    version: str | None


def latest_files(con: duckdb.DuckDBPyConnection, data_root: Path) -> dict[str, BronzeFile]:
    rows = con.execute("""
        SELECT source, arg_max(path, ingested_at), arg_max(sha256, ingested_at),
               arg_max(source_version, ingested_at)
        FROM bronze_raw.files GROUP BY source""").fetchall()
    return {s: BronzeFile(s, (data_root / p).as_posix(), p, sha, v) for s, p, sha, v in rows}


def read_json(path: str):
    raw = Path(path).read_bytes()
    return json.loads(gzip.decompress(raw) if path.endswith(".gz") else raw)


def columns_of(con, path: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()
            if r[0] != "ingest_date"]


def build_silver(con: duckdb.DuckDBPyConnection, data_root: Path,
                 date_reference: str | None = None) -> dict[str, BronzeFile]:
    """Crée le schéma `silver` dans `con`. Renvoie les fichiers bronze utilisés.

    `date_reference` (AAAA-MM-JJ, défaut : aujourd'hui) fixe ce qu'est une date « future » :
    à bronze et date identiques, la silver est identique.
    """
    date_reference = date_reference or date.today().isoformat()
    files = latest_files(con, data_root)
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    _schema_contracts(con, files)
    _calendar(con, files)
    _comptages(con, files, date_reference)
    _validations(con, files)
    _profils(con, files)
    _gares(con, files)
    return files


# --------------------------------------------------------------------------- contrats
def _schema_contracts(con, files: dict[str, BronzeFile]) -> None:
    rows = []
    for source, f in sorted(files.items()):
        family = next((k for k in SCHEMA_CONTRACTS if source.startswith(k)), None)
        if family is None or f.path.endswith((".json", ".json.gz")):
            continue
        contract = SCHEMA_CONTRACTS[family]
        actual = columns_of(con, f.path)
        renamed = [old for old in contract.get("renames", {}) if old in actual]
        mapped = [contract.get("renames", {}).get(c, c) for c in actual]
        missing = [c for c in contract["columns"] if c not in mapped]
        extra = [c for c in mapped if c not in contract["columns"]]
        rows.append([source, f.relative, missing, extra, renamed])
    con.execute("""CREATE OR REPLACE TABLE silver.schema_contrats (
        source VARCHAR, bronze_path VARCHAR, colonnes_manquantes VARCHAR[],
        colonnes_inattendues VARCHAR[], colonnes_renommees VARCHAR[])""")
    if rows:
        con.executemany("INSERT INTO silver.schema_contrats VALUES (?, ?, ?, ?, ?)", rows)


# --------------------------------------------------------------------------- calendrier
def _calendar(con, files) -> None:
    feries = read_json(files["jours_feries_metropole"].path) if "jours_feries_metropole" in files else {}
    con.execute("CREATE OR REPLACE TEMP TABLE _feries (jour DATE, libelle VARCHAR)")
    if feries:
        con.executemany("INSERT INTO _feries VALUES (?, ?)", list(feries.items()))
    vacances = "SELECT NULL::DATE AS debut, NULL::DATE AS fin, NULL AS libelle WHERE false"
    if "education_calendrier_scolaire" in files:
        # Dates publiées en UTC (22:00Z = minuit à Paris) : conversion avant comparaison.
        # Le jour de fin est le jour de reprise, donc exclu (constat 6.1).
        vacances = f"""SELECT DISTINCT (start_date AT TIME ZONE 'Europe/Paris')::DATE AS debut,
                              (end_date AT TIME ZONE 'Europe/Paris')::DATE AS fin, description AS libelle
                       FROM '{files["education_calendrier_scolaire"].path}'
                       WHERE zones = 'Zone C' AND description LIKE 'Vacances%'
                         AND population IN ('-', 'Élèves')"""
    con.execute(f"""
        CREATE OR REPLACE TABLE silver.calendrier AS
        WITH jours AS (SELECT unnest(range(DATE '2019-01-01', DATE '2028-01-01', INTERVAL 1 DAY))::DATE AS jour),
             vac AS ({vacances})
        SELECT j.jour, isodow(j.jour) AS jour_semaine,
               f.libelle IS NOT NULL AS est_ferie, f.libelle AS ferie,
               v.libelle IS NOT NULL AS vacances_zone_c, v.libelle AS vacances,
               CASE WHEN isodow(j.jour) = 7 OR f.libelle IS NOT NULL THEN 'DIM'
                    WHEN isodow(j.jour) = 6 THEN 'SAM' ELSE 'JOB' END AS type_jour_sncf,
               CASE WHEN isodow(j.jour) = 7 OR f.libelle IS NOT NULL THEN 'DIJFP'
                    WHEN isodow(j.jour) = 6 THEN IF(v.libelle IS NULL, 'SAHV', 'SAVS')
                    ELSE IF(v.libelle IS NULL, 'JOHV', 'JOVS') END AS cat_jour_idfm
        FROM jours j
        LEFT JOIN _feries f ON f.jour = j.jour
        LEFT JOIN (SELECT jour, min(libelle) AS libelle FROM (
                     SELECT unnest(range(debut, fin, INTERVAL 1 DAY))::DATE AS jour, libelle FROM vac)
                   GROUP BY jour) v ON v.jour = j.jour
        ORDER BY j.jour""")


# --------------------------------------------------------------------------- comptages SNCF
def _comptages(con, files, date_reference: str) -> None:
    f = files["sncf_comptage_montants_trains"]
    today = f"DATE '{date_reference}'"
    con.execute(f"""
        CREATE OR REPLACE TABLE silver.comptages AS
        WITH brut AS (
          SELECT * EXCLUDE (file_row_number), file_row_number AS ordre
          FROM read_parquet('{f.path}', file_row_number = true)),
        dates AS (
          SELECT *,
                 -- Un type de jour est-il cohérent avec une date ? (constat 2.6)
                 date::DATE AS date_publiee,
                 CASE WHEN day(date) <= 12
                      THEN make_date(year(date), day(date)::INT, month(date)::INT) END AS date_inversee
          FROM brut),
        coherence AS (
          SELECT *,
                 (type_jour = 'JOB' AND isodow(date_publiee) <= 5) OR (type_jour = 'SAM' AND isodow(date_publiee) = 6)
                 OR (type_jour = 'DIM' AND isodow(date_publiee) = 7) AS date_coherente,
                 date_inversee IS NOT NULL AND date_inversee <= {today} AND (
                   (type_jour = 'JOB' AND isodow(date_inversee) <= 5) OR (type_jour = 'SAM' AND isodow(date_inversee) = 6)
                   OR (type_jour = 'DIM' AND isodow(date_inversee) = 7)) AS inversion_coherente
          FROM dates)
        SELECT md5(concat_ws('|', code_gare, ligne, axe, date_publiee, type_jour, tranche_horaire, ordre)) AS row_id,
               trim(nom_gare) AS nom_gare, code_gare, ligne, axe, type_jour, tranche_horaire,
               date_publiee,
               CASE WHEN (NOT date_coherente OR date_publiee > {today}) AND inversion_coherente
                    THEN date_inversee ELSE date_publiee END AS date,
               (NOT date_coherente OR date_publiee > {today}) AND inversion_coherente AS date_corrigee,
               date_coherente, date_publiee > {today} AS date_future, inversion_coherente,
               somme_de_montants AS montants_brut,
               try_cast(somme_de_montants AS BIGINT) AS montants,
               '{f.source}' AS source, '{f.relative}' AS bronze_path, '{f.sha256}' AS bronze_sha256,
               {_sql_str(f.version)} AS source_version
        FROM coherence ORDER BY code_gare, ligne, axe, date_publiee, tranche_horaire, ordre""")


def _sql_str(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------- validations IDFM
def _normalized(con, path: str, columns: list[str]) -> str:
    """SELECT qui renomme et type un trimestre IDFM selon le contrat (constat 4.1)."""
    actual = columns_of(con, path)
    zdc = "ida" if "ida" in actual and "id_zdc" not in actual else "id_zdc"
    select = {
        "jour": "jour::DATE AS jour",
        "code_stif_trns": "try_cast(code_stif_trns AS DOUBLE)::INT AS code_trns",
        "code_stif_res": "code_stif_res::VARCHAR AS code_res",
        "code_stif_arret": "code_stif_arret::VARCHAR AS code_arret",
        "id_zdc": f"try_cast({zdc} AS DOUBLE)::BIGINT AS id_zdc",
        "libelle_arret": "libelle_arret",
        # Catégories : sans accent, casse normalisée (constat 4.3)
        "categorie_titre": "upper(strip_accents(trim(categorie_titre))) AS categorie",
        "nb_vald": "nb_vald::VARCHAR AS nb_vald_brut, try_cast(nb_vald AS DOUBLE)::BIGINT AS nb_vald",
        "cat_jour": "cat_jour",
        "trnc_horr_60": "trnc_horr_60 AS tranche_60",
        "pourcentage_validations": "pourcentage_validations::DOUBLE AS pct",
    }
    return ", ".join(select[c] for c in columns)


def _validations(con, files) -> None:
    contract = SCHEMA_CONTRACTS["idfm_validations_ferre_jour"]["columns"]
    parts = [f"SELECT '{f.source}' AS source, '{f.relative}' AS bronze_path, {_normalized(con, f.path, contract)} "
             f"FROM '{f.path}'"
             for s, f in sorted(files.items()) if s.startswith("idfm_validations_ferre_jour")]
    parts = parts or ["SELECT NULL::VARCHAR AS source, NULL::VARCHAR AS bronze_path, NULL::DATE AS jour, "
                      "NULL::INT AS code_trns, NULL::VARCHAR AS code_res, NULL::VARCHAR AS code_arret, "
                      "NULL::BIGINT AS id_zdc, NULL::VARCHAR AS libelle_arret, NULL::VARCHAR AS categorie, "
                      "NULL::VARCHAR AS nb_vald_brut, NULL::BIGINT AS nb_vald WHERE false"]
    con.execute("CREATE OR REPLACE TEMP TABLE _validations AS " + " UNION ALL ".join(parts))
    # Une ligne par (jour, zone, catégorie) : somme sur les arrêts de la zone (constat 4.5).
    # `lignes_source` et `doublons` gardent la trace de ce qui a été additionné.
    con.execute("""
        CREATE OR REPLACE TABLE silver.validations_jour AS
        WITH k AS (
          SELECT *, count(*) OVER (PARTITION BY jour, code_trns, code_res, code_arret, id_zdc, categorie) AS n_cle
          FROM _validations)
        SELECT jour, id_zdc, min(libelle_arret) AS libelle, categorie,
               sum(nb_vald) AS nb_vald, count(*) AS lignes_source,
               count(*) FILTER (WHERE n_cle > 1) AS lignes_en_double,
               count(*) FILTER (WHERE nb_vald IS NULL) AS valeurs_non_numeriques,
               list(DISTINCT source ORDER BY source) AS sources
        FROM k GROUP BY jour, id_zdc, categorie ORDER BY jour, id_zdc, categorie""")
    con.execute("""
        CREATE OR REPLACE TABLE silver.validations_zone_jour AS
        SELECT v.jour, v.id_zdc, min(v.libelle) AS libelle, sum(v.nb_vald) AS nb_vald,
               min(c.cat_jour_idfm) AS cat_jour_idfm, min(c.type_jour_sncf) AS type_jour_sncf
        FROM silver.validations_jour v JOIN silver.calendrier c USING (jour)
        GROUP BY v.jour, v.id_zdc ORDER BY v.jour, v.id_zdc""")


def _profils(con, files) -> None:
    contract = SCHEMA_CONTRACTS["idfm_validations_ferre_profils_horaires"]["columns"]
    parts = [f"SELECT '{f.source}' AS source, {_normalized(con, f.path, contract)} FROM '{f.path}'"
             for s, f in sorted(files.items()) if s.startswith("idfm_validations_ferre_profils_horaires")]
    parts = parts or ["SELECT NULL::VARCHAR AS source, NULL::INT AS code_trns, NULL::VARCHAR AS code_res, "
                      "NULL::VARCHAR AS code_arret, NULL::BIGINT AS id_zdc, NULL::VARCHAR AS libelle_arret, "
                      "NULL::VARCHAR AS cat_jour, NULL::VARCHAR AS tranche_60, NULL::DOUBLE AS pct WHERE false"]
    # Profil d'une zone = moyenne des profils de ses arrêts et des trimestres disponibles,
    # agrégé dans les 5 tranches SNCF. Les profils publiés deux fois (somme 200 %) sont ramenés à 100 %.
    con.execute(f"""
        CREATE OR REPLACE TABLE silver.profils_tranches AS
        WITH p AS ({" UNION ALL ".join(parts)}),
        h AS (SELECT *, try_cast(regexp_extract(tranche_60, '^(\\d+)H', 1) AS INT) AS h FROM p),
        norm AS (
          SELECT source, id_zdc, code_arret, cat_jour, h, pct * 100 / sum(pct) OVER (
                   PARTITION BY source, id_zdc, code_arret, cat_jour) AS pct
          FROM h WHERE h IS NOT NULL),
        tr AS (
          SELECT source, id_zdc, code_arret, cat_jour, {TRANCHE_SQL} AS tranche_horaire, sum(pct) AS pct
          FROM norm GROUP BY ALL),
        moy AS (SELECT id_zdc, cat_jour, tranche_horaire, avg(pct) AS pct, count(*) AS profils_moyennes
                FROM tr GROUP BY ALL)
        -- Un arrêt sans validation dans une tranche n'y publie aucune ligne : la moyenne
        -- est donc ramenée à 100 % par zone et type de jour.
        SELECT id_zdc, cat_jour, tranche_horaire,
               round(pct * 100 / sum(pct) OVER (PARTITION BY id_zdc, cat_jour), 6) AS pct, profils_moyennes
        FROM moy ORDER BY id_zdc, cat_jour, tranche_horaire""")


# --------------------------------------------------------------------------- référentiels
def _gares(con, files) -> None:
    con.execute("CREATE OR REPLACE TABLE silver.gares_sncf (code_uic VARCHAR, libelle VARCHAR, x DOUBLE, y DOUBLE, "
                "annuel_voyageurs BIGINT, annee_voyageurs INT)")
    if "sncf_liste_gares" in files:
        freq = files.get("sncf_frequentation_gares")
        freq_sql = (f"""LEFT JOIN (SELECT code_uic_complet, total_voyageurs_non_voyageurs_2024 AS annuel
                       FROM '{freq.path}') f ON f.code_uic_complet = g.code_uic""" if freq else
                    "LEFT JOIN (SELECT NULL AS code_uic_complet, NULL AS annuel) f ON false")
        con.execute(f"""
            INSERT INTO silver.gares_sncf
            SELECT g.code_uic, min(g.libelle), round(avg(g.x_l93), 1), round(avg(g.y_l93), 1), max(f.annuel),
                   IF(max(f.annuel) IS NULL, NULL, 2024)
            FROM '{files["sncf_liste_gares"].path}' g {freq_sql}
            GROUP BY g.code_uic ORDER BY g.code_uic""")
    con.execute("CREATE OR REPLACE TABLE silver.zones_idfm (id_zdc BIGINT, nom_zdc VARCHAR, modes VARCHAR[], x DOUBLE, y DOUBLE)")
    if "idfm_emplacement_gares" in files:
        con.execute(f"""
            INSERT INTO silver.zones_idfm
            SELECT id_ref_zdc, min(nom_zdc), list(DISTINCT mode ORDER BY mode), round(avg(x), 1), round(avg(y), 1)
            FROM '{files["idfm_emplacement_gares"].path}' GROUP BY id_ref_zdc ORDER BY id_ref_zdc""")
    # Correspondance UIC <-> ZdC : zone ferrée (RER/TRAIN/TRAM-train) la plus proche, sinon la
    # zone au nom le plus proche. Chaque appariement garde sa preuve (distance, similarité du
    # nom) et un statut explicite ; rien n'est apparié en silence.
    con.execute(f"""
        CREATE OR REPLACE TABLE silver.correspondance_gares AS
        WITH comptees AS (SELECT code_gare, min(nom_gare) AS nom_gare FROM silver.comptages GROUP BY code_gare),
        cand AS (
          SELECT c.code_gare, z.id_zdc, z.nom_zdc,
                 sqrt((g.x - z.x) ^ 2 + (g.y - z.y) ^ 2) AS distance_m,
                 jaro_winkler_similarity({_nom_sql('c.nom_gare')}, {_nom_sql('z.nom_zdc')}) AS similarite_nom
          FROM comptees c LEFT JOIN silver.gares_sncf g ON g.code_uic = c.code_gare
          JOIN silver.zones_idfm z ON list_has_any(z.modes, ['RER', 'TRAIN', 'TRAM'])),
        proche AS (SELECT * FROM cand WHERE distance_m IS NOT NULL
                   QUALIFY row_number() OVER (PARTITION BY code_gare ORDER BY distance_m) = 1),
        nom AS (SELECT * FROM cand WHERE coalesce(distance_m, 0) <= {NAME_MATCH_MAX_M}
                QUALIFY row_number() OVER (PARTITION BY code_gare ORDER BY similarite_nom DESC, distance_m) = 1),
        choix AS (
          SELECT c.code_gare, c.nom_gare, p.distance_m <= {MATCH_DISTANCE_M} AS par_distance,
                 n.similarite_nom >= {NAME_MATCH_MIN_SIM} AS par_nom,
                 g.code_uic IS NOT NULL AND g.x IS NOT NULL AS a_coordonnees,
                 CASE WHEN p.distance_m <= {MATCH_DISTANCE_M} THEN p ELSE n END AS m, p AS plus_proche
          FROM comptees c LEFT JOIN proche p USING (code_gare) LEFT JOIN nom n USING (code_gare)
          LEFT JOIN silver.gares_sncf g ON g.code_uic = c.code_gare)
        SELECT code_gare, nom_gare,
               IF(par_distance OR par_nom, m.id_zdc, NULL) AS id_zdc,
               IF(par_distance OR par_nom, m.nom_zdc, NULL) AS nom_zdc,
               round(IF(par_distance OR par_nom, m.distance_m, plus_proche.distance_m)) AS distance_m,
               round(IF(par_distance OR par_nom, m.similarite_nom, plus_proche.similarite_nom), 3) AS similarite_nom,
               plus_proche.nom_zdc AS zone_la_plus_proche,
               CASE WHEN par_distance THEN 'APPARIEE'
                    WHEN par_nom THEN IF(a_coordonnees, 'APPARIEE_PAR_NOM', 'APPARIEE_PAR_NOM_SANS_COORDONNEES')
                    WHEN NOT a_coordonnees THEN 'SANS_COORDONNEES'
                    ELSE 'NON_APPARIEE' END AS statut
        FROM choix ORDER BY code_gare""")


def _nom_sql(col: str) -> str:
    """Nom comparable : sans accents ni tirets, en minuscules, sans les préfixes « gare de », « paris »."""
    return (f"trim(regexp_replace(lower(strip_accents(replace({col}, '-', ' '))), "
            f"'^(paris )?(gare (de |du |d'')?)?', ''))")
