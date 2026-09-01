from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, sentinel


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from arxiv_client import make_arxiv_client  # noqa: E402
from acquire_mini_corpus import collect_arxiv_candidates  # noqa: E402


class ArxivClientTests(unittest.TestCase):
    def test_client_preserves_courtesy_delay_and_adds_request_timeout(self) -> None:
        client = make_arxiv_client(
            page_size=12,
            delay_seconds=3,
            num_retries=2,
            request_timeout_seconds=17,
        )
        try:
            self.assertEqual(client.page_size, 12)
            self.assertEqual(client.delay_seconds, 3)
            self.assertEqual(client.num_retries, 2)
            with patch(
                "requests.sessions.Session.request",
                return_value=sentinel.response,
            ) as request:
                self.assertIs(
                    client._session.get("https://example.invalid/query"),
                    sentinel.response,
                )
            self.assertEqual(request.call_args.kwargs["timeout"], 17)
        finally:
            client._session.close()

    def test_explicit_request_timeout_takes_precedence(self) -> None:
        client = make_arxiv_client(page_size=5, request_timeout_seconds=17)
        try:
            with patch(
                "requests.sessions.Session.request",
                return_value=sentinel.response,
            ) as request:
                client._session.get("https://example.invalid/query", timeout=4)
            self.assertEqual(request.call_args.kwargs["timeout"], 4)
        finally:
            client._session.close()

    def test_timeout_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            make_arxiv_client(page_size=5, request_timeout_seconds=0)

    def test_normalized_candidates_are_reused_without_another_api_request(self) -> None:
        scope = {
            "recent_from_year": 2020,
            "foundation_from_year": 2000,
            "foundation_before_year": 2020,
            "foundation_limit": 0,
            "arxiv_categories": ["cs.IR"],
            "exclude_title_prefixes": ["erratum"],
        }
        query = {
            "id": "q01",
            "label": "Synthetic query",
            "phrases": ["synthetic retrieval"],
            "categories": ["cs.IR"],
            "date_lane": "recent",
        }
        result = SimpleNamespace(
            title="Synthetic retrieval paper",
            summary="Synthetic abstract",
            published=datetime(2024, 1, 2, tzinfo=UTC),
            journal_ref=None,
            doi=None,
            categories=["cs.IR"],
            pdf_url="https://arxiv.org/pdf/2401.00001",
            get_short_id=lambda: "2401.00001v2",
        )

        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            first_client = MagicMock()
            first_client.results.return_value = [result]
            first_client._session = MagicMock()
            with patch(
                "acquire_mini_corpus.make_arxiv_client",
                return_value=first_client,
            ):
                first_candidates, first_attempts = collect_arxiv_candidates(
                    [query],
                    max_results_per_query=5,
                    scope=scope,
                    cache_dir=cache_dir,
                    refresh=False,
                )

            cached_client = MagicMock()
            cached_client.results.side_effect = AssertionError("unexpected arXiv request")
            cached_client._session = MagicMock()
            with patch(
                "acquire_mini_corpus.make_arxiv_client",
                return_value=cached_client,
            ):
                cached_candidates, cached_attempts = collect_arxiv_candidates(
                    [query],
                    max_results_per_query=5,
                    scope=scope,
                    cache_dir=cache_dir,
                    refresh=False,
                )

        self.assertEqual(cached_candidates, first_candidates)
        self.assertEqual(first_attempts[0].status, "ok")
        self.assertEqual(cached_attempts[0].message, "cache-hit")
        cached_client.results.assert_not_called()
        first_client._session.close.assert_called_once_with()
        cached_client._session.close.assert_called_once_with()

    def test_candidate_cache_identity_includes_filtering_rules(self) -> None:
        scope = {
            "recent_from_year": 2020,
            "foundation_from_year": 2000,
            "foundation_before_year": 2020,
            "foundation_limit": 0,
            "arxiv_categories": ["cs.IR"],
            "exclude_title_prefixes": ["erratum"],
        }
        query = {
            "id": "q01",
            "label": "Synthetic query",
            "phrases": ["synthetic retrieval"],
            "date_lane": "recent",
        }
        result = SimpleNamespace(
            title="Synthetic retrieval paper",
            summary="Synthetic abstract",
            published=datetime(2024, 1, 2, tzinfo=UTC),
            journal_ref=None,
            doi=None,
            categories=["cs.IR"],
            pdf_url="https://arxiv.org/pdf/2401.00001",
            get_short_id=lambda: "2401.00001",
        )

        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            first_client = MagicMock()
            first_client.results.return_value = [result]
            first_client._session = MagicMock()
            with patch(
                "acquire_mini_corpus.make_arxiv_client",
                return_value=first_client,
            ):
                collect_arxiv_candidates(
                    [query],
                    max_results_per_query=5,
                    scope=scope,
                    cache_dir=cache_dir,
                    refresh=False,
                )

            changed_scope = dict(scope)
            changed_scope["exclude_title_prefixes"] = ["erratum", "synthetic"]
            second_client = MagicMock()
            second_client.results.return_value = [result]
            second_client._session = MagicMock()
            with patch(
                "acquire_mini_corpus.make_arxiv_client",
                return_value=second_client,
            ):
                filtered, _ = collect_arxiv_candidates(
                    [query],
                    max_results_per_query=5,
                    scope=changed_scope,
                    cache_dir=cache_dir,
                    refresh=False,
                )

        second_client.results.assert_called_once()
        self.assertEqual(filtered, [])


if __name__ == "__main__":
    unittest.main()
