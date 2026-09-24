"""Anonymous topic digest containing papers from the configured journals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape
import re

from .collectors import collect_all
from .config import Settings, Topic, load_journals
from .selection import select_papers, save_selection_audit
from .mailer import send_email
from .models import Paper, merge_papers
from .pipeline import _subscriber_configuration
from .scoring import score_paper
from .storage import Repository
from .summarizer import summarize_missing


TopicDigest = tuple[str, str, list[Paper]]


@dataclass(frozen=True)
class EmailDigestResult:
    sent: int
    topics: int
    paper_entries: int
    unique_papers: int
    summaries_created: int
    warnings: tuple[str, ...]


def _paper_url(paper: Paper) -> str:
    if paper.doi:
        return f"https://doi.org/{paper.doi}"
    return paper.landing_url or paper.pdf_url


def render_topic_papers_email(
    topic_digests: list[TopicDigest],
    target: date,
    scan_days: int,
) -> tuple[str, str, str, int, int]:
    """Render a journal digest containing no subscriber identity or ownership data."""
    topic_count = len(topic_digests)
    paper_entries = sum(len(rows) for _name, _query, rows in topic_digests)
    unique_papers = len(
        {paper.key for _name, _query, rows in topic_digests for paper in rows}
    )
    subject = f"[PaperTrend] 주제별 최신 저널 논문 — {target.isoformat()}"
    text_lines = [
        "PaperTrend 주제별 최신 저널 논문",
        f"검색 범위: 최근 {scan_days}일",
        "수집처: 지정된 APS·Nature 저널 (arXiv·Semantic Scholar 제외)",
        f"주제 {topic_count}개 · 논문 목록 {paper_entries}건(고유 {unique_papers}건)",
        "",
    ]
    topic_sections: list[str] = []
    for topic_name, query, rows in topic_digests:
        text_lines.append(f"{topic_name} ({query}) — {len(rows)}건")
        paper_cards: list[str] = []
        for index, paper in enumerate(rows, 1):
            authors = [author for author in paper.authors if author.lower() != "anonymous"]
            author_text = ", ".join(authors[:5]) or "저자 정보 없음"
            if len(authors) > 5:
                author_text += f" 외 {len(authors) - 5}명"
            published = paper.published.isoformat() if paper.published else "날짜 미상"
            venue = paper.venue or "/".join(sorted(paper.sources)) or "출처 미상"
            url = _paper_url(paper)
            text_lines.append(f"  {index}. {paper.title}")
            text_lines.append(f"     {venue} · {published} · {author_text}")
            if paper.summary_ko:
                text_lines.append(f"     요약: {paper.summary_ko}")
            if paper.selection_reasons:
                text_lines.append('     선정 근거: ' + '; '.join(paper.selection_reasons))
            if url:
                text_lines.append(f"     링크: {url}")

            title_html = escape(paper.title)
            if url:
                title_html = (
                    f'<a href="{escape(url, quote=True)}" '
                    f'style="color:#2537a5;text-decoration:none">{title_html}</a>'
                )
            summary_html = ""
            if paper.summary_ko:
                summary_html = (
                    '<p style="margin:8px 0 0;color:#344054;line-height:1.55">'
                    f'💡 {escape(paper.summary_ko)}</p>'
                )
            paper_cards.append(
                '<article style="padding:15px 0;border-top:1px solid #edf0f5">'
                f'<div style="font-size:16px;font-weight:700;line-height:1.45">{index}. {title_html}</div>'
                f'<div style="margin-top:6px;color:#667085;font-size:13px">{escape(venue)} · {published} · '
                f'트렌드 {paper.trend_score:.1f}</div>'
                f'<div style="margin-top:4px;color:#667085;font-size:13px">👤 {escape(author_text)}</div>'
                f'{summary_html}<p>선정 근거: {escape("; ".join(paper.selection_reasons))}</p></article>'
            )
        if not rows:
            text_lines.append("  새로 포착된 저널 논문 없음")
            paper_cards.append(
                '<p style="color:#667085;margin:12px 0 2px">새로 포착된 저널 논문이 없습니다.</p>'
            )
        text_lines.append("")
        topic_sections.append(
            '<section style="margin:14px 0;background:#fff;border:1px solid #e5eaf1;'
            'border-radius:14px;padding:18px">'
            f'<h2 style="margin:0;font-size:19px">🔬 {escape(topic_name)} '
            f'<span style="color:#667085;font-size:13px;font-weight:400">· {len(rows)}건</span></h2>'
            f'<code style="display:block;margin-top:5px;color:#5b5bd6">{escape(query)}</code>'
            f'{"".join(paper_cards)}</section>'
        )

    html = f"""
    <div style="background:#f4f7fb;padding:28px;font-family:system-ui,-apple-system,'Noto Sans KR',sans-serif;color:#172033">
      <div style="max-width:820px;margin:auto">
        <header style="background:linear-gradient(135deg,#292b5f,#5b5bd6);color:#fff;padding:28px;border-radius:20px">
          <div style="font-size:12px;opacity:.75;letter-spacing:.12em">PAPERTREND · JOURNAL DIGEST</div>
          <h1 style="margin:6px 0;font-size:28px">주제별 최신 저널 논문</h1>
          <div>{target.isoformat()} · 최근 {scan_days}일 · APS &amp; Nature</div>
        </header>
        <div style="display:flex;gap:10px;margin:14px 0;flex-wrap:wrap">
          <div style="background:#fff;padding:14px 18px;border-radius:14px"><b>{topic_count}</b> 주제</div>
          <div style="background:#fff;padding:14px 18px;border-radius:14px"><b>{paper_entries}</b> 목록 논문</div>
          <div style="background:#fff;padding:14px 18px;border-radius:14px"><b>{unique_papers}</b> 고유 논문</div>
        </div>
        {"".join(topic_sections)}
        <p style="color:#667085;font-size:12px;text-align:center">지정된 APS·Nature 저널 논문만 포함합니다.</p>
      </div>
    </div>
    """
    return subject, "\n".join(text_lines), html, paper_entries, unique_papers


def scan_and_send_user_papers_email(settings: Settings, to: str = "") -> EmailDigestResult:
    """Scan anonymous subscriber topics against journals and send one digest."""
    target = date.today()
    users, topics_by_chat, _routed_topics, _display_names = _subscriber_configuration(settings)
    grouped_topics: dict[str, tuple[str, str, float]] = {}
    for user in users:
        for topic in topics_by_chat.get(user.chat_id, []):
            key = " ".join(topic.query.casefold().split())
            existing = grouped_topics.get(key)
            if existing is None:
                grouped_topics[key] = (topic.name, topic.query, topic.journal_match_ratio)
            elif topic.journal_match_ratio < existing[2]:
                grouped_topics[key] = (existing[0], existing[1], topic.journal_match_ratio)
    journal_topics: list[Topic] = []
    display_by_route: dict[str, tuple[str, str]] = {}
    for index, (name, query, match_ratio) in enumerate(grouped_topics.values(), 1):
        route = f"__journal_email_{index}"
        journal_topics.append(
            Topic(route, query, ("aps", "nature"), match_ratio)
        )
        display_by_route[route] = (name, query)

    raw, _counts, errors = collect_all(journal_topics, settings)
    papers = merge_papers(raw)
    papers,decisions = select_papers(papers,journal_topics,load_journals(settings.journals_file))
    save_selection_audit(settings.reports_dir,target,decisions,'email')
    routes_by_paper = {id(paper): set(paper.topics) for paper in papers}

    with Repository(settings.database_path) as repository:
        for paper in papers:
            score_paper(paper, repository.previous(paper), target)
            paper.summary_ko = repository.existing_summary(paper)
    papers.sort(key=lambda paper: (paper.trend_score, paper.citation_count), reverse=True)

    topic_digests: list[TopicDigest] = []
    selected_by_key: dict[str, Paper] = {}
    for route, (name, query) in display_by_route.items():
        selected = [
            paper for paper in papers if route in routes_by_paper[id(paper)]
        ][: settings.report_top_n]
        topic_digests.append((name, query, selected))
        for paper in selected:
            selected_by_key[paper.key] = paper

    summaries_created, summary_errors = summarize_missing(
        list(selected_by_key.values()), settings
    )
    errors.extend(summary_errors)
    # Cache scan results and summaries without touching per-user Telegram delivery history.
    with Repository(settings.database_path) as repository:
        for paper in papers:
            paper.topics = {
                display_by_route[route][0]
                for route in routes_by_paper[id(paper)]
                if route in display_by_route
            }
            repository.save(paper, target)
        repository.export_csv(settings.csv_path)
    subject, text, html, paper_entries, unique_papers = render_topic_papers_email(
        topic_digests,
        target,
        settings.scan_days,
    )
    recipient_text = to or settings.email_to
    recipients = [
        address.strip()
        for address in re.split(r"[,;]", recipient_text)
        if address.strip()
    ]
    sent = sum(
        send_email(settings, subject, text, html, recipient)
        for recipient in recipients
    )
    return EmailDigestResult(
        sent=sent,
        topics=len(topic_digests),
        paper_entries=paper_entries,
        unique_papers=unique_papers,
        summaries_created=summaries_created,
        warnings=tuple(errors),
    )
