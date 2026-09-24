"""On-demand PDF downloader. Daily scans never call this module automatically."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import requests

from .config import Settings
from .models import Paper, normalize_arxiv_id


class DownloadError(RuntimeError):
    pass


def safe_name(value: str, max_length: int = 120) -> str:
    value = re.sub(r"[^0-9A-Za-z가-힣._()\[\] -]+", "_", value).strip(" ._")
    return value[:max_length].rstrip() or "paper"


def download_pdf(paper: Paper, settings: Settings, overwrite: bool = False) -> Path:
    if not paper.pdf_url:
        raise DownloadError("이 논문에는 공개 PDF 주소가 없습니다.")
    identifier = normalize_arxiv_id(paper.arxiv_id) if paper.arxiv_id else paper.doi or "paper"
    output_dir = settings.downloads_dir / date.today().isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"[{safe_name(identifier, 50)}] {safe_name(paper.title)}.pdf"
    if output.exists() and not overwrite:
        return output
    temporary = output.with_suffix(".pdf.part")
    try:
        with requests.get(
            paper.pdf_url,
            headers={"User-Agent": settings.user_agent, "Accept": "application/pdf"},
            timeout=settings.request_timeout,
            stream=True,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            first = True
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    if first and b"%PDF-" not in chunk[:1024]:
                        raise DownloadError(
                            f"PDF가 아닌 응답을 받았습니다 ({response.headers.get('Content-Type', 'unknown')})."
                        )
                    first = False
                    handle.write(chunk)
            if first:
                raise DownloadError("빈 PDF 응답을 받았습니다.")
        temporary.replace(output)
        return output
    except requests.RequestException as exc:
        raise DownloadError(f"PDF 다운로드 실패: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)

