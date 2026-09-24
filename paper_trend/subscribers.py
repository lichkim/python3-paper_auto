"""Invite-only Telegram subscribers and per-chat topic storage."""

from __future__ import annotations

import csv
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_SOURCES, Topic


SUBSCRIBER_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id TEXT PRIMARY KEY,
    telegram_user_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL DEFAULT 'private',
    role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    joined_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriber_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES subscribers(chat_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    journal_match_ratio REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, name COLLATE NOCASE),
    UNIQUE(chat_id, query COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS invite_codes (
    code TEXT PRIMARY KEY,
    created_by TEXT NOT NULL REFERENCES subscribers(chat_id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS subscriber_papers (
    chat_id TEXT NOT NULL REFERENCES subscribers(chat_id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    topics_json TEXT NOT NULL DEFAULT '[]',
    first_delivered_at TEXT NOT NULL,
    last_delivered_at TEXT NOT NULL,
    delivery_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(chat_id, paper_id)
);
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status);
CREATE INDEX IF NOT EXISTS idx_subscriber_topics_chat ON subscriber_topics(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_subscriber_papers_chat_date
    ON subscriber_papers(chat_id, last_delivered_at DESC);
"""


class SubscriberError(ValueError):
    pass


@dataclass(frozen=True)
class Subscriber:
    chat_id: str
    telegram_user_id: str
    username: str
    display_name: str
    chat_type: str
    role: str
    status: str


@dataclass(frozen=True)
class SubscriberTopic:
    id: int
    chat_id: str
    name: str
    query: str
    sources: tuple[str, ...]
    journal_match_ratio: float

    def as_topic(self, internal_name: str | None = None) -> Topic:
        return Topic(
            name=internal_name or self.name,
            query=self.query,
            sources=self.sources,
            journal_match_ratio=self.journal_match_ratio,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SubscriberStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SUBSCRIBER_SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SubscriberStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def bootstrap_owner(self, chat_id: str, legacy_topics: list[Topic]) -> None:
        if not chat_id:
            raise SubscriberError("관리자 TELEGRAM_CHAT_ID가 필요합니다.")
        now_datetime = _now()
        now = now_datetime.isoformat()
        migration_key = f"legacy_topics_migrated:{chat_id}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO subscribers(chat_id,display_name,role,status,joined_at,last_seen)
                VALUES(?, 'Owner', 'admin', 'active', ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET role='admin', status='active'
                """,
                (chat_id, now, now),
            )
            migrated = self.connection.execute(
                "SELECT 1 FROM app_meta WHERE key=?", (migration_key,)
            ).fetchone()
            if migrated is None:
                count = self.connection.execute(
                    "SELECT COUNT(*) AS count FROM subscriber_topics WHERE chat_id=?",
                    (chat_id,),
                ).fetchone()["count"]
                if int(count) == 0:
                    for topic in legacy_topics:
                        self._insert_topic(
                            chat_id, topic.name, topic.query,
                            topic.sources, topic.journal_match_ratio,
                        )
                self.connection.execute(
                    "INSERT INTO app_meta(key,value) VALUES(?,?)",
                    (migration_key, now),
                )
            # Clamp any still-unused codes created under an older, longer policy.
            for row in self.connection.execute(
                "SELECT code,created_at,expires_at FROM invite_codes WHERE active=1"
            ).fetchall():
                maximum = datetime.fromisoformat(row["created_at"]) + timedelta(hours=12)
                current = datetime.fromisoformat(row["expires_at"])
                if current <= now_datetime:
                    self.connection.execute(
                        "UPDATE invite_codes SET active=0 WHERE code=?", (row["code"],)
                    )
                elif current > maximum:
                    self.connection.execute(
                        "UPDATE invite_codes SET expires_at=? WHERE code=?",
                        (maximum.isoformat(), row["code"]),
                    )

    def _subscriber(self, row: sqlite3.Row | None) -> Subscriber | None:
        if row is None:
            return None
        return Subscriber(
            chat_id=str(row["chat_id"]),
            telegram_user_id=str(row["telegram_user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            chat_type=str(row["chat_type"]),
            role=str(row["role"]),
            status=str(row["status"]),
        )

    def get_subscriber(self, chat_id: str) -> Subscriber | None:
        row = self.connection.execute(
            "SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return self._subscriber(row)

    def active_subscribers(self) -> list[Subscriber]:
        rows = self.connection.execute(
            """
            SELECT * FROM subscribers WHERE status='active'
            ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, joined_at
            """
        ).fetchall()
        return [self._subscriber(row) for row in rows if row is not None]

    def update_identity(
        self,
        chat_id: str,
        telegram_user_id: str,
        username: str,
        display_name: str,
        chat_type: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE subscribers SET telegram_user_id=?, username=?, display_name=?,
                    chat_type=?, last_seen=? WHERE chat_id=?
                """,
                (
                    telegram_user_id,
                    username,
                    display_name,
                    chat_type,
                    _now().isoformat(),
                    chat_id,
                ),
            )

    def topics(self, chat_id: str) -> list[SubscriberTopic]:
        rows = self.connection.execute(
            "SELECT * FROM subscriber_topics WHERE chat_id=? ORDER BY id", (chat_id,)
        ).fetchall()
        return [
            SubscriberTopic(
                id=int(row["id"]),
                chat_id=str(row["chat_id"]),
                name=str(row["name"]),
                query=str(row["query"]),
                sources=tuple(json.loads(row["sources_json"])),
                journal_match_ratio=float(row["journal_match_ratio"]),
            )
            for row in rows
        ]

    def record_papers(
        self,
        chat_id: str,
        papers: list[tuple[int, set[str]]],
        delivered_at: datetime | None = None,
    ) -> int:
        """Record papers successfully delivered to one subscriber, deduplicated by ID."""
        delivered = (delivered_at or _now()).isoformat()
        recorded = 0
        with self.connection:
            for paper_id, topics in papers:
                row = self.connection.execute(
                    """
                    SELECT topics_json FROM subscriber_papers
                    WHERE chat_id=? AND paper_id=?
                    """,
                    (chat_id, paper_id),
                ).fetchone()
                if row is None:
                    self.connection.execute(
                        """
                        INSERT INTO subscriber_papers(
                            chat_id,paper_id,topics_json,first_delivered_at,
                            last_delivered_at,delivery_count
                        ) VALUES(?,?,?,?,?,1)
                        """,
                        (
                            chat_id,
                            paper_id,
                            json.dumps(sorted(topics), ensure_ascii=False),
                            delivered,
                            delivered,
                        ),
                    )
                else:
                    combined = set(json.loads(row["topics_json"])) | set(topics)
                    self.connection.execute(
                        """
                        UPDATE subscriber_papers
                        SET topics_json=?, last_delivered_at=?, delivery_count=delivery_count+1
                        WHERE chat_id=? AND paper_id=?
                        """,
                        (
                            json.dumps(sorted(combined), ensure_ascii=False),
                            delivered,
                            chat_id,
                            paper_id,
                        ),
                    )
                recorded += 1
        return recorded

    def export_papers_csv(self, chat_id: str, path: Path) -> int:
        """Export only the requesting subscriber's cumulative delivery history."""
        paper_table = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='papers'"
        ).fetchone()
        if paper_table is None:
            return 0
        rows = self.connection.execute(
            """
            SELECT sp.*, p.title, p.summary_ko, p.authors_json, p.published,
                   p.venue, p.doi, p.arxiv_id, p.landing_url, p.citation_count,
                   p.sources_json,
                   COALESCE((
                       SELECT m.trend_score FROM metrics m
                       WHERE m.paper_id=p.id ORDER BY m.observed_on DESC LIMIT 1
                   ), 0) AS latest_trend_score
            FROM subscriber_papers sp
            JOIN papers p ON p.id=sp.paper_id
            WHERE sp.chat_id=?
            ORDER BY sp.last_delivered_at DESC, p.title
            """,
            (chat_id,),
        ).fetchall()
        fieldnames = [
            "처음_받은_날짜", "최근_받은_날짜", "받은_횟수", "내_주제",
            "제목", "저자", "발표일", "저널", "DOI", "arXiv", "원문_링크",
            "한줄_요약", "트렌드_점수", "인용수", "출처",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "처음_받은_날짜": row["first_delivered_at"],
                        "최근_받은_날짜": row["last_delivered_at"],
                        "받은_횟수": row["delivery_count"],
                        "내_주제": "; ".join(json.loads(row["topics_json"])),
                        "제목": row["title"],
                        "저자": "; ".join(json.loads(row["authors_json"])),
                        "발표일": row["published"] or "",
                        "저널": row["venue"],
                        "DOI": row["doi"],
                        "arXiv": row["arxiv_id"],
                        "원문_링크": row["landing_url"],
                        "한줄_요약": row["summary_ko"],
                        "트렌드_점수": row["latest_trend_score"],
                        "인용수": row["citation_count"],
                        "출처": "; ".join(json.loads(row["sources_json"])),
                    }
                )
        temporary.replace(path)
        return len(rows)

    def _insert_topic(
        self,
        chat_id: str,
        name: str,
        query: str,
        sources: tuple[str, ...] = DEFAULT_SOURCES,
        journal_match_ratio: float = 0.5,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO subscriber_topics(
                chat_id,name,query,sources_json,journal_match_ratio,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                chat_id,
                name,
                query,
                json.dumps(list(sources)),
                journal_match_ratio,
                _now().isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def add_topic(self, chat_id: str, name: str, query: str) -> SubscriberTopic:
        name = " ".join(name.split())
        query = " ".join(query.split())
        if not name or not query:
            raise SubscriberError("주제명과 검색어를 모두 입력해주세요.")
        if len(name) > 60 or len(query) > 200:
            raise SubscriberError("주제명 또는 검색어가 너무 깁니다.")
        subscriber = self.get_subscriber(chat_id)
        if subscriber is None or subscriber.status != "active":
            raise SubscriberError("등록된 사용자가 아닙니다.")
        try:
            with self.connection:
                topic_id = self._insert_topic(chat_id, name, query)
        except sqlite3.IntegrityError as exc:
            raise SubscriberError("같은 이름 또는 검색어의 주제가 이미 등록되어 있습니다.") from exc
        return next(topic for topic in self.topics(chat_id) if topic.id == topic_id)

    def remove_topic(self, chat_id: str, selector: str) -> SubscriberTopic:
        rows = self.topics(chat_id)
        selector = selector.strip()
        target: SubscriberTopic | None = None
        if selector.isdigit() and 0 < int(selector) <= len(rows):
            target = rows[int(selector) - 1]
        else:
            matches = [row for row in rows if row.name.casefold() == selector.casefold()]
            if len(matches) == 1:
                target = matches[0]
        if target is None:
            raise SubscriberError(f"일치하는 주제가 없습니다: {selector}")
        with self.connection:
            self.connection.execute(
                "DELETE FROM subscriber_topics WHERE id=? AND chat_id=?",
                (target.id, chat_id),
            )
        return target

    def create_invite(self, created_by: str, valid_hours: int = 12) -> tuple[str, datetime]:
        subscriber = self.get_subscriber(created_by)
        if subscriber is None or subscriber.role != "admin" or subscriber.status != "active":
            raise SubscriberError("관리자만 초대 코드를 만들 수 있습니다.")
        created = _now()
        expires = created + timedelta(hours=valid_hours)
        with self.connection:
            for _ in range(10):
                code = secrets.token_hex(4).upper()
                try:
                    self.connection.execute(
                        """
                        INSERT INTO invite_codes(
                            code,created_by,created_at,expires_at,max_uses,use_count,active
                        ) VALUES(?,?,?,?,1,0,1)
                        """,
                        (code, created_by, created.isoformat(), expires.isoformat()),
                    )
                    return code, expires
                except sqlite3.IntegrityError:
                    continue
        raise SubscriberError("초대 코드 생성에 실패했습니다.")

    def redeem_invite(
        self,
        code: str,
        chat_id: str,
        telegram_user_id: str,
        username: str,
        display_name: str,
        chat_type: str,
    ) -> Subscriber:
        code = code.strip().upper()
        if not code:
            raise SubscriberError("초대 코드가 필요합니다.")
        now = _now()
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM invite_codes WHERE code=?", (code,)
            ).fetchone()
            if row is None:
                raise SubscriberError("유효하지 않은 초대 코드입니다.")
            if int(row["use_count"]) >= int(row["max_uses"]):
                raise SubscriberError("이미 사용된 초대 코드입니다.")
            if not int(row["active"]):
                raise SubscriberError("유효하지 않은 초대 코드입니다.")
            if datetime.fromisoformat(row["expires_at"]) < now:
                raise SubscriberError("만료된 초대 코드입니다.")
            self.connection.execute(
                """
                INSERT INTO subscribers(
                    chat_id,telegram_user_id,username,display_name,chat_type,
                    role,status,joined_at,last_seen
                ) VALUES(?,?,?,?,?,'member','active',?,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    telegram_user_id=excluded.telegram_user_id,
                    username=excluded.username,
                    display_name=excluded.display_name,
                    chat_type=excluded.chat_type,
                    status='active', last_seen=excluded.last_seen
                """,
                (
                    chat_id,
                    telegram_user_id,
                    username,
                    display_name,
                    chat_type,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self.connection.execute(
                "UPDATE invite_codes SET use_count=use_count+1, active=0 WHERE code=?",
                (code,),
            )
        subscriber = self.get_subscriber(chat_id)
        if subscriber is None:
            raise SubscriberError("사용자 등록을 확인할 수 없습니다.")
        return subscriber

    def revoke(self, admin_chat_id: str, selector: str) -> Subscriber:
        admin = self.get_subscriber(admin_chat_id)
        if admin is None or admin.role != "admin":
            raise SubscriberError("관리자만 사용자를 해제할 수 있습니다.")
        members = [user for user in self.active_subscribers() if user.role != "admin"]
        selector = selector.strip().lstrip("@")
        target: Subscriber | None = None
        if selector.isdigit() and len(selector) <= 3 and 0 < int(selector) <= len(members):
            target = members[int(selector) - 1]
        else:
            matches = [
                user for user in members
                if user.chat_id == selector or user.username.casefold() == selector.casefold()
            ]
            if len(matches) == 1:
                target = matches[0]
        if target is None:
            raise SubscriberError(f"일치하는 활성 사용자가 없습니다: {selector}")
        with self.connection:
            self.connection.execute(
                "UPDATE subscribers SET status='revoked' WHERE chat_id=?", (target.chat_id,)
            )
        return target
