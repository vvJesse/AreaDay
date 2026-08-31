"""Persistent local state for ResearchRamp's continuing research workflow."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from global_learning import GlobalLearningStore, LearningItem
from terminology_catalog import TerminologyCatalog


CONTINUOUS_DIR = "continuous"
DATABASE_NAME = "researchramp.sqlite3"
SETTINGS_NAME = "schedule.json"
SAFE_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None) -> Any:
    if not value:
        raise ValueError("ResearchRamp database record is empty")
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("ResearchRamp database record contains invalid JSON") from error


def discovery_keys(candidate: dict[str, Any]) -> set[str]:
    """Cross-provider identities used only to prevent repeat recommendations."""

    keys: set[str] = set()
    doi = str(candidate.get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    if doi:
        keys.add(f"doi:{doi}")
    arxiv_id = str(candidate.get("arxiv_id") or "").strip().lower()
    if arxiv_id:
        keys.add(f"arxiv:{arxiv_id}")
    title = re.sub(
        r"[^a-z0-9]+", " ", str(candidate.get("title") or "").casefold()
    ).strip()
    if title:
        keys.add(f"title:{title}")
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if candidate_id:
        keys.add(f"candidate:{candidate_id}")
    source_url = str(candidate.get("source_url") or "").strip()
    if source_url:
        parsed = urlparse(source_url)
        normalized_url = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"
        if normalized_url:
            keys.add(f"url:{normalized_url}")
    return keys


def default_settings() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "weekly_brief": {
            "enabled": True,
            "weekday": 1,
            "time": "09:00",
        },
        "daily_review": {
            "enabled": True,
            "time": "20:00",
            "only_when_due": True,
        },
        "timezone": "local",
        "updated_at": None,
    }


def validate_settings(payload: dict[str, Any]) -> dict[str, Any]:
    result = default_settings()
    for section_name in ("weekly_brief", "daily_review"):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            raise ValueError(f"{section_name} must be an object")
        result[section_name]["enabled"] = bool(section.get("enabled"))
        raw_time = str(section.get("time") or "")
        try:
            parsed_time = datetime.strptime(raw_time, "%H:%M")
        except ValueError as error:
            raise ValueError(f"{section_name}.time must use HH:MM") from error
        result[section_name]["time"] = parsed_time.strftime("%H:%M")
    raw_weekday = payload["weekly_brief"].get("weekday")
    if isinstance(raw_weekday, bool) or not isinstance(raw_weekday, int) or not 1 <= raw_weekday <= 7:
        raise ValueError("weekly_brief.weekday must be an integer from 1 to 7")
    result["weekly_brief"]["weekday"] = raw_weekday
    result["daily_review"]["only_when_due"] = True
    result["updated_at"] = utc_iso()
    return result


class ContinuousStore:
    """SQLite-backed state shared by briefs, preheats, and FSRS reviews."""

    def __init__(
        self,
        workspace: Path,
        *,
        domain_id: str | None = None,
        display_name: str | None = None,
        learning_store: GlobalLearningStore | None = None,
    ):
        self.workspace = workspace.resolve()
        profile_identity = self._profile_identity()
        self.domain_id = (domain_id or profile_identity or self.workspace.name).strip()
        self.display_name = (display_name or profile_identity or self.workspace.name).strip()
        self.root = self.workspace / CONTINUOUS_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / DATABASE_NAME
        self.settings_path = self.root / SETTINGS_NAME
        self.learning_store = learning_store or GlobalLearningStore(
            self.root / "global-learning.sqlite3"
        )
        self._terminology_catalog: TerminologyCatalog | None = None
        self._contextual_glosses: dict[str, Any] | None = None
        self._initialize()

    def terminology_catalog(self) -> TerminologyCatalog:
        if self._terminology_catalog is None:
            self._terminology_catalog = TerminologyCatalog(
                self.workspace,
                domain_id=self.domain_id,
                domain_label=self.display_name,
                learning_store=self.learning_store,
            )
        return self._terminology_catalog

    def _hydrate_word(self, paper_id: str, item: dict[str, Any]) -> dict[str, Any]:
        copy = dict(item)
        if str(copy.get("meaning_en") or "").strip():
            return copy
        if self._contextual_glosses is None:
            path = self.workspace / "analysis" / "contextual-glosses.json"
            if not path.is_file():
                raise FileNotFoundError(
                    f"已有简报缺少中英文语境解释，且迁移产物不存在：{path}"
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"语境解释迁移产物格式错误：{path}")
            self._contextual_glosses = payload
        lemma = str(copy.get("lemma") or "").strip().casefold()
        gloss = (self._contextual_glosses.get(paper_id) or {}).get(lemma)
        if not isinstance(gloss, dict):
            raise ValueError(f"已有简报词汇 {lemma} 缺少主代理撰写的语境解释")
        copy.update(gloss)
        return copy

    def _profile_identity(self) -> str | None:
        for name in ("research-profile-input.json", "research-profile.json"):
            path = self.workspace / name
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            value = str(payload.get("display_name") or payload.get("profile_id") or "").strip()
            if value:
                return value
        return None

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS briefs (
                    brief_id TEXT PRIMARY KEY,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    headline TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS papers (
                    paper_id TEXT PRIMARY KEY,
                    brief_id TEXT NOT NULL REFERENCES briefs(brief_id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL,
                    item_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    publication_date TEXT,
                    venue TEXT,
                    payload_json TEXT NOT NULL,
                    preheat_started_at TEXT
                );

                CREATE TABLE IF NOT EXISTS vocabulary (
                    lemma TEXT PRIMARY KEY,
                    meaning TEXT NOT NULL DEFAULT '',
                    part_of_speech TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'learning',
                    fsrs_card_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vocabulary_sources (
                    lemma TEXT NOT NULL REFERENCES vocabulary(lemma) ON DELETE CASCADE,
                    paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
                    context TEXT NOT NULL DEFAULT '',
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (lemma, paper_id)
                );

                CREATE TABLE IF NOT EXISTS review_logs (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lemma TEXT NOT NULL REFERENCES vocabulary(lemma) ON DELETE CASCADE,
                    rating TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    fsrs_log_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS discoveries (
                    identity TEXT PRIMARY KEY,
                    discovered_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    recommended_brief_id TEXT
                );
                """
            )

    def get_settings(self) -> dict[str, Any]:
        if not self.settings_path.is_file():
            return default_settings()
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return validate_settings(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"ResearchRamp 计划设置损坏，已停止载入：{self.settings_path}"
            ) from error

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        validated = validate_settings(payload)
        temporary = self.settings_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.settings_path)
        return validated

    def automation_handoff(self) -> dict[str, Any]:
        settings = self.get_settings()
        workspace = str(self.workspace)
        prefix = f"ResearchRamp · {self.display_name}"
        return {
            "schema_version": 2,
            "domain_id": self.domain_id,
            "display_name": self.display_name,
            "workspace": workspace,
            "daily_discovery": {
                "enabled": settings["weekly_brief"]["enabled"],
                "time": "04:00",
                "notification_policy": "failed_runs_only",
                "automation_key": f"researchramp:{self.domain_id}:discovery",
                "name": f"{prefix} · 后台研究发现",
                "prompt": (
                    "Use $researchramp in silent discovery mode for the initialized "
                    f"workspace {workspace}. Fetch metadata for newly published research "
                    "inside the confirmed scope and update the local candidate pool. Do "
                    "not write a brief, download full text, or notify the user unless the "
                    "run fails."
                ),
            },
            "weekly_brief": {
                **settings["weekly_brief"],
                "automation_key": f"researchramp:{self.domain_id}:weekly-brief",
                "name": f"{prefix} · 每周研究简报",
                "prompt": (
                    "Use $researchramp in continuing weekly-brief mode for the initialized "
                    f"workspace {workspace}. Prefer strong newly published papers, then "
                    "recent unrecommended papers, then older high-value papers. If those "
                    "lanes still provide fewer than two strong items, use public reports "
                    "or research updates with readable source text. Prepare 2–5 "
                    "identity-verified sources, generate source-grounded shadow previews, "
                    "finalize local derived data, and notify the user. Preserve truthful "
                    "freshness and content-type labels."
                ),
            },
            "daily_review": {
                **settings["daily_review"],
                "automation_key": f"researchramp:{self.domain_id}:daily-review",
                "name": f"{prefix} · 今日语言复习",
                "prompt": (
                    "Use $researchramp in due-review reminder mode for the initialized "
                    f"workspace {workspace}. Check the local FSRS queue and notify the user "
                    "only when one or more learning items (words or terms) are due; "
                    "otherwise finish silently."
                ),
            },
        }

    def import_brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_brief(payload)
        with self.connect() as connection:
            existing_row = connection.execute(
                "SELECT payload_json FROM briefs WHERE brief_id = ?",
                (normalized["brief_id"],),
            ).fetchone()
            if existing_row is not None:
                existing = _loads(existing_row["payload_json"])
                existing_comparable = dict(existing)
                normalized_comparable = dict(normalized)
                existing_comparable.pop("created_at", None)
                normalized_comparable.pop("created_at", None)
                if existing_comparable != normalized_comparable:
                    raise ValueError(
                        "A finalized brief ID cannot be reused with different content"
                    )
                return existing
            connection.execute(
                """
                INSERT INTO briefs(brief_id, period_start, period_end, created_at, headline, summary, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["brief_id"],
                    normalized["period_start"],
                    normalized["period_end"],
                    normalized["created_at"],
                    normalized["headline"],
                    normalized["summary"],
                    _json(normalized),
                ),
            )
            for item in normalized["items"]:
                connection.execute(
                    """
                    INSERT INTO papers(
                        paper_id, brief_id, rank, item_type, title, source_url,
                        publication_date, venue, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["item_id"],
                        normalized["brief_id"],
                        item["rank"],
                        item["item_type"],
                        item["title"],
                        item["source_url"],
                        item.get("publication_date"),
                        item.get("venue"),
                        _json(item),
                    ),
                )
                connection.execute(
                    "UPDATE discoveries SET recommended_brief_id = ? WHERE identity = ?",
                    (normalized["brief_id"], item["item_id"]),
                )
        return normalized

    def list_briefs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM briefs ORDER BY period_end DESC, created_at DESC"
            ).fetchall()
        result = [_loads(row["payload_json"]) for row in rows]
        catalog = self.terminology_catalog()
        for brief in result:
            for item in brief.get("items") or []:
                item["estimated_terms"] = len(catalog.terms_for_paper(item["item_id"]))
        return result

    def seen_discovery_ids(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT identity FROM discoveries").fetchall()
        return {str(row["identity"]) for row in rows}

    def seen_discovery_keys(self, *, discovery_scope_id: str | None = None) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT identity, metadata_json FROM discoveries"
            ).fetchall()
        keys: set[str] = set()
        for row in rows:
            payload = _loads(row["metadata_json"])
            if isinstance(payload, dict):
                if discovery_scope_id is not None and str(
                    payload.get("discovery_scope_id") or ""
                ) != discovery_scope_id:
                    continue
                keys.update(discovery_keys(payload))
            keys.add(f"candidate:{row['identity']}")
        return keys

    def record_discoveries(self, candidates: Iterable[dict[str, Any]]) -> int:
        inserted = 0
        now = utc_iso()
        with self.connect() as connection:
            for candidate in candidates:
                identity = str(candidate.get("candidate_id") or "").strip()
                if not identity:
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO discoveries(identity, discovered_at, metadata_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(identity) DO UPDATE SET
                        discovered_at = excluded.discovered_at,
                        metadata_json = excluded.metadata_json
                    WHERE discoveries.recommended_brief_id IS NULL
                    """,
                    (identity, now, _json(candidate)),
                )
                inserted += max(0, cursor.rowcount)
        return inserted

    def unrecommended_discoveries(
        self, *, discovery_scope_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the durable candidate pool that has not appeared in a brief."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT discovered_at, metadata_json
                FROM discoveries
                WHERE recommended_brief_id IS NULL
                ORDER BY discovered_at DESC
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row["metadata_json"])
            if not isinstance(payload, dict) or not payload:
                continue
            if discovery_scope_id is not None and str(
                payload.get("discovery_scope_id") or ""
            ) != discovery_scope_id:
                continue
            copy = dict(payload)
            copy["first_discovered_at"] = row["discovered_at"]
            result.append(copy)
        return result

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json, preheat_started_at FROM papers WHERE paper_id = ?",
                (paper_id,),
            ).fetchone()
        if row is None:
            return None
        payload = _loads(row["payload_json"])
        payload["preheat_started"] = bool(row["preheat_started_at"])
        payload["preheat_started_at"] = row["preheat_started_at"]
        payload["vocabulary"] = self.paper_vocabulary(paper_id, payload.get("vocabulary") or [])
        payload["terminology"] = self.terminology_catalog().terms_for_paper(paper_id)
        payload["estimated_unfamiliar_words"] = len(payload["vocabulary"])
        payload["estimated_terms"] = len(payload["terminology"])
        return payload

    def paper_vocabulary(
        self, paper_id: str, vocabulary: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            paper_row = connection.execute(
                "SELECT title, source_url FROM papers WHERE paper_id = ?",
                (paper_id,),
            ).fetchone()
        if paper_row is None:
            raise ValueError("没有找到词汇对应的论文来源")
        result = []
        for item in vocabulary:
            copy = self._hydrate_word(paper_id, dict(item))
            lemma = str(copy.get("lemma") or "").strip().lower()
            meaning_zh = str(copy.get("meaning_zh") or copy.get("meaning") or "").strip()
            state = self.learning_store.status_for(
                "word",
                lemma,
                meaning_zh=meaning_zh,
                sense_key=str(copy.get("sense_key") or "").strip() or None,
            )
            copy["item_id"] = state["item_id"]
            copy["global_status"] = state["status"]
            copy["meaning_zh"] = meaning_zh
            copy["meaning_en"] = str(copy.get("meaning_en") or "").strip()
            copy["evidence_context_id"] = str(copy.get("evidence_context_id") or "").strip()
            if not copy["meaning_en"] or not copy["meaning_zh"]:
                raise ValueError(f"词汇 {lemma} 缺少论文语境对应的中英文解释")
            if not str(copy.get("context") or "").strip():
                raise ValueError(f"词汇 {lemma} 缺少真实论文语境")
            if not copy["evidence_context_id"]:
                raise ValueError(f"词汇 {lemma} 缺少可核验的语境证据标识")
            if not str(copy.get("sense_key") or "").strip():
                raise ValueError(f"词汇 {lemma} 缺少跨领域共享所需的词义标识")
            copy["source_id"] = paper_id
            copy["source_title"] = str(paper_row["title"])
            copy["source_url"] = str(paper_row["source_url"])
            result.append(copy)
        return result

    def _record_paper_word(self, paper: dict[str, Any], item: dict[str, Any]) -> str:
        lemma = str(item.get("lemma") or "").strip().lower()
        meaning_en = str(item.get("meaning_en") or "").strip()
        meaning_zh = str(item.get("meaning_zh") or item.get("meaning") or "").strip()
        context = str(item.get("context") or "").strip()
        if not meaning_en or not meaning_zh or not context:
            raise ValueError(
                f"词汇 {lemma} 缺少由 Codex / Work Buddy 生成并经论文语境对齐的中英文解释"
            )
        return self.learning_store.upsert(
            item_type="word",
            display_form=lemma,
            part_of_speech=str(item.get("part_of_speech") or "").strip(),
            meaning_en=meaning_en,
            meaning_zh=meaning_zh,
            domain_label=self.display_name,
            confidence=(
                float(item["confidence"]) if item.get("confidence") is not None else None
            ),
            domain_id=self.domain_id,
            paper_id=str(paper["item_id"]),
            source_title=str(paper["title"]),
            source_url=str(paper["source_url"]),
            context=context,
            evidence_context_id=str(item["evidence_context_id"]),
            sense_key=str(item.get("sense_key") or "").strip() or None,
        )

    def start_preheat(self, paper_id: str) -> dict[str, Any]:
        paper = self.get_paper(paper_id)
        if paper is None:
            raise ValueError("没有找到这篇论文")
        vocabulary = paper.get("vocabulary") or []
        for item in vocabulary:
            self._record_paper_word(paper, item)
        with self.connect() as connection:
            connection.execute(
                "UPDATE papers SET preheat_started_at = COALESCE(preheat_started_at, ?) WHERE paper_id = ?",
                (utc_iso(), paper_id),
            )
        return self.get_paper(paper_id) or paper

    def mark_paper_word_mastered(self, paper_id: str, item_id: str) -> None:
        paper = self.get_paper(paper_id)
        if paper is None:
            raise ValueError("没有找到这篇论文")
        item = next((word for word in paper["vocabulary"] if word["item_id"] == item_id), None)
        if item is None:
            raise ValueError("这篇论文中没有找到对应词汇")
        resolved = self._record_paper_word(paper, item)
        self.learning_store.set_mastered(resolved)

    def list_terms(self) -> list[dict[str, Any]]:
        return self.terminology_catalog().list_terms()

    def set_term_status(self, item_id: str, status: str) -> None:
        item = self.terminology_catalog().get(item_id)
        if status not in {"learning", "mastered"}:
            raise ValueError("术语状态必须是 learning 或 mastered")
        resolved = self.terminology_catalog().record(item, status=status)
        if status == "mastered":
            self.learning_store.set_mastered(resolved)

    def restore(self, item_id: str) -> None:
        self.learning_store.restore(item_id)

    def mark_item_mastered(self, item_id: str) -> None:
        self.learning_store.set_mastered(item_id)

    def due_words(self, limit: int = 100) -> list[LearningItem]:
        return self.learning_store.due_items(limit)

    def review(self, item_id: str, rating_name: str) -> LearningItem | None:
        return self.learning_store.review(item_id, rating_name)

    def summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            brief_count = connection.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]
        global_counts = self.learning_store.counts()
        return {
            "brief_count": brief_count,
            **global_counts,
        }

    def migrate_legacy_learning(self) -> dict[str, int]:
        """Move the old per-domain queue into the shared learner model once."""

        migration_key = f"legacy-domain-learning:{self.domain_id}:v1"
        if self.learning_store.migration_done(migration_key):
            return {"migrated": 0}
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT v.lemma, v.meaning, v.part_of_speech, v.status,
                       v.fsrs_card_json, s.context, s.paper_id,
                       p.title AS source_title, p.source_url, p.payload_json
                FROM vocabulary v
                JOIN vocabulary_sources s ON s.lemma=v.lemma AND s.context<>''
                JOIN papers p ON p.paper_id=s.paper_id
                ORDER BY v.updated_at, s.added_at
                """
            ).fetchall()
        migrated = 0
        for row in rows:
            paper = _loads(row["payload_json"])
            payload_item = next(
                (
                    item for item in paper.get("vocabulary") or []
                    if str(item.get("lemma") or "").strip().casefold() == row["lemma"].casefold()
                ),
                None,
            )
            if payload_item is None:
                raise ValueError(f"旧学习记录 {row['lemma']} 无法对齐回原论文词汇")
            payload_item = self._hydrate_word(row["paper_id"], payload_item)
            context = str(row["context"]).strip()
            evidence_id = str(payload_item.get("evidence_context_id") or "").strip()
            if not evidence_id:
                raise ValueError(f"旧学习记录 {row['lemma']} 缺少可核验的语境证据标识")
            self.learning_store.upsert(
                item_type="word",
                display_form=row["lemma"],
                part_of_speech=row["part_of_speech"],
                meaning_en=str(payload_item.get("meaning_en") or "").strip(),
                meaning_zh=row["meaning"],
                domain_label=self.display_name,
                confidence=(
                    float(payload_item["confidence"])
                    if payload_item.get("confidence") is not None else None
                ),
                domain_id=self.domain_id,
                paper_id=row["paper_id"],
                source_title=row["source_title"],
                source_url=row["source_url"],
                context=context,
                evidence_context_id=evidence_id,
                sense_key=str(payload_item.get("sense_key") or "").strip() or None,
                status="mastered" if row["status"] == "known" else "learning",
                fsrs_card_json=row["fsrs_card_json"],
            )
            migrated += 1
        self.learning_store.complete_migration(migration_key, {"migrated": migrated})
        return {"migrated": migrated}


def validate_brief(payload: dict[str, Any]) -> dict[str, Any]:
    required_text = ("brief_id", "period_start", "period_end", "headline", "summary")
    normalized = dict(payload)
    for key in required_text:
        if not str(normalized.get(key) or "").strip():
            raise ValueError(f"brief.{key} is required")
        normalized[key] = str(normalized[key]).strip()
    if not SAFE_RECORD_ID.fullmatch(normalized["brief_id"]):
        raise ValueError("brief.brief_id contains unsafe characters")
    normalized["created_at"] = str(normalized.get("created_at") or utc_iso())
    items = normalized.get("items")
    if not isinstance(items, list) or not 2 <= len(items) <= 5:
        raise ValueError("brief.items must contain 2 to 5 recommendations")
    seen_ids: set[str] = set()
    normalized_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"brief.items[{index}] must be an object")
        item = dict(raw_item)
        for key in ("item_id", "title", "source_url", "value_reason", "shadow_preview"):
            if not str(item.get(key) or "").strip():
                raise ValueError(f"brief.items[{index}].{key} is required")
            item[key] = str(item[key]).strip()
        if item["item_id"] in seen_ids:
            raise ValueError(f"duplicate brief item id: {item['item_id']}")
        if not SAFE_RECORD_ID.fullmatch(item["item_id"]):
            raise ValueError(f"brief.items[{index}].item_id contains unsafe characters")
        source_url = urlparse(item["source_url"])
        if source_url.scheme not in {"http", "https"} or not source_url.netloc:
            raise ValueError(f"brief.items[{index}].source_url must be an http(s) URL")
        seen_ids.add(item["item_id"])
        if item.get("item_type") not in {
            "new_paper",
            "recent_paper",
            "classic_paper",
            "backlog_paper",
            "public_report",
            "research_update",
        }:
            raise ValueError(f"brief.items[{index}].item_type is invalid")
        item["rank"] = index
        item["estimated_minutes"] = max(1, int(item.get("estimated_minutes") or 1))
        vocabulary = item.get("vocabulary") or []
        if not isinstance(vocabulary, list):
            raise ValueError(f"brief.items[{index}].vocabulary must be a list")
        unique_vocabulary: list[dict[str, Any]] = []
        seen_lemmas: set[str] = set()
        for raw_word in vocabulary:
            if not isinstance(raw_word, dict):
                continue
            lemma = str(raw_word.get("lemma") or "").strip().lower()
            if not lemma or lemma in seen_lemmas:
                continue
            seen_lemmas.add(lemma)
            meaning_zh = str(
                raw_word.get("meaning_zh") or raw_word.get("meaning") or ""
            ).strip()
            unique_vocabulary.append(
                {
                    "lemma": lemma,
                    "meaning": meaning_zh,
                    "meaning_zh": meaning_zh,
                    "meaning_en": str(raw_word.get("meaning_en") or "").strip(),
                    "part_of_speech": str(raw_word.get("part_of_speech") or "").strip(),
                    "context": str(raw_word.get("context") or "").strip(),
                    "sense_key": str(raw_word.get("sense_key") or "").strip(),
                    "confidence": raw_word.get("confidence"),
                    "evidence_context_id": str(
                        raw_word.get("evidence_context_id") or ""
                    ).strip(),
                }
            )
            if not unique_vocabulary[-1]["meaning_en"] or not unique_vocabulary[-1]["meaning_zh"]:
                raise ValueError(
                    f"brief.items[{index}] vocabulary bilingual meaning is required: {lemma}"
                )
            if not unique_vocabulary[-1]["context"]:
                raise ValueError(f"brief.items[{index}] vocabulary context is required: {lemma}")
            if not unique_vocabulary[-1]["evidence_context_id"]:
                raise ValueError(
                    f"brief.items[{index}] vocabulary evidence_context_id is required: {lemma}"
                )
            if not unique_vocabulary[-1]["sense_key"]:
                raise ValueError(
                    f"brief.items[{index}] vocabulary sense_key is required: {lemma}"
                )
        item["vocabulary"] = unique_vocabulary
        item["estimated_unfamiliar_words"] = len(unique_vocabulary)
        normalized_items.append(item)
    normalized["items"] = normalized_items
    return normalized
