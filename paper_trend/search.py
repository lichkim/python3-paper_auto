"""On-demand relevance search for papers and lecture-note style resources."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from html import escape

import requests
from openai import OpenAI

from .collectors import (
    ARXIV_API,
    ATOM,
    CROSSREF_API,
    SEMANTIC_SCHOLAR_API,
    _clean_markup,
    _crossref_date,
    _date,
    _session,
)
from .config import Settings, Topic, load_journals
from .selection import select_papers
from .http_client import RateLimitExhausted, get_with_rate_limit_retry
from .models import Paper, merge_papers, normalize_doi, normalize_title


LECTURE_WORDS = {
    "lecture", "lectures", "tutorial", "pedagogical", "review", "primer",
    "introduction", "handbook", "course", "notes",
}

RATE_LIMIT_WARNING = re.compile(r"(?:\b429\b|too many requests)", re.IGNORECASE)


@dataclass(frozen=True)
class SearchResult:
    original_query: str
    effective_query: str
    papers: list[Paper]
    lecture_notes: list[Paper]
    warnings: list[str]
    selection_decisions: list[dict] = field(default_factory=list)


def _english_query(query: str, settings: Settings) -> tuple[str, str]:
    query = " ".join(query.split())
    if not re.search(r"[가-힣]", query) or not settings.deepseek_api_key:
        return query, ""
    try:
        client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=float(settings.request_timeout),
        )
        response = client.chat.completions.create(
            model=settings.summary_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "한국어 연구 주제를 학술 데이터베이스 검색에 적합한 간결한 영어 "
                        "검색 구문으로 번역하세요. 설명, 따옴표, 목록 없이 검색 구문만 출력하세요."
                    ),
                },
                {"role": "user", "content": query},
            ],
            temperature=0,
            max_tokens=60,
        )
        translated = " ".join((response.choices[0].message.content or "").split())
        translated = translated.strip('"“”')
        return (translated or query), ""
    except Exception as exc:
        return query, f"한국어 검색어의 영문 변환 실패: {exc}"


def _arxiv(query: str, settings: Settings, lecture: bool = False) -> list[Paper]:
    search_query = f'all:"{query}"'
    if lecture:
        search_query += " AND (all:lecture OR all:tutorial OR all:review OR all:pedagogical)"
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max(15, settings.max_results_per_source),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    response = get_with_rate_limit_retry(
        _session(settings),
        ARXIV_API,
        settings=settings,
        provider="arxiv",
        params=params,
        timeout=settings.request_timeout,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    papers: list[Paper] = []
    for entry in root.findall("a:entry", ATOM):
        raw_id = (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip()
        arxiv_id = raw_id.rsplit("/", 1)[-1]
        title = " ".join((entry.findtext("a:title", default="", namespaces=ATOM) or "").split())
        abstract = " ".join((entry.findtext("a:summary", default="", namespaces=ATOM) or "").split())
        if lecture and not _looks_like_lecture(title, abstract):
            continue
        authors = [
            " ".join((node.findtext("a:name", default="", namespaces=ATOM) or "").split())
            for node in entry.findall("a:author", ATOM)
        ]
        doi = entry.findtext("arxiv:doi", default="", namespaces=ATOM) or ""
        papers.append(
            Paper(
                title=title,
                abstract=abstract,
                authors=[author for author in authors if author],
                published=_date(entry.findtext("a:published", default="", namespaces=ATOM)),
                updated=_date(entry.findtext("a:updated", default="", namespaces=ATOM)),
                venue="arXiv",
                doi=normalize_doi(doi) if doi else "",
                arxiv_id=arxiv_id,
                landing_url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                sources={"arxiv"},
            )
        )
    return papers


def _semantic_scholar(query: str, settings: Settings) -> list[Paper]:
    session = _session(settings)
    if settings.semantic_scholar_api_key:
        session.headers["x-api-key"] = settings.semantic_scholar_api_key
    response = get_with_rate_limit_retry(
        session,
        SEMANTIC_SCHOLAR_API,
        settings=settings,
        provider="semantic_scholar",
        params={
            "query": query,
            "limit": min(max(15, settings.max_results_per_source), 100),
            "fields": "title,abstract,authors,publicationDate,citationCount,externalIds,openAccessPdf,url,venue,journal",
        },
        timeout=settings.request_timeout,
    )
    response.raise_for_status()
    papers: list[Paper] = []
    for item in response.json().get("data", []):
        external = item.get("externalIds") or {}
        arxiv_id = str(external.get("ArXiv") or "")
        doi = str(external.get("DOI") or "")
        open_pdf = item.get("openAccessPdf") or {}
        papers.append(
            Paper(
                title=" ".join(str(item.get("title") or "").split()),
                abstract=" ".join(str(item.get("abstract") or "").split()),
                authors=[
                    str(author.get("name")) for author in item.get("authors") or []
                    if author.get("name")
                ],
                published=_date(item.get("publicationDate")),
                venue=str(item.get("venue") or (item.get("journal") or {}).get("name") or "Semantic Scholar"),
                doi=normalize_doi(doi) if doi else "",
                arxiv_id=arxiv_id,
                landing_url=str(item.get("url") or ""),
                pdf_url=str(open_pdf.get("url") or ""),
                citation_count=max(0, int(item.get("citationCount") or 0)),
                sources={"semantic_scholar"},
            )
        )
    return [paper for paper in papers if paper.title]


def _crossref(query: str, settings: Settings, lecture: bool = False) -> list[Paper]:
    search = f"{query} lecture notes tutorial review" if lecture else query
    response = _session(settings).get(
        CROSSREF_API,
        params={
            "query.bibliographic": search,
            "sort": "relevance",
            "order": "desc",
            "rows": max(15, settings.max_results_per_source),
        },
        timeout=settings.request_timeout,
    )
    response.raise_for_status()
    papers: list[Paper] = []
    for item in response.json().get("message", {}).get("items", []):
        title = _clean_markup(str((item.get("title") or [""])[0]))
        abstract = _clean_markup(str(item.get("abstract") or ""))
        venue = _clean_markup(str((item.get("container-title") or ["Crossref"])[0]))
        if not title or (lecture and not _looks_like_lecture(title, abstract, venue)):
            continue
        authors = [
            " ".join(
                part for part in (
                    str(author.get("given") or ""), str(author.get("family") or "")
                ) if part
            )
            for author in item.get("author") or []
        ]
        doi = normalize_doi(str(item.get("DOI") or ""))
        papers.append(
            Paper(
                title=title,
                abstract=abstract,
                authors=[author for author in authors if author],
                published=_crossref_date(item),
                venue=venue,
                doi=doi,
                landing_url=f"https://doi.org/{doi}" if doi else str(item.get("URL") or ""),
                citation_count=max(0, int(item.get("is-referenced-by-count") or 0)),
                sources={"crossref"},
            )
        )
    return papers


def _looks_like_lecture(*values: str) -> bool:
    words = set(normalize_title(" ".join(values)).split())
    return bool(words & LECTURE_WORDS)


def _relevance(paper: Paper, query: str) -> float:
    terms = [word for word in normalize_title(query).split() if len(word) > 1]
    title = normalize_title(paper.title)
    abstract = normalize_title(paper.abstract)
    title_words = set(title.split())
    abstract_words = set(abstract.split())
    if not terms:
        return 0
    score = 0.0
    phrase = " ".join(terms)
    if phrase and phrase in title:
        score += 50
    score += 30 * sum(term in title_words for term in terms) / len(terms)
    score += 12 * sum(term in abstract_words for term in terms) / len(terms)
    score += min(8.0, math.log10(paper.citation_count + 1) * 2.5)
    score += min(6.0, len(paper.sources) * 2.0)
    if paper.published:
        age = max(0, (date.today() - paper.published).days)
        score += max(0.0, 5.0 - age / 730)
    return round(score, 1)


def _rank(papers: list[Paper], query: str, limit: int) -> list[Paper]:
    merged = merge_papers(papers)
    for paper in merged:
        paper.trend_score = _relevance(paper, query)
    merged.sort(key=lambda paper: (paper.trend_score, paper.citation_count), reverse=True)
    return merged[:limit]


def search_relevant(query: str, settings: Settings, paper_limit: int = 6, lecture_limit: int = 4) -> SearchResult:
    original = " ".join(query.split())
    if not original:
        raise ValueError("검색어를 입력해주세요.")
    effective, translation_warning = _english_query(original, settings)
    warnings = [translation_warning] if translation_warning else []
    papers: list[Paper] = []
    for name, collector in (
        ("arXiv", lambda: _arxiv(effective, settings)),
        ("Semantic Scholar", lambda: _semantic_scholar(effective, settings)),
        ("Crossref", lambda: _crossref(effective, settings)),
    ):
        try:
            papers.extend(collector())
        except RateLimitExhausted:
            continue
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            warnings.append(f"{name} 검색 실패: {exc}")

    lecture_notes: list[Paper] = []
    for name, collector in (
        ("arXiv 강의자료", lambda: _arxiv(effective, settings, lecture=True)),
        ("Crossref 강의자료", lambda: _crossref(effective, settings, lecture=True)),
    ):
        try:
            lecture_notes.extend(collector())
        except RateLimitExhausted:
            continue
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            warnings.append(f"{name} 검색 실패: {exc}")

    for paper in papers:
        paper.topics = {'search'}
    papers, decisions = select_papers(merge_papers(papers), [Topic('search',effective,('arxiv','semantic_scholar','aps','nature'))], load_journals(settings.journals_file))
    ranked_papers = _rank(papers, effective, paper_limit)
    paper_keys = {key for paper in ranked_papers for key in paper.all_keys}
    ranked_lectures = [
        paper for paper in _rank(lecture_notes, effective, lecture_limit * 2)
        if paper.all_keys.isdisjoint(paper_keys)
    ][:lecture_limit]
    return SearchResult(original, effective, ranked_papers, ranked_lectures, warnings, decisions)


def render_search_html(result: SearchResult) -> str:
    lines = [
        "🔍 <b>관련성 높은 논문 검색</b>",
        f"검색어: <code>{escape(result.original_query)}</code>",
    ]
    if result.effective_query != result.original_query:
        lines.append(f"영문 검색: <code>{escape(result.effective_query)}</code>")
    lines.extend(["", f"📄 <b>논문 · {len(result.papers)}건</b>", ""])
    if not result.papers:
        lines.append("검색 결과가 없습니다.")
    for index, paper in enumerate(result.papers, 1):
        lines.extend(_search_paper_lines(paper, index))
        lines.append("")

    excluded = [d for d in result.selection_decisions if d['status']=='excluded']
    if excluded:
        lines.extend(['',f'필터 제외 {len(excluded)}건 (예시 최대 3건):', *[f"• {escape(d['title'][:80])}: {escape(d['reason'])}" for d in excluded[:3]]])
    lines.extend(["━━━━━━━━━━━━━━", f"📘 <b>Lecture notes · {len(result.lecture_notes)}건</b>", "강의 자료는 저널 논문 제한과 별도 검색입니다.", ""])
    if not result.lecture_notes:
        lines.append("조건에 맞는 강의노트·튜토리얼·리뷰 자료를 찾지 못했습니다.")
    for index, paper in enumerate(result.lecture_notes, 1):
        lines.extend(_search_paper_lines(paper, index, include_score=False))
        lines.append("")
    visible_warnings = [
        warning for warning in result.warnings
        if not RATE_LIMIT_WARNING.search(warning)
    ]
    if visible_warnings:
        lines.extend(
            [
                "⚠️ 일부 검색원 응답 지연 또는 제한",
                *[f"• {escape(item)}" for item in visible_warnings],
            ]
        )
    return "\n".join(lines).strip()


def _search_paper_lines(paper: Paper, index: int, include_score: bool = True) -> list[str]:
    link = paper.landing_url or paper.pdf_url
    title = escape(paper.title)
    if link:
        title = f'<a href="{escape(link, quote=True)}">{title}</a>'
    meta = [escape(paper.venue or "출처 미상")]
    if paper.published:
        meta.append(paper.published.isoformat())
    if include_score:
        meta.append(f"관련도 {paper.trend_score:.1f}")
    if paper.citation_count:
        meta.append(f"인용 {paper.citation_count}")
    return [f"<b>{index}.</b> {title}", " · ".join(meta), *(['선정 근거: '+escape('; '.join(paper.selection_reasons))] if paper.selection_reasons else [])]
