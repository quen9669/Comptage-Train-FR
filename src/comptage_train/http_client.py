"""Client HTTP avec nouvelles tentatives et télémétrie de chaque appel.

Chaque tentative est enregistrée (statut, latence, taille) : c'est la matière
première du Dashboard 0. On réessaie sur les erreurs réseau, les 429 (quota)
et les 5xx, avec une attente exponentielle ; on respecte Retry-After si l'API
le fournit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "comptage-train-fr/0.1 (projet personnel Open Data)"


@dataclass
class CallRecord:
    url: str
    attempt: int
    started_at: datetime
    duration_ms: float
    http_status: int | None
    bytes: int
    error: str | None


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None, attempts: int):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


@dataclass
class FetchResult:
    content: bytes
    status: int
    attempts: int
    content_type: str


def fetch(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_attempts: int = 4,
    backoff_s: float = 2.0,
    on_call: Callable[[CallRecord], None] = lambda record: None,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """Télécharge une ressource, en réessayant si l'erreur est transitoire."""
    last_status: int | None = None
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        wait = backoff_s * 2 ** (attempt - 1)
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            last_status, last_error = None, f"{type(exc).__name__}: {exc}"
            on_call(CallRecord(url, attempt, started, _ms(t0), None, 0, last_error))
        else:
            last_status = response.status_code
            on_call(CallRecord(url, attempt, started, _ms(t0), last_status,
                               len(response.content), None))
            if response.is_success:
                return FetchResult(response.content, last_status, attempt,
                                   response.headers.get("content-type", ""))
            last_error = f"HTTP {last_status}"
            if last_status not in RETRY_STATUSES:
                break  # 4xx définitif (404, 401...) : inutile d'insister
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
        if attempt < max_attempts:
            sleep(wait)
    raise FetchError(last_error, last_status, attempt)


def make_client(timeout_s: float = 120.0) -> httpx.Client:
    return httpx.Client(timeout=timeout_s, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)
