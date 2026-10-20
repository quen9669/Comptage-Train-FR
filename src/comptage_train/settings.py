"""Lecture de la configuration : fichier .env + catalogue config/sources.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES_FILE = PROJECT_ROOT / "config" / "sources.yaml"


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Charge un .env minimal (CLE=valeur) sans dépendance externe.

    Les variables déjà présentes dans l'environnement gardent la priorité,
    ce qui permet de surcharger depuis systemd ou la ligne de commande.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Source:
    """Une entrée du catalogue sources.yaml."""

    name: str
    kind: str
    schedule: str = "daily"
    enabled: bool = True
    verified: bool = False
    base_url: str | None = None
    dataset_id: str | None = None
    url: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def load_sources(path: Path = DEFAULT_SOURCES_FILE) -> list[Source]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Source(**entry) for entry in raw["sources"]]


def data_dir() -> Path:
    """Racine des données : bronze/ (fichiers bruts) et logs/ (journaux Parquet)."""
    return Path(os.environ.get("COMPTAGE_DATA_DIR", PROJECT_ROOT / "data")).resolve()
