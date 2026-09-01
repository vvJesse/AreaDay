#!/usr/bin/env python3
"""Run ResearchRamp's unified local application."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import mimetypes
import os
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np
from wordfreq import zipf_frequency


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from continuous_state import ContinuousStore  # noqa: E402
from global_learning import GlobalLearningStore  # noqa: E402
from domain_registry import (  # noqa: E402
    DomainRegistration,
    DomainRegistry,
    validate_completed_workspace,
    validate_corpus_launch_workspace,
    validate_initialized_workspace,
)
from workbench_protocol import (  # noqa: E402
    WORKBENCH_IDENTITY_PATH,
    WORKBENCH_IDENTITY_VERSION,
    WORKBENCH_SERVICE,
)


QUESTION_LIMIT = 30
APP_API_VERSION = 5
DEFAULT_KNOWN_THRESHOLD = 0.90
MIN_KNOWN_THRESHOLD = 0.75
MAX_KNOWN_THRESHOLD = 0.98
IMPORTANT_BOUNDARY_MARGIN = 0.05
UNKNOWN_THRESHOLD = 0.30
THETA_GRID = np.linspace(-5.0, 5.0, 801)

# A B2 learner has probably encountered lower-CEFR vocabulary, but CEFR list
# membership is only weak evidence of personal knowledge. Adjustments are made
# on the log-odds scale so they refine the frequency prior without replacing it.
CEFR_LOGIT_ADJUSTMENTS = {
    "A1": 0.80,
    "A2": 0.60,
    "B1": 0.35,
    "B2": 0.15,
}
CEFR_UNLISTED_ADJUSTMENT = 0.0
CEFR_LEVEL_ORDER = {"A1": 0, "A2": 1, "B1": 2, "B2": 3}
EXAM_LOGIT_ADJUSTMENTS = {
    "gk": 0.30,
    "cet4": 0.20,
    "cet6": 0.10,
}
EXAM_PROFILE_TAGS = {
    "none": set(),
    "gaokao": {"gk"},
    "cet4": {"gk", "cet4"},
    "cet6": {"gk", "cet4", "cet6"},
}


@dataclass(frozen=True)
class Word:
    lemma: str
    part_of_speech: str
    total_count: int
    document_count: int
    document_share: float
    zipf: float
    frequency_prior_probability: float
    cefr_level: str | None
    cefr_adjustment: float
    exam_tags: tuple[str, ...]
    exam_adjustment: float
    education_adjustment: float
    prior_probability: float


def sigmoid(value: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -30.0, 30.0)))


def logit(probability: float) -> float:
    probability = min(max(probability, 1e-6), 1.0 - 1e-6)
    return math.log(probability / (1.0 - probability))


def frequency_prior_probability(lemma: str, zipf: float) -> float:
    """The original, deliberately simple population prior.

    Word frequency provides most of the signal; length only makes a small
    correction. The user's answers calibrate the person-specific ability shift.
    """

    length_penalty = 0.075 * max(len(lemma) - 7, 0)
    return float(sigmoid(1.55 * (zipf - 3.65) - length_penalty))


def load_cefr_levels(path: Path) -> dict[str, str]:
    """Load the bundled commercial-use CEFR-J mapping."""

    levels: dict[str, str] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if set(reader.fieldnames or []) != {"headword", "cefr"}:
            raise ValueError("CEFR-J data must contain headword and cefr columns")
        for row in reader:
            headword = row["headword"].strip().lower()
            level = row["cefr"].strip().upper()
            if not headword or level not in CEFR_LEVEL_ORDER:
                continue
            previous = levels.get(headword)
            if previous is None or CEFR_LEVEL_ORDER[level] < CEFR_LEVEL_ORDER[previous]:
                levels[headword] = level
    return levels


def load_exam_tags(path: Path) -> dict[str, tuple[str, ...]]:
    """Load ECDICT's GaoKao/CET exposure labels without dictionary content."""

    tags_by_word: dict[str, tuple[str, ...]] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if set(reader.fieldnames or []) != {"headword", "tags"}:
            raise ValueError("ECDICT exam data must contain headword and tags columns")
        for row in reader:
            headword = row["headword"].strip().lower()
            tags = tuple(
                tag for tag in row["tags"].split() if tag in EXAM_LOGIT_ADJUSTMENTS
            )
            if headword and tags:
                tags_by_word[headword] = tags
    return tags_by_word


def load_words(
    path: Path,
    cefr_levels: dict[str, str] | None = None,
    exam_tags: dict[str, tuple[str, ...]] | None = None,
    exam_profile: str = "none",
) -> list[Word]:
    words: list[Word] = []
    use_education_prior = cefr_levels is not None or exam_tags is not None
    cefr_levels = cefr_levels or {}
    exam_tags = exam_tags or {}
    allowed_exam_tags = EXAM_PROFILE_TAGS[exam_profile]
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"lemma", "part_of_speech", "total_count", "document_count", "document_share"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Vocabulary file is missing columns: {sorted(missing)}")
        for row in reader:
            lemma = row["lemma"].strip().lower()
            if not lemma:
                continue
            zipf = float(zipf_frequency(lemma, "en"))
            frequency_prior = frequency_prior_probability(lemma, zipf)
            cefr_level = cefr_levels.get(lemma)
            cefr_adjustment = (
                CEFR_LOGIT_ADJUSTMENTS[cefr_level]
                if cefr_level is not None
                else 0.0
            )
            matched_exam_tags = tuple(
                tag for tag in exam_tags.get(lemma, ()) if tag in allowed_exam_tags
            )
            exam_adjustment = max(
                (EXAM_LOGIT_ADJUSTMENTS[tag] for tag in matched_exam_tags),
                default=0.0,
            )
            positive_adjustments = [
                value for value in (cefr_adjustment, exam_adjustment) if value > 0
            ]
            education_adjustment = (
                max(positive_adjustments)
                if positive_adjustments
                else CEFR_UNLISTED_ADJUSTMENT if use_education_prior else 0.0
            )
            adjusted_prior = float(
                sigmoid(logit(frequency_prior) + education_adjustment)
            )
            words.append(
                Word(
                    lemma=lemma,
                    part_of_speech=row["part_of_speech"],
                    total_count=int(row["total_count"]),
                    document_count=int(row["document_count"]),
                    document_share=float(row["document_share"]),
                    zipf=zipf,
                    frequency_prior_probability=frequency_prior,
                    cefr_level=cefr_level,
                    cefr_adjustment=cefr_adjustment,
                    exam_tags=matched_exam_tags,
                    exam_adjustment=exam_adjustment,
                    education_adjustment=education_adjustment,
                    prior_probability=adjusted_prior,
                )
            )
    if not words:
        raise ValueError("Vocabulary file contained no words")
    return words


class CalibrationSession:
    def __init__(
        self,
        words: list[Word],
        state_path: Path,
        corpus_label: str,
        *,
        result_path: Path | None = None,
        export_path: Path | None = None,
        enforce_snapshot_match: bool = False,
    ):
        self.words = words
        self.by_lemma = {word.lemma: word for word in words}
        if len(self.by_lemma) < QUESTION_LIMIT:
            raise ValueError(
                "Vocabulary calibration requires at least 30 unique lemmas"
            )
        self.state_path = state_path
        self._result_path = result_path or state_path.with_name(
            "vocabulary-calibration-result.json"
        )
        self._export_path = export_path or state_path.with_name(
            "personalized-vocabulary.tsv"
        )
        self.enforce_snapshot_match = enforce_snapshot_match
        self.corpus_label = corpus_label
        self.answers: list[dict[str, Any]] = []
        self.known_threshold = DEFAULT_KNOWN_THRESHOLD
        self.mutation_revision = 0
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._frozen_result: dict[str, Any] | None = None
        self._load()
        if len(self.answers) >= QUESTION_LIMIT:
            self._load_frozen_result()
            if self._frozen_result is None:
                raise RuntimeError(
                    "已完成的个人词表产物缺失或不一致；为避免按当前原始词表静默重算，"
                    "ResearchRamp 已停止载入。请恢复该领域的结果与导出文件，或由用户明确重新校准。"
                )

    @property
    def result_path(self) -> Path:
        return self._result_path

    @property
    def export_path(self) -> Path:
        return self._export_path

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            answers = data.get("answers", [])
            if not isinstance(answers, list):
                raise ValueError("answers must be a list")
            valid = []
            seen: set[str] = set()
            for answer in answers:
                if not isinstance(answer, dict):
                    raise ValueError("every calibration answer must be an object")
                lemma = answer.get("lemma")
                response = answer.get("response")
                if lemma not in self.by_lemma:
                    raise ValueError(f"calibration answer is absent from its Vocabulary Map: {lemma}")
                if response not in {"known", "unknown", "unsure"}:
                    raise ValueError(f"invalid calibration response for {lemma}")
                if lemma in seen:
                    raise ValueError(f"duplicate calibration answer: {lemma}")
                valid.append(answer)
                seen.add(lemma)
            self.answers = valid[:QUESTION_LIMIT]
            threshold = float(data.get("known_threshold", DEFAULT_KNOWN_THRESHOLD))
            if MIN_KNOWN_THRESHOLD <= threshold <= MAX_KNOWN_THRESHOLD:
                self.known_threshold = round(threshold, 2)
            self.started_at = float(data.get("started_at", self.started_at))
            revision = data.get("mutation_revision", 0)
            if isinstance(revision, int) and revision >= 0:
                self.mutation_revision = revision
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"词汇校准状态损坏，已停止载入：{self.state_path}"
            ) from error

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "started_at": self.started_at,
            "updated_at": time.time(),
            "question_limit": QUESTION_LIMIT,
            "known_threshold": self.known_threshold,
            "mutation_revision": self.mutation_revision,
            "answers": self.answers,
        }
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _load_frozen_result(self) -> None:
        """Preserve a completed vocabulary decision until the user changes it."""

        if not self.result_path.is_file() or not self.export_path.is_file():
            return
        try:
            result = json.loads(self.result_path.read_text(encoding="utf-8"))
            if not isinstance(result.get("counts"), dict) or not isinstance(
                result.get("importance"), dict
            ):
                return
            if not isinstance(result.get("threshold"), dict):
                return
            if "important_boundary_protected" not in result["counts"]:
                return
            saved_snapshot = str(result.get("vocabulary_snapshot_sha256") or "")
            saved_export_hash = str(
                result.get("personalized_vocabulary_sha256") or ""
            )
            if not saved_snapshot or not saved_export_hash:
                return
            expected_rows = int(result["counts"].get("total") or 0)
            export_bytes = self.export_path.read_bytes()
            export_rows = max(0, export_bytes.count(b"\n") - 1)
            if expected_rows <= 0 or export_rows != expected_rows:
                return
            if hashlib.sha256(export_bytes).hexdigest() != saved_export_hash:
                return
            if (
                self.enforce_snapshot_match
                and saved_snapshot != self.vocabulary_snapshot_sha256()
            ):
                return
            self._frozen_result = result
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._frozen_result = None

    def _write_final_outputs(self) -> None:
        result = self.result()
        result["completed_at"] = time.time()
        result["answers"] = self.answers
        result["vocabulary_snapshot_sha256"] = self.vocabulary_snapshot_sha256()
        export_content = self.export_tsv()
        result["personalized_vocabulary_sha256"] = hashlib.sha256(
            export_content.encode("utf-8")
        ).hexdigest()
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        result_temporary = self.result_path.with_suffix(self.result_path.suffix + ".tmp")
        export_temporary = self.export_path.with_suffix(self.export_path.suffix + ".tmp")
        result_temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        export_temporary.write_text(export_content, encoding="utf-8")
        os.replace(export_temporary, self.export_path)
        os.replace(result_temporary, self.result_path)
        self._frozen_result = result

    def vocabulary_snapshot_sha256(self) -> str:
        snapshot = "\n".join(
            f"{word.lemma}\t{word.total_count}\t{word.document_count}"
            for word in sorted(self.words, key=lambda item: item.lemma)
        )
        return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    def posterior(self) -> np.ndarray:
        # A moderate prior keeps the first few predictions from becoming extreme.
        log_weights = -0.5 * (THETA_GRID / 1.5) ** 2
        for answer in self.answers:
            if answer["response"] == "unsure":
                continue
            word = self.by_lemma[answer["lemma"]]
            probabilities = sigmoid(logit(word.prior_probability) + THETA_GRID)
            if answer["response"] == "known":
                log_weights += np.log(np.clip(probabilities, 1e-12, 1.0))
            else:
                log_weights += np.log(np.clip(1.0 - probabilities, 1e-12, 1.0))
        log_weights -= float(np.max(log_weights))
        weights = np.exp(log_weights)
        return weights / float(np.sum(weights))

    def probabilities(self) -> dict[str, float]:
        posterior = self.posterior()
        result: dict[str, float] = {}
        for word in self.words:
            conditional = sigmoid(logit(word.prior_probability) + THETA_GRID)
            result[word.lemma] = float(np.sum(conditional * posterior))
        # A direct self-report is stronger evidence than the population model.
        # Keep "unsure" model-derived, but never contradict an explicit answer.
        for answer in self.answers:
            if answer["response"] == "known":
                result[answer["lemma"]] = 1.0
            elif answer["response"] == "unknown":
                result[answer["lemma"]] = 0.0
        return result

    def theta_summary(self) -> tuple[float, float, float]:
        posterior = self.posterior()
        cumulative = np.cumsum(posterior)
        mean = float(np.sum(THETA_GRID * posterior))
        lower = float(THETA_GRID[int(np.searchsorted(cumulative, 0.05))])
        upper = float(THETA_GRID[min(int(np.searchsorted(cumulative, 0.95)), len(THETA_GRID) - 1)])
        return mean, lower, upper

    def _seed_question(self, asked: set[str]) -> Word | None:
        targets = [0.93, 0.78, 0.63, 0.48, 0.33, 0.18]
        target = targets[len(self.answers)] if len(self.answers) < len(targets) else None
        if target is None:
            return None
        candidates = [word for word in self.words if word.lemma not in asked]
        return min(
            candidates,
            key=lambda word: (
                abs(word.prior_probability - target),
                -word.document_count,
                -word.total_count,
            ),
        )

    def next_word(self) -> Word | None:
        if len(self.answers) >= QUESTION_LIMIT:
            return None
        asked = {answer["lemma"] for answer in self.answers}
        seed = self._seed_question(asked)
        if seed is not None:
            return seed

        probabilities = self.probabilities()
        max_documents = max(word.document_count for word in self.words)
        pos_counts: dict[str, int] = {}
        for answer in self.answers:
            pos = self.by_lemma[answer["lemma"]].part_of_speech
            pos_counts[pos] = pos_counts.get(pos, 0) + 1

        def score(word: Word) -> float:
            probability = probabilities[word.lemma]
            information = probability * (1.0 - probability)
            relevance = 0.80 + 0.20 * (word.document_count / max_documents)
            diversity = 1.0 / (1.0 + 0.08 * pos_counts.get(word.part_of_speech, 0))
            return information * relevance * diversity

        candidates = [word for word in self.words if word.lemma not in asked]
        return max(candidates, key=score)

    def answer(self, lemma: str, response: str) -> None:
        if response not in {"known", "unknown", "unsure"}:
            raise ValueError("Invalid response")
        with self._lock:
            expected = self.next_word()
            if expected is None:
                raise ValueError("Calibration is already complete")
            if lemma != expected.lemma:
                raise ValueError("This is not the current question")
            self.answers.append({"lemma": lemma, "response": response, "answered_at": time.time()})
            self._save()
            if len(self.answers) >= QUESTION_LIMIT:
                self._write_final_outputs()

    def _accept_mutation_revision(self, revision: int | None) -> bool:
        if revision is None:
            self.mutation_revision += 1
            return True
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("mutation_revision must be a positive integer")
        if revision <= self.mutation_revision:
            return False
        self.mutation_revision = revision
        return True

    def reset(self, mutation_revision: int | None = None) -> None:
        with self._lock:
            if not self._accept_mutation_revision(mutation_revision):
                return
            self.answers = []
            self.known_threshold = DEFAULT_KNOWN_THRESHOLD
            self.started_at = time.time()
            self._frozen_result = None
            self._save()
            self.result_path.unlink(missing_ok=True)
            self.export_path.unlink(missing_ok=True)

    def set_threshold_percent(
        self, threshold_percent: int, mutation_revision: int | None = None
    ) -> None:
        if not 75 <= threshold_percent <= 98:
            raise ValueError("Threshold must be an integer from 75 to 98")
        with self._lock:
            if not self._accept_mutation_revision(mutation_revision):
                return
            if round(self.known_threshold * 100) == threshold_percent:
                self._save()
                return
            self.known_threshold = threshold_percent / 100.0
            self._frozen_result = None
            self._save()
            if len(self.answers) >= QUESTION_LIMIT:
                self._write_final_outputs()

    @staticmethod
    def importance_tier(word: Word) -> str:
        if word.document_count >= 10:
            return "A"
        if word.document_count >= 5:
            return "B"
        if word.document_count >= 3:
            return "C"
        return "D"

    @staticmethod
    def _mastery_summary(
        rows: list[tuple[str, str, str]], mastered_word_forms: set[str]
    ) -> dict[str, Any]:
        group_definitions = (
            ("priority", "重要与核心词", {"A", "B"}),
            ("other", "其他词", {"C", "D"}),
        )
        groups = []
        for key, label, tiers in group_definitions:
            group = [
                lemma
                for lemma, tier, classification in rows
                if tier in tiers and classification != "likely_known"
            ]
            mastered = sum(
                classification == "important_boundary"
                or lemma.casefold() in mastered_word_forms
                for lemma, tier, classification in rows
                if tier in tiers and classification != "likely_known"
            )
            total = len(group)
            groups.append(
                {
                    "key": key,
                    "label": label,
                    "tiers": sorted(tiers),
                    "mastered_count": mastered,
                    "total_count": total,
                    "mastery_percent": round(
                        (mastered / total) * 100, 1
                    )
                    if total
                    else 0.0,
                }
            )
        return {
            "basis": "personal_vocabulary_calibrated_and_confirmed_mastery",
            "groups": groups,
        }

    def personal_vocabulary_mastery(
        self, mastered_word_forms: set[str]
    ) -> dict[str, Any] | None:
        try:
            reader = csv.DictReader(
                self.persisted_export_tsv().splitlines(), delimiter="\t"
            )
            if not reader.fieldnames or not {
                "lemma",
                "importance_tier",
                "classification",
            }.issubset(reader.fieldnames):
                return None
            rows = [
                (
                    str(row.get("lemma") or ""),
                    str(row.get("importance_tier") or ""),
                    str(row.get("classification") or ""),
                )
                for row in reader
            ]
        except (OSError, ValueError, TypeError, csv.Error):
            return None
        return self._mastery_summary(rows, mastered_word_forms)

    def result(self) -> dict[str, Any]:
        probabilities = self.probabilities()
        protected_important = [
            word
            for word in self.words
            if self.importance_tier(word) in {"A", "B"}
            and self.known_threshold <= probabilities[word.lemma]
            < min(1.0, self.known_threshold + IMPORTANT_BOUNDARY_MARGIN)
        ]
        protected_lemmas = {word.lemma for word in protected_important}
        likely_known = [
            word
            for word in self.words
            if probabilities[word.lemma] >= self.known_threshold
            and word.lemma not in protected_lemmas
        ]
        known_lemmas = {word.lemma for word in likely_known}
        retained = [word for word in self.words if word.lemma not in known_lemmas]
        likely_unknown = [
            word for word in retained if probabilities[word.lemma] <= UNKNOWN_THRESHOLD
        ]
        uncertain = [
            word
            for word in retained
            if probabilities[word.lemma] > UNKNOWN_THRESHOLD
        ]
        corpus_document_count = max(
            (
                round(word.document_count / word.document_share)
                for word in self.words
                if word.document_share > 0
            ),
            default=0,
        )

        importance_tiers = [
            {
                "key": "A",
                "name": "核心词",
                "range": "至少出现在 10 篇论文",
                "count": sum(word.document_count >= 10 for word in retained),
            },
            {
                "key": "B",
                "name": "高价值词",
                "range": "出现在 5–9 篇论文",
                "count": sum(5 <= word.document_count <= 9 for word in retained),
            },
            {
                "key": "C",
                "name": "偶发词",
                "range": "出现在 3–4 篇论文",
                "count": sum(3 <= word.document_count <= 4 for word in retained),
            },
            {
                "key": "D",
                "name": "文章局部词",
                "range": "只出现在 2 篇论文",
                "count": sum(word.document_count == 2 for word in retained),
            },
        ]
        priority_word_count = importance_tiers[0]["count"] + importance_tiers[1]["count"]
        occasional_word_count = importance_tiers[2]["count"] + importance_tiers[3]["count"]
        theta, theta_low, theta_high = self.theta_summary()
        direct_responses = {
            answer["lemma"]: answer["response"] for answer in self.answers
        }

        def serialized(word: Word) -> dict[str, Any]:
            return {
                "lemma": word.lemma,
                "part_of_speech": word.part_of_speech,
                "probability_known": round(probabilities[word.lemma], 4),
                "document_count": word.document_count,
                "total_count": word.total_count,
                "zipf": round(word.zipf, 3),
                "frequency_prior_probability": round(word.frequency_prior_probability, 4),
                "cefr_level": word.cefr_level,
                "cefr_adjustment": word.cefr_adjustment,
                "exam_tags": list(word.exam_tags),
                "exam_adjustment": word.exam_adjustment,
                "education_adjustment": word.education_adjustment,
                "direct_response": direct_responses.get(word.lemma),
                "importance_tier": self.importance_tier(word),
                "important_boundary_protected": word.lemma in protected_lemmas,
            }

        # These review lists expose the decision boundary, where mistakes matter most.
        known_boundary = sorted(likely_known, key=lambda word: probabilities[word.lemma])[:30]
        remaining_boundary = sorted(
            retained,
            key=lambda word: abs(probabilities[word.lemma] - self.known_threshold),
        )[:30]
        return {
            "counts": {
                "total": len(self.words),
                "likely_known": len(likely_known),
                "uncertain": len(uncertain),
                "likely_unknown": len(likely_unknown),
                "important_boundary_protected": len(protected_important),
                "remaining_after_conservative_exclusion": len(retained),
            },
            "threshold": {
                "selected_percent": round(self.known_threshold * 100),
                "default_percent": round(DEFAULT_KNOWN_THRESHOLD * 100),
                "minimum_percent": round(MIN_KNOWN_THRESHOLD * 100),
                "maximum_percent": round(MAX_KNOWN_THRESHOLD * 100),
                "step_percent": 1,
                "important_boundary_margin_percent": round(
                    IMPORTANT_BOUNDARY_MARGIN * 100
                ),
            },
            "importance": {
                "corpus_document_count": corpus_document_count,
                "priority_word_count": priority_word_count,
                "occasional_word_count": occasional_word_count,
                "tiers": importance_tiers,
            },
            "theta": {"mean": round(theta, 3), "p05": round(theta_low, 3), "p95": round(theta_high, 3)},
            "prior": {
                "name": (
                    "wordfreq_plus_education_prior"
                    if any(word.education_adjustment for word in self.words)
                    else "wordfreq_only"
                ),
                "cefr_matches": sum(1 for word in self.words if word.cefr_level is not None),
                "exam_matches": sum(1 for word in self.words if word.exam_tags),
            },
            "known_boundary": [serialized(word) for word in known_boundary],
            "remaining_boundary": [serialized(word) for word in remaining_boundary],
        }

    def public_state(self) -> dict[str, Any]:
        current = self.next_word()
        complete = current is None
        payload: dict[str, Any] = {
            "corpus_label": self.corpus_label,
            "threshold": {
                "selected_percent": round(self.known_threshold * 100),
                "default_percent": round(DEFAULT_KNOWN_THRESHOLD * 100),
                "minimum_percent": round(MIN_KNOWN_THRESHOLD * 100),
                "maximum_percent": round(MAX_KNOWN_THRESHOLD * 100),
                "step_percent": 1,
            },
            "answered": len(self.answers),
            "mutation_revision": self.mutation_revision,
            "question_limit": QUESTION_LIMIT,
            "complete": complete,
            "responses": {
                label: sum(1 for answer in self.answers if answer["response"] == label)
                for label in ("known", "unknown", "unsure")
            },
        }
        if current is not None:
            payload["word"] = {
                "lemma": current.lemma,
                "part_of_speech": current.part_of_speech,
            }
        else:
            result = dict(self._frozen_result or self.result())
            result["output_files"] = {
                "result": str(self.result_path),
                "personalized_vocabulary": str(self.export_path),
            }
            payload["result"] = result
        return payload

    def export_tsv(self) -> str:
        probabilities = self.probabilities()
        protected_lemmas = {
            word.lemma
            for word in self.words
            if self.importance_tier(word) in {"A", "B"}
            and self.known_threshold <= probabilities[word.lemma]
            < min(1.0, self.known_threshold + IMPORTANT_BOUNDARY_MARGIN)
        }
        direct_responses = {
            answer["lemma"]: answer["response"] for answer in self.answers
        }
        lines = [
            "lemma\tpart_of_speech\tprobability_known\tclassification\ttotal_count\t"
            "document_count\tzipf\tfrequency_prior_probability\tcefr_level\tcefr_adjustment\t"
            "exam_tags\texam_adjustment\teducation_adjustment\tdirect_response\t"
            "importance_tier\timportant_boundary_protected\tselected_threshold"
        ]
        for word in sorted(self.words, key=lambda item: probabilities[item.lemma], reverse=True):
            probability = probabilities[word.lemma]
            if word.lemma in protected_lemmas:
                classification = "important_boundary"
            elif probability >= self.known_threshold:
                classification = "likely_known"
            elif probability <= UNKNOWN_THRESHOLD:
                classification = "likely_unknown"
            else:
                classification = "uncertain"
            lines.append(
                f"{word.lemma}\t{word.part_of_speech}\t{probability:.6f}\t{classification}\t"
                f"{word.total_count}\t{word.document_count}\t{word.zipf:.3f}\t"
                f"{word.frequency_prior_probability:.6f}\t{word.cefr_level or ''}\t"
                f"{word.cefr_adjustment:.2f}\t{' '.join(word.exam_tags)}\t"
                f"{word.exam_adjustment:.2f}\t{word.education_adjustment:.2f}\t"
                f"{direct_responses.get(word.lemma, '')}\t{self.importance_tier(word)}\t"
                f"{str(word.lemma in protected_lemmas).lower()}\t{self.known_threshold:.2f}"
            )
        return "\n".join(lines) + "\n"

    def persisted_export_tsv(self) -> str:
        """Serve the completed decision exactly as saved until an explicit change."""

        if self._frozen_result is not None and self.export_path.is_file():
            return self.export_path.read_text(encoding="utf-8")
        return self.export_tsv()


@dataclass(frozen=True)
class DomainContext:
    domain_id: str
    display_name: str
    workspace: Path | None
    session: CalibrationSession
    continuous_store: ContinuousStore | None

    def public_descriptor(self) -> dict[str, str]:
        return {
            "domain_id": self.domain_id,
            "display_name": self.display_name,
        }


class AppRuntime:
    """Route every request to one explicitly selected, isolated workspace."""

    def __init__(
        self,
        contexts: list[DomainContext],
        *,
        initial_domain_id: str,
        initial_view: str,
        registry: DomainRegistry | None = None,
        standalone: bool = False,
        instance_id: str | None = None,
    ):
        if not contexts:
            raise ValueError("ResearchRamp requires at least one registered domain")
        self.contexts = {context.domain_id: context for context in contexts}
        if len(self.contexts) != len(contexts):
            raise ValueError("ResearchRamp domain IDs must be unique")
        if initial_domain_id not in self.contexts:
            raise ValueError(f"Unknown initial ResearchRamp domain: {initial_domain_id}")
        self.initial_domain_id = initial_domain_id
        self.initial_view = initial_view
        self.registry = registry
        self.standalone = standalone
        self.instance_id = (instance_id or uuid.uuid4().hex).strip()
        if not self.instance_id:
            raise ValueError("ResearchRamp workbench instance ID cannot be empty")

    def identity(self) -> dict[str, Any]:
        """Return the complete, side-effect-free launcher identity contract."""

        return {
            "service": WORKBENCH_SERVICE,
            "identity_version": WORKBENCH_IDENTITY_VERSION,
            "registry": str(self.registry.path) if self.registry is not None else None,
            "instance_id": self.instance_id,
            "domain_ids": sorted(self.contexts),
        }

    def context(self, domain_id: str | None) -> DomainContext:
        selected = domain_id or self.initial_domain_id
        context = self.contexts.get(selected)
        if context is None:
            raise ValueError(f"Unknown ResearchRamp domain: {selected}")
        return context

    def app_state(self, domain_id: str | None = None) -> dict[str, Any]:
        context = self.context(domain_id)
        store = context.continuous_store
        terms = store.list_terms() if store is not None else []
        return {
            "api_version": APP_API_VERSION,
            "domain_id": context.domain_id,
            "domains": [item.public_descriptor() for item in self.contexts.values()],
            "domain_switching": len(self.contexts) > 1,
            "standalone": self.standalone,
            "initial_view": "vocabulary" if self.standalone else self.initial_view,
            "calibration": self.calibration_state(context),
            "terminology": {
                "count": len(terms),
                "terms": terms,
            }
            if store is not None
            else None,
            "continuous": store.summary() if store is not None else None,
            "settings": store.get_settings() if store is not None else None,
            "briefs": store.list_briefs() if store is not None else [],
        }

    def mastery(self, context: DomainContext) -> dict[str, Any] | None:
        store = context.continuous_store
        if store is None:
            return None
        return context.session.personal_vocabulary_mastery(
            store.learning_store.mastered_word_forms()
        )

    def calibration_state(self, context: DomainContext) -> dict[str, Any]:
        calibration = context.session.public_state()
        if not calibration.get("complete"):
            return calibration
        result = dict(calibration["result"])
        mastery = self.mastery(context)
        if mastery is not None:
            result["mastery"] = mastery
        calibration["result"] = result
        return calibration

    @staticmethod
    def require_continuous(context: DomainContext) -> ContinuousStore:
        if context.continuous_store is None:
            raise ValueError("Standalone vocabulary mode does not include briefs or review")
        return context.continuous_store


class AppHandler(BaseHTTPRequestHandler):
    runtime: AppRuntime
    static_dir: Path
    exit_on_settings_save: bool = False
    settings_saved: bool = False
    settings_saved_domain_id: str | None = None
    settings_saved_section: str | None = None

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_bytes(self, payload: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send_bytes(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _send_api_json(
        self,
        data: dict[str, Any],
        *,
        domain_id: str | None = None,
        status: int = HTTPStatus.OK,
    ) -> None:
        payload = {**data, "api_version": APP_API_VERSION}
        if domain_id is not None:
            payload["domain_id"] = domain_id
        self._send_json(payload, status)

    def _requested_domain_id(
        self, parsed: Any, body: dict[str, Any] | None = None
    ) -> str | None:
        query_value = parse_qs(parsed.query).get("domain_id", [None])[0]
        header_value = self.headers.get("X-ResearchRamp-Domain")
        body_value = (body or {}).get("domain_id")
        candidates = [value for value in (query_value, header_value, body_value) if value]
        if not candidates:
            return None
        if len(set(str(value) for value in candidates)) != 1:
            raise ValueError("Conflicting ResearchRamp domain IDs in one request")
        return str(candidates[0])

    def _context(
        self, parsed: Any, body: dict[str, Any] | None = None
    ) -> DomainContext:
        requested = self._requested_domain_id(parsed, body)
        if len(self.runtime.contexts) > 1 and requested is None:
            raise ValueError("Every scoped request must name its ResearchRamp domain")
        return self.runtime.context(requested)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == WORKBENCH_IDENTITY_PATH:
                self._send_json(self.runtime.identity())
                return
            if path == "/api/domains":
                self._send_api_json(
                    {
                        "active_domain_id": self.runtime.initial_domain_id,
                        "domains": [
                            item.public_descriptor()
                            for item in self.runtime.contexts.values()
                        ],
                        "domain_switching": len(self.runtime.contexts) > 1,
                        "standalone": self.runtime.standalone,
                    }
                )
                return
            if path == "/api/state":
                context = self._context(parsed)
                self._send_api_json(
                    self.runtime.calibration_state(context), domain_id=context.domain_id
                )
                return
            if path == "/api/app-state":
                self._send_json(
                    self.runtime.app_state(self._requested_domain_id(parsed))
                )
                return
            if path == "/api/paper":
                context = self._context(parsed)
                store = self.runtime.require_continuous(context)
                paper_id = parse_qs(parsed.query).get("id", [""])[0]
                paper = store.get_paper(paper_id)
                if paper is None:
                    self._send_api_json(
                        {"error": "没有找到这篇论文"},
                        domain_id=context.domain_id,
                        status=HTTPStatus.NOT_FOUND,
                    )
                else:
                    self._send_api_json(paper, domain_id=context.domain_id)
                return
            if path == "/api/review/due":
                context = self._context(parsed)
                store = self.runtime.require_continuous(context)
                due = [word.__dict__ for word in store.due_words()]
                self._send_api_json(
                    {"count": len(due), "words": due}, domain_id=context.domain_id
                )
                return
            if path == "/api/terms":
                context = self._context(parsed)
                store = self.runtime.require_continuous(context)
                terms = store.list_terms()
                self._send_api_json(
                    {"count": len(terms), "terms": terms},
                    domain_id=context.domain_id,
                )
                return
            if path == "/api/settings":
                context = self._context(parsed)
                store = self.runtime.require_continuous(context)
                self._send_api_json(
                    {
                        "settings": store.get_settings(),
                        "handoff": store.automation_handoff(),
                    },
                    domain_id=context.domain_id,
                )
                return
            if path == "/api/export.tsv":
                context = self._context(parsed)
                self._send_bytes(
                    context.session.persisted_export_tsv().encode("utf-8"),
                    "text/tab-separated-values; charset=utf-8",
                )
                return
        except (KeyError, ValueError, TypeError) as error:
            self._send_api_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/":
            path = "/index.html"
        target = (self.static_dir / path.lstrip("/")).resolve()
        if self.static_dir.resolve() not in target.parents or not target.is_file():
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), content_type)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if self.headers.get("X-ResearchRamp-API-Version") != str(APP_API_VERSION):
            self._send_api_json(
                {
                    "error": (
                        "页面与本地服务版本不一致，请关闭旧页面并重新启动 ResearchRamp"
                    )
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
            context = self._context(parsed, body)
            if path == "/api/answer":
                context.session.answer(
                    str(body.get("lemma", "")), str(body.get("response", ""))
                )
                self._send_api_json(
                    self.runtime.calibration_state(context), domain_id=context.domain_id
                )
                return
            if path == "/api/reset":
                context.session.reset(body.get("mutation_revision"))
                self._send_api_json(
                    self.runtime.calibration_state(context), domain_id=context.domain_id
                )
                return
            if path == "/api/threshold":
                raw_threshold = body.get("threshold_percent")
                if isinstance(raw_threshold, bool) or not isinstance(
                    raw_threshold, (int, float)
                ):
                    raise ValueError("threshold_percent must be an integer from 75 to 98")
                threshold_percent = int(raw_threshold)
                if threshold_percent != raw_threshold:
                    raise ValueError("threshold_percent must be a whole number")
                context.session.set_threshold_percent(
                    threshold_percent, body.get("mutation_revision")
                )
                self._send_api_json(
                    self.runtime.calibration_state(context), domain_id=context.domain_id
                )
                return
            if path == "/api/preheat/start":
                store = self.runtime.require_continuous(context)
                paper = store.start_preheat(str(body.get("paper_id") or ""))
                self._send_api_json(
                    {
                        "paper": paper,
                        "continuous": store.summary(),
                    },
                    domain_id=context.domain_id,
                )
                return
            if path == "/api/learning/mastered":
                store = self.runtime.require_continuous(context)
                item_type = str(body.get("item_type") or "")
                item_id = str(body.get("item_id") or "")
                if item_type == "word":
                    paper_id = str(body.get("paper_id") or "")
                    if paper_id:
                        store.mark_paper_word_mastered(paper_id, item_id)
                    else:
                        store.mark_item_mastered(item_id)
                elif item_type == "term":
                    store.mark_item_mastered(item_id)
                else:
                    raise ValueError("学习项目类型必须是 word 或 term")
                self._send_api_json(
                    {
                        "continuous": store.summary(),
                        "mastery": self.runtime.mastery(context),
                    },
                    domain_id=context.domain_id,
                )
                return
            if path == "/api/learning/restore":
                store = self.runtime.require_continuous(context)
                store.restore(str(body.get("item_id") or ""))
                self._send_api_json(
                    {"continuous": store.summary()}, domain_id=context.domain_id
                )
                return
            if path == "/api/terms/status":
                store = self.runtime.require_continuous(context)
                store.set_term_status(
                    str(body.get("item_id") or ""), str(body.get("status") or "")
                )
                self._send_api_json(
                    {"continuous": store.summary()}, domain_id=context.domain_id
                )
                return
            if path == "/api/review/answer":
                store = self.runtime.require_continuous(context)
                next_word = store.review(
                    str(body.get("item_id") or ""), str(body.get("rating") or "")
                )
                self._send_api_json(
                    {
                        "next": next_word.__dict__ if next_word else None,
                        "continuous": store.summary(),
                    },
                    domain_id=context.domain_id,
                )
                return
            if path == "/api/settings":
                store = self.runtime.require_continuous(context)
                section = str(body.get("section") or "").strip()
                if section:
                    section_settings = body.get("settings")
                    if not isinstance(section_settings, dict):
                        raise ValueError("settings must be an object")
                    settings = store.save_setting(section, section_settings)
                    handoff = store.automation_handoff(section)
                else:
                    settings = store.save_settings(body)
                    handoff = store.automation_handoff()
                type(self).settings_saved = True
                type(self).settings_saved_domain_id = context.domain_id
                type(self).settings_saved_section = section or None
                self._send_api_json(
                    {"settings": settings, "handoff": handoff, "saved": True},
                    domain_id=context.domain_id,
                )
                if self.exit_on_settings_save:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._send_api_json(
                {"error": "Not found"},
                domain_id=context.domain_id,
                status=HTTPStatus.NOT_FOUND,
            )
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._send_api_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--corpus",
        type=Path,
        help=(
            "ResearchRamp corpus directory. Loads analysis/vocabulary-map.tsv "
            "and stores this corpus's calibration state beside it."
        ),
    )
    source.add_argument(
        "--vocabulary",
        type=Path,
        help="Explicit vocabulary TSV for standalone calibration.",
    )
    source.add_argument(
        "--library",
        type=Path,
        help=(
            "Explicit ResearchRamp domain registry. Only workspaces already listed "
            "in this file are loaded; the filesystem is never scanned."
        ),
    )
    parser.add_argument(
        "--cefr-data",
        type=Path,
        default=Path(__file__).with_name("data") / "cefr_j_v1_6.tsv.gz",
    )
    parser.add_argument(
        "--disable-cefr-prior",
        action="store_true",
        help="Disable the CEFR-J component of the education prior.",
    )
    parser.add_argument(
        "--exam-data",
        type=Path,
        default=Path(__file__).with_name("data") / "ecdict_exam_tags.tsv.gz",
    )
    parser.add_argument(
        "--exam-profile",
        choices=sorted(EXAM_PROFILE_TAGS),
        default="cet6",
        help="Highest assumed Chinese exam exposure for the learner profile.",
    )
    parser.add_argument("--disable-exam-prior", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--label", help="Short research-area label shown in the page header.")
    parser.add_argument(
        "--domain",
        help="Initial registered domain ID when --library is used.",
    )
    parser.add_argument(
        "--ready-calibration-domain",
        help=(
            "Registered domain whose corpus and terminology are finalized but "
            "whose 30-answer calibration may still be incomplete."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--instance-id",
        help="Launcher-owned workbench instance ID used only for startup convergence.",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("vocabulary", "briefs", "review", "schedule"),
        default="vocabulary",
        help="Initial page shown by the unified local ResearchRamp interface.",
    )
    parser.add_argument(
        "--exit-on-settings-save",
        action="store_true",
        help="Stop the temporary settings server after a valid schedule is saved.",
    )
    return parser.parse_args()


def resolve_calibration_paths(args: argparse.Namespace) -> tuple[Path, Path, str]:
    if args.corpus is not None:
        corpus = args.corpus.resolve()
        vocabulary = corpus / "analysis" / "vocabulary-map.tsv"
        state = (
            args.state.resolve()
            if args.state is not None
            else corpus / "analysis" / "vocabulary-calibration-session.json"
        )
        label = args.label
        for profile_name in ("research-profile.json", "research-profile-input.json"):
            profile_path = corpus / profile_name
            if label or not profile_path.is_file():
                continue
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                label = str(profile.get("profile_id") or "").strip() or None
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        label = label or corpus.name
    else:
        vocabulary = args.vocabulary.resolve()
        vocabulary_fingerprint = hashlib.sha256(
            vocabulary.read_bytes()
        ).hexdigest()[:12]
        state = (
            args.state.resolve()
            if args.state is not None
            else vocabulary.parent
            / f".researchramp-{vocabulary.stem}-{vocabulary_fingerprint}-session.json"
        )
        label = args.label or vocabulary.stem

    if not vocabulary.is_file():
        raise FileNotFoundError(f"Vocabulary Map not found: {vocabulary}")
    return vocabulary, state, label


def _education_assets(
    args: argparse.Namespace,
) -> tuple[dict[str, str] | None, dict[str, tuple[str, ...]] | None]:
    cefr_levels = (
        None if args.disable_cefr_prior else load_cefr_levels(args.cefr_data.resolve())
    )
    exam_tags = (
        None if args.disable_exam_prior else load_exam_tags(args.exam_data.resolve())
    )
    return cefr_levels, exam_tags


def _workspace_context(
    registration: DomainRegistration,
    args: argparse.Namespace,
    cefr_levels: dict[str, str] | None,
    exam_tags: dict[str, tuple[str, ...]] | None,
    learning_store: GlobalLearningStore,
    *,
    allow_incomplete_calibration: bool = False,
) -> DomainContext:
    workspace = Path(registration.workspace).resolve()
    if allow_incomplete_calibration:
        validate_initialized_workspace(workspace)
    else:
        validate_completed_workspace(workspace)
    vocabulary = workspace / "analysis" / "vocabulary-map.tsv"
    state = workspace / "analysis" / "vocabulary-calibration-session.json"
    words = load_words(vocabulary, cefr_levels, exam_tags, args.exam_profile)
    try:
        continuous_store = ContinuousStore(
            workspace,
            domain_id=registration.domain_id,
            display_name=registration.display_name,
            learning_store=learning_store,
        )
        continuous_store.terminology_catalog().list_terms()
    except (OSError, sqlite3.Error) as error:
        raise RuntimeError(
            "ResearchRamp could not initialize domain state for "
            f"{registration.domain_id} at {workspace / 'continuous'}: {error}"
        ) from error
    return DomainContext(
        domain_id=registration.domain_id,
        display_name=registration.display_name,
        workspace=workspace,
        session=CalibrationSession(words, state, registration.display_name),
        continuous_store=continuous_store,
    )


def build_runtime(args: argparse.Namespace) -> AppRuntime:
    cefr_levels, exam_tags = _education_assets(args)
    if args.library is not None:
        ready_calibration_domain = getattr(
            args, "ready_calibration_domain", None
        )
        if args.state is not None or args.label is not None:
            raise ValueError("--state and --label cannot be combined with --library")
        registry = DomainRegistry(args.library)
        if not registry.domains:
            raise ValueError(
                "ResearchRamp domain registry is empty; complete and register an init first"
            )
        global_learning_path = (
            args.library.expanduser().resolve().parent / "global-learning.sqlite3"
        )
        try:
            learning_store = GlobalLearningStore(global_learning_path)
        except (OSError, sqlite3.Error) as error:
            raise RuntimeError(
                "ResearchRamp could not initialize global learning state at "
                f"{global_learning_path}: {error}"
            ) from error
        if (
            ready_calibration_domain is not None
            and args.domain != ready_calibration_domain
        ):
            raise ValueError(
                "--ready-calibration-domain must equal the explicitly selected --domain"
            )
        completed_registrations = []
        for item in registry.domains:
            try:
                if item.domain_id == ready_calibration_domain:
                    validate_initialized_workspace(Path(item.workspace))
                else:
                    validate_completed_workspace(Path(item.workspace))
            except FileNotFoundError:
                continue
            completed_registrations.append(item)
        if not completed_registrations:
            raise ValueError(
                "ResearchRamp domains are registered, but none has completed initialization"
            )
        completed_domain_ids = {
            item.domain_id for item in completed_registrations
        }
        if args.domain is not None and args.domain not in completed_domain_ids:
            registry.get(args.domain)
            raise ValueError(
                f"ResearchRamp domain is registered but initialization is incomplete: {args.domain}"
            )
        contexts = [
            _workspace_context(
                item,
                args,
                cefr_levels,
                exam_tags,
                learning_store,
                allow_incomplete_calibration=(
                    item.domain_id == ready_calibration_domain
                ),
            )
            for item in completed_registrations
        ]
        for context in contexts:
            if context.continuous_store is None:
                continue
            try:
                context.continuous_store.migrate_legacy_learning()
            except (OSError, sqlite3.Error) as error:
                raise RuntimeError(
                    "ResearchRamp could not migrate domain state for "
                    f"{context.domain_id} while accessing "
                    f"{context.workspace / 'continuous'} and "
                    f"{global_learning_path}: "
                    f"{error}"
                ) from error
        initial_domain_id = args.domain or registry.active_domain_id
        if initial_domain_id not in completed_domain_ids:
            initial_domain_id = completed_registrations[0].domain_id
        if initial_domain_id is None:
            raise ValueError("ResearchRamp domain registry has no active domain")
        runtime = AppRuntime(
            contexts,
            initial_domain_id=initial_domain_id,
            initial_view=args.mode,
            registry=registry,
            instance_id=args.instance_id,
        )
    else:
        if getattr(args, "ready_calibration_domain", None) is not None:
            raise ValueError("--ready-calibration-domain requires --library")
        if args.domain is not None:
            raise ValueError("--domain requires --library")
        if args.corpus is not None:
            validate_corpus_launch_workspace(args.corpus)
        vocabulary_path, state_path, corpus_label = resolve_calibration_paths(args)
        words = load_words(vocabulary_path, cefr_levels, exam_tags, args.exam_profile)
        workspace = args.corpus.resolve() if args.corpus is not None else None
        standalone = args.vocabulary is not None
        if standalone and args.mode != "vocabulary":
            raise ValueError(
                "Standalone --vocabulary mode supports vocabulary calibration only"
            )
        continuous_store = None
        if workspace is not None:
            try:
                continuous_store = ContinuousStore(
                    workspace,
                    domain_id="current-domain",
                    display_name=corpus_label,
                )
                continuous_store.terminology_catalog().list_terms()
            except (OSError, sqlite3.Error) as error:
                raise RuntimeError(
                    "ResearchRamp could not initialize corpus state at "
                    f"{workspace / 'continuous'}: {error}"
                ) from error
        context = DomainContext(
            domain_id="standalone" if standalone else "current-domain",
            display_name=corpus_label,
            workspace=workspace,
            session=CalibrationSession(
                words,
                state_path,
                corpus_label,
                result_path=(
                    state_path.with_name(f"{state_path.stem}-result.json")
                    if standalone
                    else None
                ),
                export_path=(
                    state_path.with_name(
                        f"{state_path.stem}-personalized-vocabulary.tsv"
                    )
                    if standalone
                    else None
                ),
                enforce_snapshot_match=standalone,
            ),
            continuous_store=continuous_store,
        )
        runtime = AppRuntime(
            [context],
            initial_domain_id=context.domain_id,
            initial_view=args.mode,
            standalone=standalone,
            instance_id=args.instance_id,
        )

    selected = runtime.context(runtime.initial_domain_id)
    if args.mode == "schedule" and not selected.session.public_state()["complete"]:
        raise RuntimeError("请先运行 $researchramp init 并完成30题词汇校准，再设置持续服务")
    return runtime


def main() -> None:
    args = parse_args()
    runtime = build_runtime(args)
    AppHandler.runtime = runtime
    AppHandler.static_dir = Path(__file__).with_name("static")
    AppHandler.exit_on_settings_save = args.exit_on_settings_save
    AppHandler.settings_saved = False
    AppHandler.settings_saved_domain_id = None
    AppHandler.settings_saved_section = None
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url = f"http://{args.host}:{args.port}"
    selected = runtime.context(runtime.initial_domain_id)
    print(
        f"Loaded {len(runtime.contexts):,} ResearchRamp domain(s); "
        f"active domain: {selected.display_name}"
    )
    print(f"Open {url}")
    print(f"Initial view: {args.mode}")
    print("Press Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if args.mode == "schedule":
            saved_domain_id = AppHandler.settings_saved_domain_id or runtime.initial_domain_id
            saved_context = runtime.context(saved_domain_id)
            store = runtime.require_continuous(saved_context)
            print(
                json.dumps(
                    {
                        "status": "saved" if AppHandler.settings_saved else "closed_without_save",
                        "domain_id": saved_context.domain_id,
                        "settings_path": str(store.settings_path),
                        "automation_handoff": store.automation_handoff(
                            AppHandler.settings_saved_section
                        ),
                    },
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    main()
