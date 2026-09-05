#!/usr/bin/env python3
"""Run AreaDay's unified local application."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
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
)
from remote_calibration import (  # noqa: E402
    CalibrationServiceError,
    InvalidCalibrationData,
    RemoteCalibrationSession,
)
from workbench_protocol import (  # noqa: E402
    WORKBENCH_IDENTITY_PATH,
    WORKBENCH_IDENTITY_VERSION,
    WORKBENCH_SERVICE,
)


QUESTION_LIMIT = 30
APP_API_VERSION = 6
CEFR_LEVEL_ORDER = {"A1": 0, "A2": 1, "B1": 2, "B2": 3}
KNOWN_EXAM_TAGS = {"gk", "cet4", "cet6"}
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
    cefr_level: str | None
    exam_tags: tuple[str, ...]


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
                tag for tag in row["tags"].split() if tag in KNOWN_EXAM_TAGS
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
            cefr_level = cefr_levels.get(lemma)
            matched_exam_tags = tuple(
                tag for tag in exam_tags.get(lemma, ()) if tag in allowed_exam_tags
            )
            words.append(
                Word(
                    lemma=lemma,
                    part_of_speech=row["part_of_speech"],
                    total_count=int(row["total_count"]),
                    document_count=int(row["document_count"]),
                    document_share=float(row["document_share"]),
                    zipf=zipf,
                    cefr_level=cefr_level,
                    exam_tags=matched_exam_tags,
                )
            )
    if not words:
        raise ValueError("Vocabulary file contained no words")
    return words


@dataclass(frozen=True)
class DomainContext:
    domain_id: str
    display_name: str
    workspace: Path | None
    session: object
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
            raise ValueError("AreaDay requires at least one registered domain")
        self.contexts = {context.domain_id: context for context in contexts}
        if len(self.contexts) != len(contexts):
            raise ValueError("AreaDay domain IDs must be unique")
        if initial_domain_id not in self.contexts:
            raise ValueError(f"Unknown initial AreaDay domain: {initial_domain_id}")
        self.initial_domain_id = initial_domain_id
        self.initial_view = initial_view
        self.registry = registry
        self.standalone = standalone
        self.instance_id = (instance_id or uuid.uuid4().hex).strip()
        if not self.instance_id:
            raise ValueError("AreaDay workbench instance ID cannot be empty")

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
            raise ValueError(f"Unknown AreaDay domain: {selected}")
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
            raise ValueError("Conflicting AreaDay domain IDs in one request")
        return str(candidates[0])

    def _context(
        self, parsed: Any, body: dict[str, Any] | None = None
    ) -> DomainContext:
        requested = self._requested_domain_id(parsed, body)
        if len(self.runtime.contexts) > 1 and requested is None:
            raise ValueError("Every scoped request must name its AreaDay domain")
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
            if path == "/api/learning/new-words":
                context = self._context(parsed)
                store = self.runtime.require_continuous(context)
                raw_limit = parse_qs(parsed.query).get("limit", ["5"])[0]
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError) as error:
                    raise ValueError("limit must be an integer") from error
                if not 1 <= limit <= 100:
                    raise ValueError("limit must be between 1 and 100")
                cards = store.new_word_candidates(limit=limit)
                self._send_api_json(
                    {"count": len(cards), "cards": cards},
                    domain_id=context.domain_id,
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
                        "页面与本地服务版本不一致，请关闭旧页面并重新启动 AreaDay"
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
            if path == "/api/learning/new-word-status":
                store = self.runtime.require_continuous(context)
                item_id = store.set_new_word_status(
                    str(body.get("card_id") or ""), str(body.get("status") or "")
                )
                self._send_api_json(
                    {"item_id": item_id, "continuous": store.summary()},
                    domain_id=context.domain_id,
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
        except CalibrationServiceError as error:
            status = (
                HTTPStatus.SERVICE_UNAVAILABLE
                if error.code == "calibration_service_unavailable"
                else HTTPStatus.FORBIDDEN
                if "license" in error.code
                else HTTPStatus.BAD_GATEWAY
            )
            self._send_api_json(
                {"error": str(error), "code": error.code},
                status=status,
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
            "AreaDay corpus directory. Loads analysis/vocabulary-map.tsv "
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
            "Explicit AreaDay domain registry. Only workspaces already listed "
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
        help="Initial page shown by the unified local AreaDay interface.",
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
        validate_corpus_launch_workspace(workspace)
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
            "AreaDay could not initialize domain state for "
            f"{registration.domain_id} at {workspace / 'continuous'}: {error}"
        ) from error
    return DomainContext(
        domain_id=registration.domain_id,
        display_name=registration.display_name,
        workspace=workspace,
        session=RemoteCalibrationSession(words, state, registration.display_name),
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
                "AreaDay domain registry is empty; complete and register an init first"
            )
        global_learning_path = (
            args.library.expanduser().resolve().parent / "global-learning.sqlite3"
        )
        try:
            learning_store = GlobalLearningStore(global_learning_path)
        except (OSError, sqlite3.Error) as error:
            raise RuntimeError(
                "AreaDay could not initialize global learning state at "
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
                    validate_corpus_launch_workspace(Path(item.workspace))
                else:
                    validate_completed_workspace(Path(item.workspace))
            except (FileNotFoundError, InvalidCalibrationData):
                continue
            completed_registrations.append(item)
        if not completed_registrations:
            raise ValueError(
                "AreaDay domains are registered, but none has completed initialization"
            )
        completed_domain_ids = {
            item.domain_id for item in completed_registrations
        }
        if args.domain is not None and args.domain not in completed_domain_ids:
            registry.get(args.domain)
            raise ValueError(
                f"AreaDay domain is registered but initialization is incomplete: {args.domain}"
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
                    "AreaDay could not migrate domain state for "
                    f"{context.domain_id} while accessing "
                    f"{context.workspace / 'continuous'} and "
                    f"{global_learning_path}: "
                    f"{error}"
                ) from error
        initial_domain_id = args.domain or registry.active_domain_id
        if initial_domain_id not in completed_domain_ids:
            initial_domain_id = completed_registrations[0].domain_id
        if initial_domain_id is None:
            raise ValueError("AreaDay domain registry has no active domain")
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
                    "AreaDay could not initialize corpus state at "
                    f"{workspace / 'continuous'}: {error}"
                ) from error
        context = DomainContext(
            domain_id="standalone" if standalone else "current-domain",
            display_name=corpus_label,
            workspace=workspace,
            session=RemoteCalibrationSession(
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
        raise RuntimeError("请先运行 $areaday init 并完成30题词汇校准，再设置持续服务")
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
        f"Loaded {len(runtime.contexts):,} AreaDay domain(s); "
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
