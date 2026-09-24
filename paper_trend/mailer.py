"""SMTP email delivery for PaperTrend reports."""
from __future__ import annotations

import smtplib
import ssl
from collections import Counter
from datetime import date
from email.message import EmailMessage
from html import escape

from .config import Settings
from .subscribers import SubscriberStore


class EmailError(RuntimeError):
    pass


def send_email(
    settings: Settings,
    subject: str,
    text: str,
    html: str = "",
    to: str = "",
) -> int:
    """Send one UTF-8 email through the configured SSL SMTP account."""
    recipient = (to or settings.email_to).strip()
    if not settings.smtp_user or not settings.smtp_password or not recipient:
        raise EmailError("SMTP 사용자·앱 비밀번호·수신 이메일 설정이 필요합니다.")
    message = EmailMessage()
    message["From"] = settings.smtp_user
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.request_timeout,
            context=context,
        ) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailError(f"이메일 전송 실패: {type(exc).__name__}") from exc
    return 1


def send_test_email(settings: Settings, to: str = "") -> int:
    recipient = (to or settings.email_to).strip()
    return send_email(
        settings,
        "[PaperTrend] 이메일 연결 테스트",
        "PaperTrend 이메일 연결이 정상적으로 완료됐습니다.\n\n앞으로 사용자별 논문 리포트를 이메일로 확장할 수 있습니다.",
        """
        <div style="font-family:system-ui,-apple-system,sans-serif;max-width:640px;margin:auto;padding:28px">
          <h1 style="font-size:24px">📚 PaperTrend</h1>
          <h2 style="font-size:19px">이메일 연결 테스트 완료</h2>
          <p>PaperTrend에서 보낸 테스트 메일입니다.</p>
          <p>앞으로 사용자별 논문 리포트를 이메일로 확장할 수 있습니다.</p>
          <hr style="border:0;border-top:1px solid #e5e7eb;margin:24px 0">
          <small style="color:#667085">수신 주소: """ + escape(recipient) + """</small>
        </div>
        """,
        recipient,
    )


def render_user_topics_email(settings: Settings) -> tuple[str, str, str]:
    """Render an owner-only overview without exposing Telegram chat IDs."""
    with SubscriberStore(settings.database_path) as store:
        users = store.active_subscribers()
        topics_by_user = {user.chat_id: store.topics(user.chat_id) for user in users}
    total_topics = sum(len(topics) for topics in topics_by_user.values())
    query_counts = Counter(
        " ".join(topic.query.casefold().split())
        for topics in topics_by_user.values()
        for topic in topics
    )
    shared_queries = sum(count > 1 for count in query_counts.values())
    subject = f"[PaperTrend] 사용자별 주제 현황 — {date.today().isoformat()}"

    text_lines = [
        "PaperTrend 사용자별 주제 현황",
        f"활성 사용자 {len(users)}명 · 등록 주제 {total_topics}개 · 고유 검색어 {len(query_counts)}개",
        "",
    ]
    cards: list[str] = []
    for user in users:
        identity = f"@{user.username}" if user.username else user.display_name or "이름 미등록 사용자"
        role = "관리자" if user.role == "admin" else "사용자"
        topics = topics_by_user[user.chat_id]
        text_lines.extend([f"[{role}] {identity} — {len(topics)}개"])
        topic_rows: list[str] = []
        for topic in topics:
            text_lines.append(f"- {topic.name}: {topic.query}")
            topic_rows.append(
                '<li style="padding:10px 0;border-bottom:1px solid #edf0f5">'
                f'<b style="display:block;color:#172033">{escape(topic.name)}</b>'
                f'<code style="color:#5b5bd6">{escape(topic.query)}</code></li>'
            )
        if not topics:
            text_lines.append("- 등록된 주제 없음")
            topic_rows.append('<li style="color:#667085">등록된 주제가 없습니다.</li>')
        text_lines.append("")
        cards.append(
            '<section style="background:#fff;border:1px solid #e5eaf1;border-radius:16px;padding:20px">'
            f'<div style="color:#667085;font-size:12px">{role}</div>'
            f'<h2 style="margin:2px 0 10px;font-size:19px">{escape(identity)}</h2>'
            f'<div style="color:#667085;margin-bottom:8px">주제 {len(topics)}개</div>'
            f'<ul style="list-style:none;margin:0;padding:0">{"".join(topic_rows)}</ul></section>'
        )

    html = f"""
    <div style="background:#f4f7fb;padding:28px;font-family:system-ui,-apple-system,'Noto Sans KR',sans-serif;color:#172033">
      <div style="max-width:760px;margin:auto">
        <header style="background:linear-gradient(135deg,#292b5f,#5b5bd6);color:#fff;padding:28px;border-radius:20px">
          <div style="font-size:12px;opacity:.75;letter-spacing:.12em">PAPERTREND · ADMIN</div>
          <h1 style="margin:6px 0;font-size:28px">사용자별 주제 현황</h1>
          <div>{date.today().isoformat()}</div>
        </header>
        <div style="display:flex;gap:10px;margin:14px 0;flex-wrap:wrap">
          <div style="background:#fff;padding:14px 18px;border-radius:14px"><b>{len(users)}</b> 활성 사용자</div>
          <div style="background:#fff;padding:14px 18px;border-radius:14px"><b>{total_topics}</b> 등록 주제</div>
          <div style="background:#fff;padding:14px 18px;border-radius:14px"><b>{len(query_counts)}</b> 고유 검색어</div>
          <div style="background:#fff;padding:14px 18px;border-radius:14px"><b>{shared_queries}</b> 중복 검색어</div>
        </div>
        <div style="display:grid;gap:12px">{"".join(cards)}</div>
        <p style="color:#667085;font-size:12px;text-align:center;margin-top:22px">Chat ID와 초대 코드는 포함하지 않았습니다.</p>
      </div>
    </div>
    """
    return subject, "\n".join(text_lines), html


def send_user_topics_email(settings: Settings, to: str = "") -> int:
    subject, text, html = render_user_topics_email(settings)
    return send_email(settings, subject, text, html, to)
