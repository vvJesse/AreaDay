"""Global, source-grounded learning state shared by ResearchRamp domains."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _stable_id(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _new_card() -> str:
    from fsrs import Card

    return Card().to_json()


def _due(card_json: str | None) -> datetime | None:
    if not card_json:
        return None
    from fsrs import Card

    return Card.from_json(card_json).due.astimezone(UTC)


@dataclass(frozen=True)
class LearningItem:
    item_id: str
    item_type: str
    display_form: str
    part_of_speech: str
    meaning_en: str
    meaning_zh: str
    domain_label: str
    confidence: float | None
    status: str
    context: str
    source_title: str
    source_id: str
    source_url: str
    source_domain_id: str
    due: str | None


class GlobalLearningStore:
    """One learner model, with domain-specific evidence attached to each item."""

    def __init__(self, database_path: Path):
        self.database_path = database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                CREATE TABLE IF NOT EXISTS learning_items (
                    item_id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL CHECK(item_type IN ('word', 'term')),
                    normalized_form TEXT NOT NULL,
                    display_form TEXT NOT NULL,
                    sense_key TEXT NOT NULL,
                    part_of_speech TEXT NOT NULL DEFAULT '',
                    meaning_en TEXT NOT NULL,
                    meaning_zh TEXT NOT NULL,
                    domain_label TEXT NOT NULL DEFAULT '',
                    confidence REAL,
                    status TEXT NOT NULL CHECK(status IN ('learning', 'mastered')),
                    fsrs_card_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_type, normalized_form, sense_key)
                );

                CREATE TABLE IF NOT EXISTS learning_sources (
                    source_key TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES learning_items(item_id) ON DELETE CASCADE,
                    domain_id TEXT NOT NULL,
                    paper_id TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    context TEXT NOT NULL,
                    evidence_context_id TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    UNIQUE(item_id, domain_id, paper_id, evidence_context_id)
                );

                CREATE TABLE IF NOT EXISTS review_logs (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL REFERENCES learning_items(item_id) ON DELETE CASCADE,
                    rating TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    fsrs_log_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS migrations (
                    migration_key TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def descriptor(
        item_type: str,
        display_form: str,
        *,
        meaning_zh: str,
        sense_key: str | None = None,
    ) -> tuple[str, str, str]:
        if item_type not in {"word", "term"}:
            raise ValueError("学习项目类型必须是 word 或 term")
        normalized = _normalize(display_form)
        if not normalized:
            raise ValueError("学习项目名称不能为空")
        resolved_sense = _normalize(sense_key or "")
        if not resolved_sense:
            if item_type == "term":
                resolved_sense = normalized
            else:
                resolved_sense = _stable_id(_normalize(meaning_zh))
        return normalized, resolved_sense, _stable_id(item_type, normalized, resolved_sense)

    def status_for(
        self,
        item_type: str,
        display_form: str,
        *,
        meaning_zh: str,
        sense_key: str | None = None,
    ) -> dict[str, str]:
        _, _, item_id = self.descriptor(
            item_type, display_form, meaning_zh=meaning_zh, sense_key=sense_key
        )
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM learning_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return {"item_id": item_id, "status": row["status"] if row else "new"}

    def upsert(
        self,
        *,
        item_type: str,
        display_form: str,
        part_of_speech: str,
        meaning_en: str,
        meaning_zh: str,
        domain_label: str,
        confidence: float | None,
        domain_id: str,
        paper_id: str,
        source_title: str,
        source_url: str,
        context: str,
        evidence_context_id: str,
        sense_key: str | None = None,
        status: str = "learning",
        fsrs_card_json: str | None = None,
    ) -> str:
        normalized, resolved_sense, item_id = self.descriptor(
            item_type, display_form, meaning_zh=meaning_zh, sense_key=sense_key
        )
        meaning_en = meaning_en.strip()
        meaning_zh = meaning_zh.strip()
        context = context.strip()
        source_title = source_title.strip()
        source_url = source_url.strip()
        evidence_context_id = evidence_context_id.strip()
        if (
            not meaning_zh
            or not context
            or not source_title
            or not source_url
            or not evidence_context_id
        ):
            raise ValueError(f"{display_form} 缺少可核验的含义或论文语境")
        if status not in {"learning", "mastered"}:
            raise ValueError("学习状态必须是 learning 或 mastered")
        now = _now()
        card_json = fsrs_card_json or _new_card()
        source_key = _stable_id(item_id, domain_id, paper_id, evidence_context_id)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT status FROM learning_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO learning_items(
                        item_id, item_type, normalized_form, display_form, sense_key,
                        part_of_speech, meaning_en, meaning_zh, domain_label,
                        confidence, status, fsrs_card_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id, item_type, normalized, display_form.strip(), resolved_sense,
                        part_of_speech.strip(), meaning_en, meaning_zh,
                        domain_label.strip(), confidence, status, card_json, now, now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE learning_items SET
                        part_of_speech = CASE WHEN part_of_speech = '' THEN ? ELSE part_of_speech END,
                        meaning_en = CASE WHEN meaning_en = '' THEN ? ELSE meaning_en END,
                        meaning_zh = CASE WHEN meaning_zh = '' THEN ? ELSE meaning_zh END,
                        domain_label = CASE WHEN domain_label = '' THEN ? ELSE domain_label END,
                        confidence = COALESCE(confidence, ?),
                        updated_at = ?
                    WHERE item_id = ?
                    """,
                    (
                        part_of_speech.strip(), meaning_en, meaning_zh,
                        domain_label.strip(), confidence, now, item_id,
                    ),
                )
            connection.execute(
                """
                INSERT INTO learning_sources(
                    source_key, item_id, domain_id, paper_id, source_title,
                    source_url, context, evidence_context_id, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    source_title=excluded.source_title,
                    source_url=excluded.source_url,
                    context=excluded.context
                """,
                (
                    source_key, item_id, domain_id, paper_id, source_title,
                    source_url, context, evidence_context_id, now,
                ),
            )
        return item_id

    def set_mastered(self, item_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE learning_items SET status='mastered', updated_at=? WHERE item_id=?",
                (_now(), item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("没有找到这个学习项目")

    def restore(self, item_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_items
                SET status='learning', updated_at=?
                WHERE item_id=? AND status='mastered'
                """,
                (_now(), item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("这个学习项目当前不需要恢复")

    def due_items(self, limit: int = 100) -> list[LearningItem]:
        now = datetime.now(UTC)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, s.domain_id AS source_domain_id, s.paper_id AS source_id,
                       s.source_title, s.source_url, s.context
                FROM learning_items i
                JOIN learning_sources s ON s.source_key = (
                    SELECT s2.source_key FROM learning_sources s2
                    WHERE s2.item_id = i.item_id
                    ORDER BY s2.added_at DESC LIMIT 1
                )
                WHERE i.status='learning'
                ORDER BY i.updated_at ASC
                """
            ).fetchall()
        result: list[LearningItem] = []
        for row in rows:
            due = _due(row["fsrs_card_json"])
            if due is not None and due > now:
                continue
            result.append(
                LearningItem(
                    item_id=row["item_id"], item_type=row["item_type"],
                    display_form=row["display_form"], part_of_speech=row["part_of_speech"],
                    meaning_en=row["meaning_en"], meaning_zh=row["meaning_zh"],
                    domain_label=row["domain_label"], confidence=row["confidence"],
                    status=row["status"], context=row["context"],
                    source_title=row["source_title"], source_id=row["source_id"],
                    source_url=row["source_url"], source_domain_id=row["source_domain_id"],
                    due=due.isoformat() if due else None,
                )
            )
            if len(result) >= limit:
                break
        return result

    def review(self, item_id: str, rating_name: str) -> LearningItem | None:
        from fsrs import Card, Rating, Scheduler

        ratings = {"again": Rating.Again, "hard": Rating.Hard, "good": Rating.Good}
        if rating_name not in ratings:
            raise ValueError("复习结果必须是 again、hard 或 good")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT fsrs_card_json FROM learning_items WHERE item_id=? AND status='learning'",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError("这个项目不在当前复习队列中")
            card = Card.from_json(row["fsrs_card_json"]) if row["fsrs_card_json"] else Card()
            card, log = Scheduler(desired_retention=0.9).review_card(card, ratings[rating_name])
            reviewed_at = log.review_datetime.astimezone(UTC).isoformat()
            connection.execute(
                "UPDATE learning_items SET fsrs_card_json=?, updated_at=? WHERE item_id=?",
                (card.to_json(), reviewed_at, item_id),
            )
            connection.execute(
                "INSERT INTO review_logs(item_id,rating,reviewed_at,fsrs_log_json) VALUES (?,?,?,?)",
                (item_id, rating_name, reviewed_at, log.to_json()),
            )
        remaining = self.due_items(limit=1)
        return remaining[0] if remaining else None

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM learning_items GROUP BY status"
            ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {
            "learning_count": counts.get("learning", 0),
            "mastered_count": counts.get("mastered", 0),
            "due_count": len(self.due_items()),
        }

    def mastered_word_forms(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT normalized_form
                FROM learning_items
                WHERE item_type='word' AND status='mastered'
                """
            ).fetchall()
        return {str(row["normalized_form"]) for row in rows}

    def complete_migration(self, key: str, details: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO migrations(migration_key,completed_at,details_json) VALUES (?,?,?)",
                (key, _now(), json.dumps(details, ensure_ascii=False, sort_keys=True)),
            )

    def migration_done(self, key: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM migrations WHERE migration_key=?", (key,)
            ).fetchone() is not None
