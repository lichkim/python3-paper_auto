"""Nightly Semantic Scholar prefetch backed by a next-day CSV cache."""
from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .collectors import (
    CollectionError,
    _text_matches_query_context,
    collect_semantic_scholar,
)
from .config import Settings, Topic
from .http_client import RateLimitExhausted
from .models import Paper


log = logging.getLogger("paper_trend.semantic_cache")
CACHE_FIELDS = (
    "cached_at", "route", "query", "title", "abstract", "authors_json",
    "published", "updated", "venue", "doi", "arxiv_id", "landing_url",
    "pdf_url", "citation_count", "sources_json",
)


@dataclass(frozen=True)
class SemanticPrefetchResult:
    topics_total: int
    topics_succeeded: int
    papers: int
    errors: tuple[str, ...]
    csv_path: Path


def _paper_row(paper: Paper, route: str, query: str, cached_at: str) -> dict[str, str]:
    return {
        "cached_at": cached_at,
        "route": route,
        "query": query,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors_json": json.dumps(paper.authors, ensure_ascii=False),
        "published": paper.published.isoformat() if paper.published else "",
        "updated": paper.updated.isoformat() if paper.updated else "",
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "landing_url": paper.landing_url,
        "pdf_url": paper.pdf_url,
        "citation_count": str(paper.citation_count),
        "sources_json": json.dumps(sorted(paper.sources), ensure_ascii=False),
    }


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error):
        return []


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CACHE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def prefetch_semantic_scholar(
    topics: list[Topic], settings: Settings
) -> SemanticPrefetchResult:
    """Fetch each routed topic sequentially and checkpoint successful rows."""
    semantic_topics = [
        Topic(topic.name, topic.query, ("semantic_scholar",), topic.journal_match_ratio)
        for topic in topics
        if "semantic_scholar" in topic.sources
    ]
    active_routes = {topic.name for topic in semantic_topics}
    existing = [
        row for row in _read_rows(settings.semantic_cache_csv)
        if row.get("route", "") in active_routes
    ]
    rows_by_route: dict[str, list[dict[str, str]]] = {}
    for row in existing:
        rows_by_route.setdefault(row.get("route", ""), []).append(row)

    freshness_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    succeeded_routes: set[str] = set()
    for route, route_rows in rows_by_route.items():
        timestamps: list[datetime] = []
        for row in route_rows:
            try:
                timestamp = datetime.fromisoformat(row.get("cached_at", ""))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                timestamps.append(timestamp)
            except ValueError:
                continue
        if timestamps and max(timestamps) >= freshness_cutoff:
            succeeded_routes.add(route)

    pending = [topic for topic in semantic_topics if topic.name not in succeeded_routes]
    last_errors: dict[str, str] = {}
    if succeeded_routes:
        log.info(
            "[Semantic 야간 캐시] 최근 성공 주제 %d개 유지, 나머지 %d개 재시도",
            len(succeeded_routes), len(pending),
        )
    for attempt in range(1, settings.semantic_prefetch_attempts + 1):
        if not pending:
            break
        next_pending: list[Topic] = []
        for index, topic in enumerate(pending):
            try:
                found = collect_semantic_scholar(topic, settings)
                cached_at = datetime.now(timezone.utc).isoformat()
                rows_by_route[topic.name] = [
                    _paper_row(paper, topic.name, topic.query, cached_at)
                    for paper in found
                ]
                succeeded_routes.add(topic.name)
                last_errors.pop(topic.name, None)
                _write_rows(
                    settings.semantic_cache_csv,
                    [row for route_rows in rows_by_route.values() for row in route_rows],
                )
                log.info(
                    "[Semantic 야간 캐시] 시도 %d/%d · %s: %d건 저장",
                    attempt, settings.semantic_prefetch_attempts, topic.query, len(found),
                )
            except (CollectionError, RateLimitExhausted) as exc:
                message = f"{topic.query}: {exc}"
                last_errors[topic.name] = message
                next_pending.append(topic)
                log.warning(
                    "[Semantic 야간 캐시 경고] 시도 %d/%d · %s",
                    attempt, settings.semantic_prefetch_attempts, message,
                )
            if index + 1 < len(pending):
                time.sleep(settings.semantic_prefetch_delay)
        pending = next_pending
        if pending and attempt < settings.semantic_prefetch_attempts:
            log.info(
                "[Semantic 야간 캐시] 실패 주제 %d개를 %.0f초 후 다시 시도",
                len(pending), settings.semantic_prefetch_retry_delay,
            )
            time.sleep(settings.semantic_prefetch_retry_delay)

    rows = [row for route_rows in rows_by_route.values() for row in route_rows]
    _write_rows(settings.semantic_cache_csv, rows)
    errors = [last_errors[topic.name] for topic in pending if topic.name in last_errors]
    return SemanticPrefetchResult(
        topics_total=len(semantic_topics),
        topics_succeeded=len(succeeded_routes),
        papers=len(rows),
        errors=tuple(errors),
        csv_path=settings.semantic_cache_csv,
    )


def load_semantic_cache(
    path: Path,
    allowed_routes: set[str],
    max_age_hours: int = 36,
) -> list[Paper]:
    """Load fresh cached rows for the current routed subscriber topics."""
    threshold = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    papers: list[Paper] = []
    for row in _read_rows(path):
        route = row.get("route", "")
        if route not in allowed_routes:
            continue
        if not _text_matches_query_context(
            row.get("title", ""), row.get("abstract", ""), row.get("query", "")
        ):
            continue
        try:
            cached_at = datetime.fromisoformat(row.get("cached_at", ""))
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            if cached_at < threshold:
                continue
            published = date.fromisoformat(row["published"]) if row.get("published") else None
            updated = date.fromisoformat(row["updated"]) if row.get("updated") else None
            papers.append(
                Paper(
                    title=row.get("title", ""),
                    abstract=row.get("abstract", ""),
                    authors=list(json.loads(row.get("authors_json", "[]"))),
                    published=published,
                    updated=updated,
                    venue=row.get("venue", "Semantic Scholar"),
                    doi=row.get("doi", ""),
                    arxiv_id=row.get("arxiv_id", ""),
                    landing_url=row.get("landing_url", ""),
                    pdf_url=row.get("pdf_url", ""),
                    citation_count=max(0, int(row.get("citation_count", "0") or 0)),
                    sources=set(json.loads(row.get("sources_json", '["semantic_scholar"]'))),
                    topics={route},
                )
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return [paper for paper in papers if paper.title]
