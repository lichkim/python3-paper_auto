"""Shared, cross-process rate limiting and one-shot 429 retry."""

from __future__ import annotations

import fcntl
import logging
import time
from typing import Any

import requests

from .config import Settings


log = logging.getLogger("paper_trend.http")


class RateLimitExhausted(requests.HTTPError):
    """Raised after a provider returns HTTP 429 twice."""


def _minimum_interval(settings: Settings, provider: str) -> float:
    if provider == "arxiv":
        return settings.arxiv_request_interval
    if provider == "semantic_scholar":
        return settings.semantic_scholar_request_interval
    return 0.0


def _wait_for_slot(settings: Settings, provider: str) -> None:
    """Serialize request starts across the bot and scheduled worker processes."""
    interval = _minimum_interval(settings, provider)
    if interval <= 0:
        return
    directory = settings.data_dir / ".rate_limits"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{provider}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        try:
            previous = float(handle.read().strip() or "0")
        except ValueError:
            previous = 0.0
        remaining = interval - (time.time() - previous)
        if remaining > 0:
            time.sleep(remaining)
        started = time.time()
        handle.seek(0)
        handle.truncate()
        handle.write(f"{started:.6f}\n")
        handle.flush()


def get_with_rate_limit_retry(
    session: requests.Session,
    url: str,
    *,
    settings: Settings,
    provider: str,
    **kwargs: Any,
) -> requests.Response:
    """GET once, then retry one time after the configured delay on HTTP 429."""
    last_response: requests.Response | None = None
    for attempt in range(2):
        _wait_for_slot(settings, provider)
        response = session.get(url, **kwargs)
        if response.status_code != 429:
            return response
        last_response = response
        response.close()
        if attempt == 0:
            log.info(
                "[%s] HTTP 429: %.0f초 후 한 번 재시도",
                provider,
                settings.rate_limit_retry_delay,
            )
            time.sleep(settings.rate_limit_retry_delay)

    raise RateLimitExhausted(
        f"{provider} rate limit persisted after retry",
        response=last_response,
    )
