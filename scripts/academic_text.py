"""Extract and conservatively clean academic PDF body text."""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path


STOP_SECTION_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:references|bibliography|works cited)\s*$",
    re.IGNORECASE,
)
ACKNOWLEDGEMENT_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?acknowledg(?:e)?ments?\s*$",
    re.IGNORECASE,
)
ABSTRACT_RE = re.compile(r"^\s*abstract\s*(?:[—–:\-]\s*)?(.*)$", re.IGNORECASE)
HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+)?[A-Z][A-Za-z0-9 /&,:()'\-]{1,90}$"
)
CAPTION_RE = re.compile(r"^(?:fig(?:ure)?|table)\s*\d+[A-Za-z]?[.:\-]?\s*", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
PAGE_NUMBER_RE = re.compile(r"^(?:page\s+)?\d+(?:\s+of\s+\d+)?$", re.I)


def extract_pdf_text(path: Path) -> tuple[str, int]:
    """Return page-separated text using PyMuPDF's sorted text blocks."""
    import pymupdf

    pages: list[str] = []
    with pymupdf.open(path) as document:
        for page in document:
            blocks = page.get_text("blocks", sort=True)
            text_blocks = [
                str(block[4]).strip()
                for block in blocks
                if len(block) >= 7 and block[6] == 0 and str(block[4]).strip()
            ]
            pages.append("\n".join(text_blocks))
    return "\f".join(pages), len(pages)


def _line_key(line: str) -> str:
    normalized = re.sub(r"\d+", "#", line.casefold())
    normalized = re.sub(r"\s+", " ", normalized).strip(" -–—|·")
    return normalized


def _repeated_margin_lines(pages: list[list[str]]) -> set[str]:
    if len(pages) < 3:
        return set()
    occurrences: Counter[str] = Counter()
    for lines in pages:
        nonempty = [line.strip() for line in lines if line.strip()]
        margin = nonempty[:3] + nonempty[-3:]
        occurrences.update({_line_key(line) for line in margin if len(line) <= 160})
    minimum_pages = max(3, math.ceil(len(pages) * 0.4))
    return {
        key
        for key, count in occurrences.items()
        if key and count >= minimum_pages
    }


def _mostly_numeric(line: str) -> bool:
    compact = re.sub(r"\s+", "", line)
    if len(compact) < 8 or sum(character.isdigit() for character in compact) < 3:
        return False
    numeric_or_symbol = sum(
        character.isdigit() or character in ".,;%±−–—()[]{}:+=*/<>"
        for character in compact
    )
    return numeric_or_symbol / len(compact) >= 0.58


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not 2 <= len(stripped) <= 100 or stripped.endswith(('.', '?', '!')):
        return False
    words = stripped.split()
    if len(words) > 12:
        return False
    return bool(HEADING_RE.match(stripped) or stripped.isupper())


def clean_academic_text(raw_text: str) -> tuple[str, dict[str, int | bool]]:
    """Keep scholarly body sections while removing common PDF extraction noise."""
    normalized = (
        raw_text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("ﬁ", "fi")
        .replace("ﬂ", "fl")
        .replace("ﬀ", "ff")
        .replace("ﬃ", "ffi")
        .replace("ﬄ", "ffl")
        .replace("\u00ad", "")
    )
    page_texts = normalized.split("\f")
    pages = [[re.sub(r"[ \t]+", " ", line).strip() for line in page.split("\n")] for page in page_texts]
    repeated_margin_keys = _repeated_margin_lines(pages)

    lines: list[str] = []
    removed_margin_lines = 0
    removed_numeric_lines = 0
    for page in pages:
        for line in page:
            if not line:
                lines.append("")
                continue
            if _line_key(line) in repeated_margin_keys or PAGE_NUMBER_RE.fullmatch(line):
                removed_margin_lines += 1
                continue
            if _mostly_numeric(line):
                removed_numeric_lines += 1
                continue
            lines.append(line)

    abstract_index: int | None = None
    abstract_remainder = ""
    for index, line in enumerate(lines[:180]):
        match = ABSTRACT_RE.match(line)
        if match:
            abstract_index = index
            abstract_remainder = match.group(1).strip()
            break
    if abstract_index is not None:
        lines = ([abstract_remainder] if abstract_remainder else []) + lines[abstract_index + 1 :]
    else:
        # When no Abstract heading is extractable, remove only obvious contact lines.
        lines = [line for line in lines if not EMAIL_RE.search(line)]

    kept: list[str] = []
    captions_seen: set[str] = set()
    skipping_acknowledgements = False
    stopped_at_references = False
    removed_duplicate_captions = 0
    for line in lines:
        stripped = line.strip()
        if STOP_SECTION_RE.fullmatch(stripped):
            stopped_at_references = True
            break
        if ACKNOWLEDGEMENT_RE.fullmatch(stripped):
            skipping_acknowledgements = True
            continue
        if skipping_acknowledgements:
            if _is_heading(stripped):
                skipping_acknowledgements = False
            else:
                continue
        caption_match = CAPTION_RE.match(stripped)
        if caption_match:
            caption_key = _line_key(CAPTION_RE.sub("", stripped))
            if caption_key in captions_seen:
                removed_duplicate_captions += 1
                continue
            captions_seen.add(caption_key)
        kept.append(stripped)

    body = "\n".join(kept)
    body = re.sub(r"(?<=[A-Za-z])-\n(?=[a-z])", "", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    word_count = len(re.findall(r"[A-Za-z]+", body))
    diagnostics: dict[str, int | bool] = {
        "page_count": len(page_texts),
        "body_word_count": word_count,
        "abstract_heading_found": abstract_index is not None,
        "stopped_at_references": stopped_at_references,
        "removed_repeated_margin_lines": removed_margin_lines,
        "removed_numeric_lines": removed_numeric_lines,
        "removed_duplicate_captions": removed_duplicate_captions,
    }
    return body, diagnostics
