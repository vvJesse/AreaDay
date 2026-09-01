from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import corpus_analysis  # noqa: E402
import lexical_assets  # noqa: E402


class FakeToken:
    def __init__(
        self,
        text: str,
        lemma: str,
        pos: str,
        *,
        is_stop: bool = False,
    ) -> None:
        self.text = text
        self.lemma_ = lemma
        self.pos_ = pos
        self.is_stop = is_stop
        self.is_alpha = text.isalpha()
        self.like_num = False
        self.is_punct = False


class FakeSentence:
    def __init__(self, text: str, tokens: list[FakeToken], start: int) -> None:
        self.text = text
        self.tokens = tokens
        self.start = start
        self.end = start + len(tokens)

    def __iter__(self):
        return iter(self.tokens)


class FakeNounChunk:
    def __init__(
        self,
        text: str,
        tokens: list[FakeToken],
        sentence: FakeSentence,
    ) -> None:
        self.text = text
        self.tokens = tokens
        self.sent = sentence

    def __iter__(self):
        return iter(self.tokens)


class FakeDoc:
    def __init__(
        self,
        sentence: FakeSentence,
        noun_chunks: list[FakeNounChunk],
    ) -> None:
        self.tokens = sentence.tokens
        self.sents = [sentence]
        self.noun_chunks = noun_chunks

    def __iter__(self):
        return iter(self.tokens)

    def __len__(self) -> int:
        return len(self.tokens)


class PipeOnlyNLP:
    def __init__(self, parsed_by_text: dict[str, FakeDoc]) -> None:
        self.parsed_by_text = parsed_by_text
        self.seen_texts: list[str] = []
        self.pipe_calls: list[dict[str, int]] = []

    def __call__(self, text: str):
        raise AssertionError("build_lexical_assets must use the streaming pipe")

    def pipe(self, texts, *, batch_size: int, n_process: int):
        self.pipe_calls.append(
            {"batch_size": batch_size, "n_process": n_process}
        )
        for text in texts:
            self.seen_texts.append(text)
            yield self.parsed_by_text[text]


def fake_parsed_chunk(
    sentence_text: str,
    model_surface: str,
    term_surface: str,
    start: int,
) -> FakeDoc:
    model = FakeToken(model_surface, "model", "NOUN")
    support = FakeToken("supports", "support", "VERB")
    robust = FakeToken("robust", "robust", "ADJ")
    analysis = FakeToken("analysis", "analysis", "NOUN")
    evidence = FakeToken("evidence", "evidence", "NOUN")
    sentence = FakeSentence(
        sentence_text,
        [model, support, robust, analysis, evidence],
        start,
    )
    noun_chunk = FakeNounChunk(term_surface, [robust, analysis], sentence)
    return FakeDoc(sentence, [noun_chunk])


def vocabulary_record() -> dict[str, object]:
    return {
        "lemma": "model",
        "part_of_speech": "NOUN",
        "total_count": 3,
        "frequency_per_million": 100000.0,
        "document_count": 2,
        "document_share": 1.0,
        "dispersion": 1.0,
        "per_document_counts": {"W1": 2, "W2": 1},
        "surface_forms": [{"form": "model", "count": 3}],
        "representative_sentences": [],
        "source_papers": ["W1", "W2"],
    }


class LexicalPipeTests(unittest.TestCase):
    def test_streaming_pipe_preserves_legacy_counts_and_encounter_order(self) -> None:
        chunks = {
            "document-one": ["first chunk", "second chunk"],
            "document-two": ["third chunk"],
            "empty-document": [],
        }
        parsed_by_text = {
            "first chunk": fake_parsed_chunk(
                "Model supports robust analysis with enough contextual evidence.",
                "Model",
                "Robust analysis",
                0,
            ),
            "second chunk": fake_parsed_chunk(
                "MODEL supports robust analysis in a second contextual example.",
                "MODEL",
                "robust analysis",
                10,
            ),
            "third chunk": fake_parsed_chunk(
                "Models support robust analysis across another evidence source.",
                "Models",
                "robust analyses",
                20,
            ),
        }
        nlp = PipeOnlyNLP(parsed_by_text)
        documents = [
            {"openalex_id": "W1", "clean_text": "document-one"},
            {"openalex_id": "W2", "clean_text": "document-two"},
            {"openalex_id": "W3", "clean_text": "empty-document"},
        ]

        with (
            patch.object(
                lexical_assets,
                "text_chunks",
                side_effect=lambda text: iter(chunks[text]),
            ),
            patch.object(
                lexical_assets,
                "_sentence_text",
                wraps=lexical_assets._sentence_text,
            ) as sentence_text,
        ):
            result = lexical_assets.build_lexical_assets(documents, nlp=nlp)

        self.assertEqual(nlp.seen_texts, ["first chunk", "second chunk", "third chunk"])
        self.assertEqual(
            nlp.pipe_calls,
            [{"batch_size": lexical_assets.SPACY_PIPE_BATCH_SIZE, "n_process": 1}],
        )
        self.assertEqual(sentence_text.call_count, 3)
        self.assertEqual(result["included_document_count"], 3)
        self.assertEqual(result["processed_spacy_token_count"], 15)

        vocabulary = {record["lemma"]: record for record in result["vocabulary"]}
        model = vocabulary["model"]
        self.assertEqual(model["total_count"], 3)
        self.assertEqual(model["per_document_counts"], {"W1": 2, "W2": 1})
        self.assertEqual(
            model["surface_forms"],
            [
                {"form": "Model", "count": 1},
                {"form": "MODEL", "count": 1},
                {"form": "Models", "count": 1},
            ],
        )
        self.assertEqual(
            [item["openalex_id"] for item in model["representative_sentences"]],
            ["W1", "W1", "W2"],
        )

        terms = {record["term"]: record for record in result["terminology_candidates"]}
        robust_analysis = terms["robust analysis"]
        self.assertEqual(robust_analysis["total_count"], 3)
        self.assertEqual(
            robust_analysis["surface_forms"],
            [
                {"form": "Robust analysis", "count": 1},
                {"form": "robust analysis", "count": 1},
                {"form": "robust analyses", "count": 1},
            ],
        )
        self.assertEqual(
            [item["openalex_id"] for item in robust_analysis["representative_sentences"]],
            ["W1", "W1", "W2"],
        )


class SerializationAliasTests(unittest.TestCase):
    def test_analysis_serializes_each_alias_group_once_and_copies_bytes(self) -> None:
        assets = {
            "vocabulary": [vocabulary_record()],
            "terminology_candidates": [],
            "included_document_count": 1,
            "processed_spacy_token_count": 5,
            "content_lemma_token_count": 3,
            "minimum_document_count": 1,
        }
        selection = {
            "included": [{"openalex_id": "W1", "clean_text": "synthetic text"}],
            "duplicate_count": 0,
            "low_relevance_count": 0,
            "relevance_cutoff": None,
        }

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            pdf_path = workspace / "papers" / "W1.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-1.4\n")
            candidates = [{"openalex_id": "W1", "title": "Synthetic paper"}]
            downloads = [
                {
                    "openalex_id": "W1",
                    "status": "downloaded",
                    "local_pdf": str(pdf_path),
                }
            ]

            with (
                patch.object(
                    corpus_analysis,
                    "select_analysis_documents",
                    return_value=selection,
                ),
                patch.object(
                    corpus_analysis,
                    "build_lexical_assets",
                    return_value=assets,
                ),
                patch.object(
                    corpus_analysis,
                    "build_orthography_review_candidates",
                    return_value=[],
                ),
                patch.object(
                    corpus_analysis,
                    "_write_vocabulary_tsv",
                    wraps=corpus_analysis._write_vocabulary_tsv,
                ) as write_vocabulary_tsv,
                patch.object(
                    corpus_analysis,
                    "write_jsonl",
                    wraps=corpus_analysis.write_jsonl,
                ) as write_jsonl,
            ):
                corpus_analysis.analyze_corpus(
                    candidates,
                    downloads,
                    workspace,
                    text_extractor=lambda _path: ("synthetic text", 1),
                )

            analysis = workspace / "analysis"
            self.assertEqual(write_vocabulary_tsv.call_count, 1)
            vocabulary_jsonl_writes = [
                Path(call.args[0]).name
                for call in write_jsonl.call_args_list
                if "vocabulary" in Path(call.args[0]).name
            ]
            self.assertEqual(
                vocabulary_jsonl_writes,
                ["pre-orthography-vocabulary-map.jsonl"],
            )
            paper_jsonl_writes = [
                Path(call.args[0]).name
                for call in write_jsonl.call_args_list
                if Path(call.args[0]).name in {"paper-decisions.jsonl", "papers.jsonl"}
            ]
            self.assertEqual(paper_jsonl_writes, ["paper-decisions.jsonl"])

            vocabulary_tsv_paths = [
                analysis / "pre-orthography-vocabulary-map.tsv",
                analysis / "vocabulary-map.tsv",
                analysis / "vocabulary.tsv",
            ]
            self.assertEqual(
                len({path.read_bytes() for path in vocabulary_tsv_paths}),
                1,
            )
            self.assertEqual(
                (analysis / "pre-orthography-vocabulary-map.jsonl").read_bytes(),
                (analysis / "vocabulary-map.jsonl").read_bytes(),
            )
            self.assertEqual(
                (analysis / "paper-decisions.jsonl").read_bytes(),
                (analysis / "papers.jsonl").read_bytes(),
            )

            vocabulary_map_before = (analysis / "vocabulary-map.tsv").read_bytes()
            (analysis / "pre-orthography-vocabulary-map.tsv").write_text(
                "changed after copying\n",
                encoding="utf-8",
            )
            self.assertEqual(
                (analysis / "vocabulary-map.tsv").read_bytes(),
                vocabulary_map_before,
            )


if __name__ == "__main__":
    unittest.main()
