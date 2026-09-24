"""SQLite persistence for papers, aliases, and daily trend snapshots."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .models import Paper


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    summary_ko TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]',
    published TEXT,
    updated TEXT,
    venue TEXT NOT NULL DEFAULT '',
    doi TEXT NOT NULL DEFAULT '',
    arxiv_id TEXT NOT NULL DEFAULT '',
    landing_url TEXT NOT NULL DEFAULT '',
    pdf_url TEXT NOT NULL DEFAULT '',
    citation_count INTEGER NOT NULL DEFAULT 0,
    sources_json TEXT NOT NULL DEFAULT '[]',
    topics_json TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    pdf_path TEXT NOT NULL DEFAULT '',
    tex_path TEXT NOT NULL DEFAULT '',
    translated_pdf_path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paper_aliases (
    alias TEXT PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS metrics (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    observed_on TEXT NOT NULL,
    citations INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    trend_score REAL NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (paper_id, observed_on)
);
CREATE INDEX IF NOT EXISTS idx_metrics_date_score ON metrics(observed_on, trend_score DESC);
"""


@dataclass(frozen=True)
class PreviousState:
    paper_id: int
    first_seen: date
    observed_on: date | None
    citations: int


class Repository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Apply small additive migrations to databases created by older versions."""
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(papers)")
        }
        if "summary_ko" not in columns:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE papers ADD COLUMN summary_ko TEXT NOT NULL DEFAULT ''"
                )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def previous(self, paper: Paper) -> PreviousState | None:
        aliases = sorted(paper.all_keys)
        placeholders = ",".join("?" for _ in aliases)
        row = self.connection.execute(
            f"""
            SELECT p.id, p.first_seen, m.observed_on, m.citations
            FROM paper_aliases a
            JOIN papers p ON p.id = a.paper_id
            LEFT JOIN metrics m ON m.paper_id = p.id
            WHERE a.alias IN ({placeholders})
            ORDER BY m.observed_on DESC
            LIMIT 1
            """,
            aliases,
        ).fetchone()
        if row is None:
            return None
        return PreviousState(
            paper_id=int(row["id"]),
            first_seen=date.fromisoformat(row["first_seen"][:10]),
            observed_on=date.fromisoformat(row["observed_on"]) if row["observed_on"] else None,
            citations=int(row["citations"] or 0),
        )

    def existing_summary(self, paper: Paper) -> str:
        aliases = sorted(paper.all_keys)
        placeholders = ",".join("?" for _ in aliases)
        row = self.connection.execute(
            f"""
            SELECT p.summary_ko
            FROM paper_aliases a JOIN papers p ON p.id=a.paper_id
            WHERE a.alias IN ({placeholders}) AND p.summary_ko != ''
            LIMIT 1
            """,
            aliases,
        ).fetchone()
        return str(row["summary_ko"]) if row else ""

    def save(self, paper: Paper, observed_on: date) -> int:
        previous = self.previous(paper)
        now = datetime.now().isoformat(timespec="seconds")
        paper_values = (
            paper.title, paper.abstract, paper.summary_ko,
            json.dumps(paper.authors, ensure_ascii=False),
            paper.published.isoformat() if paper.published else None,
            paper.updated.isoformat() if paper.updated else None,
            paper.venue, paper.doi, paper.arxiv_id, paper.landing_url, paper.pdf_url,
            paper.citation_count,
            json.dumps(sorted(paper.sources), ensure_ascii=False),
            json.dumps(sorted(paper.topics), ensure_ascii=False),
        )
        artifact_values = (paper.pdf_path, paper.tex_path, paper.translated_pdf_path)
        with self.connection:
            if previous is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO papers (
                        canonical_key,title,abstract,summary_ko,authors_json,published,updated,venue,doi,arxiv_id,
                        landing_url,pdf_url,citation_count,sources_json,topics_json,first_seen,last_seen,
                        pdf_path,tex_path,translated_pdf_path
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        paper.key,
                        *paper_values,
                        observed_on.isoformat(),
                        now,
                        *artifact_values,
                    ),
                )
                paper_id = int(cursor.lastrowid)
            else:
                paper_id = previous.paper_id
                self.connection.execute(
                    """
                    UPDATE papers SET
                        title=?, abstract=?, summary_ko=?, authors_json=?, published=?, updated=?, venue=?, doi=?, arxiv_id=?,
                        landing_url=?, pdf_url=?, citation_count=?, sources_json=?, topics_json=?, last_seen=?,
                        pdf_path=?, tex_path=?, translated_pdf_path=?
                    WHERE id=?
                    """,
                    (*paper_values, now, *artifact_values, paper_id),
                )
            for alias in paper.all_keys:
                self.connection.execute(
                    "INSERT INTO paper_aliases(alias,paper_id) VALUES(?,?) ON CONFLICT(alias) DO UPDATE SET paper_id=excluded.paper_id",
                    (alias, paper_id),
                )
            self.connection.execute(
                """
                INSERT INTO metrics(paper_id,observed_on,citations,source_count,trend_score,reasons_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(paper_id,observed_on) DO UPDATE SET
                    citations=excluded.citations,
                    source_count=excluded.source_count,
                    trend_score=excluded.trend_score,
                    reasons_json=excluded.reasons_json
                """,
                (
                    paper_id,
                    observed_on.isoformat(),
                    paper.citation_count,
                    len(paper.sources),
                    paper.trend_score,
                    json.dumps(paper.score_reasons, ensure_ascii=False),
                ),
            )
        return paper_id

    def top(self, limit: int = 20, observed_on: date | None = None) -> list[Paper]:
        if observed_on is None:
            row = self.connection.execute("SELECT MAX(observed_on) AS day FROM metrics").fetchone()
            if row is None or not row["day"]:
                return []
            observed_on = date.fromisoformat(row["day"])
        rows = self.connection.execute(
            """
            SELECT p.*, m.trend_score, m.reasons_json
            FROM metrics m JOIN papers p ON p.id=m.paper_id
            WHERE m.observed_on=?
            ORDER BY m.trend_score DESC, p.citation_count DESC
            LIMIT ?
            """,
            (observed_on.isoformat(), limit),
        ).fetchall()
        papers: list[Paper] = []
        for row in rows:
            papers.append(
                Paper(
                    title=row["title"],
                    abstract=row["abstract"],
                    summary_ko=row["summary_ko"],
                    authors=json.loads(row["authors_json"]),
                    published=date.fromisoformat(row["published"]) if row["published"] else None,
                    updated=date.fromisoformat(row["updated"]) if row["updated"] else None,
                    venue=row["venue"],
                    doi=row["doi"],
                    arxiv_id=row["arxiv_id"],
                    landing_url=row["landing_url"],
                    pdf_url=row["pdf_url"],
                    citation_count=int(row["citation_count"]),
                    sources=set(json.loads(row["sources_json"])),
                    topics=set(json.loads(row["topics_json"])),
                    trend_score=float(row["trend_score"]),
                    score_reasons=json.loads(row["reasons_json"]),
                    pdf_path=row["pdf_path"],
                    tex_path=row["tex_path"],
                    translated_pdf_path=row["translated_pdf_path"],
                )
            )
        return papers

    def export_csv(self, path: Path) -> int:
        """Export the cumulative deduplicated paper table for human inspection."""
        rows = self.connection.execute(
            """
            SELECT p.*,
                   COALESCE((
                       SELECT m.trend_score FROM metrics m
                       WHERE m.paper_id=p.id ORDER BY m.observed_on DESC LIMIT 1
                   ), 0) AS latest_trend_score
            FROM papers p
            ORDER BY p.first_seen DESC, p.title
            """
        ).fetchall()
        fieldnames = [
            "canonical_key", "title", "abstract_summary_ko", "authors", "published",
            "venue", "doi", "arxiv_id", "landing_url", "pdf_url", "sources", "topics",
            "first_seen", "last_seen", "citation_count", "latest_trend_score",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "canonical_key": row["canonical_key"],
                        "title": row["title"],
                        "abstract_summary_ko": row["summary_ko"],
                        "authors": "; ".join(json.loads(row["authors_json"])),
                        "published": row["published"] or "",
                        "venue": row["venue"],
                        "doi": row["doi"],
                        "arxiv_id": row["arxiv_id"],
                        "landing_url": row["landing_url"],
                        "pdf_url": row["pdf_url"],
                        "sources": "; ".join(json.loads(row["sources_json"])),
                        "topics": "; ".join(json.loads(row["topics_json"])),
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                        "citation_count": row["citation_count"],
                        "latest_trend_score": row["latest_trend_score"],
                    }
                )
        temporary.replace(path)
        return len(rows)

    def update_artifacts(self, paper: Paper) -> None:
        """Update explicitly requested local files without creating a ranking snapshot."""
        previous = self.previous(paper)
        if previous is None:
            raise ValueError("DB에서 선택한 논문을 찾을 수 없습니다.")
        with self.connection:
            self.connection.execute(
                "UPDATE papers SET pdf_path=?, tex_path=?, translated_pdf_path=? WHERE id=?",
                (paper.pdf_path, paper.tex_path, paper.translated_pdf_path, previous.paper_id),
            )
