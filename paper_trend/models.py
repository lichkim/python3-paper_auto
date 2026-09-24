"""Shared paper model and identity helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date


def normalize_doi(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.rstrip("/.,")


def normalize_arxiv_id(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value)
    value = value.removesuffix(".pdf")
    return re.sub(r"v\d+$", "", value)


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return " ".join(re.findall(r"[a-z0-9가-힣]+", value))


@dataclass
class Paper:
    title: str
    abstract: str = ""
    summary_ko: str = ""
    authors: list[str] = field(default_factory=list)
    published: date | None = None
    updated: date | None = None
    venue: str = ""
    doi: str = ""
    arxiv_id: str = ""
    landing_url: str = ""
    pdf_url: str = ""
    citation_count: int = 0
    sources: set[str] = field(default_factory=set)
    topics: set[str] = field(default_factory=set)
    trend_score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)
    selection_reasons: list[str] = field(default_factory=list)
    identity_aliases: set[str] = field(default_factory=set)
    pdf_path: str = ""
    tex_path: str = ""
    translated_pdf_path: str = ""

    @property
    def key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        if self.arxiv_id:
            return f"arxiv:{normalize_arxiv_id(self.arxiv_id)}"
        return f"title:{normalize_title(self.title)}"

    @property
    def all_keys(self) -> set[str]:
        keys = {f"title:{normalize_title(self.title)}"} | self.identity_aliases
        if self.doi:
            keys.add(f"doi:{normalize_doi(self.doi)}")
        if self.arxiv_id:
            keys.add(f"arxiv:{normalize_arxiv_id(self.arxiv_id)}")
        return keys

    def merge_from(self, other: "Paper") -> None:
        self.identity_aliases.update(self.all_keys | other.all_keys)
        self.sources.update(other.sources)
        self.topics.update(other.topics)
        if len(other.abstract) > len(self.abstract):
            self.abstract = other.abstract
        self.summary_ko = self.summary_ko or other.summary_ko
        if len(other.authors) > len(self.authors):
            self.authors = other.authors
        self.citation_count = max(self.citation_count, other.citation_count)
        self.doi = self.doi or other.doi
        self.arxiv_id = self.arxiv_id or other.arxiv_id
        self.pdf_url = self.pdf_url or other.pdf_url
        self.landing_url = self.landing_url or other.landing_url
        generic_venues = {"", "arXiv", "Semantic Scholar", "Crossref"}
        if self.venue in generic_venues and other.venue not in generic_venues:
            self.venue = other.venue
        else:
            self.venue = self.venue or other.venue
        if other.published and (not self.published or other.published < self.published):
            self.published = other.published
        if other.updated and (not self.updated or other.updated > self.updated):
            self.updated = other.updated


def merge_papers(papers: list[Paper]) -> list[Paper]:
    """Merge cross-source records by DOI, arXiv ID, or normalized exact title."""
    merged: list[Paper] = []
    key_to_paper: dict[str, Paper] = {}
    for paper in papers:
        matches = [p for p in merged if p.all_keys & paper.all_keys]
        if not matches:
            merged.append(paper)
            match = paper
        else:
            match = matches[0]
            for other in matches[1:]:
                match.merge_from(other)
                merged.remove(other)
            match.merge_from(paper)
        for key in match.all_keys | paper.all_keys:
            key_to_paper[key] = match
    return merged
