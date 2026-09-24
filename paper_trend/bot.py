"""Invite-only Telegram command bot with per-subscriber topics."""

from __future__ import annotations

import hashlib
import logging
import time
from html import escape
from pathlib import Path

from .config import Settings, load_topics
from .search import render_search_html, search_relevant
from .subscribers import SubscriberError, SubscriberStore
from .telegram import TelegramClient, TelegramError


log = logging.getLogger("paper_trend.bot")

MEMBER_COMMANDS = [
    ("start", "PaperTrend 시작 및 가입"),
    ("topics", "내 추적 주제 목록"),
    ("topic_add", "내 주제 추가"),
    ("topic_remove", "내 주제 제거"),
    ("search", "관련 논문과 강의노트 검색"),
    ("lecture", "강의노트와 튜토리얼 검색"),
    ("papers_csv", "내가 받은 논문 CSV"),
    ("whoami", "현재 Chat ID 확인"),
    ("help", "전체 사용법"),
]
ADMIN_COMMANDS = MEMBER_COMMANDS + [
    ("invite", "일회용 사용자 초대 링크"),
    ("users", "활성 사용자 목록"),
    ("revoke", "사용자 이용 해제"),
]


def _help_html(is_admin: bool = False) -> str:
    text = """📚 <b>PaperTrend 사용법</b>

관심 주제의 최신 논문을 arXiv·Semantic Scholar와 PRX Quantum·Physical Review X·Nature Physics·npj Quantum Information에서 찾아 중복을 제거하고 보내드립니다.

<b>1️⃣ 자동 리포트</b>
• 월~금 오전 9시: 내 주제별 최신 논문 링크
• 토요일 오전 9시: 최근 7일 주간 리포트
• Telegram에는 논문 제목·저자·날짜·DOI·원문 링크만 전송
• PDF·번역본은 Telegram에 첨부하지 않음

<b>2️⃣ 내 주제 관리</b>
<code>/topics</code> — 내 주제 목록
<code>/topic_add 주제명 | 영문 검색어</code> — 주제 추가
예: <code>/topic_add 양자 오류 정정 | quantum error correction</code>
<code>/topic_remove 번호</code> — 목록 번호로 제거

<b>3️⃣ 지금 바로 검색</b>
<code>/search 검색어</code> — 관련성 높은 논문 + 강의자료
예: <code>/search 플럭소늄 큐비트</code>
<code>/lecture 검색어</code> — lecture notes·튜토리얼·리뷰만
예: <code>/lecture quantum computation</code>

<b>4️⃣ 기타</b>
<code>/papers_csv</code> — 내가 받은 누적 논문 CSV
<code>/whoami</code> — 현재 Chat ID 확인
<code>/help</code> — 이 사용법 다시 보기

사용자마다 주제가 따로 저장되며, 한국어 검색어도 사용할 수 있습니다."""
    if is_admin:
        text += """

<b>👑 관리자 기능</b>
<code>/invite</code> — 12시간 유효한 일회용 초대 링크
<code>/users</code> — 활성 사용자 목록
<code>/revoke 번호</code> — 사용자 이용 권한 해제"""
    return text


def _topics_html(store: SubscriberStore, chat_id: str) -> str:
    topics = store.topics(chat_id)
    lines = [f"📌 <b>내 주제 · {len(topics)}개</b>", ""]
    if not topics:
        lines.extend(
            [
                "아직 등록된 주제가 없습니다.",
                "",
                "<code>/topic_add 주제명 | 영문 검색어</code>",
            ]
        )
        return "\n".join(lines)
    for index, topic in enumerate(topics, 1):
        lines.append(f"<b>{index}. {escape(topic.name)}</b>")
        lines.append(f"<code>{escape(topic.query)}</code>")
        lines.append("")
    lines.extend(
        [
            "추가: <code>/topic_add 주제명 | 영문 검색어</code>",
            "제거: <code>/topic_remove 번호</code>",
        ]
    )
    return "\n".join(lines).strip()


def _users_html(store: SubscriberStore) -> str:
    users = store.active_subscribers()
    members = [user for user in users if user.role != "admin"]
    admins = [user for user in users if user.role == "admin"]
    lines = [f"👥 <b>활성 사용자 · {len(users)}명</b>", ""]
    for user in admins:
        identity = f"@{user.username}" if user.username else user.display_name or user.chat_id
        lines.append(f"👑 {escape(identity)}")
    for index, user in enumerate(members, 1):
        identity = f"@{user.username}" if user.username else user.display_name or user.chat_id
        lines.append(f"<b>{index}.</b> {escape(identity)} · 주제 {len(store.topics(user.chat_id))}개")
    if not members:
        lines.append("초대된 일반 사용자가 없습니다.")
    lines.extend(["", "해제: <code>/revoke 번호</code>"])
    return "\n".join(lines)


def _command_and_argument(text: str) -> tuple[str, str]:
    first, _, rest = text.strip().partition(" ")
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def _personal_csv_path(settings: Settings, chat_id: str) -> Path:
    private_key = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]
    return settings.data_dir / "exports" / private_key / "개별_논문.csv"


def handle_message(
    text: str,
    settings: Settings,
    client: TelegramClient,
    store: SubscriberStore,
    chat_id: str,
    telegram_user_id: str = "",
    username: str = "",
    display_name: str = "",
    chat_type: str = "private",
) -> None:
    command, argument = _command_and_argument(text)
    subscriber = store.get_subscriber(chat_id)

    if command == "/whoami":
        client.send_html_to(chat_id, f"현재 Chat ID: <code>{escape(chat_id)}</code>")
        return

    if command == "/start" and (subscriber is None or subscriber.status != "active"):
        if chat_type != "private":
            client.send_html_to(chat_id, "초대 가입은 봇과의 개인 대화에서 진행해주세요.")
            return
        if not argument:
            client.send_html_to(
                chat_id,
                "🔒 초대 전용 봇입니다. 관리자에게 받은 링크를 누르거나 "
                "<code>/start 초대코드</code>를 입력해주세요.",
            )
            return
        subscriber = store.redeem_invite(
            argument,
            chat_id,
            telegram_user_id,
            username,
            display_name,
            chat_type,
        )
        client.send_html_to(
            chat_id,
            "✅ <b>PaperTrend 가입이 완료됐습니다.</b>\n\n"
            "먼저 관심 주제를 추가해주세요.\n"
            "<code>/topic_add 주제명 | 영문 검색어</code>\n\n"
            + _help_html(False),
        )
        return

    if subscriber is None or subscriber.status != "active":
        client.send_html_to(
            chat_id,
            "🔒 등록된 사용자가 아닙니다. 관리자에게 일회용 초대 링크를 요청해주세요.",
        )
        return

    store.update_identity(chat_id, telegram_user_id, username, display_name, chat_type)
    subscriber = store.get_subscriber(chat_id) or subscriber

    if command in {"/start", "/help"}:
        client.send_html_to(chat_id, _help_html(subscriber.role == "admin"))
        return
    if command == "/topics":
        client.send_html_to(chat_id, _topics_html(store, chat_id))
        return
    if command == "/papers_csv":
        path = _personal_csv_path(settings, chat_id)
        count = store.export_papers_csv(chat_id, path)
        if count == 0:
            client.send_html_to(
                chat_id,
                "아직 전달 이력에 저장된 논문이 없습니다. 다음 자동 스캔부터 누적됩니다.",
            )
            return
        client.send_document_to(
            chat_id,
            path,
            filename="개별_논문.csv",
            caption=f"📄 내가 받은 누적 논문 · {count:,}건",
        )
        return
    if command == "/topic_add":
        if "|" not in argument:
            client.send_html_to(
                chat_id,
                "사용법: <code>/topic_add 주제명 | 영문 검색어</code>\n"
                "예: <code>/topic_add 양자 오류 정정 | quantum error correction</code>",
            )
            return
        name, query = (part.strip() for part in argument.split("|", 1))
        topic = store.add_topic(chat_id, name, query)
        client.send_html_to(
            chat_id,
            f"✅ 내 주제를 추가했습니다.\n\n<b>{escape(topic.name)}</b>\n"
            f"<code>{escape(topic.query)}</code>\n\n{_topics_html(store, chat_id)}",
        )
        return
    if command == "/topic_remove":
        topic = store.remove_topic(chat_id, argument)
        client.send_html_to(
            chat_id,
            f"🗑 내 주제를 제거했습니다: <b>{escape(topic.name)}</b>\n\n"
            f"{_topics_html(store, chat_id)}",
        )
        return
    if command == "/invite":
        if chat_id != settings.telegram_chat_id:
            raise SubscriberError("봇 소유자만 초대 링크를 만들 수 있습니다.")
        code, expires = store.create_invite(chat_id)
        bot_username = escape(settings.telegram_bot_username)
        invite_url = f"https://t.me/{bot_username}?start={code}"
        client.send_html_to(
            chat_id,
            "🎟 <b>일회용 초대 링크</b>\n\n"
            f'<a href="{invite_url}">{invite_url}</a>\n\n'
            f"코드: <code>{code}</code>\n"
            f"만료: {expires.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
            "한 사람이 가입하거나 12시간이 지나면 즉시 만료됩니다.",
        )
        return
    if command == "/users":
        if subscriber.role != "admin":
            raise SubscriberError("관리자만 사용자 목록을 볼 수 있습니다.")
        client.send_html_to(chat_id, _users_html(store))
        return
    if command == "/revoke":
        target = store.revoke(chat_id, argument)
        identity = f"@{target.username}" if target.username else target.display_name or target.chat_id
        client.send_html_to(chat_id, f"⛔ 사용자를 해제했습니다: <b>{escape(identity)}</b>")
        try:
            client.send_html_to(target.chat_id, "PaperTrend 이용 권한이 관리자에 의해 해제됐습니다.")
        except TelegramError:
            pass
        return
    if command in {"/search", "/lecture"}:
        if not argument:
            client.send_html_to(chat_id, f"사용법: <code>{command} 검색어</code>")
            return
        client.send_html_to(chat_id, f"🔎 <code>{escape(argument)}</code> 검색 중입니다…")
        result = search_relevant(argument, settings)
        rendered = render_search_html(result)
        if command == "/lecture":
            marker = "━━━━━━━━━━━━━━\n📘"
            lecture_part = rendered.split(marker, 1)
            rendered = (
                f"📘 <b>Lecture notes 검색</b>\n"
                f"검색어: <code>{escape(result.original_query)}</code>\n\n"
                + ("📘" + lecture_part[1] if len(lecture_part) == 2 else "결과가 없습니다.")
            )
        client.send_html_to(chat_id, rendered)
        return
    if command.startswith("/"):
        client.send_html_to(chat_id, "알 수 없는 명령입니다. /help 를 입력해주세요.")


def _read_offset(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(str(offset) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_bot(settings: Settings, once: bool = False) -> None:
    if not settings.telegram_configured:
        raise TelegramError("TELEGRAM_BOT_TOKEN과 관리자 TELEGRAM_CHAT_ID가 필요합니다.")
    client = TelegramClient.from_settings(settings)
    offset_path = settings.data_dir / "telegram_update_offset"
    offset = _read_offset(offset_path)

    with SubscriberStore(settings.database_path) as store:
        store.bootstrap_owner(settings.telegram_chat_id, load_topics(settings.topics_file))
        try:
            client.set_commands(MEMBER_COMMANDS)
            client.set_commands(ADMIN_COMMANDS, settings.telegram_chat_id)
        except TelegramError as exc:
            log.warning("명령 힌트 등록 실패: %s", exc)
        if offset is None:
            backlog = client.get_updates(timeout=0)
            if backlog:
                offset = max(int(item["update_id"]) for item in backlog) + 1
                _write_offset(offset_path, offset)
            log.info("Telegram 명령 수신 준비 완료")

        while True:
            try:
                updates = client.get_updates(offset=offset, timeout=0 if once else 30)
                for update in updates:
                    update_id = int(update["update_id"])
                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    sender = message.get("from") or {}
                    chat_id = str(chat.get("id") or "")
                    text = str(message.get("text") or "").strip()
                    offset = update_id + 1
                    _write_offset(offset_path, offset)
                    if not chat_id or not text:
                        continue
                    display_name = " ".join(
                        part for part in (
                            str(sender.get("first_name") or ""),
                            str(sender.get("last_name") or ""),
                        ) if part
                    ) or str(chat.get("title") or "")
                    try:
                        handle_message(
                            text,
                            settings,
                            client,
                            store,
                            chat_id,
                            str(sender.get("id") or ""),
                            str(sender.get("username") or ""),
                            display_name,
                            str(chat.get("type") or "private"),
                        )
                    except (SubscriberError, ValueError) as exc:
                        client.send_html_to(chat_id, f"⚠️ {escape(str(exc))}")
                    except Exception as exc:
                        log.exception("명령 처리 실패")
                        client.send_html_to(
                            chat_id,
                            f"⚠️ 명령 처리 중 오류가 발생했습니다: {escape(type(exc).__name__)}",
                        )
                if once:
                    return
            except TelegramError as exc:
                if once:
                    raise
                log.warning("%s — 5초 후 재시도", exc)
                time.sleep(5)
