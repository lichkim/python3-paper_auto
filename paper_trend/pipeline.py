"""Daily scan pipeline: collect, deduplicate, score, persist metadata, report, Telegram."""

from __future__ import annotations

import copy
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from .collectors import collect_all
from .config import Settings, Topic, load_topics, load_journals
from .selection import select_papers, save_selection_audit
from .models import Paper, merge_papers
from .report import (
    render_daily_report,
    render_telegram_messages,
    render_weekly_report,
    render_weekly_telegram_messages,
    save_daily_report,
    save_weekly_report,
)
from .scoring import score_paper
from .semantic_cache import load_semantic_cache
from .storage import Repository
from .subscribers import Subscriber, SubscriberStore, SubscriberTopic
from .summarizer import summarize_missing
from .telegram import TelegramClient, TelegramError


log = logging.getLogger("paper_trend")


def _topic_signature(topic: SubscriberTopic) -> tuple[str, tuple[str, ...]]:
    return (
        " ".join(topic.query.casefold().split()),
        tuple(sorted(topic.sources)),
    )


def _route_name(signature: tuple[str, tuple[str, ...]]) -> str:
    digest = hashlib.sha256(repr(signature).encode("utf-8")).hexdigest()[:16]
    return f"__route_{digest}"


def _delivered_papers(
    papers: list[Paper],
    topics: list[Topic],
    target: date,
    per_topic: int,
    weekly: bool,
) -> list[Paper]:
    """Mirror the Telegram renderer's per-topic limit for delivery history."""
    candidates = papers
    if weekly:
        start = target - timedelta(days=6)
        candidates = [
            paper for paper in papers
            if paper.published is None or start <= paper.published <= target
        ]
    selected: dict[str, Paper] = {}
    for topic in topics:
        for paper in [p for p in candidates if topic.name in p.topics][:per_topic]:
            existing = selected.get(paper.key)
            if existing is None:
                existing = copy.copy(paper)
                existing.topics = set()
                selected[paper.key] = existing
            existing.topics.add(topic.name)
    return list(selected.values())


def _subscriber_configuration(
    settings: Settings,
) -> tuple[list[Subscriber], dict[str, list[SubscriberTopic]], list[Topic], dict[str, set[str]]]:
    legacy_topics = load_topics(settings.topics_file)
    with SubscriberStore(settings.database_path) as store:
        store.bootstrap_owner(settings.telegram_chat_id, legacy_topics)
        subscribers = store.active_subscribers()
        topics_by_chat = {user.chat_id: store.topics(user.chat_id) for user in subscribers}

    unique: dict[tuple[str, tuple[str, ...]], Topic] = {}
    display_names: dict[str, set[str]] = {}
    for topics in topics_by_chat.values():
        for subscriber_topic in topics:
            signature = _topic_signature(subscriber_topic)
            route = _route_name(signature)
            existing = unique.get(signature)
            if existing is None or subscriber_topic.journal_match_ratio < existing.journal_match_ratio:
                unique[signature] = subscriber_topic.as_topic(route)
            display_names.setdefault(route, set()).add(subscriber_topic.name)
    return subscribers, topics_by_chat, list(unique.values()), display_names


@dataclass(frozen=True)
class DailyResult:
    papers: list[Paper]
    counts: dict[str, int]
    errors: list[str]
    report_path: Path | None
    csv_path: Path | None
    summaries_created: int
    telegram_messages: int


def run_daily(
    settings: Settings,
    target: date | None = None,
    dry_run: bool = False,
    skip_telegram: bool = False,
    weekly: bool = False,
) -> DailyResult:
    target = target or date.today()
    settings.ensure_dirs()
    subscribers: list[Subscriber] = []
    topics_by_chat: dict[str, list[SubscriberTopic]] = {}
    display_names: dict[str, set[str]] = {}
    if dry_run or not settings.telegram_configured:
        topics = load_topics(settings.topics_file)
    else:
        subscribers, topics_by_chat, topics, display_names = _subscriber_configuration(settings)

    live_topics = [
        replace(
            topic,
            sources=tuple(
                source for source in topic.sources if source != "semantic_scholar"
            ),
        )
        for topic in topics
    ]
    raw, counts, errors = collect_all(
        [topic for topic in live_topics if topic.sources], settings
    )
    cached_semantic = load_semantic_cache(
        settings.semantic_cache_csv,
        {topic.name for topic in topics},
    )
    raw.extend(cached_semantic)
    counts["semantic_scholar"] = len(cached_semantic)
    papers = merge_papers(raw)
    papers, selection_decisions = select_papers(papers, topics, load_journals(settings.journals_file))
    counts['selection_excluded'] = sum(d['status']=='excluded' for d in selection_decisions)
    counts['selection_accepted'] = len(papers)

    routes_by_paper: dict[int, set[str]] = {}
    report_topics: list[Topic] = topics
    if subscribers:
        routes_by_paper = {id(paper): set(paper.topics) for paper in papers}
        for paper in papers:
            paper.topics = {
                display_name
                for route in routes_by_paper[id(paper)]
                for display_name in display_names.get(route, set())
            }
        seen_report_topics: set[tuple[str, str]] = set()
        report_topics = []
        for subscriber_topics in topics_by_chat.values():
            for subscriber_topic in subscriber_topics:
                identity = (subscriber_topic.name, subscriber_topic.query)
                if identity in seen_report_topics:
                    continue
                seen_report_topics.add(identity)
                report_topics.append(subscriber_topic.as_topic())

    if dry_run:
        for paper in papers:
            score_paper(paper, None, target)
        papers.sort(key=lambda paper: (paper.trend_score, paper.citation_count), reverse=True)
        return DailyResult(papers, counts, errors, None, None, 0, 0)

    paper_ids_by_key: dict[str, int] = {}
    save_selection_audit(settings.reports_dir,target,selection_decisions,'weekly' if weekly else 'daily')
    with Repository(settings.database_path) as repository:
        for paper in papers:
            score_paper(paper, repository.previous(paper), target)
            paper.summary_ko = repository.existing_summary(paper)
        papers.sort(key=lambda paper: (paper.trend_score, paper.citation_count), reverse=True)
        summaries_created, summary_errors = summarize_missing(papers, settings)
        errors.extend(summary_errors)
        for paper in papers:
            paper_ids_by_key[paper.key] = repository.save(paper, target)
        csv_path = settings.csv_path
        repository.export_csv(csv_path)

    if weekly:
        report_content = render_weekly_report(
            papers, report_topics, target, settings.report_top_n
        )
        report_path = save_weekly_report(report_content, settings.reports_dir, target)
    else:
        report_content = render_daily_report(
            papers, report_topics, counts, errors, target, settings.report_top_n
        )
        report_path = save_daily_report(report_content, settings.reports_dir, target)
    excluded = [d for d in selection_decisions if d['status']=='excluded']
    audit_section = '\n\n## 선정·제외 정책\n\n- 정책: topic-journal-v1.0\n- 전체 판정: 같은 날짜의 *-selection.json\n'
    audit_section += '\n'.join(f"- 제외: {d['title']} — {d['reason']}" for d in excluded)
    report_path.write_text(report_content+audit_section+'\n',encoding='utf-8')
    telegram_messages = 0
    if settings.telegram_configured and not skip_telegram:
        client = TelegramClient.from_settings(settings)
        recipients = subscribers or [
            Subscriber(settings.telegram_chat_id, "", "", "Owner", "private", "admin", "active")
        ]
        fallback_topics = load_topics(settings.topics_file)
        with SubscriberStore(settings.database_path) as delivery_store:
            for subscriber in recipients:
                subscriber_topics = topics_by_chat.get(subscriber.chat_id, [])
                user_topics = [topic.as_topic() for topic in subscriber_topics] or (
                    fallback_topics if not subscribers else []
                )
                if not user_topics:
                    continue
                user_papers: list[Paper] = []
                if subscribers:
                    for paper in papers:
                        matched_names = {
                            topic.name
                            for topic in subscriber_topics
                            if _route_name(_topic_signature(topic)) in routes_by_paper[id(paper)]
                        }
                        if matched_names:
                            selected = copy.copy(paper)
                            selected.topics = matched_names
                            user_papers.append(selected)
                else:
                    user_papers = papers
                try:
                    renderer = (
                        render_weekly_telegram_messages if weekly else render_telegram_messages
                    )
                    for telegram_text in renderer(
                        user_papers, user_topics, target, settings.report_top_n
                    ):
                        telegram_messages += client.send_html_to(
                            subscriber.chat_id, telegram_text
                        )
                    delivered = _delivered_papers(
                        user_papers, user_topics, target, settings.report_top_n, weekly
                    )
                    delivery_store.record_papers(
                        subscriber.chat_id,
                        [
                            (paper_ids_by_key[paper.key], set(paper.topics))
                            for paper in delivered if paper.key in paper_ids_by_key
                        ],
                    )
                except TelegramError as exc:
                    message = f"{subscriber.chat_id}: {exc}"
                    errors.append(message)
                    log.warning("[Telegram 경고] %s", message)
    return DailyResult(
        papers, counts, errors, report_path, csv_path, summaries_created, telegram_messages
    )
