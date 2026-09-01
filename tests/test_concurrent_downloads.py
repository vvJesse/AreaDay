from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from concurrent_downloads import download_candidates_concurrently  # noqa: E402
from fulltext import (  # noqa: E402
    MAX_DIRECT_PDF_BYTES,
    download_licensed_open_access_pdf,
    download_openalex_content_pdf,
)


def candidate(
    index: int,
    host: str = "papers.example",
    *,
    alternates: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": f"paper-{index}",
        "provider": "synthetic-oa",
        "title": f"Synthetic paper {index}",
        "pdf_url": f"https://{host}/{index}.pdf",
        "alternate_pdf_urls": alternates or [],
    }


def write_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\nsynthetic test PDF\n")


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ConcurrencyProbe:
    def __init__(self, delay: float = 0.04) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.active_by_host: dict[str, int] = {}
        self.max_by_host: dict[str, int] = {}
        self.calls: list[str] = []

    def download(self, url: str, destination: Path, session: object) -> None:
        host = urlparse(url).hostname or ""
        with self.lock:
            self.calls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.active_by_host[host] = self.active_by_host.get(host, 0) + 1
            self.max_by_host[host] = max(
                self.max_by_host.get(host, 0), self.active_by_host[host]
            )
        try:
            time.sleep(self.delay)
            write_pdf(destination)
        finally:
            with self.lock:
                self.active -= 1
                self.active_by_host[host] -= 1


class ConcurrentDownloadTests(unittest.TestCase):
    def test_global_and_per_host_limits_are_both_enforced(self) -> None:
        probe = ConcurrencyProbe()
        papers = [
            candidate(0, "a.example"),
            candidate(1, "a.example"),
            candidate(2, "b.example"),
            candidate(3, "b.example"),
            candidate(4, "c.example"),
            candidate(5, "c.example"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            results = download_candidates_concurrently(
                papers,
                Path(temporary),
                target_papers=6,
                openalex_api_key=None,
                direct_downloader=probe.download,
                session_factory=FakeSession,
            )
        self.assertEqual(len(results), 6)
        self.assertEqual(probe.max_active, 4)
        self.assertTrue(all(value <= 2 for value in probe.max_by_host.values()))

    def test_result_order_is_stable_and_target_stops_extra_candidates(self) -> None:
        calls: list[str] = []
        lock = threading.Lock()

        def download(url: str, destination: Path, session: object) -> None:
            index = int(Path(urlparse(url).path).stem)
            with lock:
                calls.append(url)
            time.sleep(0.01 * (3 - index))
            write_pdf(destination)

        main_thread = threading.get_ident()
        checkpoint_threads: list[int] = []
        checkpoints: list[list[str]] = []

        def checkpoint(path: Path, records: object) -> None:
            rows = list(records)  # type: ignore[arg-type]
            checkpoint_threads.append(threading.get_ident())
            checkpoints.append([row["candidate_id"] for row in rows])

        with tempfile.TemporaryDirectory() as temporary:
            results = download_candidates_concurrently(
                [candidate(index, f"h{index}.example") for index in range(5)],
                Path(temporary),
                target_papers=3,
                openalex_api_key=None,
                direct_downloader=download,
                session_factory=FakeSession,
                checkpoint_writer=checkpoint,
            )
        self.assertEqual(
            [item["candidate_id"] for item in results],
            ["paper-0", "paper-1", "paper-2"],
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(checkpoint_threads, [main_thread] * len(checkpoint_threads))
        self.assertTrue(
            all(
                items
                == sorted(items, key=lambda value: int(value.split("-")[1]))
                for items in checkpoints
            )
        )

    def test_routes_fall_back_serially_within_one_candidate(self) -> None:
        calls: list[str] = []

        def download(url: str, destination: Path, session: object) -> None:
            calls.append(url)
            if "primary" in url:
                raise RuntimeError("synthetic primary failure")
            write_pdf(destination)

        paper = candidate(
            0,
            "primary.example",
            alternates=[
                {
                    "provider": "synthetic-mirror",
                    "url": "https://mirror.example/0.pdf",
                }
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            results = download_candidates_concurrently(
                [paper],
                Path(temporary),
                target_papers=1,
                openalex_api_key=None,
                direct_downloader=download,
                session_factory=FakeSession,
            )
        self.assertEqual(
            calls,
            [
                "https://primary.example/0.pdf",
                "https://primary.example/0.pdf",
                "https://mirror.example/0.pdf",
            ],
        )
        self.assertEqual(
            [item["status"] for item in results[0]["attempts"]],
            ["failed", "failed", "downloaded"],
        )
        self.assertEqual(results[0]["provider"], "synthetic-mirror")

    def test_openalex_content_remains_the_last_keyed_fallback(self) -> None:
        calls: list[str] = []

        def direct(url: str, destination: Path, session: object) -> None:
            calls.append("direct")
            raise RuntimeError("synthetic direct failure")

        def openalex(
            work_id: str,
            api_key: str,
            destination: Path,
            session: object,
        ) -> None:
            calls.append(f"openalex:{work_id}:{api_key}")
            write_pdf(destination)

        paper = candidate(0)
        paper["openalex_id"] = "W123"
        paper["has_openalex_pdf"] = True
        with tempfile.TemporaryDirectory() as temporary:
            results = download_candidates_concurrently(
                [paper],
                Path(temporary),
                target_papers=1,
                openalex_api_key="synthetic-key",
                direct_downloader=direct,
                openalex_downloader=openalex,
                session_factory=FakeSession,
            )
        self.assertEqual(
            calls,
            ["direct", "direct", "openalex:W123:synthetic-key"],
        )
        self.assertEqual(results[0]["provider"], "openalex-content")
        self.assertIsNone(results[0]["pdf_url"])

    def test_ineligible_http_route_is_rejected_before_download(self) -> None:
        calls: list[str] = []

        def download(url: str, destination: Path, session: object) -> None:
            calls.append(url)
            write_pdf(destination)

        paper = candidate(
            0,
            alternates=[
                {
                    "provider": "synthetic-mirror",
                    "url": "https://mirror.example/0.pdf",
                }
            ],
        )
        paper["pdf_url"] = "http://ineligible.example/0.pdf"
        with tempfile.TemporaryDirectory() as temporary:
            results = download_candidates_concurrently(
                [paper],
                Path(temporary),
                target_papers=1,
                openalex_api_key=None,
                direct_downloader=download,
                session_factory=FakeSession,
            )
        self.assertEqual(calls, ["https://mirror.example/0.pdf"])
        self.assertIn("absolute HTTPS URL", results[0]["attempts"][0]["error"])

    def test_invalid_pdf_signature_is_removed_before_fallback(self) -> None:
        calls: list[str] = []

        def download(url: str, destination: Path, session: object) -> None:
            calls.append(url)
            if "primary" in url:
                destination.write_bytes(b"not a PDF")
            else:
                write_pdf(destination)

        paper = candidate(
            0,
            "primary.example",
            alternates=[
                {
                    "provider": "synthetic-mirror",
                    "url": "https://mirror.example/0.pdf",
                }
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            results = download_candidates_concurrently(
                [paper],
                workspace,
                target_papers=1,
                openalex_api_key=None,
                direct_downloader=download,
                session_factory=FakeSession,
            )
            downloaded = workspace / "papers" / "paper-0.pdf"
            self.assertTrue(downloaded.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(len(calls), 3)
        self.assertIn("valid PDF", results[0]["attempts"][0]["error"])

    def test_transient_route_failure_is_retried_before_fallback(self) -> None:
        calls: list[str] = []

        def download(url: str, destination: Path, session: object) -> None:
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("synthetic transient failure")
            write_pdf(destination)

        with tempfile.TemporaryDirectory() as temporary:
            results = download_candidates_concurrently(
                [candidate(0)],
                Path(temporary),
                target_papers=1,
                openalex_api_key=None,
                direct_downloader=download,
                session_factory=FakeSession,
            )

        self.assertEqual(calls, ["https://papers.example/0.pdf"] * 2)
        self.assertEqual(
            [item["status"] for item in results[0]["attempts"]],
            ["failed", "downloaded"],
        )

    def test_failure_admits_one_replacement_without_overshooting_target(self) -> None:
        calls: list[int] = []
        lock = threading.Lock()

        def download(url: str, destination: Path, session: object) -> None:
            index = int(Path(urlparse(url).path).stem)
            with lock:
                calls.append(index)
            if index == 0:
                raise RuntimeError("synthetic failure")
            time.sleep(0.02)
            write_pdf(destination)

        with tempfile.TemporaryDirectory() as temporary:
            results = download_candidates_concurrently(
                [candidate(index, f"h{index}.example") for index in range(4)],
                Path(temporary),
                target_papers=2,
                openalex_api_key=None,
                max_downloads=2,
                direct_downloader=download,
                session_factory=FakeSession,
            )
        self.assertEqual(sorted(calls), [0, 0, 1, 2])
        self.assertNotIn(3, calls)
        self.assertEqual(
            sum(item["status"] in {"downloaded", "existing"} for item in results),
            2,
        )

    def test_resume_reuses_valid_pdf_and_cleans_stale_part(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            existing = workspace / "papers" / "paper-0.pdf"
            write_pdf(existing)
            stale = workspace / "papers" / "paper-1.pdf.part"
            stale.write_bytes(b"partial")

            def download(url: str, destination: Path, session: object) -> None:
                calls.append(url)
                write_pdf(destination)

            results = download_candidates_concurrently(
                [candidate(0), candidate(1)],
                workspace,
                target_papers=2,
                openalex_api_key=None,
                direct_downloader=download,
                session_factory=FakeSession,
            )
            self.assertFalse(stale.exists())
        self.assertEqual([item["status"] for item in results], ["existing", "downloaded"])
        self.assertEqual(calls, ["https://papers.example/1.pdf"])

    def test_worker_reuses_one_session_and_closes_it(self) -> None:
        sessions: list[FakeSession] = []
        observed: list[object] = []

        def factory() -> FakeSession:
            session = FakeSession()
            sessions.append(session)
            return session

        def download(url: str, destination: Path, session: object) -> None:
            observed.append(session)
            write_pdf(destination)

        with tempfile.TemporaryDirectory() as temporary:
            download_candidates_concurrently(
                [candidate(0), candidate(1)],
                Path(temporary),
                target_papers=2,
                openalex_api_key=None,
                max_downloads=1,
                direct_downloader=download,
                session_factory=factory,
            )
        self.assertEqual(len(sessions), 1)
        self.assertIs(observed[0], observed[1])
        self.assertTrue(sessions[0].closed)

    def test_interrupt_cleans_part_file(self) -> None:
        def interrupt(url: str, destination: Path, session: object) -> None:
            part = destination.with_suffix(".pdf.part")
            part.write_bytes(b"partial")
            raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with self.assertRaises(KeyboardInterrupt):
                download_candidates_concurrently(
                    [candidate(0)],
                    workspace,
                    target_papers=1,
                    openalex_api_key=None,
                    direct_downloader=interrupt,
                    session_factory=FakeSession,
                )
            self.assertFalse((workspace / "papers" / "paper-0.pdf.part").exists())

    def test_declared_file_over_100_mb_is_rejected_without_body_read(self) -> None:
        class OversizeResponse:
            url = "https://papers.example/oversize.pdf"
            headers = {"Content-Length": str(MAX_DIRECT_PDF_BYTES + 1)}

            def __enter__(self) -> "OversizeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int) -> object:
                raise AssertionError("oversize response body must not be read")

        class OversizeSession:
            def get(self, url: str, **kwargs: object) -> OversizeResponse:
                return OversizeResponse()

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "oversize.pdf"
            with self.assertRaisesRegex(RuntimeError, "100 MB"):
                download_licensed_open_access_pdf(
                    "https://papers.example/oversize.pdf",
                    destination,
                    session=OversizeSession(),
                )
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".pdf.part").exists())

    def test_openalex_redirect_must_remain_on_eligible_https(self) -> None:
        class IneligibleRedirectResponse:
            url = "http://content.example/redirected.pdf"
            headers: dict[str, str] = {}

            def __enter__(self) -> "IneligibleRedirectResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int) -> object:
                raise AssertionError("ineligible redirect body must not be read")

        class RedirectSession:
            def get(self, url: str, **kwargs: object) -> IneligibleRedirectResponse:
                return IneligibleRedirectResponse()

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "openalex.pdf"
            with self.assertRaisesRegex(RuntimeError, "eligible HTTPS"):
                download_openalex_content_pdf(
                    "W123",
                    "synthetic-key",
                    destination,
                    session=RedirectSession(),
                )
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
