"""Tests de l'étape 1 sans réseau : httpx.MockTransport simule les API."""

from __future__ import annotations

import httpx
import pytest

from comptage_train import warehouse
from comptage_train.http_client import FetchError, fetch
from comptage_train.ingest import run_ingestion
from comptage_train.settings import Source

ODS = Source(name="sncf_test", kind="opendatasoft",
             base_url="https://ods.example", dataset_id="jeu-test")
METEO = Source(name="meteo_test", kind="http_json", url="https://meteo.example/v1",
               params={"latitude": 48.8})
PRIM = Source(name="prim_test", kind="prim", url="https://prim.example/et")


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def no_sleep(_):
    pass


def test_retry_on_429_then_success():
    calls = []
    responses = iter([httpx.Response(429), httpx.Response(200, content=b"ok")])
    with client_for(lambda req: next(responses)) as client:
        result = fetch(client, "https://x.example", on_call=calls.append, sleep=no_sleep)
    assert result.content == b"ok"
    assert [c.http_status for c in calls] == [429, 200]


def test_no_retry_on_404():
    with client_for(lambda req: httpx.Response(404)) as client:
        with pytest.raises(FetchError) as err:
            fetch(client, "https://x.example", sleep=no_sleep)
    assert err.value.status == 404 and err.value.attempts == 1


def test_ingestion_writes_bronze_and_skips_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIM_API_KEY", raising=False)
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        if request.url.path.endswith("/exports/parquet"):
            return httpx.Response(200, content=b"PAR1-fake-parquet")
        return httpx.Response(200, content=b"pas du json")  # métadonnées illisibles

    with client_for(handler) as client:
        first = run_ingestion([ODS, PRIM], tmp_path, client)
        second = run_ingestion([ODS], tmp_path, client)

    assert [r.outcome for r in first] == ["new", "skipped"]
    assert second[0].outcome == "unchanged"  # repli sur le SHA-256
    assert seen_urls[1].endswith("/catalog/datasets/jeu-test/exports/parquet")

    files = list((tmp_path / "bronze" / "sncf_test").rglob("*.parquet"))
    assert len(files) == 1 and files[0].read_bytes() == b"PAR1-fake-parquet"

    con = warehouse.connect(tmp_path)
    outcomes = con.execute(
        "SELECT outcome FROM monitoring.ingestion_runs ORDER BY started_at").fetchall()
    assert [o[0] for o in outcomes] == ["new", "skipped", "unchanged"]
    assert con.execute("SELECT count(*) FROM monitoring.http_calls").fetchone()[0] == 4


def test_unchanged_producer_version_skips_download(tmp_path):
    # Opendatasoft renvoie des octets différents à chaque export : seule la
    # version annoncée dans les métadonnées permet de détecter l'absence de changement.
    exports = iter([b"export-1", b"export-2", b"export-3"])
    version = {"value": "2026-10-01T00:00:00+00:00"}

    def handler(request):
        if request.url.path.endswith("/exports/parquet"):
            return httpx.Response(200, content=next(exports))
        return httpx.Response(200, json={"metas": {"default": {"data_processed": version["value"]}}})

    with client_for(handler) as client:
        outcomes = [run_ingestion([ODS], tmp_path, client)[0].outcome]
        outcomes.append(run_ingestion([ODS], tmp_path, client)[0].outcome)
        version["value"] = "2026-10-15T00:00:00+00:00"
        outcomes.append(run_ingestion([ODS], tmp_path, client)[0].outcome)

    assert outcomes == ["new", "unchanged", "new"]
    con = warehouse.connect(tmp_path)
    assert con.execute("SELECT list(source_version ORDER BY ingested_at) FROM bronze_raw.files"
                       ).fetchone()[0] == ["2026-10-01T00:00:00+00:00", "2026-10-15T00:00:00+00:00"]


def test_old_logs_without_new_columns_stay_readable(tmp_path):
    import duckdb
    old = tmp_path / "logs" / "bronze_files" / "ingest_date=2026-10-20"
    old.mkdir(parents=True)
    duckdb.sql("SELECT 'a' AS source, TIMESTAMPTZ '2026-10-20 10:00:00+00' AS ingested_at, "
               "'abc' AS sha256, 1::BIGINT AS bytes, 'json' AS format, 'p' AS path, "
               f"'u' AS origin_url").write_parquet(str(old / "old.parquet"))
    assert warehouse.last_deposit_by_source(tmp_path) == {"a": ("abc", None)}


def test_error_is_logged_not_raised(tmp_path):
    with client_for(lambda req: httpx.Response(503)) as client:
        results = run_ingestion([METEO], tmp_path, client, sleep=no_sleep)
    assert results[0].outcome == "error"
    con = warehouse.connect(tmp_path)
    assert con.execute("SELECT count(*) FROM monitoring.http_calls").fetchone()[0] == 4


def test_prim_sends_key_header(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIM_API_KEY", "secret")
    headers = {}

    def handler(request):
        headers.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    with client_for(handler) as client:
        results = run_ingestion([PRIM], tmp_path, client)
    assert results[0].outcome == "new" and headers["apikey"] == "secret"


def test_json_is_stored_gzipped(tmp_path):
    import gzip
    payload = b'{"Siri": {"x": "' + b"a" * 10_000 + b'"}}'
    with client_for(lambda req: httpx.Response(200, content=payload)) as client:
        run_ingestion([METEO], tmp_path, client)
    (stored,) = (tmp_path / "bronze" / "meteo_test").rglob("*.gz")
    assert stored.name.endswith(".json.gz") and stored.stat().st_size < len(payload) / 10
    assert gzip.decompress(stored.read_bytes()) == payload


def test_logs_are_append_only_parquet_files(tmp_path):
    with client_for(lambda req: httpx.Response(200, content=b"x")) as client:
        run_ingestion([METEO], tmp_path, client)
        run_ingestion([METEO], tmp_path, client)
    runs = sorted((tmp_path / "logs" / "ingestion_runs").rglob("*.parquet"))
    assert len(runs) == 2  # un fichier par exécution, aucun n'est réécrit
    con = warehouse.connect(tmp_path)
    assert con.execute("SELECT count(*) FROM bronze_raw.files").fetchone()[0] == 1


def test_connect_on_empty_data_dir(tmp_path):
    con = warehouse.connect(tmp_path)
    assert con.execute("SELECT count(*) FROM monitoring.ingestion_runs").fetchone()[0] == 0
