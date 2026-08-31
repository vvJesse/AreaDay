from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from academic_text import clean_academic_text  # noqa: E402
from acquire_mini_corpus import (  # noqa: E402
    _candidate_from_openalex,
    download_candidates,
    merge_candidates,
)
from continuous_workflow import _context_window, _verify_pdf_identity  # noqa: E402
from corpus_analysis import analyze_corpus, extract_pdf_text  # noqa: E402
from corpus_selection import select_analysis_documents  # noqa: E402
from research_profile import ProfileValidationError, validate_profile  # noqa: E402
from researchramp_core import extract_arxiv_id  # noqa: E402
from setup_dependencies import (  # noqa: E402
    CHINA_HF_MIRROR,
    OFFICIAL_HF_ENDPOINT,
    embedding_download_endpoints,
    venv_python,
)


def valid_test_profile() -> dict[str, object]:
    """Return an intentionally synthetic profile used only by validation tests."""
    return {
        "version": 1,
        "profile_id": "synthetic-test-domain-v1",
        "confirmed": True,
        "title": "Synthetic test domain",
        "user_statement": "Synthetic fixture used to test profile validation.",
        "research_summary": "A deliberately generic profile with no user domain data.",
        "clarifications": [
            {"question": "Synthetic question one?", "answer": "Synthetic answer one."},
            {"question": "Synthetic question two?", "answer": "Synthetic answer two."},
            {"question": "Synthetic question three?", "answer": "Synthetic answer three."},
        ],
        "search_queries": [
            {"id": "q01", "label": "Example A", "query": "synthetic research example"},
            {"id": "q02", "label": "Example B", "query": "generic scholarly fixture"},
            {"id": "q03", "label": "Example C", "query": "test domain literature"},
        ],
        "retrieval_scope": {
            "confirmed": True,
            "providers": ["openalex"],
            "openalex_primary_filter": {
                "level": "field",
                "ids": ["test-field"],
                "labels": ["Synthetic Test Field"],
            },
            "language": "en",
            "recent_from_year": 2020,
            "foundation_from_year": 2000,
            "foundation_before_year": 2020,
            "foundation_limit": 0,
            "exclude_title_prefixes": ["erratum", "correction", "withdrawn"],
        },
        "arxiv_search_queries": [],
    }


def write_fake_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% deterministic test file\n")


class ProfileAndDiscoveryTests(unittest.TestCase):
    def test_example_profile_is_confirmed_and_valid(self) -> None:
        validate_profile(valid_test_profile())

    def test_unconfirmed_profile_is_rejected(self) -> None:
        profile = valid_test_profile()
        profile["confirmed"] = False
        with self.assertRaises(ProfileValidationError):
            validate_profile(profile)

    def test_arxiv_id_is_extracted_from_explicit_location(self) -> None:
        work = {
            "best_oa_location": {
                "landing_page_url": "https://arxiv.org/abs/2401.12345v2"
            }
        }
        self.assertEqual(extract_arxiv_id(work), "2401.12345")

    def test_openalex_keyless_mode_requires_a_public_download_route(self) -> None:
        work = {
            "id": "https://openalex.org/W123",
            "title": "A cross-disciplinary paper",
            "open_access": {"is_oa": True, "oa_status": "green"},
            "has_content": {"pdf": True},
        }
        query = {"id": "q1", "label": "Core", "query": "target trial emulation"}
        self.assertIsNone(
            _candidate_from_openalex(work, query, api_key_configured=False)
        )
        self.assertIsNotNone(
            _candidate_from_openalex(work, query, api_key_configured=True)
        )

    def test_openalex_content_is_a_keyed_download_fallback(self) -> None:
        candidate = {
            "candidate_id": "OpenAlex:W123",
            "provider": "openalex",
            "title": "Cached open paper",
            "pdf_url": "",
            "openalex_id": "W123",
            "has_openalex_pdf": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "acquire_mini_corpus.download_openalex_content_pdf",
                side_effect=lambda work_id, key, destination: write_fake_pdf(destination),
            ):
                results = download_candidates(
                    [candidate],
                    Path(temporary),
                    target_papers=1,
                    openalex_api_key="test-key",
                )
        self.assertEqual(results[0]["status"], "downloaded")
        self.assertEqual(results[0]["provider"], "openalex-content")

    def test_same_title_with_conflicting_doi_is_never_merged(self) -> None:
        common = {
            "title": "A shared title",
            "query_hits": [{"query_id": "q1"}],
            "alternate_pdf_urls": [],
            "has_openalex_pdf": False,
        }
        left = {
            **common,
            "candidate_id": "OpenAlex:W1",
            "provider": "openalex",
            "doi": "10.1000/a",
            "pdf_url": "https://example.invalid/a.pdf",
        }
        right = {
            **common,
            "candidate_id": "S2:B",
            "provider": "semantic-scholar",
            "doi": "10.1000/b",
            "pdf_url": "https://example.invalid/b.pdf",
        }
        merged = merge_candidates([left], [right])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].get("alternate_pdf_urls"), [])

    def test_downloaded_pdf_identity_rejects_wrong_front_matter(self) -> None:
        candidate = {
            "title": "Target Trial Emulation with Electronic Health Records",
            "doi": "10.1000/correct",
        }
        correct = _verify_pdf_identity(
            candidate,
            "Target Trial Emulation with Electronic Health Records\nDOI: 10.1000/correct",
        )
        wrong = _verify_pdf_identity(
            candidate,
            "A Different Clinical Study\nDOI: 10.1000/wrong\nunrelated content",
        )
        self.assertTrue(correct["verified"])
        self.assertFalse(wrong["verified"])
        self.assertIn("different DOI", wrong["reason"])


class CorpusAnalysisTests(unittest.TestCase):
    def test_long_source_context_keeps_the_target_word_visible(self) -> None:
        sentence = ("background " * 70) + "targets appear here."
        start = sentence.index("targets")
        context = _context_window(sentence, start, start + len("targets"))
        self.assertLessEqual(len(context), 506)
        self.assertIn("targets", context)

    def test_installed_pdf_reader_extracts_a_local_pdf(self) -> None:
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as temporary:
            pdf_path = Path(temporary) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with pdf_path.open("wb") as handle:
                writer.write(handle)
            text, pages = extract_pdf_text(pdf_path)
            self.assertEqual(text, "")
            self.assertEqual(pages, 1)

    def test_analysis_writes_complete_document_frequency_table(self) -> None:
        candidates = [
            {"openalex_id": "W1", "title": "One"},
            {"openalex_id": "W2", "title": "Two"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first = workspace / "papers" / "W1.pdf"
            second = workspace / "papers" / "W2.pdf"
            write_fake_pdf(first)
            write_fake_pdf(second)
            results = [
                {"openalex_id": "W1", "status": "downloaded", "local_pdf": str(first)},
                {"openalex_id": "W2", "status": "downloaded", "local_pdf": str(second)},
            ]

            def fake_extract(path: Path) -> tuple[str, int]:
                if path.name == "W1.pdf":
                    return "Evidence supports an implied claim. Evidence matters.", 2
                return "An implied claim can mislead.", 1

            stats = analyze_corpus(
                candidates, results, workspace, text_extractor=fake_extract
            )
            self.assertEqual(stats["analyzed_pdf_count"], 2)
            with (workspace / "analysis" / "vocabulary.tsv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = {row["token"]: row for row in csv.DictReader(handle, delimiter="\t")}
            self.assertEqual(rows["evidence"]["total_count"], "2")
            self.assertEqual(rows["evidence"]["document_count"], "1")
            self.assertEqual(rows["imply"]["document_count"], "2")

    def test_academic_cleaner_keeps_body_and_stops_at_references(self) -> None:
        raw = """Conference Header 2026
Author Name
University Department
Abstract
Synthetic statements provide useful test content.
Figure 1: A useful framework.
\fConference Header 2027
1 Introduction
The framework separates source content from derived analysis.
Figure 2: A useful framework.
\fConference Header 2028
2 Results
Evidence can support or reject the test claim.
References
Smith, A. 2020. An unrelated title.
"""
        cleaned, diagnostics = clean_academic_text(raw)
        self.assertNotIn("Author Name", cleaned)
        self.assertNotIn("Conference Header", cleaned)
        self.assertNotIn("Smith, A.", cleaned)
        self.assertIn("Synthetic statements", cleaned)
        self.assertIn("Evidence can support", cleaned)
        self.assertEqual(cleaned.count("A useful framework"), 1)
        self.assertTrue(diagnostics["abstract_heading_found"])
        self.assertTrue(diagnostics["stopped_at_references"])

    def test_exact_duplicate_keeps_better_extracted_version(self) -> None:
        documents = [
            {
                "openalex_id": "W1",
                "title": "Synthetic Duplicate Study",
                "doi": "10.1/same",
                "status": "extracted",
                "body_word_count": 200,
            },
            {
                "openalex_id": "W2",
                "title": "Synthetic duplicate study",
                "doi": "https://doi.org/10.1/same",
                "status": "extracted",
                "body_word_count": 800,
            },
        ]
        selection = select_analysis_documents(documents, None)
        self.assertEqual([item["openalex_id"] for item in selection["included"]], ["W2"])
        self.assertEqual(documents[0]["analysis_decision"], "duplicate")
        self.assertEqual(documents[0]["duplicate_of"], "W2")

    def test_relevance_filter_only_excludes_extreme_low_tail(self) -> None:
        profile = {
            "title": "Synthetic domain relevance benchmark",
            "search_queries": [
                {"query": "synthetic domain relevance"},
                {"query": "generic research benchmark"},
            ],
        }
        repeated_abstract = " ".join(["relevant"] * 25)
        documents = [
            {
                "openalex_id": f"W{index}",
                "title": f"Unique relevant paper {index}",
                "abstract": repeated_abstract,
                "status": "extracted",
                "body_word_count": 500,
            }
            for index in range(7)
        ]
        documents.append(
            {
                "openalex_id": "W-low",
                "title": "Unrelated crystal lattice measurement",
                "abstract": " ".join(["crystal"] * 25),
                "status": "extracted",
                "body_word_count": 500,
            }
        )

        def fake_embeddings(texts: list[str]) -> np.ndarray:
            return np.asarray(
                [[0.0, 1.0] if "crystal" in text.casefold() else [1.0, 0.0] for text in texts],
                dtype=np.float32,
            )

        selection = select_analysis_documents(
            documents,
            profile,
            embedding_fn=fake_embeddings,
        )
        self.assertEqual(selection["low_relevance_count"], 1)
        self.assertEqual(documents[-1]["analysis_decision"], "low-relevance")
        self.assertEqual(len(selection["included"]), 7)


class CrossPlatformSetupTests(unittest.TestCase):
    def test_runtime_paths_are_platform_specific(self) -> None:
        root = Path("runtime")
        self.assertEqual(venv_python(root, "win32"), root / "Scripts" / "python.exe")
        self.assertEqual(venv_python(root, "darwin"), root / "bin" / "python")

    def test_embedding_source_fallback_respects_explicit_configuration(self) -> None:
        self.assertEqual(
            embedding_download_endpoints(None, None),
            [OFFICIAL_HF_ENDPOINT, CHINA_HF_MIRROR],
        )
        self.assertEqual(
            embedding_download_endpoints(
                "https://assets.example",
                "https://internal.example",
            ),
            [
                "https://assets.example",
                "https://internal.example",
                OFFICIAL_HF_ENDPOINT,
                CHINA_HF_MIRROR,
            ],
        )


if __name__ == "__main__":
    unittest.main()
