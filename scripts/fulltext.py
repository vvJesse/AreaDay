"""Automatic, local-only full-text acquisition for a raw candidate list."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
MAX_DIRECT_PDF_BYTES = 100 * 1024 * 1024


def valid_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def download_licensed_open_access_pdf(url: str, destination: Path) -> None:
    """Download an eligible OA PDF while validating transport, size, and bytes."""
    import requests

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Open-access PDF URL must be an absolute HTTPS URL")
    if parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Localhost is not a permitted PDF host")

    staging = destination.with_suffix(".pdf.part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ResearchRamp/0.1; local academic client)",
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.2",
            },
            stream=True,
            allow_redirects=True,
            timeout=(20, 120),
        ) as response:
            response.raise_for_status()
            final_url = urllib.parse.urlparse(response.url)
            if final_url.scheme != "https":
                raise RuntimeError("PDF redirect left HTTPS")
            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) > MAX_DIRECT_PDF_BYTES:
                raise RuntimeError("PDF exceeds the 100 MB safety limit")
            written = 0
            with staging.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_DIRECT_PDF_BYTES:
                        raise RuntimeError("PDF exceeds the 100 MB safety limit")
                    handle.write(chunk)
        staging.replace(destination)
        if not valid_pdf(destination):
            raise RuntimeError("Open-access URL did not return a PDF")
    finally:
        staging.unlink(missing_ok=True)
        if destination.exists() and not valid_pdf(destination):
            destination.unlink(missing_ok=True)


def download_openalex_content_pdf(
    work_id: str, api_key: str, destination: Path
) -> None:
    """Download one cached OpenAlex PDF without persisting the user's API key."""
    if not re.fullmatch(r"W\d+", work_id):
        raise ValueError(f"Invalid OpenAlex work identifier: {work_id}")
    if not api_key:
        raise ValueError("OpenAlex content download requires an API key")
    import requests

    url = f"https://content.openalex.org/works/{work_id}.pdf"
    staging = destination.with_suffix(".pdf.part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "ResearchRamp/0.1 (local academic research client)",
                "Accept": "application/pdf,application/octet-stream;q=0.9",
            },
            stream=True,
            allow_redirects=True,
            timeout=(20, 120),
        ) as response:
            response.raise_for_status()
            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) > MAX_DIRECT_PDF_BYTES:
                raise RuntimeError("PDF exceeds the 100 MB safety limit")
            written = 0
            with staging.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_DIRECT_PDF_BYTES:
                        raise RuntimeError("PDF exceeds the 100 MB safety limit")
                    handle.write(chunk)
        staging.replace(destination)
        if not valid_pdf(destination):
            raise RuntimeError("OpenAlex content endpoint did not return a PDF")
    finally:
        staging.unlink(missing_ok=True)
        if destination.exists() and not valid_pdf(destination):
            destination.unlink(missing_ok=True)
