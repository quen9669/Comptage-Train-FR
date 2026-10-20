"""Ligne de commande : `comptage ingest` et `comptage check-sources`."""

from __future__ import annotations

import argparse
import sys

from .connectors import catalog_url
from .http_client import FetchError, fetch, make_client
from .ingest import run_ingestion
from .settings import data_dir, load_dotenv, load_sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="comptage")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="collecter les sources vers la couche bronze")
    ingest.add_argument("--schedule", choices=["daily", "weekly"],
                        help="ne collecter que les sources de cette fréquence")
    ingest.add_argument("--source", help="ne collecter qu'une source (nom du catalogue)")
    sub.add_parser("check-sources", help="vérifier que chaque jeu Opendatasoft existe")
    args = parser.parse_args(argv)

    load_dotenv()
    sources = load_sources()

    if args.command == "check-sources":
        return _check_sources(sources)

    if args.source:
        sources = [s for s in sources if s.name == args.source]
        if not sources:
            parser.error(f"source inconnue : {args.source}")
    with make_client() as client:
        results = run_ingestion(sources, data_dir(), client, schedule=args.schedule)
    for r in results:
        print(f"{r.outcome:<10} {r.source}  {r.message}")
    # Code de sortie non nul si une source a échoué : visible dans systemd
    return 1 if any(r.outcome == "error" for r in results) else 0


def _check_sources(sources) -> int:
    failures = 0
    with make_client(timeout_s=30) as client:
        for source in sources:
            url = catalog_url(source)
            if url is None:
                print(f"--   {source.name}  (pas de catalogue à vérifier)")
                continue
            try:
                fetch(client, url, max_attempts=2)
                print(f"OK   {source.name}  {source.dataset_id}")
            except FetchError as exc:
                failures += 1
                print(f"KO   {source.name}  {source.dataset_id}  {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
