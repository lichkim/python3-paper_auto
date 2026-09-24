"""Atomic topic configuration updates initiated by Telegram commands."""

from __future__ import annotations

import json
from pathlib import Path

from .config import DEFAULT_SOURCES, Topic, load_topics


class TopicError(ValueError):
    pass


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise TopicError(f"주제 설정을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("topics"), list):
        raise TopicError("topics.json 형식이 올바르지 않습니다.")
    return data


def _write(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # Validate before replacing the live configuration.
    load_topics(temporary)
    temporary.replace(path)


def add_topic(path: Path, name: str, query: str) -> Topic:
    name = " ".join(name.split())
    query = " ".join(query.split())
    if not name or not query:
        raise TopicError("주제명과 검색어를 모두 입력해주세요.")
    if len(name) > 60 or len(query) > 200:
        raise TopicError("주제명 또는 검색어가 너무 깁니다.")

    data = _read(path)
    active = load_topics(path)
    if any(topic.name.casefold() == name.casefold() for topic in active):
        raise TopicError(f"이미 등록된 주제입니다: {name}")
    data["topics"].append(
        {
            "name": name,
            "query": query,
            "journal_match_ratio": 0.5,
            "enabled": True,
            "sources": list(DEFAULT_SOURCES),
        }
    )
    _write(path, data)
    return Topic(name=name, query=query, sources=DEFAULT_SOURCES, journal_match_ratio=0.5)


def remove_topic(path: Path, selector: str) -> Topic:
    selector = selector.strip()
    if not selector:
        raise TopicError("삭제할 주제 번호나 이름을 입력해주세요.")
    data = _read(path)
    enabled_rows = [row for row in data["topics"] if row.get("enabled", True) is not False]
    if len(enabled_rows) <= 1:
        raise TopicError("마지막 활성 주제는 삭제할 수 없습니다.")

    target: dict | None = None
    if selector.isdigit():
        index = int(selector) - 1
        if 0 <= index < len(enabled_rows):
            target = enabled_rows[index]
    else:
        matches = [
            row for row in enabled_rows
            if str(row.get("name", "")).casefold() == selector.casefold()
        ]
        if len(matches) == 1:
            target = matches[0]
    if target is None:
        raise TopicError(f"일치하는 활성 주제가 없습니다: {selector}")

    data["topics"].remove(target)
    removed = Topic(
        name=str(target["name"]),
        query=str(target["query"]),
        sources=tuple(target.get("sources", DEFAULT_SOURCES)),
        journal_match_ratio=float(target.get("journal_match_ratio", 1.0)),
    )
    _write(path, data)
    return removed
