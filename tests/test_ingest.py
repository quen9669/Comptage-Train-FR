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
        return httpx.Response(200, content=b"PAR1-fake-parquet")

    with client_for(handler) as client:
        first = run_ingestion([ODS, PRIM], tmp_path, client)
        second = run_ingestion([ODS], tmp_path, client)

    assert [r.outcome for r in first] == ["new", "skipped"]
    assert second[0].outcome == "unchanged"
    assert seen_urls[0].endswith("/catalog/datasets/jeu-test/exports/parquet")

    files = list((tmp_path / "bronze" / "sncf_test").rglob("*.parquet"))
    assert len(files) == 1 and files[0].read_bytes() == b"PAR1-fake-parquet"

    con = warehouse.connect(tmp_path)
    outcomes = con.execute(
        "SELECT outcome FROM monitoring.ingestion_runs ORDER BY started_at").fetchall()
    assert [o[0] for o in outcomes] == ["new", "skipped", "unchanged"]
    assert con.execute("SELECT count(*) FROM monitoring.http_calls").fetchone()[0] == 2


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
