"""Compte officiel (couche gold) : un nombre de voyageurs montants par gare, ligne,
date et tranche horaire, avec la méthode et les composants qui l'expliquent.

Hiérarchie (config/compte_officiel.yaml) :
  1. MESURE            comptage SNCF de la tranche, sans règle en ERREUR
  2. CORRIGE_OFFRE     non applicable : pas de numéro de train dans les comptages
  3. RECONCILIE_IDFM   alpha(gare, ligne) x validations IDFM du jour x part de la tranche
  4. EXTRAPOLE         alpha(gare, ligne) x validations attendues x part de la tranche

alpha(gare, ligne) est appris sur les journées mesurées : montants SNCF de la journée
divisés par les validations IDFM moyennes de la zone pour la même catégorie de jour.
Il absorbe ce que les deux sources ne mesurent pas pareil (correspondances, fraude,
part de la ligne dans une zone partagée avec le métro...).

Tables créées dans le schéma `gold` :
  * gold.etalonnage       un facteur par (gare, ligne), avec ses journées sources
  * gold.compte_officiel  le compte, une ligne par (gare, ligne, date, tranche)
  * gold.backtest         validation croisée : chaque journée mesurée prédite sans elle
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from .settings import PROJECT_ROOT
from .silver import TRANCHES

CONFIG_PATH = PROJECT_ROOT / "config" / "compte_officiel.yaml"


@dataclass(frozen=True)
class Config:
    version: str
    date_min: str
    score: dict


def load_config(path: Path = CONFIG_PATH) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Config(str(raw["version"]), str(raw["etalonnage"]["date_min"]), raw["score"])


def _score_sql(score: dict) -> str:
    """Score d'un chiffre calculé : plafond de la méthode moins les pénalités du facteur."""
    return (f"{score['plafond']} - IF(e.n_journees = 1, {score['penalite_une_seule_mesure']}, 0) "
            f"- IF(e.dispersion > {score['seuil_dispersion']}, {score['penalite_dispersion']}, 0)")


def build_gold(con: duckdb.DuckDBPyConnection, date_reference: str, config: Config | None = None) -> Config:
    """Crée le schéma `gold` à partir des schémas `silver` et `quality`."""
    config = config or load_config()
    # Repart de zéro : la connexion peut exposer un gold/ précédent sous forme de vues.
    con.execute("DROP SCHEMA IF EXISTS gold CASCADE")
    con.execute("CREATE SCHEMA gold")
    _mesures(con)
    _etalonnage(con, config)
    _attendu(con)
    _compte(con, config, date_reference)
    _backtest(con)
    return config


def _mesures(con) -> None:
    # Une tranche est mesurée si chaque axe compté ce jour-là l'a publiée, sans erreur.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _tranches AS
        WITH axes AS (
          SELECT code_gare, ligne, date, count(DISTINCT axe) AS nb_axes
          FROM quality.comptages_qualifies GROUP BY ALL)
        SELECT q.code_gare, q.ligne, q.date, q.tranche_horaire, min(q.nom_gare) AS nom_gare,
               min(q.type_jour) AS type_jour, sum(q.montants) AS montants,
               min(q.score_confiance) AS score_mesure,
               count(DISTINCT q.axe) = any_value(a.nb_axes) AND bool_and(q.statut <> 'ERREUR') AS mesure_ok,
               list(q.row_id ORDER BY q.row_id) AS comptages_source
        FROM quality.comptages_qualifies q JOIN axes a USING (code_gare, ligne, date)
        GROUP BY q.code_gare, q.ligne, q.date, q.tranche_horaire""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _journees AS
        SELECT t.code_gare, t.ligne, t.date, min(t.type_jour) AS type_jour, sum(t.montants) AS montants,
               count(*) = 5 AND bool_and(t.mesure_ok) AS complete, cg.id_zdc, c.cat_jour_idfm
        FROM _tranches t
        LEFT JOIN silver.correspondance_gares cg USING (code_gare)
        LEFT JOIN silver.calendrier c ON c.jour = t.date
        GROUP BY t.code_gare, t.ligne, t.date, cg.id_zdc, c.cat_jour_idfm""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _vbar AS
        SELECT id_zdc, cat_jour_idfm, avg(nb_vald) AS validations_moyennes, count(*) AS jours
        FROM silver.validations_zone_jour GROUP BY ALL""")


def _etalonnage(con, config: Config) -> None:
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.etalonnage AS
        WITH r AS (
          SELECT j.*, v.validations_moyennes, j.montants / v.validations_moyennes AS rapport
          FROM _journees j JOIN _vbar v USING (id_zdc, cat_jour_idfm)
          WHERE j.complete AND j.date >= DATE '{config.date_min}' AND v.validations_moyennes > 0),
        paires AS (SELECT code_gare, ligne, min(id_zdc) AS id_zdc FROM _journees GROUP BY ALL),
        f AS (
          SELECT code_gare, ligne, median(rapport) AS alpha, count(*) AS n_journees,
                 (max(rapport) - min(rapport)) / nullif(median(rapport), 0) AS dispersion,
                 min(rapport) AS rapport_min, max(rapport) AS rapport_max,
                 list(date ORDER BY date) AS journees_source
          FROM r GROUP BY ALL)
        SELECT p.code_gare, p.ligne, p.id_zdc, round(f.alpha, 6) AS alpha, coalesce(f.n_journees, 0) AS n_journees,
               round(f.dispersion, 6) AS dispersion, round(f.rapport_min, 6) AS rapport_min,
               round(f.rapport_max, 6) AS rapport_max, f.journees_source,
               CASE WHEN p.id_zdc IS NULL THEN 'SANS_ZONE_IDFM'
                    WHEN f.alpha IS NULL THEN 'SANS_JOURNEE_COMPLETE'
                    ELSE 'ETALONNE' END AS statut,
               '{config.version}' AS version_moteur
        FROM paires p LEFT JOIN f USING (code_gare, ligne)
        ORDER BY p.code_gare, p.ligne""")


def _attendu(con) -> None:
    # Validations attendues un jour sans donnée IDFM : médiane de la zone pour la même
    # catégorie de jour et le même mois de l'année, sinon pour la catégorie toute l'année.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _attendu_mois AS
        SELECT id_zdc, cat_jour_idfm, month(jour) AS mois, median(nb_vald) AS attendu
        FROM silver.validations_zone_jour GROUP BY ALL""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _attendu_annee AS
        SELECT id_zdc, cat_jour_idfm, median(nb_vald) AS attendu
        FROM silver.validations_zone_jour GROUP BY ALL""")
    # Profil horaire de secours : part moyenne mesurée sur la ligne, même type de jour.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _profil_ligne AS
        SELECT t.ligne, t.type_jour, t.tranche_horaire, avg(t.montants / j.montants) * 100 AS pct
        FROM _tranches t JOIN _journees j USING (code_gare, ligne, date)
        WHERE j.complete AND j.montants > 0 GROUP BY ALL""")


def _compte(con, config: Config, date_reference: str) -> None:
    tranches = ", ".join(f"'{t}'" for t in TRANCHES)
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.compte_officiel AS
        WITH paires AS (SELECT * FROM gold.etalonnage WHERE statut = 'ETALONNE'),
        fenetre AS (SELECT min(jour) AS debut FROM silver.validations_zone_jour),
        jours AS (
          SELECT p.code_gare, p.ligne, unnest(range(f.debut, DATE '{date_reference}' + 1, INTERVAL 1 DAY))::DATE AS date
          FROM paires p, fenetre f WHERE f.debut IS NOT NULL
          UNION
          SELECT j.code_gare, j.ligne, j.date FROM _journees j JOIN paires p USING (code_gare, ligne)
          WHERE j.date <= DATE '{date_reference}'),
        grille AS (SELECT j.*, unnest([{tranches}]) AS tranche_horaire FROM jours j),
        noms AS (SELECT code_gare, min(nom_gare) AS nom_gare FROM silver.comptages GROUP BY 1),
        base AS (
          SELECT g.code_gare, n.nom_gare, g.ligne, g.date, c.cat_jour_idfm, c.type_jour_sncf AS type_jour,
                 g.tranche_horaire, e.id_zdc, e.alpha, e.n_journees, e.dispersion,
                 t.montants AS montants_mesures, t.mesure_ok, t.score_mesure, t.comptages_source,
                 v.nb_vald AS validations_jour,
                 coalesce(am.attendu, aa.attendu) AS validations_attendues,
                 coalesce(p.pct, pl.pct) AS part_tranche,
                 IF(p.pct IS NULL, 'PROFIL_LIGNE_SNCF', 'PROFIL_IDFM') AS source_profil,
                 {_score_sql(config.score['reconcilie'])} AS score_reconcilie,
                 {_score_sql(config.score['extrapole'])} AS score_extrapole
          FROM grille g
          JOIN gold.etalonnage e USING (code_gare, ligne)
          JOIN silver.calendrier c ON c.jour = g.date
          LEFT JOIN noms n USING (code_gare)
          LEFT JOIN _tranches t USING (code_gare, ligne, date, tranche_horaire)
          LEFT JOIN silver.validations_zone_jour v ON v.id_zdc = e.id_zdc AND v.jour = g.date
          LEFT JOIN _attendu_mois am ON am.id_zdc = e.id_zdc AND am.cat_jour_idfm = c.cat_jour_idfm
                                    AND am.mois = month(g.date)
          LEFT JOIN _attendu_annee aa ON aa.id_zdc = e.id_zdc AND aa.cat_jour_idfm = c.cat_jour_idfm
          LEFT JOIN silver.profils_tranches p ON p.id_zdc = e.id_zdc AND p.cat_jour = c.cat_jour_idfm
                                             AND p.tranche_horaire = g.tranche_horaire
          LEFT JOIN _profil_ligne pl ON pl.ligne = g.ligne AND pl.type_jour = c.type_jour_sncf
                                    AND pl.tranche_horaire = g.tranche_horaire),
        m AS (
          SELECT *, CASE WHEN mesure_ok THEN 'MESURE'
                         WHEN validations_jour IS NOT NULL THEN 'RECONCILIE_IDFM'
                         WHEN validations_attendues IS NOT NULL THEN 'EXTRAPOLE'
                         ELSE 'NON_CALCULABLE' END AS methode
          FROM base)
        SELECT code_gare, nom_gare, ligne, date, strftime(date, '%Y-%m') AS mois, type_jour, cat_jour_idfm,
               tranche_horaire, methode,
               CASE methode WHEN 'MESURE' THEN montants_mesures
                    WHEN 'RECONCILIE_IDFM' THEN round(alpha * validations_jour * part_tranche / 100)::BIGINT
                    WHEN 'EXTRAPOLE' THEN round(alpha * validations_attendues * part_tranche / 100)::BIGINT
               END::BIGINT AS voyageurs,
               CASE methode WHEN 'MESURE' THEN score_mesure WHEN 'RECONCILIE_IDFM' THEN score_reconcilie
                    WHEN 'EXTRAPOLE' THEN score_extrapole ELSE 0 END::INTEGER AS score_confiance,
               -- Composants : de quoi refaire le calcul à la main
               montants_mesures, IF(methode = 'MESURE', comptages_source, NULL) AS comptages_source,
               alpha, n_journees AS alpha_n_journees, id_zdc,
               validations_jour, validations_attendues, round(part_tranche, 4) AS part_tranche, source_profil,
               '{config.version}' AS version_moteur, DATE '{date_reference}' AS date_reference
        FROM m
        ORDER BY mois, code_gare, ligne, date, tranche_horaire""")


def _backtest(con) -> None:
    # Chaque journée mesurée est prédite avec un facteur appris SANS elle : c'est l'erreur
    # attendue d'une journée reconstruite, sur des journées dont on connaît la vérité.
    con.execute("""
        CREATE OR REPLACE TABLE gold.backtest AS
        WITH r AS (
          SELECT j.code_gare, j.ligne, j.date, j.type_jour, j.montants, v.validations_moyennes,
                 j.montants / v.validations_moyennes AS rapport
          FROM _journees j JOIN _vbar v USING (id_zdc, cat_jour_idfm)
          JOIN gold.etalonnage e USING (code_gare, ligne)
          WHERE j.complete AND e.statut = 'ETALONNE' AND list_contains(e.journees_source, j.date)),
        p AS (
          SELECT a.code_gare, a.ligne, a.date, a.type_jour, a.montants AS mesure, a.validations_moyennes,
                 median(b.rapport) AS alpha_sans_la_journee, count(b.rapport) AS n_autres
          FROM r a LEFT JOIN r b ON b.code_gare = a.code_gare AND b.ligne = a.ligne AND b.date <> a.date
          GROUP BY ALL)
        SELECT *, round(alpha_sans_la_journee * validations_moyennes) AS prediction,
               round(abs(alpha_sans_la_journee * validations_moyennes - mesure) / nullif(mesure, 0), 4)
                 AS erreur_relative
        FROM p WHERE n_autres > 0
        ORDER BY code_gare, ligne, date""")


def summary(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """(méthode, lignes, voyageurs, score moyen) pour la CLI."""
    return con.execute("""
        SELECT methode, count(*), sum(voyageurs), round(avg(score_confiance), 1)
        FROM gold.compte_officiel GROUP BY 1 ORDER BY 1""").fetchall()


def write_gold(con: duckdb.DuckDBPyConnection, data_root: Path) -> None:
    """Écrit gold/ : compte_officiel partitionné par mois, pour que Git ne stocke
    chaque jour que le mois qui a changé (les fichiers sont reproductibles)."""
    import shutil

    out = data_root / "gold"
    out.mkdir(parents=True, exist_ok=True)
    for table in ("etalonnage", "backtest"):
        con.execute(f"COPY gold.{table} TO '{(out / f'{table}.parquet').as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    target = out / "compte_officiel"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    for (mois,) in con.execute("SELECT DISTINCT mois FROM gold.compte_officiel ORDER BY 1").fetchall():
        path = (target / f"mois={mois}.parquet").as_posix()
        con.execute(f"COPY (SELECT * FROM gold.compte_officiel WHERE mois = '{mois}' "
                    f"ORDER BY code_gare, ligne, date, tranche_horaire) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")


def run_gold(data_root: Path, date_reference: str | None = None) -> list[tuple]:
    """`comptage compte-officiel` : lit silver/ et quality/ (écrits par `comptage qualify`),
    écrit gold/ et journalise l'exécution dans monitoring.gold_runs."""
    from datetime import date, datetime, timezone

    from . import warehouse
    from .ingest import new_run_id

    date_reference = date_reference or date.today().isoformat()
    con = warehouse.connect_derived(data_root)
    try:
        con.execute("SET enable_progress_bar = false")
        config = build_gold(con, date_reference)
        write_gold(con, data_root)
        run_at = datetime.now(timezone.utc)
        run_id = new_run_id(run_at)
        log = warehouse.RunLog()
        rows = summary(con)
        for methode, lignes, voyageurs, score in rows:
            log.add("gold_runs", [run_id, run_at, config.version, date_reference, methode, lignes, voyageurs, score])
        n, err = con.execute("SELECT count(*), median(erreur_relative) * 100 FROM gold.backtest").fetchone()
        log.add("gold_runs", [run_id, run_at, config.version, date_reference, "BACKTEST", n, n, err])
        log.flush(data_root, run_id, run_at.date().isoformat())
        return rows
    finally:
        con.close()
