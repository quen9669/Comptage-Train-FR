"""Connecteurs : traduisent une Source du catalogue en requête HTTP.

Un connecteur ne télécharge rien lui-même : il décrit QUOI demander.
Le téléchargement, les tentatives et la télémétrie sont centralisés dans
http_client / ingest, pour que toutes les sources soient monitorées pareil.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from ..settings import Source


class SourceNotReady(Exception):
    """La source ne peut pas être collectée maintenant (ex. clé API absente)."""


@dataclass(frozen=True)
class RequestSpec:
    url: str
    file_format: str                     # extension du fichier bronze
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


def build_request(source: Source) -> RequestSpec:
    if source.kind == "opendatasoft":
        # L'endpoint "exports" renvoie le jeu complet en un fichier, sans la
        # limite de 10 000 lignes de l'endpoint "records" : pas de pagination.
        # Parquet : compact, typé, lisible directement par DuckDB.
        return RequestSpec(
            url=f"{source.base_url}/api/explore/v2.1/catalog/datasets/"
                f"{source.dataset_id}/exports/parquet",
            file_format="parquet",
        )
    if source.kind == "http_json":
        return RequestSpec(url=source.url, file_format="json", params=dict(source.params))
    if source.kind == "prim":
        key = os.environ.get("PRIM_API_KEY", "").strip()
        if not key:
            raise SourceNotReady("PRIM_API_KEY absente du .env")
        return RequestSpec(url=source.url, file_format="json",
                           params=dict(source.params), headers={"apikey": key})
    raise ValueError(f"Type de source inconnu : {source.kind}")


def catalog_url(source: Source) -> str | None:
    """URL des métadonnées du jeu (Opendatasoft seulement)."""
    if source.kind == "opendatasoft":
        return f"{source.base_url}/api/explore/v2.1/catalog/datasets/{source.dataset_id}"
    return None


def parse_version(catalog_json: bytes) -> str | None:
    """Date de dernier traitement des données annoncée par Opendatasoft.

    Indispensable car l'export Parquet d'Opendatasoft n'est pas reproductible :
    deux exports des mêmes lignes n'ont pas les mêmes octets, donc le SHA-256
    seul ne suffit pas à détecter « rien n'a changé ».
    """
    try:
        metas = json.loads(catalog_json).get("metas", {}).get("default", {})
    except (ValueError, AttributeError):
        return None  # réponse inattendue : on retombe sur la comparaison SHA-256
    return metas.get("data_processed") or metas.get("modified")
