"""Deterministic trend scoring with history-aware citation velocity."""

from __future__ import annotations

import math
from datetime import date

from .models import Paper
from .storage import PreviousState


def score_paper(paper: Paper, previous: PreviousState | None, today: date | None = None) -> float:
    today = today or date.today()
    reasons: list[str] = list(paper.selection_reasons)

    reference_date = paper.updated or paper.published
    age_days = max(0, (today - reference_date).days) if reference_date else 365
    recency = max(0.0, 30.0 * (1.0 - min(age_days, 30) / 30.0))
    if reference_date and reference_date > today:
        reasons.append("온라인 선공개/게재 예정")
    elif age_days <= 3:
        reasons.append("최근 3일 내 공개")
    elif age_days <= 7:
        reasons.append("최근 7일 내 공개")

    novelty = 15.0 if previous is None else 0.0
    if novelty:
        reasons.append("오늘 처음 발견")

    diversity = min(15.0, max(0, len(paper.sources) - 1) * 7.5)
    if len(paper.sources) >= 2:
        reasons.append(f"{len(paper.sources)}개 출처에서 동시 포착")

    citation_strength = min(15.0, math.log1p(max(0, paper.citation_count)) / math.log(101) * 15.0)
    velocity = 0.0
    if previous and previous.observed_on and today > previous.observed_on:
        elapsed = max(1, (today - previous.observed_on).days)
        gain_per_day = max(0.0, (paper.citation_count - previous.citations) / elapsed)
        velocity = min(25.0, math.log1p(gain_per_day) / math.log(11) * 25.0)
        if gain_per_day >= 1:
            reasons.append(f"인용 증가 +{gain_per_day:.1f}/일")

    access = 5.0 if paper.pdf_url else 0.0
    if paper.pdf_url:
        reasons.append("공개 PDF 확인")
    breadth = min(10.0, max(0, len(paper.topics) - 1) * 5.0)
    if len(paper.topics) >= 2:
        reasons.append(f"{len(paper.topics)}개 관심 주제와 연관")

    score = min(100.0, recency + novelty + diversity + citation_strength + velocity + access + breadth)
    paper.trend_score = round(score, 1)
    paper.score_reasons = reasons or ["기본 관련성 신호"]
    return paper.trend_score
