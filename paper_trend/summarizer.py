"""Cached one-line Korean abstract summaries using the DeepSeek API."""

from __future__ import annotations

import re

from openai import OpenAI

from .config import Settings
from .models import Paper


def _clean_summary(value: str) -> str:
    value = " ".join(value.strip().split())
    value = re.sub(r"^(?:요약|한줄 요약|한 줄 요약)\s*[:：-]\s*", "", value)
    return value.strip('"“”')


def summarize_missing(papers: list[Paper], settings: Settings) -> tuple[int, list[str]]:
    candidates = [paper for paper in papers if paper.abstract and not paper.summary_ko]
    if not candidates:
        return 0, []
    if not settings.deepseek_api_key:
        return 0, ["DeepSeek API 키가 없어 초록 한 줄 요약을 생략했습니다."]

    client = OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=float(settings.request_timeout),
    )
    completed = 0
    errors: list[str] = []
    for paper in candidates[: settings.summary_max_per_run]:
        try:
            response = client.chat.completions.create(
                model=settings.summary_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 물리학 논문 편집자입니다. 제공된 제목과 초록만 근거로 "
                            "핵심 연구 내용과 결과를 자연스러운 한국어 한 문장으로 요약하세요. "
                            "과장하거나 초록에 없는 내용을 추가하지 말고, 접두사나 목록 기호 없이 "
                            "120자 이내의 한 문장만 출력하세요."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"제목: {paper.title}\n\n초록: {paper.abstract}",
                    },
                ],
                temperature=0.1,
                max_tokens=180,
            )
            content = response.choices[0].message.content if response.choices else ""
            summary = _clean_summary(content or "")
            if not summary:
                raise ValueError("빈 응답")
            paper.summary_ko = summary
            completed += 1
        except Exception as exc:
            errors.append(f"DeepSeek 요약 실패: {paper.title} ({exc})")
    return completed, errors
