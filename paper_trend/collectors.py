"""Collectors for arXiv, Semantic Scholar, and Crossref."""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from html import unescape

import requests

from .config import Settings, Topic, load_journals
from .http_client import RateLimitExhausted, get_with_rate_limit_retry
from .models import Paper, normalize_doi, normalize_title


log = logging.getLogger("paper_trend")
ARXIV_API = "https://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"
SEMANTIC_SCHOLAR_BULK_API = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
CROSSREF_API = "https://api.crossref.org/works"
CROSSREF_JOURNAL_API = "https://api.crossref.org/journals/{issn}/works"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class CollectionError(RuntimeError):
    pass


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _crossref_date(item: dict) -> date | None:
    for field in ("published-online", "published-print", "published", "created"):
        parts = (item.get(field) or {}).get("date-parts", [[]])
        if not parts or not parts[0]:
            continue
        values = list(parts[0]) + [1, 1]
        try:
            return date(int(values[0]), int(values[1]), int(values[2]))
        except (TypeError, ValueError):
            continue
    return None


def _query_terms(query: str) -> list[str]:
    """Return meaningful query tokens for strict Crossref title filtering."""
    normalized = normalize_title(query)
    stopwords = {"and", "or", "the", "of", "in", "for", "with", "a", "an"}
    return [term for term in normalized.split() if term not in stopwords and len(term) > 1]


def _title_matches_query(title: str, query: str, minimum_ratio: float = 1.0) -> bool:
    words = set(normalize_title(title).split())
    terms = _query_terms(query)
    hits = sum(term in words for term in terms)
    return bool(terms) and hits >= max(1, math.ceil(len(terms) * minimum_ratio))


def _text_matches_query_context(title: str, abstract: str, query: str) -> bool:
    """Require all query terms to occur in one nearby title/abstract context.

    Semantic Scholar bulk search is intentionally broad and can return papers that
    contain common query words in unrelated parts of a long abstract.  A compact
    token window keeps genuine variants such as ``perfect state transfer`` while
    rejecting coincidental matches such as optical phosphor papers for
    ``quantum state transfer``.
    """
    terms = _query_terms(query)
    if not terms:
        return False
    words = normalize_title(f"{title} {abstract}").split()
    window_size = max(8, len(terms) * 4)
    for start in range(len(words)):
        if terms[0] not in words[start : start + window_size]:
            continue
        if all(term in words[start : start + window_size] for term in terms):
            return True
    return False


def _clean_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(value).split())


def _session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": settings.user_agent, "Accept": "application/json"})
    return session


def collect_arxiv(topic: Topic, settings: Settings) -> list[Paper]:
    session = _session(settings)
    # arXiv returns an Atom feed.  Asking it for JSON can produce HTTP 406.
    session.headers["Accept"] = "application/atom+xml, application/xml;q=0.9, */*;q=0.8"
    query = topic.query if any(prefix in topic.query for prefix in ("ti:", "abs:", "cat:", "au:")) else f'all:"{topic.query}"'
    params = {
        "search_query": query,
        "start": 0,
        "max_results": settings.max_results_per_source,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        response = get_with_rate_limit_retry(
            session,
            ARXIV_API,
            settings=settings,
            provider="arxiv",
            params=params,
            timeout=settings.request_timeout,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except RateLimitExhausted:
        raise
    except (requests.RequestException, ET.ParseError) as exc:
        raise CollectionError(f"arXiv 수집 실패: {exc}") from exc

    cutoff = date.today() - timedelta(days=settings.scan_days)
    results: list[Paper] = []
    for entry in root.findall("a:entry", ATOM):
        raw_id = (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip()
        arxiv_id = raw_id.rsplit("/", 1)[-1]
        title = " ".join((entry.findtext("a:title", default="", namespaces=ATOM) or "").split())
        published = _date(entry.findtext("a:published", default="", namespaces=ATOM))
        updated = _date(entry.findtext("a:updated", default="", namespaces=ATOM))
        if published and published < cutoff and (not updated or updated < cutoff):
            continue
        doi = entry.findtext("arxiv:doi", default="", namespaces=ATOM) or ""
        authors = [
            " ".join((node.findtext("a:name", default="", namespaces=ATOM) or "").split())
            for node in entry.findall("a:author", ATOM)
        ]
        results.append(
            Paper(
                title=title,
                abstract=" ".join((entry.findtext("a:summary", default="", namespaces=ATOM) or "").split()),
                authors=[author for author in authors if author],
                published=published,
                updated=updated,
                venue="arXiv",
                doi=normalize_doi(doi) if doi else "",
                arxiv_id=arxiv_id,
                landing_url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                sources={"arxiv"},
                topics={topic.name},
            )
        )
    return results


def collect_semantic_scholar(topic: Topic, settings: Settings) -> list[Paper]:
    session = _session(settings)
    if settings.semantic_scholar_api_key:
        session.headers["x-api-key"] = settings.semantic_scholar_api_key
    cutoff = date.today() - timedelta(days=settings.scan_days)
    normalized_query = re.sub(r"[-‐‑‒–—]+", " ", topic.query)
    normalized_query = " ".join(normalized_query.split())
    params = {
        "query": normalized_query,
        "fields": "title,abstract,authors,publicationDate,citationCount,externalIds,openAccessPdf,url,venue,journal",
        "publicationDateOrYear": f"{cutoff.isoformat()}:{date.today().isoformat()}",
        "sort": "publicationDate:desc",
    }
    try:
        response = get_with_rate_limit_retry(
            session,
            SEMANTIC_SCHOLAR_BULK_API,
            settings=settings,
            provider="semantic_scholar",
            params=params,
            timeout=settings.request_timeout,
        )
        response.raise_for_status()
        rows = response.json().get("data", [])[: settings.max_results_per_source]
    except RateLimitExhausted:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise CollectionError(f"Semantic Scholar 수집 실패: {exc}") from exc

    results: list[Paper] = []
    for item in rows:
        title = " ".join(str(item.get("title") or "").split())
        abstract = " ".join(str(item.get("abstract") or "").split())
        if not _text_matches_query_context(title, abstract, topic.query):
            continue
        published = _date(item.get("publicationDate"))
        if published and published < cutoff:
            continue
        external = item.get("externalIds") or {}
        arxiv_id = str(external.get("ArXiv") or "")
        doi = str(external.get("DOI") or "")
        open_pdf = item.get("openAccessPdf") or {}
        pdf_url = str(open_pdf.get("url") or "")
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        results.append(
            Paper(
                title=title,
                abstract=abstract,
                authors=[str(author.get("name") or "") for author in (item.get("authors") or []) if author.get("name")],
                published=published,
                venue=str(item.get("venue") or (item.get("journal") or {}).get("name") or "Semantic Scholar"),
                doi=normalize_doi(doi) if doi else "",
                arxiv_id=arxiv_id,
                landing_url=str(item.get("url") or ""),
                pdf_url=pdf_url,
                citation_count=max(0, int(item.get("citationCount") or 0)),
                sources={"semantic_scholar"},
                topics={topic.name},
            )
        )
    return [paper for paper in results if paper.title]


def collect_crossref(topic: Topic, settings: Settings) -> list[Paper]:
    session = _session(settings)
    cutoff = date.today() - timedelta(days=settings.scan_days)
    future_limit = date.today() + timedelta(days=180)
    params = {
        "query.title": topic.query,
        "filter": (
            f"from-pub-date:{cutoff.isoformat()},"
            f"until-pub-date:{future_limit.isoformat()},type:journal-article"
        ),
        "sort": "published",
        "order": "desc",
        "rows": settings.max_results_per_source,
    }
    try:
        response = session.get(CROSSREF_API, params=params, timeout=settings.request_timeout)
        response.raise_for_status()
        rows = response.json().get("message", {}).get("items", [])
    except (requests.RequestException, ValueError) as exc:
        raise CollectionError(f"Crossref 수집 실패: {exc}") from exc

    results: list[Paper] = []
    for item in rows:
        title = _clean_markup(str((item.get("title") or [""])[0]))
        if not _title_matches_query(title, topic.query):
            continue
        published = _crossref_date(item)
        if published and published > future_limit:
            continue
        pdf_url = ""
        for link in item.get("link") or []:
            content_type = str(link.get("content-type") or "").lower()
            if "pdf" in content_type:
                pdf_url = str(link.get("URL") or "")
                break
        authors = [
            " ".join(part for part in (str(author.get("given") or ""), str(author.get("family") or "")) if part)
            for author in item.get("author") or []
        ]
        results.append(
            Paper(
                title=title,
                abstract=_clean_markup(str(item.get("abstract") or "")),
                authors=[author for author in authors if author],
                published=published,
                venue=unescape(str((item.get("container-title") or ["Crossref"])[0])),
                doi=normalize_doi(str(item.get("DOI") or "")),
                landing_url=str(item.get("URL") or ""),
                pdf_url=pdf_url,
                citation_count=max(0, int(item.get("is-referenced-by-count") or 0)),
                sources={"crossref"},
                topics={topic.name},
            )
        )
    return [paper for paper in results if paper.title]


def collect_journal_group(topic: Topic, settings: Settings, group: str) -> list[Paper]:
    """Search an explicit APS or Nature journal allow-list through Crossref ISSNs."""
    session = _session(settings)
    cutoff = date.today() - timedelta(days=settings.scan_days)
    future_limit = date.today() + timedelta(days=180)
    journals = [journal for journal in load_journals(settings.journals_file) if journal.group == group]
    results: list[Paper] = []
    failures: list[str] = []
    for journal in journals:
        params = {
            "query.title": topic.query,
            "filter": (
                f"from-pub-date:{cutoff.isoformat()},"
                f"until-pub-date:{future_limit.isoformat()},type:journal-article"
            ),
            "sort": "published",
            "order": "desc",
            "rows": settings.max_results_per_source,
        }
        try:
            response = session.get(
                CROSSREF_JOURNAL_API.format(issn=journal.issn),
                params=params,
                timeout=settings.request_timeout,
            )
            response.raise_for_status()
            rows = response.json().get("message", {}).get("items", [])
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{journal.name}: {exc}")
            continue
        for item in rows:
            title = _clean_markup(str((item.get("title") or [""])[0]))
            if not _title_matches_query(
                title, topic.query, minimum_ratio=topic.journal_match_ratio
            ):
                continue
            published = _crossref_date(item)
            if published and (published < cutoff or published > future_limit):
                continue
            pdf_url = ""
            for link in item.get("link") or []:
                if "pdf" in str(link.get("content-type") or "").lower():
                    pdf_url = str(link.get("URL") or "")
                    break
            doi = normalize_doi(str(item.get("DOI") or ""))
            authors = [
                " ".join(
                    part
                    for part in (str(author.get("given") or ""), str(author.get("family") or ""))
                    if part
                )
                for author in item.get("author") or []
            ]
            results.append(
                Paper(
                    title=title,
                    abstract=_clean_markup(str(item.get("abstract") or "")),
                    authors=[author for author in authors if author],
                    published=published,
                    venue=journal.name,
                    doi=doi,
                    landing_url=f"https://doi.org/{doi}" if doi else str(item.get("URL") or ""),
                    pdf_url=pdf_url,
                    citation_count=max(0, int(item.get("is-referenced-by-count") or 0)),
                    sources={group},
                    topics={topic.name},
                )
            )
    if journals and len(failures) == len(journals):
        raise CollectionError(f"{group.upper()} 저널 수집 전체 실패: {failures[0]}")
    for failure in failures:
        log.warning("[%s 저널 경고] %s", group, failure)
    return results


def collect_aps(topic: Topic, settings: Settings) -> list[Paper]:
    return collect_journal_group(topic, settings, "aps")


def collect_nature(topic: Topic, settings: Settings) -> list[Paper]:
    return collect_journal_group(topic, settings, "nature")


COLLECTORS = {
    "arxiv": collect_arxiv,
    "semantic_scholar": collect_semantic_scholar,
    "aps": collect_aps,
    "nature": collect_nature,
    "crossref": collect_crossref,
}


def collect_all(topics: list[Topic], settings: Settings) -> tuple[list[Paper], dict[str, int], list[str]]:
    """Collect every configured topic while isolating failures per source/topic."""
    papers: list[Paper] = []
    counts = {source: 0 for topic in topics for source in topic.sources}
    errors: list[str] = []
    for topic in topics:
        for source in topic.sources:
            try:
                found = COLLECTORS[source](topic, settings)
                papers.extend(found)
                counts[source] += len(found)
                log.info("[%s] %s: %d건", source, topic.name, len(found))
            except CollectionError as exc:
                message = f"{topic.name}/{source}: {exc}"
                errors.append(message)
                log.warning("[수집 경고] %s", message)
            except RateLimitExhausted:
                log.info("[%s] 429 재시도 실패: 이번 수집에서 조용히 생략", source)
    return papers, counts, errors
