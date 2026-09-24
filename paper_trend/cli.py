"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .bot import run_bot
from .config import Settings, load_journals, load_topics
from .downloader import DownloadError, download_pdf
from .email_digest import scan_and_send_user_papers_email
from .mailer import EmailError, send_test_email, send_user_topics_email
from .pipeline import _subscriber_configuration, run_daily
from .report import render_topic_sections
from .search import render_search_html, search_relevant
from .semantic_cache import prefetch_semantic_scholar
from .storage import Repository
from .telegram import TelegramClient, TelegramError
from .translator import TranslationError, translate_pdf


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-trend",
        description="주제별 최신·트렌드 논문을 찾아 Telegram으로 링크를 보내는 도구",
    )
    parser.add_argument("--verbose", action="store_true", help="자세한 실행 로그")
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("daily", help="오늘의 수집·점수·리포트·Telegram 실행")
    daily.add_argument("--dry-run", action="store_true", help="DB/리포트/Telegram 저장 없이 수집 결과만 확인")
    daily.add_argument("--skip-telegram", action="store_true", help="Telegram 전송 생략")
    weekly = sub.add_parser("weekly", help="최근 7일 수집·주간 리포트·Telegram 실행")
    weekly.add_argument("--dry-run", action="store_true", help="DB/리포트/Telegram 저장 없이 수집 결과만 확인")
    weekly.add_argument("--skip-telegram", action="store_true", help="Telegram 전송 생략")

    top = sub.add_parser("top", help="DB에 저장된 최근 트렌드 순위 조회")
    top.add_argument("--limit", type=int, default=10)

    fetch = sub.add_parser("fetch", help="선택한 최근 순위 논문만 PDF 다운로드")
    fetch.add_argument("rank", type=int, help="top 명령에 표시되는 순위(1부터 시작)")
    fetch.add_argument("--translate", action="store_true", help="다운로드 후 한국어 LaTeX 변환")
    fetch.add_argument("--force", action="store_true")

    translate = sub.add_parser("translate", help="선택한 로컬 PDF를 한국어 LaTeX로 변환")
    translate.add_argument("pdf", type=Path)
    translate.add_argument("--title", default="")
    translate.add_argument("--force", action="store_true")

    sub.add_parser("doctor", help="설정과 실행 준비 상태 점검")
    test = sub.add_parser("telegram-test", help="Telegram에 짧은 연결 확인 메시지 전송")
    test.add_argument("--message", default="paper_trend Telegram 연결 확인 완료")
    email_test = sub.add_parser("email-test", help="설정된 주소로 이메일 연결 테스트")
    email_test.add_argument("--to", default="", help="선택 수신 주소(기본 PAPER_TREND_EMAIL_TO)")
    email_topics = sub.add_parser("email-topics", help="관리자에게 사용자별 주제 현황 이메일 전송")
    email_topics.add_argument("--to", default="", help="선택 수신 주소(기본 PAPER_TREND_EMAIL_TO)")
    email_papers = sub.add_parser("email-papers", help="익명 주제별 최신 APS·Nature 저널 논문 이메일 전송")
    email_papers.add_argument(
        "--to",
        default="",
        help="쉼표로 구분한 선택 수신 주소(각 주소에 개별 발송, 기본 PAPER_TREND_EMAIL_TO)",
    )
    sub.add_parser(
        "semantic-prefetch",
        help="모든 사용자 주제의 Semantic Scholar 결과를 야간 CSV 캐시에 순차 저장",
    )
    bot = sub.add_parser("bot", help="Telegram 주제 관리·즉시 검색 명령 수신")
    bot.add_argument("--once", action="store_true", help="대기 중 업데이트를 한 번만 확인")
    search = sub.add_parser("search", help="관련 논문과 lecture notes 즉시 검색")
    search.add_argument("query", nargs="+", help="한국어 또는 영어 연구 주제")
    return parser


def _print_top(settings: Settings, limit: int) -> list:
    with Repository(settings.database_path) as repository:
        papers = repository.top(limit=max(1, limit))
    if not papers:
        print("저장된 순위가 없습니다. 먼저 daily를 실행하세요.")
        return []
    for index, paper in enumerate(papers, 1):
        published = paper.published.isoformat() if paper.published else "날짜 미상"
        print(f"{index:>2}. {paper.trend_score:>5.1f}  {paper.title}")
        print(f"     {paper.venue or '-'} · {published} · {paper.landing_url or paper.pdf_url}")
    return papers


def _doctor(settings: Settings) -> int:
    failures = 0
    try:
        topics = load_topics(settings.topics_file)
        print(f"[OK] topics.json: 활성 주제 {len(topics)}개")
    except ValueError as exc:
        print(f"[FAIL] topics.json: {exc}")
        failures += 1
    try:
        journals = load_journals(settings.journals_file)
        aps_count = sum(journal.group == "aps" for journal in journals)
        nature_count = sum(journal.group == "nature" for journal in journals)
        print(f"[OK] journals.json: APS {aps_count}개 / Nature {nature_count}개")
    except ValueError as exc:
        print(f"[FAIL] journals.json: {exc}")
        failures += 1
    print(f"[OK] 데이터 경로: {settings.data_dir}")
    if settings.semantic_scholar_api_key:
        print("[OK] Semantic Scholar API 키 설정됨")
    else:
        print("[INFO] Semantic Scholar API 키 없음: 낮은 호출 한도로 계속 동작")
    if settings.telegram_configured:
        print("[OK] Telegram 설정됨")
    else:
        print("[INFO] Telegram 미설정: 일일 리포트만 로컬 저장")
    if settings.deepseek_api_key:
        print("[OK] DeepSeek 설정됨: 선택 논문 번역 가능")
    else:
        print("[INFO] DeepSeek 미설정: 수집은 가능, 선택 번역은 불가")
    if settings.email_configured:
        print(f"[OK] 이메일 설정됨: {settings.email_to}")
    else:
        print("[INFO] 이메일 미설정: SMTP 사용자·앱 비밀번호·수신 주소 확인 필요")
    if shutil.which("latexmk"):
        print("[OK] latexmk 발견: 번역 TeX의 PDF 컴파일 가능")
    else:
        print("[INFO] latexmk 없음: PAPER_TREND_COMPILE_TEX=false로 TeX만 생성 가능")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    try:
        settings = Settings.from_environment()
        settings.ensure_dirs()
        if args.command == "doctor":
            return _doctor(settings)
        if args.command in {"daily", "weekly"}:
            result = run_daily(
                settings,
                dry_run=args.dry_run,
                skip_telegram=args.skip_telegram,
                weekly=args.command == "weekly",
            )
            print("\n" + render_topic_sections(
                result.papers,
                load_topics(settings.topics_file),
                settings.report_top_n,
            ))
            print(f"\n수집: {sum(result.counts.values())}건 → 통합 {len(result.papers)}건")
            if result.report_path:
                print(f"리포트: {result.report_path}")
            if result.csv_path:
                print(f"누적 CSV: {result.csv_path}")
                print(f"DeepSeek 신규 한 줄 요약: {result.summaries_created}건")
            if result.telegram_messages:
                kind = "주간" if args.command == "weekly" else "일일"
                print(f"Telegram: {kind} 메시지 {result.telegram_messages}개 전송")
            elif not settings.telegram_configured:
                print("Telegram: 설정되지 않아 생략")
            if result.errors:
                print(f"경고: {len(result.errors)}건")
            return 0 if result.papers else 1
        if args.command == "top":
            return 0 if _print_top(settings, args.limit) else 1
        if args.command == "fetch":
            papers = _print_top(settings, max(args.rank, 1))
            if args.rank < 1 or args.rank > len(papers):
                print(f"유효한 순위를 입력하세요: 1~{len(papers)}", file=sys.stderr)
                return 2
            paper = papers[args.rank - 1]
            pdf_path = download_pdf(paper, settings, overwrite=args.force)
            paper.pdf_path = str(pdf_path)
            print(f"PDF 저장: {pdf_path}")
            if args.translate:
                translated = translate_pdf(pdf_path, paper.title, settings, force=args.force)
                paper.tex_path = str(translated.tex_path)
                paper.translated_pdf_path = str(translated.pdf_path or "")
                print(f"한국어 TeX: {translated.tex_path}")
                if translated.pdf_path:
                    print(f"한국어 PDF: {translated.pdf_path}")
            with Repository(settings.database_path) as repository:
                repository.update_artifacts(paper)
            return 0
        if args.command == "translate":
            pdf_path = args.pdf.expanduser().resolve()
            if not pdf_path.is_file():
                print(f"PDF 파일을 찾을 수 없습니다: {pdf_path}", file=sys.stderr)
                return 2
            result = translate_pdf(pdf_path, args.title or pdf_path.stem, settings, force=args.force)
            print(f"한국어 TeX: {result.tex_path}")
            if result.pdf_path:
                print(f"한국어 PDF: {result.pdf_path}")
            return 0
        if args.command == "telegram-test":
            sent = TelegramClient.from_settings(settings).send_text(args.message)
            print(f"Telegram 메시지 {sent}개 전송 완료")
            return 0
        if args.command == "email-test":
            sent = send_test_email(settings, args.to)
            print(f"이메일 {sent}개 전송 완료: {args.to or settings.email_to}")
            return 0
        if args.command == "email-topics":
            sent = send_user_topics_email(settings, args.to)
            print(f"사용자별 주제 이메일 {sent}개 전송 완료: {args.to or settings.email_to}")
            return 0
        if args.command == "email-papers":
            result = scan_and_send_user_papers_email(settings, args.to)
            print(
                f"주제별 저널 논문 이메일 {result.sent}개 전송 완료: "
                f"주제 {result.topics}개 · "
                f"목록 {result.paper_entries}건(고유 {result.unique_papers}건) · "
                f"신규 요약 {result.summaries_created}건"
            )
            if result.warnings:
                print(f"수집/요약 경고: {len(result.warnings)}건")
            return 0
        if args.command == "semantic-prefetch":
            _users, _topics_by_chat, topics, _display_names = (
                _subscriber_configuration(settings)
            )
            result = prefetch_semantic_scholar(topics, settings)
            print(
                f"Semantic Scholar 야간 캐시 완료: "
                f"주제 {result.topics_succeeded}/{result.topics_total}개 · "
                f"CSV 논문 {result.papers}건 · {result.csv_path}"
            )
            if result.errors:
                print(f"수집 경고: {len(result.errors)}건")
            return 0 if result.topics_succeeded else 1
        if args.command == "bot":
            run_bot(settings, once=args.once)
            return 0
        if args.command == "search":
            result = search_relevant(" ".join(args.query), settings)
            print(render_search_html(result))
            return 0 if result.papers or result.lecture_notes else 1
    except (ValueError, DownloadError, TranslationError, TelegramError, EmailError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
