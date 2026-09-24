"""Daily Markdown and Telegram text rendering."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from html import escape
from pathlib import Path

from .config import Topic
from .models import Paper


REPORT_SOURCE_ORDER = (
    "Physical Review X",
    "PRX Quantum",
    "Nature Physics",
    "npj Quantum Information",
    "arXiv",
    "Semantic Scholar",
)
REPORT_JOURNALS = frozenset(REPORT_SOURCE_ORDER[:4])


def _report_source(paper: Paper) -> str:
    """Use collection provenance for broad indexes, not their venue metadata."""
    if paper.venue in REPORT_JOURNALS and paper.sources & {"aps", "nature"}:
        return paper.venue
    if "arxiv" in paper.sources:
        return "arXiv"
    if "semantic_scholar" in paper.sources:
        return "Semantic Scholar"
    return paper.venue or "/".join(sorted(paper.sources)) or "기타"


def _group_for_report(papers: list[Paper]) -> dict[str, list[Paper]]:
    grouped: dict[str, list[Paper]] = defaultdict(list)
    for paper in papers:
        grouped[_report_source(paper)].append(paper)
    return dict(
        sorted(
            grouped.items(),
            key=lambda item: (
                REPORT_SOURCE_ORDER.index(item[0])
                if item[0] in REPORT_SOURCE_ORDER
                else len(REPORT_SOURCE_ORDER),
                item[0],
            ),
        )
    )


def _paper_lines(paper: Paper, include_score: bool = True) -> list[str]:
    authors = ", ".join(paper.authors[:8]) if paper.authors else "확인되지 않음"
    if len(paper.authors) > 8:
        authors += " 외"
    lines = [f"- **{paper.title}**", f"  - 저자: {authors}"]
    if paper.summary_ko:
        lines.append(f"  - 한 줄 요약: {paper.summary_ko}")
    if paper.published:
        lines.append(f"  - 날짜: {paper.published.isoformat()}")
    if paper.doi:
        lines.append(f"  - DOI: https://doi.org/{paper.doi}")
    if paper.arxiv_id:
        lines.append(f"  - arXiv: https://arxiv.org/abs/{paper.arxiv_id}")
    link = paper.landing_url or paper.pdf_url
    if link:
        lines.append(f"  - 링크: {link}")
    if include_score:
        reasons = ", ".join(paper.score_reasons[:3])
        lines.append(f"  - 트렌드 점수: {paper.trend_score:.1f} ({reasons})")
    return lines


def render_topic_sections(papers: list[Paper], topics: list[Topic], per_topic: int) -> str:
    lines: list[str] = []
    for topic in topics:
        selected = [paper for paper in papers if topic.name in paper.topics][:per_topic]
        lines.append(f"## {topic.name}  ({topic.query})")
        if not selected:
            lines.extend(["", "- 새로 포착된 논문 없음", ""])
            continue
        for venue, venue_papers in _group_for_report(selected).items():
            lines.extend(["", f"### {venue} ({len(venue_papers)}건)"])
            for paper in venue_papers:
                lines.extend(_paper_lines(paper))
        lines.append("")
    return "\n".join(lines).strip()


def render_daily_report(
    papers: list[Paper],
    topics: list[Topic],
    counts: dict[str, int],
    errors: list[str],
    target: date,
    per_topic: int,
) -> str:
    lines = [
        f"# Paper Trend — {target.isoformat()}",
        "",
        "## 수집 현황",
        "",
        *[f"- {source}: {count}건" for source, count in sorted(counts.items())],
        f"- 중복 통합 후: {len(papers)}건",
    ]
    if errors:
        lines.extend(["", "### 수집 경고", *[f"- {error}" for error in errors]])
    lines.extend(["", render_topic_sections(papers, topics, per_topic), ""])
    return "\n".join(lines)


def save_daily_report(content: str, reports_dir: Path, target: date) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{target.isoformat()}.md"
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path


def _papers_in_week(papers: list[Paper], target: date) -> tuple[date, list[Paper]]:
    start = target - timedelta(days=6)
    selected = [
        paper
        for paper in papers
        if paper.published is None or start <= paper.published <= target
    ]
    return start, selected


def render_weekly_report(
    papers: list[Paper], topics: list[Topic], target: date, per_topic: int
) -> str:
    start, weekly_papers = _papers_in_week(papers, target)
    return "\n".join(
        [
            f"# Paper Trend — Weekly {target.isocalendar()[0]}-W{target.isocalendar()[1]:02d}",
            "",
            f"- 집계 기간: {start.isoformat()} ~ {target.isoformat()}",
            f"- 포착 논문: {len(weekly_papers)}건",
            "",
            render_topic_sections(weekly_papers, topics, per_topic),
            "",
        ]
    )


def save_weekly_report(content: str, reports_dir: Path, target: date) -> Path:
    weekly_dir = reports_dir / "weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)
    iso_year, iso_week, _ = target.isocalendar()
    path = weekly_dir / f"{iso_year}-W{iso_week:02d}.md"
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path


def render_telegram_messages(
    papers: list[Paper], topics: list[Topic], target: date, per_topic: int
) -> list[str]:
    """Render one independent Telegram message for each configured topic."""
    return [
        _render_telegram_topic(papers, topic, target, per_topic)
        for topic in topics
    ]


def render_weekly_telegram_messages(
    papers: list[Paper], topics: list[Topic], target: date, per_topic: int
) -> list[str]:
    """Render one seven-day summary message per configured topic."""
    start, weekly_papers = _papers_in_week(papers, target)
    return [
        _render_weekly_telegram_topic(weekly_papers, topic, start, target, per_topic)
        for topic in topics
    ]


def render_telegram(papers: list[Paper], topics: list[Topic], target: date, per_topic: int) -> str:
    """Render all topic messages together for previews and backward compatibility."""
    return "\n\n".join(render_telegram_messages(papers, topics, target, per_topic))


def _render_telegram_topic(
    papers: list[Paper], topic: Topic, target: date, per_topic: int
) -> str:
    lines = [
        "📚 <b>오늘의 논문 트렌드</b>",
        f"🗓 <code>{target.isoformat()}</code>",
        "",
        "━━━━━━━━━━━━━━",
        f"🔬 <b>{escape(topic.name)}</b>",
        f"🔎 <code>{escape(topic.query)}</code>",
    ]
    selected = [paper for paper in papers if topic.name in paper.topics][:per_topic]
    if not selected:
        lines.extend(["", "새로 포착된 논문이 없습니다."])
        return "\n".join(lines).strip()

    for venue, venue_papers in _group_for_report(selected).items():
        lines.extend(
            [
                "",
                f"📖 <b>{escape(venue)}</b> · {len(venue_papers)}건",
                "",
            ]
        )
        for index, paper in enumerate(venue_papers, start=1):
            lines.extend(_telegram_paper_lines(paper, index))
            lines.append("")
    return "\n".join(lines).strip()


def _render_weekly_telegram_topic(
    papers: list[Paper], topic: Topic, start: date, target: date, per_topic: int
) -> str:
    matched = [paper for paper in papers if topic.name in paper.topics]
    selected = matched[:per_topic]
    lines = [
        "📊 <b>논문 주간 리포트</b>",
        f"🗓 <code>{start.isoformat()} ~ {target.isoformat()}</code>",
        "",
        "━━━━━━━━━━━━━━",
        f"🔬 <b>{escape(topic.name)}</b>",
        f"🔎 <code>{escape(topic.query)}</code>",
        f"이번 주 포착: <b>{len(matched)}건</b> · 상위 {len(selected)}건",
    ]
    if not selected:
        lines.extend(["", "이번 주에 새로 포착된 논문이 없습니다."])
        return "\n".join(lines).strip()

    for venue, venue_papers in _group_for_report(selected).items():
        lines.extend(["", f"📖 <b>{escape(venue)}</b> · {len(venue_papers)}건", ""])
        for index, paper in enumerate(venue_papers, start=1):
            lines.extend(_telegram_paper_lines(paper, index))
            lines.append("")
    return "\n".join(lines).strip()


def _telegram_paper_lines(paper: Paper, index: int) -> list[str]:
    authors = [author for author in paper.authors if author.lower() != "anonymous"]
    if not authors:
        author_text = "저자 정보 없음"
    elif len(authors) > 5:
        author_text = f"{', '.join(authors[:5])} 외 {len(authors) - 5}명"
    else:
        author_text = ", ".join(authors)

    lines = [
        f"<b>{index}. {escape(paper.title)}</b>",
        f"👤 {escape(author_text)}",
    ]
    if paper.summary_ko:
        summary = paper.summary_ko
        if len(summary) > 120:
            summary = summary[:119].rstrip() + "…"
        lines.append(f"💡 {escape(summary)}")
    details: list[str] = []
    if paper.published:
        details.append(f"🗓 {paper.published.isoformat()}")
    details.append(f"📈 {paper.trend_score:.1f}")
    if paper.score_reasons:
        details.append(escape(", ".join(paper.score_reasons[:3])))
    lines.append(" · ".join(details))

    links: list[tuple[str, str]] = []
    if paper.doi:
        links.append(("DOI", f"https://doi.org/{paper.doi}"))
    if paper.arxiv_id:
        links.append(("arXiv", f"https://arxiv.org/abs/{paper.arxiv_id}"))
    if paper.landing_url or paper.pdf_url:
        links.append(("원문", paper.landing_url or paper.pdf_url))

    unique_links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for label, url in links:
        normalized = url.rstrip("/")
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        unique_links.append((label, url))
    if unique_links:
        linked = "  ·  ".join(
            f'<a href="{escape(url, quote=True)}">{label}</a>'
            for label, url in unique_links
        )
        lines.append(f"🔗 {linked}")
    return lines
