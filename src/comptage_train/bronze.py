"""Couche bronze : dépôt des fichiers bruts, immuables et horodatés.

Règles :
  * on n'écrase ni ne modifie jamais un fichier déjà déposé ;
  * si le contenu est identique au dernier dépôt (même SHA-256), on ne
    réécrit pas : l'historique reste compact mais complet ;
  * chemin partitionné par source et date de collecte, lisible par DuckDB :
    bronze/<source>/ingest_date=AAAA-MM-JJ/<horodatage>_<sha8>.<ext>
"""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime
from pathlib import Path


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def bronze_path(root: Path, source: str, ingested_at: datetime, digest: str, ext: str) -> Path:
    stamp = ingested_at.strftime("%Y%m%dT%H%M%SZ")
    return (root / "bronze" / source / f"ingest_date={ingested_at:%Y-%m-%d}"
            / f"{stamp}_{digest[:8]}.{ext}")


# Formats texte compressés en gzip avant dépôt : un instantané temps réel PRIM
# (SIRI JSON) pèse ~25 Mo brut et ~10 fois moins compressé. Parquet est déjà
# compressé. DuckDB lit les .json.gz directement (read_json).
COMPRESSED_FORMATS = {"json"}


def encode(content: bytes, file_format: str) -> tuple[bytes, str]:
    """Contenu à écrire et extension finale. Le SHA-256 reste celui du brut."""
    if file_format in COMPRESSED_FORMATS:
        # mtime=0 : même contenu brut -> mêmes octets compressés
        return gzip.compress(content, compresslevel=9, mtime=0), f"{file_format}.gz"
    return content, file_format


def write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(content)
    # mode "x" + rename atomique : un fichier à moitié écrit n'est jamais visible
    with open(path, "xb"):
        pass
    tmp.replace(path)
    path.chmod(0o444)  # lecture seule : protège contre une modification accidentelle
