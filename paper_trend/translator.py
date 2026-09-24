"""On-demand English PDF to Korean LaTeX translation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from pypdf import PdfReader

from .config import Settings


PREAMBLE = r"""\documentclass[a4paper,11pt]{article}
\usepackage{kotex}
\usepackage{amsmath,amssymb,mathtools}
\usepackage{booktabs,longtable,array}
\usepackage{geometry}
\usepackage[hidelinks]{hyperref}
\usepackage{url}
\geometry{margin=2.5cm}
"""


class TranslationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranslationResult:
    tex_path: Path
    pdf_path: Path | None


def _extract_pages(path: Path) -> list[str]:
    try:
        reader = PdfReader(path)
        pages = [re.sub(r"\n{4,}", "\n\n\n", (page.extract_text() or "").strip()) for page in reader.pages]
    except Exception as exc:
        raise TranslationError(f"PDF 텍스트 추출 실패: {exc}") from exc
    if not any(pages):
        raise TranslationError("PDF에서 텍스트를 찾지 못했습니다. 스캔본이라면 OCR이 필요합니다.")
    return pages


def _chunks(pages: list[str], limit: int = 18_000) -> list[str]:
    chunks: list[str] = []
    current = ""
    for page_number, page in enumerate(pages, 1):
        remaining = page
        while remaining:
            room = max(1, limit - len(current) - 40)
            part, remaining = remaining[:room], remaining[room:]
            marked = f"\n\n[원본 PDF {page_number}쪽]\n{part}"
            if current and len(current) + len(marked) > limit:
                chunks.append(current.strip())
                current = marked
            else:
                current += marked
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _normalize_fragment(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^```(?:latex|tex)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"```\s*$", "", value)
    if r"\begin{document}" in value:
        value = value.split(r"\begin{document}", 1)[1]
    value = value.replace(r"\end{document}", "")
    return "\n".join(
        line for line in value.splitlines()
        if not line.lstrip().startswith((r"\documentclass", r"\usepackage"))
    ).strip()


def _escape_title(value: str) -> str:
    replacements = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}"}
    return "".join(replacements.get(char, char) for char in value)


def _compile(tex_path: Path) -> Path:
    executable = shutil.which("latexmk")
    if not executable:
        raise TranslationError("latexmk가 없어 TeX는 저장했지만 PDF 컴파일은 할 수 없습니다.")
    build_dir = tex_path.parent / ".build" / tex_path.stem
    build_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [executable, "-xelatex", "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={build_dir}", str(tex_path.resolve())],
        text=True,
        capture_output=True,
        check=False,
    )
    built = build_dir / tex_path.with_suffix(".pdf").name
    if completed.returncode != 0 or not built.exists():
        raise TranslationError(f"LaTeX 컴파일 실패: {build_dir / (tex_path.stem + '.log')}")
    output = tex_path.with_suffix(".pdf")
    shutil.copy2(built, output)
    return output


def translate_pdf(pdf_path: Path, title: str, settings: Settings, force: bool = False) -> TranslationResult:
    """Translate one explicitly selected local PDF; never called by the daily scan."""
    if not settings.deepseek_api_key:
        raise TranslationError("DEEPSEEK_API_KEY가 설정되지 않았습니다.")
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:12]
    output_dir = settings.translations_dir / digest
    output_dir.mkdir(parents=True, exist_ok=True)
    tex_path = output_dir / "translated.tex"
    if tex_path.exists() and not force:
        pdf_output = tex_path.with_suffix(".pdf")
        return TranslationResult(tex_path, pdf_output if pdf_output.exists() else None)

    prompt_path = Path(__file__).with_name("prompts.json")
    prompts = json.loads(prompt_path.read_text(encoding="utf-8"))
    chunks = _chunks(_extract_pages(pdf_path))
    cache_dir = output_dir / ".parts"
    cache_dir.mkdir(exist_ok=True)
    client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, timeout=300.0)
    fragments: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        cache = cache_dir / f"part-{index:04d}.tex"
        if cache.exists() and not force:
            fragments.append(cache.read_text(encoding="utf-8"))
            continue
        user_prompt = prompts["chunk_prompt"].format(
            title=title,
            total=len(chunks),
            index=index,
            text=chunk,
        )
        try:
            response = client.chat.completions.create(
                model=settings.translation_model,
                messages=[
                    {"role": "system", "content": prompts["system_instruction"]},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=32_768,
            )
            content = response.choices[0].message.content if response.choices else ""
        except Exception as exc:
            raise TranslationError(f"번역 API 호출 실패 (묶음 {index}/{len(chunks)}): {exc}") from exc
        if not content:
            raise TranslationError(f"번역 API가 빈 응답을 반환했습니다 (묶음 {index}).")
        fragment = _normalize_fragment(content)
        cache.write_text(fragment, encoding="utf-8")
        fragments.append(fragment)

    document = (
        PREAMBLE
        + "\n\\begin{document}\n"
        + f"\\title{{{_escape_title(title)}}}\n\\author{{}}\n\\date{{}}\n\\maketitle\n\n"
        + "\n\n".join(fragments)
        + "\n\\end{document}\n"
    )
    temporary = tex_path.with_suffix(".tex.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(tex_path)
    pdf_output = _compile(tex_path) if settings.compile_tex else None
    return TranslationResult(tex_path, pdf_output)

