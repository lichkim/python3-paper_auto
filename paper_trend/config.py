"""Environment and topic configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_SOURCES = frozenset({"arxiv", "semantic_scholar", "aps", "nature", "crossref"})
DEFAULT_SOURCES = ("arxiv", "semantic_scholar", "aps", "nature")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _integer(name: str, default: int, minimum: int = 0) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} 값은 {minimum} 이상이어야 합니다.")
    return value


def _number(name: str, default: float, minimum: float = 0.0) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} 값은 {minimum} 이상이어야 합니다.")
    return value


def _secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    secret_file = os.getenv(f"{name}_FILE", "").strip()
    if not secret_file:
        return ""
    return Path(secret_file).expanduser().read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Topic:
    name: str
    query: str
    sources: tuple[str, ...]
    journal_match_ratio: float = 1.0


@dataclass(frozen=True)
class Journal:
    name: str
    issn: str
    group: str


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    topics_file: Path
    journals_file: Path
    database_path: Path
    csv_path: Path
    reports_dir: Path
    downloads_dir: Path
    translations_dir: Path
    scan_days: int
    max_results_per_source: int
    report_top_n: int
    compile_tex: bool
    request_timeout: int
    user_agent: str
    semantic_scholar_api_key: str
    semantic_cache_csv: Path
    semantic_prefetch_delay: float
    semantic_prefetch_retry_delay: float
    semantic_prefetch_attempts: int
    deepseek_api_key: str
    deepseek_base_url: str
    translation_model: str
    summary_model: str
    summary_max_per_run: int
    arxiv_request_interval: float
    semantic_scholar_request_interval: float
    rate_limit_retry_delay: float
    telegram_bot_token: str
    telegram_bot_username: str
    telegram_chat_id: str
    telegram_message_thread_id: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_to: str

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        data_dir = Path(os.getenv("PAPER_TREND_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser().resolve()
        topics_file = Path(
            os.getenv("PAPER_TREND_TOPICS_FILE", str(PROJECT_ROOT / "topics.json"))
        ).expanduser().resolve()
        journals_file = Path(
            os.getenv("PAPER_TREND_JOURNALS_FILE", str(PROJECT_ROOT / "journals.json"))
        ).expanduser().resolve()
        return cls(
            project_root=PROJECT_ROOT,
            data_dir=data_dir,
            topics_file=topics_file,
            journals_file=journals_file,
            database_path=data_dir / "paper_trend.db",
            csv_path=Path(
                os.getenv("PAPER_TREND_CSV_PATH", str(data_dir / "papers.csv"))
            ).expanduser().resolve(),
            reports_dir=Path(os.getenv("PAPER_TREND_REPORTS_DIR", str(PROJECT_ROOT / "reports"))).expanduser().resolve(),
            downloads_dir=Path(os.getenv("PAPER_TREND_DOWNLOADS_DIR", str(PROJECT_ROOT / "downloads"))).expanduser().resolve(),
            translations_dir=Path(os.getenv("PAPER_TREND_TRANSLATIONS_DIR", str(PROJECT_ROOT / "translations"))).expanduser().resolve(),
            scan_days=_integer("PAPER_TREND_SCAN_DAYS", 14, 1),
            max_results_per_source=_integer("PAPER_TREND_MAX_RESULTS_PER_SOURCE", 20, 1),
            report_top_n=_integer("PAPER_TREND_REPORT_TOP_N", 10, 1),
            compile_tex=_bool("PAPER_TREND_COMPILE_TEX", True),
            request_timeout=_integer("PAPER_TREND_REQUEST_TIMEOUT", 45, 1),
            user_agent=os.getenv(
                "PAPER_TREND_USER_AGENT",
                "PaperTrend/0.1 (mailto:replace-me@example.com)",
            ),
            semantic_scholar_api_key=_secret("SEMANTIC_SCHOLAR_API_KEY"),
            semantic_cache_csv=Path(
                os.getenv(
                    "PAPER_TREND_SEMANTIC_CACHE_CSV",
                    str(data_dir / "semantic_scholar_prefetch.csv"),
                )
            ).expanduser().resolve(),
            semantic_prefetch_delay=_number(
                "PAPER_TREND_SEMANTIC_PREFETCH_DELAY", 15.0, 1.0
            ),
            semantic_prefetch_retry_delay=_number(
                "PAPER_TREND_SEMANTIC_PREFETCH_RETRY_DELAY", 120.0, 1.0
            ),
            semantic_prefetch_attempts=_integer(
                "PAPER_TREND_SEMANTIC_PREFETCH_ATTEMPTS", 3, 1
            ),
            deepseek_api_key=_secret("DEEPSEEK_API_KEY"),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            translation_model=os.getenv("PAPER_TREND_TRANSLATION_MODEL", "deepseek-v4-flash"),
            summary_model=os.getenv("PAPER_TREND_SUMMARY_MODEL", "deepseek-chat"),
            summary_max_per_run=_integer("PAPER_TREND_SUMMARY_MAX_PER_RUN", 20, 1),
            arxiv_request_interval=_number(
                "PAPER_TREND_ARXIV_DELAY", 3.0, 3.0
            ),
            semantic_scholar_request_interval=_number("SS_DELAY", 1.0, 1.0),
            rate_limit_retry_delay=_number(
                "PAPER_TREND_RATE_LIMIT_RETRY_DELAY", 15.0, 0.0
            ),
            telegram_bot_token=_secret("TELEGRAM_BOT_TOKEN"),
            telegram_bot_username=os.getenv("TELEGRAM_BOT_USERNAME", "PaperTrendBot").strip().lstrip("@"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_message_thread_id=os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "").strip(),
            smtp_host=os.getenv("PAPER_TREND_SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_port=_integer("PAPER_TREND_SMTP_PORT", 465, 1),
            smtp_user=os.getenv("PAPER_TREND_SMTP_USER", "").strip(),
            smtp_password=_secret("PAPER_TREND_SMTP_PASSWORD"),
            email_to=os.getenv("PAPER_TREND_EMAIL_TO", "").strip(),
        )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def email_configured(self) -> bool:
        return bool(
            self.smtp_host and self.smtp_port and self.smtp_user
            and self.smtp_password and self.email_to
        )

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.reports_dir, self.downloads_dir, self.translations_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_topics(path: Path) -> list[Topic]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"주제 설정 파일이 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"주제 설정 JSON이 올바르지 않습니다: {path} ({exc})") from exc

    raw_topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(raw_topics, list):
        raise ValueError("topics.json의 'topics'는 배열이어야 합니다.")

    topics: list[Topic] = []
    for index, raw in enumerate(raw_topics, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"topics[{index}]는 객체여야 합니다.")
        if raw.get("enabled", True) is False:
            continue
        name = str(raw.get("name", "")).strip()
        query = str(raw.get("query", "")).strip()
        sources = tuple(raw.get("sources", DEFAULT_SOURCES))
        journal_match_ratio = float(raw.get("journal_match_ratio", 1.0))
        if not name or not query:
            raise ValueError(f"topics[{index}]의 name과 query는 필수입니다.")
        unknown = set(sources) - SUPPORTED_SOURCES
        if unknown:
            raise ValueError(f"topics[{index}]의 알 수 없는 source: {', '.join(sorted(unknown))}")
        if not 0 < journal_match_ratio <= 1:
            raise ValueError(f"topics[{index}].journal_match_ratio는 0 초과 1 이하여야 합니다.")
        topics.append(
            Topic(
                name=name,
                query=query,
                sources=sources,
                journal_match_ratio=journal_match_ratio,
            )
        )
    if not topics:
        raise ValueError("활성화된 주제가 하나도 없습니다.")
    return topics


def load_journals(path: Path) -> list[Journal]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"저널 설정 파일이 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"저널 설정 JSON이 올바르지 않습니다: {path} ({exc})") from exc
    rows = data.get("journals") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("journals.json의 'journals'는 배열이어야 합니다.")
    journals: list[Journal] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"journals[{index}]는 객체여야 합니다.")
        if row.get("enabled", True) is False:
            continue
        name = str(row.get("name", "")).strip()
        issn = str(row.get("issn", "")).strip()
        group = str(row.get("group", "")).strip().lower()
        if not name or not issn or group not in {"aps", "nature"}:
            raise ValueError(f"journals[{index}]의 name, issn, group(aps/nature)을 확인하세요.")
        journals.append(Journal(name=name, issn=issn, group=group))
    if not journals:
        raise ValueError("활성화된 저널이 하나도 없습니다.")
    return journals
