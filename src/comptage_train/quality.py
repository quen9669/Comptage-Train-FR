"""Moteur de qualité : applique config/regles_qualite.yaml aux comptages silver.

Pour chaque ligne de comptage (gare x ligne x axe x date x tranche), chaque règle
produit un résultat (OK, ATTENTION, ERREUR, PAS_TESTE, NON_APPLICABLE) et la
valeur observée qui le justifie. On en déduit un statut et un score de
confiance, et on garde le lignage complet (fichier bronze, empreinte, version
du producteur, version des règles).

Tables créées dans le schéma `quality` :
  * quality.controles            une ligne par (comptage, règle) : la preuve
  * quality.comptages_qualifies  une ligne par comptage : statut, score, lignage
  * quality.regles               le référentiel des règles appliqué
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

from .settings import PROJECT_ROOT

RULES_PATH = PROJECT_ROOT / "config" / "regles_qualite.yaml"
RESULTATS = ("OK", "ATTENTION", "ERREUR", "PAS_TESTE", "NON_APPLICABLE")


@dataclass(frozen=True)
class Rule:
    id: str
    nom: str
    severite: str
    description: str
    penalite: int = 0
    seuils: dict = field(default_factory=dict)
    non_applicable: str | None = None
    calibration: str | None = None


@dataclass(frozen=True)
class RuleSet:
    version: str
    base: int
    plafond: int
    bonus: int
    rules: list[Rule]

    def get(self, rule_id: str) -> Rule:
        return next(r for r in self.rules if r.id == rule_id)


def load_rules(path: Path = RULES_PATH) -> RuleSet:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = [Rule(**r) for r in raw["regles"]]
    score = raw["score"]
    return RuleSet(str(raw["version"]), score["base"], score["plafond"], score["bonus_croisement"], rules)


def _q(value) -> str:
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _rule_sql(rules: RuleSet) -> dict[str, tuple[str, str]]:
    """Règle -> (expression du résultat, expression de la valeur observée), sur la table _q."""
    s = {r.id: r.seuils for r in rules.rules}
    sev = {r.id: _q(r.severite) for r in rules.rules}
    rm02, rm05, rm06, rm07, rm08, rm09 = (s[k] for k in ("RM_02", "RM_05", "RM_06", "RM_07", "RM_08", "RM_09"))
    tranches_pointe = ", ".join(_q(t) for t in rm05["tranches"])
    ok = lambda cond, rid: f"IF({cond}, 'OK', {sev[rid]})"  # noqa: E731
    return {
        "RT_01": (ok("code_gare IS NOT NULL AND ligne IS NOT NULL AND axe IS NOT NULL AND date_publiee IS NOT NULL "
                     "AND type_jour IS NOT NULL AND tranche_horaire IS NOT NULL", "RT_01"), "NULL"),
        "RT_02": (ok("coalesce(regexp_full_match(code_gare, '87[0-9]{6}'), false)", "RT_02"), "code_gare"),
        "RT_03": (ok("montants_brut IS NULL OR montants IS NOT NULL", "RT_03"), "montants_brut::VARCHAR"),
        "RT_04": (ok("n_cle = 1", "RT_04"), "n_cle::VARCHAR"),
        "RT_05": (ok("montants_brut IS NOT NULL", "RT_05"), "NULL"),
        "RT_06": (ok("dans_referentiel", "RT_06"), "code_gare"),
        "RT_07": (ok(f"date <= date_reference AND date >= DATE {_q(s['RT_07']['date_min'])}", "RT_07"),
                  "date::VARCHAR"),
        "RT_08": (ok("nb_tranches = 5", "RT_08"), "nb_tranches::VARCHAR"),
        "RT_09": (ok("id_zdc IS NOT NULL", "RT_09"), "statut_correspondance"),
        "RT_10": (ok("colonnes_manquantes = 0", "RT_10"), "colonnes_manquantes::VARCHAR"),
        "RT_11": ("CASE WHEN date_coherente AND NOT date_corrigee THEN 'OK' "
                  f"WHEN date_corrigee THEN {sev['RT_11']} ELSE 'ERREUR' END",
                  "IF(date_corrigee, date_publiee::VARCHAR || ' -> ' || date::VARCHAR, date_publiee::VARCHAR)"),
        "RT_12": (ok("tranche_horaire IN ('Avant 6h', 'De 6h à 10h', 'De 10h à 16h', 'De 16h à 20h', 'Après 20h') "
                     "AND type_jour IN ('JOB', 'SAM', 'DIM')", "RT_12"), "type_jour || ' / ' || tranche_horaire"),
        "RM_01": ("CASE WHEN montants IS NULL THEN 'PAS_TESTE' WHEN montants >= 0 THEN 'OK' "
                  f"ELSE {sev['RM_01']} END", "montants::VARCHAR"),
        "RM_02": ("CASE WHEN ratio_plafond IS NULL THEN 'PAS_TESTE' "
                  f"WHEN ratio_plafond > {rm02['erreur']} THEN 'ERREUR' "
                  f"WHEN ratio_plafond > {rm02['attention']} THEN 'ATTENTION' ELSE 'OK' END",
                  "round(ratio_plafond, 3)::VARCHAR"),
        "RM_05": (f"CASE WHEN montants IS NULL THEN 'PAS_TESTE' WHEN type_jour = {_q(rm05['type_jour'])} "
                  f"AND tranche_horaire IN ({tranches_pointe}) AND montants = 0 THEN {sev['RM_05']} ELSE 'OK' END",
                  "montants::VARCHAR"),
        "RM_06": ("CASE WHEN facteur_idfm IS NULL THEN 'PAS_TESTE' "
                  f"WHEN facteur_idfm > {rm06['facteur_max']} OR facteur_idfm < 1.0 / {rm06['facteur_max']} "
                  f"THEN {sev['RM_06']} ELSE 'OK' END", "round(facteur_idfm, 3)::VARCHAR"),
        "RM_07": ("CASE WHEN ecart_profil IS NULL THEN 'PAS_TESTE' "
                  f"WHEN abs(ecart_profil) > {rm07['ecart_points']} THEN {sev['RM_07']} ELSE 'OK' END",
                  "round(ecart_profil, 1)::VARCHAR"),
        "RM_08": ("CASE WHEN type_jour = 'JOB' THEN 'NON_APPLICABLE' WHEN ratio_job IS NULL THEN 'PAS_TESTE' "
                  f"WHEN ratio_job > {rm08['ratio_max']} THEN {sev['RM_08']} ELSE 'OK' END",
                  "round(ratio_job, 3)::VARCHAR"),
        "RM_09": ("CASE WHEN z_robuste IS NULL THEN 'PAS_TESTE' "
                  f"WHEN abs(z_robuste) > {rm09['z_max']} THEN {sev['RM_09']} ELSE 'OK' END",
                  "round(z_robuste, 2)::VARCHAR"),
    }


def _features(con, date_reference: str) -> None:
    """Table _q : chaque comptage avec les grandeurs dont les règles ont besoin."""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _q AS
        WITH c AS (
          SELECT *, DATE {_q(date_reference)} AS date_reference,
                 count(*) OVER (PARTITION BY code_gare, ligne, axe, date_publiee, type_jour, tranche_horaire) AS n_cle,
                 count(*) OVER (PARTITION BY code_gare, ligne, axe, date) AS nb_tranches,
                 sum(montants) OVER (PARTITION BY code_gare, ligne, axe, date) AS total_ligne_jour
          FROM silver.comptages),
        gare_jour AS (
          SELECT code_gare, date, sum(montants) AS total_gare_jour, bool_and(nb_tranches = 5) AS journee_complete
          FROM c GROUP BY ALL),
        idfm AS (
          SELECT id_zdc, cat_jour_idfm, avg(nb_vald) AS validations_moyennes
          FROM silver.validations_zone_jour GROUP BY ALL),
        base AS (
          SELECT c.*, gj.total_gare_jour, gj.journee_complete,
                 g.code_uic IS NOT NULL AS dans_referentiel, g.annuel_voyageurs,
                 cg.id_zdc, cg.statut AS statut_correspondance, cal.cat_jour_idfm,
                 coalesce(len(sc.colonnes_manquantes), 0) AS colonnes_manquantes,
                 gj.total_gare_jour * 365.0 / nullif(g.annuel_voyageurs, 0) AS ratio_plafond,
                 IF(gj.journee_complete, gj.total_gare_jour / nullif(i.validations_moyennes, 0), NULL) AS ratio_idfm
          FROM c
          JOIN gare_jour gj USING (code_gare, date)
          LEFT JOIN silver.gares_sncf g ON g.code_uic = c.code_gare
          LEFT JOIN silver.correspondance_gares cg USING (code_gare)
          LEFT JOIN silver.calendrier cal ON cal.jour = c.date
          LEFT JOIN silver.schema_contrats sc ON sc.bronze_path = c.bronze_path
          LEFT JOIN idfm i ON i.id_zdc = cg.id_zdc AND i.cat_jour_idfm = cal.cat_jour_idfm),
        -- RM_06 : le ratio SNCF/IDFM varie par ligne (zones partagées avec le métro...),
        -- on le compare donc au ratio médian des gares-jours de la même ligne.
        ligne_ratio AS (
          SELECT ligne, median(ratio_idfm) AS ratio_median_ligne
          FROM (SELECT DISTINCT ligne, code_gare, date, ratio_idfm FROM base WHERE ratio_idfm > 0) GROUP BY ligne),
        part_gare AS (
          SELECT code_gare, date, tranche_horaire,
                 sum(montants) / nullif(any_value(total_gare_jour), 0) * 100 AS part_gare
          FROM base WHERE journee_complete GROUP BY ALL),
        job AS (
          SELECT code_gare, ligne, axe, median(total_ligne_jour) AS total_job
          FROM (SELECT DISTINCT code_gare, ligne, axe, date, total_ligne_jour FROM base
                WHERE type_jour = 'JOB' AND nb_tranches = 5) GROUP BY ALL),
        parts AS (
          SELECT *, IF(nb_tranches = 5, montants / nullif(total_ligne_jour, 0), NULL) AS part_ligne FROM base),
        z AS (
          SELECT *, part_ligne - median(part_ligne) OVER w AS ecart_mediane FROM parts
          WINDOW w AS (PARTITION BY ligne, type_jour, tranche_horaire)),
        mad AS (
          SELECT *, median(abs(ecart_mediane)) OVER (PARTITION BY ligne, type_jour, tranche_horaire) AS mad FROM z)
        SELECT m.*, m.ratio_idfm / nullif(lr.ratio_median_ligne, 0) AS facteur_idfm,
               pg.part_gare - p.pct AS ecart_profil,
               IF(m.nb_tranches = 5, m.total_ligne_jour / nullif(j.total_job, 0), NULL) AS ratio_job,
               m.ecart_mediane / nullif(1.4826 * m.mad, 0) AS z_robuste
        FROM mad m
        LEFT JOIN ligne_ratio lr USING (ligne)
        LEFT JOIN part_gare pg USING (code_gare, date, tranche_horaire)
        LEFT JOIN silver.profils_tranches p
               ON p.id_zdc = m.id_zdc AND p.cat_jour = m.cat_jour_idfm AND p.tranche_horaire = m.tranche_horaire
        LEFT JOIN job j USING (code_gare, ligne, axe)""")


def qualify(con: duckdb.DuckDBPyConnection, date_reference: str, rules: RuleSet | None = None) -> RuleSet:
    """Crée le schéma `quality` à partir du schéma `silver` (voir silver.build_silver)."""
    rules = rules or load_rules()
    con.execute("CREATE SCHEMA IF NOT EXISTS quality")
    _features(con, date_reference)
    sql = _rule_sql(rules)

    parts = []
    for r in rules.rules:
        if r.non_applicable:
            parts.append(f"SELECT row_id, {_q(r.id)} AS regle, 'NON_APPLICABLE' AS resultat, "
                         f"NULL AS valeur_observee FROM _q")
        else:
            resultat, valeur = sql[r.id]
            parts.append(f"SELECT row_id, {_q(r.id)} AS regle, {resultat} AS resultat, "
                         f"{valeur} AS valeur_observee FROM _q")
    con.execute("CREATE OR REPLACE TABLE quality.controles AS SELECT * FROM (" + " UNION ALL ".join(parts) + ") "
                "ORDER BY row_id, regle")

    rules_rows = [[r.id, r.nom, r.severite, r.penalite, str(r.seuils) if r.seuils else None, r.description,
                   r.non_applicable, r.calibration, rules.version] for r in rules.rules]
    con.execute("""CREATE OR REPLACE TABLE quality.regles (regle VARCHAR, nom VARCHAR, severite VARCHAR,
        penalite INTEGER, seuils VARCHAR, description VARCHAR, non_applicable VARCHAR, calibration VARCHAR,
        version_regles VARCHAR)""")
    con.executemany("INSERT INTO quality.regles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rules_rows)

    bonus_max = rules.get("RM_06").seuils["facteur_bonus"]
    con.execute(f"""
        CREATE OR REPLACE TABLE quality.comptages_qualifies AS
        WITH agg AS (
          SELECT k.row_id,
                 count(*) FILTER (WHERE k.resultat IN ('OK', 'ATTENTION', 'ERREUR')) AS regles_testees,
                 list(k.regle ORDER BY k.regle) FILTER (WHERE k.resultat = 'ERREUR') AS regles_erreur,
                 list(k.regle ORDER BY k.regle) FILTER (WHERE k.resultat = 'ATTENTION') AS regles_attention,
                 list(k.regle ORDER BY k.regle) FILTER (WHERE k.resultat = 'PAS_TESTE') AS regles_non_testees,
                 coalesce(sum(r.penalite) FILTER (WHERE k.resultat = 'ATTENTION'), 0) AS penalites
          FROM quality.controles k JOIN quality.regles r USING (regle) GROUP BY k.row_id)
        SELECT q.row_id, q.code_gare, q.nom_gare, q.ligne, q.axe, q.date, q.date_publiee, q.type_jour,
               q.tranche_horaire, q.montants_brut, q.montants,
               CASE WHEN len(a.regles_erreur) > 0 THEN 'ERREUR'
                    WHEN len(a.regles_attention) > 0 THEN 'ATTENTION' ELSE 'VALIDE' END AS statut,
               CASE WHEN len(a.regles_erreur) > 0 THEN 0
                    ELSE greatest(0, least({rules.plafond}, {rules.base} - a.penalites
                         + IF(q.facteur_idfm BETWEEN 1.0 / {bonus_max} AND {bonus_max}, {rules.bonus}, 0)))
               END::INTEGER AS score_confiance,
               q.facteur_idfm BETWEEN 1.0 / {bonus_max} AND {bonus_max} AS bonus_croisement,
               a.penalites, a.regles_testees,
               coalesce(a.regles_erreur, []) AS regles_erreur,
               coalesce(a.regles_attention, []) AS regles_attention,
               coalesce(a.regles_non_testees, []) AS regles_non_testees,
               q.id_zdc, q.statut_correspondance,
               q.source, q.bronze_path, q.bronze_sha256, q.source_version,
               {_q(rules.version)} AS version_regles, q.date_reference
        FROM _q q JOIN agg a USING (row_id)
        ORDER BY q.code_gare, q.ligne, q.axe, q.date, q.tranche_horaire, q.row_id""")
    return rules


def summary(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """(statut, lignes, score moyen) : ce que la CLI affiche."""
    return con.execute("""
        SELECT statut, count(*), round(avg(score_confiance), 1) FROM quality.comptages_qualifies
        GROUP BY statut ORDER BY statut""").fetchall()


def run_quality(data_root: Path, date_reference: str | None = None) -> list[tuple]:
    """`comptage qualify` : silver + règles, écrit les tables dérivées et journalise l'exécution.

    Les tables silver/ et quality/ sont remplacées à chaque exécution (elles se
    recalculent depuis le bronze, et Git garde leurs versions successives) ;
    le journal monitoring.quality_runs, lui, est en ajout seul.
    """
    from datetime import date, datetime, timezone

    from . import silver, warehouse
    from .ingest import new_run_id

    date_reference = date_reference or date.today().isoformat()
    con = warehouse.connect(data_root)
    try:
        con.execute("SET enable_progress_bar = false")
        files = silver.build_silver(con, data_root, date_reference)
        rules = qualify(con, date_reference)
        for schema in ("silver", "quality"):
            tables = warehouse.DERIVED[schema]
            (data_root / schema).mkdir(parents=True, exist_ok=True)
            for table in tables:
                out = (data_root / schema / f"{table}.parquet").as_posix()
                con.execute(f"COPY {schema}.{table} TO '{out}' (FORMAT parquet, COMPRESSION zstd)")

        run_at = datetime.now(timezone.utc)
        run_id = new_run_id(run_at)
        source = files["sncf_comptage_montants_trains"].sha256
        log = warehouse.RunLog()
        counts = con.execute("""
            SELECT regle, resultat, count(*) FROM quality.controles GROUP BY ALL
            UNION ALL SELECT 'STATUT', statut, count(*) FROM quality.comptages_qualifies GROUP BY ALL
            ORDER BY 1, 2""").fetchall()
        for regle, resultat, n in counts:
            log.add("quality_runs", [run_id, run_at, rules.version, date_reference, source, regle, resultat, n])
        log.flush(data_root, run_id, run_at.date().isoformat())
        return summary(con)
    finally:
        con.close()
