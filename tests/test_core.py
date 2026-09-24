from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from paper_trend.bot import handle_message
from paper_trend.config import Settings, Topic, load_journals, load_topics
from paper_trend.collectors import (
    _clean_markup,
    _text_matches_query_context,
    _title_matches_query,
    collect_arxiv,
    collect_semantic_scholar,
)
from paper_trend.email_digest import render_topic_papers_email
from paper_trend.http_client import RateLimitExhausted, get_with_rate_limit_retry
from paper_trend.models import Paper, merge_papers, normalize_arxiv_id
from paper_trend.mailer import EmailError, render_user_topics_email, send_test_email
from paper_trend.report import (
    render_telegram,
    render_telegram_messages,
    render_topic_sections,
    render_weekly_telegram_messages,
)
from paper_trend.pipeline import run_daily
from paper_trend.scoring import score_paper
from paper_trend.search import SearchResult, render_search_html, search_relevant
from paper_trend.semantic_cache import load_semantic_cache, prefetch_semantic_scholar
from paper_trend.storage import PreviousState, Repository
from paper_trend.subscribers import SubscriberError, SubscriberStore
from paper_trend.telegram import TelegramClient, split_message
from paper_trend.topic_manager import add_topic, remove_topic


def sample_paper(**overrides) -> Paper:
    values = {
        "title": "Scalable Transmon Architecture",
        "authors": ["Ada Kim", "Max Lee"],
        "published": date(2026, 8, 28),
        "venue": "Physical Review Quantum",
        "doi": "10.1000/example",
        "arxiv_id": "2608.12345v2",
        "landing_url": "https://example.org/paper",
        "pdf_url": "https://example.org/paper.pdf",
        "citation_count": 10,
        "sources": {"arxiv"},
        "topics": {"Transmon 큐비트"},
    }
    values.update(overrides)
    return Paper(**values)


class ModelTests(unittest.TestCase):
    @patch("paper_trend.collectors.get_with_rate_limit_retry")
    def test_arxiv_requests_atom_feed(self, request_mock):
        response = request_mock.return_value
        response.content = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        settings = replace(Settings.from_environment(), max_results_per_source=1)

        papers = collect_arxiv(Topic("Transmon", "transmon qubit", ("arxiv",)), settings)

        self.assertEqual(papers, [])
        session = request_mock.call_args.args[0]
        self.assertIn("application/atom+xml", session.headers["Accept"])

    @patch("paper_trend.collectors.get_with_rate_limit_retry")
    def test_semantic_scholar_uses_recent_bulk_search_and_normalizes_hyphens(
        self, request_mock
    ):
        response = request_mock.return_value
        response.json.return_value = {
            "data": [
                {
                    "title": "Recent weak coupling result",
                    "abstract": "A recent result at the weak coupling limits.",
                    "authors": [{"name": "Ada Kim"}],
                    "publicationDate": date.today().isoformat(),
                    "citationCount": 2,
                    "externalIds": {"DOI": "10.1000/recent"},
                    "openAccessPdf": None,
                    "url": "https://example.org/recent",
                    "venue": "Journal",
                }
            ]
        }
        settings = replace(
            Settings.from_environment(),
            scan_days=14,
            max_results_per_source=20,
        )
        papers = collect_semantic_scholar(
            Topic("Weak coupling", "weak-coupling limits", ("semantic_scholar",)),
            settings,
        )

        self.assertEqual(len(papers), 1)
        url = request_mock.call_args.args[1]
        params = request_mock.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/paper/search/bulk"))
        self.assertEqual(params["query"], "weak coupling limits")
        self.assertEqual(params["sort"], "publicationDate:desc")
        self.assertIn("publicationDateOrYear", params)
        self.assertNotIn("limit", params)

    def test_semantic_context_filter_rejects_scattered_false_positive(self):
        self.assertFalse(
            _text_matches_query_context(
                "Optical performance of red phosphors",
                (
                    "Quantum dots are discussed in a spectroscopy experiment with several "
                    "unrelated optical measurements and material characterization steps. "
                    "The excited state is stable, while energy transfer occurs much later."
                ),
                "quantum state transfer",
            )
        )
        self.assertTrue(
            _text_matches_query_context(
                "Implementation of quantum gates",
                "We demonstrate high-fidelity quantum state transfer protocols.",
                "quantum state transfer",
            )
        )

    def test_production_journal_allowlist_is_exact(self):
        journals = load_journals(Settings.from_environment().journals_file)
        self.assertEqual(
            {(journal.name, journal.issn) for journal in journals},
            {
                ("Physical Review X", "2160-3308"),
                ("PRX Quantum", "2691-3399"),
                ("Nature Physics", "1745-2481"),
                ("npj Quantum Information", "2056-6387"),
            },
        )

    def test_arxiv_versions_share_identity(self):
        self.assertEqual(normalize_arxiv_id("2608.12345v3"), "2608.12345")

    def test_cross_source_merge_preserves_real_venue(self):
        arxiv = sample_paper(doi="", venue="arXiv", sources={"arxiv"})
        journal = sample_paper(arxiv_id="", sources={"crossref"})
        merged = merge_papers([arxiv, journal])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].venue, "Physical Review Quantum")
        self.assertEqual(merged[0].sources, {"arxiv", "crossref"})

    def test_crossref_title_filter_requires_all_topic_terms(self):
        self.assertTrue(
            _title_matches_query(
                "Fast quantum state transfer in a spin chain", "quantum state transfer"
            )
        )
        self.assertEqual(
            _clean_markup('Critical scaling with <mml:mi>T</mml:mi><mml:mi>c</mml:mi>'),
            "Critical scaling with T c",
        )
        self.assertFalse(
            _title_matches_query(
                "Knowledge transfer for state universities", "quantum state transfer"
            )
        )
        self.assertTrue(
            _title_matches_query(
                "Scalable fluxonium-transmon architecture", "transmon qubit", minimum_ratio=0.5
            )
        )


class HttpClientTests(unittest.TestCase):
    @patch("paper_trend.http_client.time.sleep")
    @patch("paper_trend.http_client._wait_for_slot")
    def test_429_waits_fifteen_seconds_and_retries_once(self, wait_mock, sleep_mock):
        limited = Mock(status_code=429)
        success = Mock(status_code=200)
        session = Mock()
        session.get.side_effect = [limited, success]
        settings = replace(
            Settings.from_environment(),
            rate_limit_retry_delay=15.0,
        )

        response = get_with_rate_limit_retry(
            session,
            "https://example.test",
            settings=settings,
            provider="arxiv",
            timeout=10,
        )

        self.assertIs(response, success)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(wait_mock.call_count, 2)
        sleep_mock.assert_called_once_with(15.0)
        limited.close.assert_called_once_with()

    @patch("paper_trend.http_client.time.sleep")
    @patch("paper_trend.http_client._wait_for_slot")
    def test_second_429_is_reported_as_exhausted(self, _wait_mock, _sleep_mock):
        session = Mock()
        session.get.side_effect = [Mock(status_code=429), Mock(status_code=429)]
        settings = replace(
            Settings.from_environment(),
            rate_limit_retry_delay=15.0,
        )

        with self.assertRaises(RateLimitExhausted):
            get_with_rate_limit_retry(
                session,
                "https://example.test",
                settings=settings,
                provider="semantic_scholar",
            )
        self.assertEqual(session.get.call_count, 2)


class ScoringTests(unittest.TestCase):
    def test_new_recent_open_paper_scores_and_explains(self):
        paper = sample_paper(published=date(2026, 8, 29))
        score = score_paper(paper, None, date(2026, 8, 30))
        self.assertGreater(score, 45)
        self.assertIn("오늘 처음 발견", paper.score_reasons)

    def test_citation_velocity_adds_reason(self):
        paper = sample_paper(citation_count=14)
        previous = PreviousState(1, date(2026, 8, 20), date(2026, 8, 28), 10)
        score_paper(paper, previous, date(2026, 8, 30))
        self.assertTrue(any("인용 증가" in reason for reason in paper.score_reasons))


class RepositoryTests(unittest.TestCase):
    def test_save_and_read_latest_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.db"
            paper = sample_paper()
            score_paper(paper, None, date(2026, 8, 30))
            with Repository(path) as repository:
                paper_id = repository.save(paper, date(2026, 8, 30))
                self.assertGreater(paper_id, 0)
                previous = repository.previous(paper)
                self.assertIsNotNone(previous)
                top = repository.top()
                self.assertEqual(top[0].title, paper.title)
                self.assertEqual(top[0].venue, "Physical Review Quantum")

                paper.pdf_path = "/chosen/paper.pdf"
                repository.update_artifacts(paper)
                self.assertEqual(repository.top()[0].pdf_path, "/chosen/paper.pdf")

    def test_same_arxiv_new_version_updates_same_paper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.db"
            first = sample_paper(doi="", arxiv_id="2608.12345v1")
            second = sample_paper(doi="", arxiv_id="2608.12345v3", citation_count=12)
            score_paper(first, None, date(2026, 8, 29))
            with Repository(path) as repository:
                first_id = repository.save(first, date(2026, 8, 29))
                score_paper(second, repository.previous(second), date(2026, 8, 30))
                second_id = repository.save(second, date(2026, 8, 30))
                self.assertEqual(first_id, second_id)

    def test_summary_is_cached_and_cumulative_csv_is_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = sample_paper(summary_ko="초록에 근거한 한국어 한 줄 요약입니다.")
            score_paper(paper, None, date(2026, 8, 30))
            csv_path = root / "papers.csv"
            with Repository(root / "radar.db") as repository:
                repository.save(paper, date(2026, 8, 30))
                self.assertEqual(repository.existing_summary(paper), paper.summary_ko)
                self.assertEqual(repository.export_csv(csv_path), 1)
            exported = csv_path.read_text(encoding="utf-8-sig")
            self.assertIn("abstract_summary_ko", exported)
            self.assertIn(paper.summary_ko, exported)


class OutputTests(unittest.TestCase):
    @patch("paper_trend.semantic_cache.time.sleep")
    @patch("paper_trend.semantic_cache.collect_semantic_scholar")
    def test_semantic_prefetch_writes_incremental_csv_for_next_day(
        self, collect_mock, sleep_mock
    ):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "semantic.csv"
            settings = replace(
                Settings.from_environment(),
                semantic_cache_csv=cache_path,
                semantic_prefetch_delay=60.0,
            )

            def found(topic, _settings):
                return [
                    sample_paper(
                        title=f"Cached {topic.query}",
                        doi=f"10.1000/{topic.name[-1]}",
                        arxiv_id="",
                        sources={"semantic_scholar"},
                        topics={topic.name},
                    )
                ]

            collect_mock.side_effect = found
            topics = [
                Topic("route-1", "first query", ("arxiv", "semantic_scholar")),
                Topic("route-2", "second query", ("semantic_scholar",)),
            ]
            result = prefetch_semantic_scholar(topics, settings)
            loaded = load_semantic_cache(cache_path, {"route-1", "route-2"})

            self.assertEqual(result.topics_succeeded, 2)
            self.assertEqual(len(loaded), 2)
            self.assertEqual({paper.topics.pop() for paper in loaded}, {"route-1", "route-2"})
            sleep_mock.assert_called_once_with(60.0)
            self.assertIn("cached_at", cache_path.read_text(encoding="utf-8-sig"))

    @patch("paper_trend.semantic_cache.time.sleep")
    @patch("paper_trend.semantic_cache.collect_semantic_scholar")
    def test_semantic_prefetch_retries_only_failed_topics(
        self, collect_mock, sleep_mock
    ):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                Settings.from_environment(),
                semantic_cache_csv=Path(directory) / "semantic.csv",
                semantic_prefetch_delay=15.0,
                semantic_prefetch_retry_delay=120.0,
                semantic_prefetch_attempts=3,
            )
            collect_mock.side_effect = [
                RateLimitExhausted("semantic_scholar", 429),
                [sample_paper(sources={"semantic_scholar"}, topics={"route-1"})],
            ]
            result = prefetch_semantic_scholar(
                [Topic("route-1", "retry query", ("semantic_scholar",))],
                settings,
            )
            self.assertEqual(result.topics_succeeded, 1)
            self.assertEqual(result.errors, ())
            self.assertEqual(collect_mock.call_count, 2)
            sleep_mock.assert_called_once_with(120.0)

    def test_user_papers_email_groups_results_by_user_and_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paper_trend.db"
            with SubscriberStore(database) as store:
                store.bootstrap_owner("10001", [])
                store.update_identity("10001", "1", "ownername", "관리자", "private")
                topic = store.add_topic("10001", "트랜스몬", "transmon qubit")
                users = store.active_subscribers()
                topics_by_chat = {"10001": store.topics("10001")}
            paper = sample_paper(summary_ko="트랜스몬 측정 시간을 단축한 연구입니다.")
            paper.trend_score = 31.0
            subject, text, html, entries, unique = render_topic_papers_email(
                [(topic.name, topic.query, [paper])],
                date(2026, 8, 31),
                14,
            )
            self.assertIn("주제별 최신 저널 논문", subject)
            self.assertIn("Scalable Transmon Architecture", text)
            self.assertIn("트랜스몬 측정 시간을 단축", html)
            self.assertIn("https://doi.org/10.1000/example", html)
            self.assertNotIn("사용자를 선택하세요", html)
            self.assertEqual((entries, unique), (1, 1))
            self.assertNotIn("10001", text + html)
            self.assertNotIn("ownername", text + html)
            self.assertNotIn("활성 사용자", text + html)

    def test_user_topics_email_groups_users_without_chat_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                Settings.from_environment(),
                database_path=root / "paper_trend.db",
            )
            with SubscriberStore(settings.database_path) as store:
                store.bootstrap_owner(
                    "10001", [Topic("양자 컴퓨팅", "quantum computing", ("arxiv",))]
                )
                store.update_identity("10001", "1", "ownername", "관리자", "private")
                code, _ = store.create_invite("10001")
                store.redeem_invite(
                    code, "20002", "2", "guestname", "사용자", "private"
                )
                store.add_topic("20002", "AI 투자", "AI investing tools")

            subject, text, html = render_user_topics_email(settings)

            self.assertIn("PaperTrend", subject)
            self.assertIn("활성 사용자 2명", text)
            self.assertIn("@ownername", text)
            self.assertIn("AI investing tools", html)
            self.assertNotIn("10001", text + html)
            self.assertNotIn("20002", text + html)

    @patch("paper_trend.mailer.smtplib.SMTP_SSL")
    def test_email_connection_message_uses_ssl_smtp(self, smtp_mock):
        settings = replace(
            Settings.from_environment(),
            smtp_host="smtp.gmail.com",
            smtp_port=465,
            smtp_user="sender@example.com",
            smtp_password="app-password",
            email_to="reader@example.com",
        )
        smtp = smtp_mock.return_value.__enter__.return_value
        self.assertEqual(send_test_email(settings), 1)
        smtp.login.assert_called_once_with("sender@example.com", "app-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "reader@example.com")
        self.assertIn("PaperTrend", message["Subject"])
        self.assertTrue(message.is_multipart())

    def test_email_requires_private_smtp_credentials(self):
        settings = replace(
            Settings.from_environment(), smtp_user="", smtp_password="", email_to=""
        )
        with self.assertRaises(EmailError):
            send_test_email(settings)

    @patch("paper_trend.telegram.requests.post")
    def test_personal_csv_is_uploaded_as_document(self, post_mock):
        response = post_mock.return_value
        response.ok = True
        response.json.return_value = {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.csv"
            path.write_text("제목\n논문", encoding="utf-8")
            client = TelegramClient("token", "owner")
            sent = client.send_document_to(
                "member", path, filename="개별_논문.csv", caption="내 논문"
            )
        self.assertEqual(sent, 1)
        args, kwargs = post_mock.call_args
        self.assertTrue(args[0].endswith("/sendDocument"))
        self.assertEqual(kwargs["data"]["chat_id"], "member")
        self.assertEqual(kwargs["files"]["document"][0], "개별_논문.csv")

    def test_topic_report_matches_requested_link_format(self):
        paper = sample_paper()
        score_paper(paper, None, date(2026, 8, 30))
        rendered = render_topic_sections(
            [paper],
            [Topic("Transmon 큐비트", "transmon qubit", ("arxiv",))],
            10,
        )
        self.assertIn("## Transmon 큐비트  (transmon qubit)", rendered)
        self.assertIn("### arXiv (1건)", rendered)
        self.assertIn("https://doi.org/10.1000/example", rendered)
        self.assertIn("https://arxiv.org/abs/2608.12345v2", rendered)

    def test_long_telegram_text_is_split(self):
        chunks = split_message(("문단\n\n" * 2000), limit=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))

    def test_telegram_uses_readable_html_without_duplicate_link(self):
        paper = sample_paper(
            authors=["Ada Kim", "Max Lee", "A", "B", "C", "D"],
            summary_ko="가" * 150,
            landing_url="https://doi.org/10.1000/example",
            arxiv_id="",
        )
        score_paper(paper, None, date(2026, 8, 30))
        rendered = render_telegram(
            [paper],
            [Topic("Transmon 큐비트", "transmon qubit", ("arxiv",))],
            date(2026, 8, 30),
            10,
        )
        self.assertIn("📚 <b>오늘의 논문 트렌드</b>", rendered)
        self.assertIn("📖 <b>arXiv</b> · 1건", rendered)
        self.assertIn("Ada Kim, Max Lee, A, B, C 외 1명", rendered)
        self.assertIn("💡 " + ("가" * 119) + "…", rendered)
        self.assertEqual(rendered.count("https://doi.org/10.1000/example"), 1)

    def test_semantic_scholar_venue_metadata_does_not_create_journal_section(self):
        paper = sample_paper(
            venue="RSC Advances",
            sources={"semantic_scholar"},
        )
        rendered = render_telegram(
            [paper],
            [Topic("Transmon 큐비트", "transmon qubit", ("semantic_scholar",))],
            date(2026, 8, 30),
            10,
        )
        self.assertIn("📖 <b>Semantic Scholar</b> · 1건", rendered)
        self.assertNotIn("📖 <b>RSC Advances</b>", rendered)

    def test_allowlisted_journal_collector_keeps_exact_journal_section(self):
        paper = sample_paper(
            venue="PRX Quantum",
            sources={"aps"},
        )
        rendered = render_topic_sections(
            [paper],
            [Topic("Transmon 큐비트", "transmon qubit", ("aps",))],
            10,
        )
        self.assertIn("### PRX Quantum (1건)", rendered)

    def test_telegram_renders_exactly_one_message_per_topic(self):
        topics = [
            Topic("Transmon 큐비트", "transmon qubit", ("arxiv",)),
            Topic("양자 상태 전송", "quantum state transfer", ("arxiv",)),
            Topic("양자 오류 정정", "quantum error correction", ("arxiv",)),
        ]
        messages = render_telegram_messages(
            [sample_paper()], topics, date(2026, 8, 30), 10
        )
        self.assertEqual(len(messages), 3)
        for topic, message in zip(topics, messages, strict=True):
            self.assertIn(topic.name, message)
            self.assertTrue(all(other.name not in message for other in topics if other != topic))

    def test_weekly_telegram_report_uses_seven_day_window(self):
        topic = Topic("Transmon 큐비트", "transmon qubit", ("arxiv",))
        recent = sample_paper(published=date(2026, 8, 28))
        old = sample_paper(title="Old paper", published=date(2026, 8, 10))
        messages = render_weekly_telegram_messages(
            [recent, old], [topic], date(2026, 8, 29), 10
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("논문 주간 리포트", messages[0])
        self.assertIn(recent.title, messages[0])
        self.assertNotIn(old.title, messages[0])
        self.assertIn("2026-08-23 ~ 2026-08-29", messages[0])

    def test_search_output_separates_papers_and_lecture_notes(self):
        lecture = sample_paper(
            title="Lecture Notes on Quantum Computing",
            doi="",
            arxiv_id="2608.99999v1",
            venue="arXiv",
        )
        rendered = render_search_html(
            SearchResult("양자 계산", "quantum computing", [sample_paper()], [lecture], [])
        )
        self.assertIn("관련성 높은 논문 검색", rendered)
        self.assertIn("영문 검색", rendered)
        self.assertIn("Lecture notes · 1건", rendered)

    def test_search_output_hides_only_rate_limit_warnings(self):
        rendered = render_search_html(
            SearchResult(
                "open quantum system",
                "open quantum system",
                [],
                [],
                [
                    "arXiv 검색 실패: 429 Client Error: Too Many Requests",
                    "Semantic Scholar 검색 실패: 429 Client Error",
                    "arXiv 강의자료 검색 실패: read timed out",
                ],
            )
        )
        self.assertNotIn("429", rendered)
        self.assertNotIn("Too Many Requests", rendered)
        self.assertIn("read timed out", rendered)

    @patch("paper_trend.search._crossref", return_value=[])
    @patch("paper_trend.search._semantic_scholar", return_value=[])
    @patch(
        "paper_trend.search._arxiv",
        side_effect=RateLimitExhausted("rate limit persisted"),
    )
    def test_search_silently_skips_provider_after_second_429(
        self, arxiv_mock, _semantic_mock, crossref_mock
    ):
        result = search_relevant("open quantum system", Settings.from_environment())
        self.assertEqual(result.warnings, [])
        self.assertEqual(arxiv_mock.call_count, 2)
        self.assertEqual(crossref_mock.call_count, 2)


class TopicManagerTests(unittest.TestCase):
    def test_add_and_remove_topic_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(
                '{"topics":['
                '{"name":"A","query":"alpha","sources":["arxiv"]},'
                '{"name":"B","query":"beta","sources":["arxiv"]}'
                ']}' ,
                encoding="utf-8",
            )
            added = add_topic(path, "양자 오류 정정", "quantum error correction")
            self.assertEqual(added.name, "양자 오류 정정")
            removed = remove_topic(path, "3")
            self.assertEqual(removed.name, "양자 오류 정정")
            self.assertEqual([topic.name for topic in load_topics(path)], ["A", "B"])


class _FakeTelegramClient:
    def __init__(self):
        self.messages: list[str] = []
        self.destinations: list[str] = []
        self.documents: list[tuple[str, Path, str, str]] = []

    def send_html(self, text: str) -> int:
        self.messages.append(text)
        return 1

    def send_html_to(self, chat_id: str, text: str) -> int:
        self.destinations.append(chat_id)
        self.messages.append(text)
        return 1

    def send_document_to(
        self, chat_id: str, path: Path, filename: str = "", caption: str = ""
    ) -> int:
        self.documents.append((chat_id, Path(path), filename, caption))
        return 1


class BotCommandTests(unittest.TestCase):
    def test_topics_command_replies_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "topics.json"
            path.write_text(
                '{"topics":[{"name":"A","query":"alpha","sources":["arxiv"]}]}',
                encoding="utf-8",
            )
            settings = replace(
                Settings.from_environment(),
                topics_file=path,
                database_path=root / "paper_trend.db",
                telegram_chat_id="owner",
            )
            client = _FakeTelegramClient()
            with SubscriberStore(settings.database_path) as store:
                store.bootstrap_owner("owner", load_topics(path))
                handle_message("/topics", settings, client, store, "owner")
                self.assertIn("내 주제 · 1개", client.messages[0])
                self.assertIn("alpha", client.messages[0])

    def test_papers_csv_command_sends_only_personal_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            topics_path = root / "topics.json"
            topics_path.write_text(
                '{"topics":[{"name":"Transmon","query":"transmon",'
                '"sources":["arxiv"]}]}',
                encoding="utf-8",
            )
            settings = replace(
                Settings.from_environment(),
                data_dir=root / "data",
                database_path=root / "data" / "paper_trend.db",
                topics_file=topics_path,
                telegram_chat_id="owner",
            )
            paper = sample_paper(summary_ko="한국어 한 줄 요약")
            score_paper(paper, None, date(2026, 8, 30))
            with Repository(settings.database_path) as repository:
                paper_id = repository.save(paper, date(2026, 8, 30))
            client = _FakeTelegramClient()
            with SubscriberStore(settings.database_path) as store:
                store.bootstrap_owner("owner", load_topics(topics_path))
                store.record_papers("owner", [(paper_id, {"Transmon"})])
                handle_message("/papers_csv", settings, client, store, "owner")
            self.assertEqual(len(client.documents), 1)
            chat_id, export_path, filename, caption = client.documents[0]
            self.assertEqual(chat_id, "owner")
            self.assertEqual(filename, "개별_논문.csv")
            self.assertIn("1건", caption)
            exported = export_path.read_text(encoding="utf-8-sig")
            self.assertIn("한국어 한 줄 요약", exported)


class SubscriberTests(unittest.TestCase):
    def test_personal_paper_exports_are_isolated_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "paper_trend.db"
            owner_paper = sample_paper(summary_ko="관리자 논문 요약")
            member_paper = sample_paper(
                title="Member Paper", doi="10.1000/member", arxiv_id="2608.99999v1",
                landing_url="https://example.org/member", summary_ko="사용자 논문 요약",
            )
            for paper in (owner_paper, member_paper):
                score_paper(paper, None, date(2026, 8, 30))
            with Repository(database) as repository:
                owner_id = repository.save(owner_paper, date(2026, 8, 30))
                member_id = repository.save(member_paper, date(2026, 8, 30))
            with SubscriberStore(database) as store:
                store.bootstrap_owner("owner", [])
                code, _ = store.create_invite("owner")
                store.redeem_invite(code, "member", "2", "guest", "Guest", "private")
                store.record_papers("owner", [(owner_id, {"A"})])
                store.record_papers("owner", [(owner_id, {"B"})])
                store.record_papers("member", [(member_id, {"C"})])
                owner_csv = root / "owner.csv"
                member_csv = root / "member.csv"
                self.assertEqual(store.export_papers_csv("owner", owner_csv), 1)
                self.assertEqual(store.export_papers_csv("member", member_csv), 1)
            owner_text = owner_csv.read_text(encoding="utf-8-sig")
            member_text = member_csv.read_text(encoding="utf-8-sig")
            self.assertIn("관리자 논문 요약", owner_text)
            self.assertIn("A; B", owner_text)
            self.assertIn(",2,", owner_text)
            self.assertNotIn("사용자 논문 요약", owner_text)
            self.assertIn("사용자 논문 요약", member_text)
            self.assertNotIn("관리자 논문 요약", member_text)

    def test_invite_is_one_time_and_topics_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_trend.db"
            legacy = [Topic("Owner topic", "owner query", ("arxiv",))]
            with SubscriberStore(path) as store:
                store.bootstrap_owner("owner", legacy)
                code, expires = store.create_invite("owner")
                remaining = expires - datetime.now(timezone.utc)
                self.assertGreater(remaining, timedelta(hours=11, minutes=59))
                self.assertLessEqual(remaining, timedelta(hours=12))
                store.redeem_invite(code, "member", "2", "guest", "Guest", "private")
                store.add_topic("member", "Member topic", "member query")
                self.assertEqual([topic.name for topic in store.topics("owner")], ["Owner topic"])
                self.assertEqual([topic.name for topic in store.topics("member")], ["Member topic"])
                with self.assertRaisesRegex(SubscriberError, "이미 사용된"):
                    store.redeem_invite(code, "other", "3", "", "Other", "private")

    def test_removed_owner_topics_are_not_restored_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_trend.db"
            legacy = [Topic("Legacy", "legacy query", ("arxiv",))]
            with SubscriberStore(path) as store:
                store.bootstrap_owner("owner", legacy)
                store.remove_topic("owner", "1")
            with SubscriberStore(path) as store:
                store.bootstrap_owner("owner", legacy)
                self.assertEqual(store.topics("owner"), [])


class PipelineTests(unittest.TestCase):
    @patch("paper_trend.pipeline.collect_all")
    def test_dry_run_does_not_create_database_or_report(self, collect_all_mock):
        collect_all_mock.return_value = ([sample_paper()], {"arxiv": 1}, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            topics = root / "topics.json"
            topics.write_text(
                '{"topics":[{"name":"Transmon 큐비트","query":"transmon qubit","sources":["arxiv"]}]}',
                encoding="utf-8",
            )
            base = Settings.from_environment()
            settings = replace(
                base,
                data_dir=root / "data",
                database_path=root / "data" / "paper_trend.db",
                topics_file=topics,
                reports_dir=root / "reports",
                downloads_dir=root / "downloads",
                translations_dir=root / "translations",
            )
            result = run_daily(settings, dry_run=True)
            self.assertIsNone(result.report_path)
            self.assertFalse(settings.database_path.exists())
            self.assertEqual(list(settings.reports_dir.glob("*.md")), [])

    @patch("paper_trend.pipeline.TelegramClient.from_settings")
    @patch("paper_trend.pipeline.summarize_missing", return_value=(0, []))
    @patch("paper_trend.pipeline.collect_all")
    def test_same_query_is_collected_once_and_sent_to_each_subscriber(
        self, collect_all_mock, _summarize_mock, telegram_mock
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            topics_path = root / "topics.json"
            topics_path.write_text(
                '{"topics":[{"name":"Owner name","query":"shared query",'
                '"sources":["arxiv","semantic_scholar","aps","nature"]}]}',
                encoding="utf-8",
            )
            settings = replace(
                Settings.from_environment(),
                data_dir=root / "data",
                database_path=root / "data" / "paper_trend.db",
                csv_path=root / "data" / "papers.csv",
                topics_file=topics_path,
                reports_dir=root / "reports",
                downloads_dir=root / "downloads",
                translations_dir=root / "translations",
                telegram_bot_token="token",
                telegram_chat_id="owner",
            )
            with SubscriberStore(settings.database_path) as store:
                store.bootstrap_owner("owner", load_topics(topics_path))
                code, _ = store.create_invite("owner")
                store.redeem_invite(code, "member", "2", "guest", "Guest", "private")
                store.add_topic("member", "Member name", "shared query")

            def collected(topics, _settings):
                self.assertEqual(len(topics), 1)
                self.assertNotIn("semantic_scholar", topics[0].sources)
                return ([sample_paper(title='Shared query quantum experiment', venue='arXiv', topics={topics[0].name})], {"arxiv": 1}, [])

            collect_all_mock.side_effect = collected
            client = _FakeTelegramClient()
            telegram_mock.return_value = client
            result = run_daily(settings, target=date(2026, 8, 30))
            self.assertEqual(result.telegram_messages, 2)
            self.assertEqual(client.destinations, ["owner", "member"])
            with SubscriberStore(settings.database_path) as store:
                owner_csv = root / "owner.csv"
                member_csv = root / "member.csv"
                self.assertEqual(store.export_papers_csv("owner", owner_csv), 1)
                self.assertEqual(store.export_papers_csv("member", member_csv), 1)
            self.assertIn("Owner name", owner_csv.read_text(encoding="utf-8-sig"))
            self.assertIn("Member name", member_csv.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
