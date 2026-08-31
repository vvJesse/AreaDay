"""Load the host-reviewed ResearchRamp terminology asset with real evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from global_learning import GlobalLearningStore


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _source_url(paper: dict[str, Any]) -> str:
    doi = str(paper.get("doi") or "").strip()
    if doi:
        return f"https://doi.org/{doi}"
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    openalex_id = str(paper.get("openalex_id") or "").strip()
    if openalex_id:
        return f"https://openalex.org/{openalex_id}"
    raise ValueError(f"论文缺少可打开的来源标识：{paper.get('title') or '未命名论文'}")


class TerminologyCatalog:
    """Strict terminology view: reviewed rows plus host-authored explanations."""

    def __init__(
        self,
        workspace: Path,
        *,
        domain_id: str,
        domain_label: str,
        learning_store: GlobalLearningStore,
    ):
        self.workspace = workspace.resolve()
        self.domain_id = domain_id
        self.domain_label = domain_label
        self.learning_store = learning_store
        analysis = self.workspace / "analysis"
        term_path = analysis / "first-terminology-map.jsonl"
        explanation_path = analysis / "terminology-explanations.json"
        papers_path = analysis / "papers.jsonl"
        for path in (term_path, explanation_path, papers_path):
            if not path.is_file():
                raise FileNotFoundError(f"ResearchRamp 术语产物缺失：{path}")
        self._raw_terms = _read_jsonl(term_path)
        self._papers = {
            str(item.get("openalex_id") or "").strip(): item
            for item in _read_jsonl(papers_path)
            if str(item.get("openalex_id") or "").strip()
        }
        explanations = json.loads(explanation_path.read_text(encoding="utf-8"))
        if not isinstance(explanations, dict):
            raise ValueError(f"术语解释必须是一个对象：{explanation_path}")
        self._explanations = explanations
        reviewed_terms = {
            str(item.get("term") or "").strip().casefold()
            for item in self._raw_terms
            if item.get("host_review_classification") == "domain-term"
        }
        missing = sorted(reviewed_terms.difference(self._explanations))
        extra = sorted(set(self._explanations).difference(reviewed_terms))
        if missing or extra:
            raise ValueError(
                "术语解释与审核后术语表不一致；"
                f"缺少 {len(missing)} 条，多出 {len(extra)} 条"
            )

    def _context_for_paper(
        self, term: str, paper_id: str, surface_forms: list[dict[str, Any]]
    ) -> str:
        paper = self._papers[paper_id]
        text_path = self.workspace / "analysis" / "text" / f"{paper_id.replace(':', '_')}.txt"
        if not text_path.is_file():
            raise FileNotFoundError(f"术语 {term} 的来源论文正文不存在：{text_path}")
        text = re.sub(r"\s+", " ", text_path.read_text(encoding="utf-8"))
        forms = [term, *(str(item.get("form") or "").strip() for item in surface_forms)]
        matches: list[str] = []
        for form in dict.fromkeys(value for value in forms if value):
            pattern = re.compile(
                rf"(?is)([^.!?]{{0,420}}\b{re.escape(form)}\b[^.!?]{{0,420}}[.!?])"
            )
            matches.extend(
                re.sub(r"\s+", " ", match).strip() for match in pattern.findall(text)
            )
        if not matches:
            raise ValueError(f"术语 {term} 无法在来源论文 {paper_id} 中重新定位")
        return max(matches, key=len)

    def _public_term(
        self, raw: dict[str, Any], preferred_paper_id: str | None = None
    ) -> dict[str, Any]:
        term = str(raw["term"]).strip()
        explanation = self._explanations[term.casefold()]
        meaning_en = str(explanation.get("meaning_en") or "").strip()
        meaning_zh = str(explanation.get("meaning_zh") or "").strip()
        concept_role = str(explanation.get("concept_role") or "").strip()
        if not meaning_en or not meaning_zh or not concept_role:
            raise ValueError(f"术语 {term} 缺少完整的中英文解释或概念作用")
        representative = raw.get("representative_sentences") or []
        evidence = None
        if preferred_paper_id:
            evidence = next(
                (
                    item for item in representative
                    if str(item.get("openalex_id") or "") == preferred_paper_id
                    and str(item.get("sentence") or "").strip()
                ),
                None,
            )
            if evidence is None:
                evidence = {
                    "openalex_id": preferred_paper_id,
                    "sentence": self._context_for_paper(
                        term, preferred_paper_id, list(raw.get("surface_forms") or [])
                    ),
                }
        else:
            evidence = next(
                (
                    item
                    for item in representative
                    if str(item.get("openalex_id") or "") in self._papers
                    and str(item.get("sentence") or "").strip()
                ),
                None,
            )
        if evidence is None:
            raise ValueError(f"术语 {term} 没有可对齐到真实论文的代表语境")
        paper_id = str(evidence["openalex_id"])
        paper = self._papers[paper_id]
        context = str(evidence["sentence"]).strip()
        evidence_context_id = hashlib.sha256(
            f"{paper_id}\x1f{context}".encode("utf-8")
        ).hexdigest()[:24]
        sense_key = str(explanation.get("sense_key") or term.casefold()).strip()
        state = self.learning_store.status_for(
            "term", term, meaning_zh=meaning_zh, sense_key=sense_key
        )
        return {
            "item_id": state["item_id"],
            "term": term,
            "item_type": "term",
            "meaning_en": meaning_en,
            "meaning_zh": meaning_zh,
            "concept_role": concept_role,
            "sense_key": sense_key,
            "global_status": state["status"],
            "total_count": int(raw.get("total_count") or 0),
            "document_count": int(raw.get("document_count") or 0),
            "document_share": float(raw.get("document_share") or 0),
            "confidence": float(raw.get("host_review_confidence") or 0),
            "source_paper_ids": list(raw.get("source_papers") or []),
            "context": context,
            "evidence_context_id": evidence_context_id,
            "source_id": paper_id,
            "source_title": str(paper.get("title") or "").strip(),
            "source_url": _source_url(paper),
        }

    def list_terms(self) -> list[dict[str, Any]]:
        result = [
            self._public_term(item)
            for item in self._raw_terms
            if item.get("host_review_classification") == "domain-term"
        ]
        result.sort(key=lambda item: (-item["document_count"], -item["total_count"], item["term"]))
        return result

    def terms_for_paper(self, paper_id: str) -> list[dict[str, Any]]:
        return [
            self._public_term(raw, preferred_paper_id=paper_id)
            for raw in self._raw_terms
            if raw.get("host_review_classification") == "domain-term"
            and paper_id in (raw.get("source_papers") or [])
        ]

    def get(self, item_id: str) -> dict[str, Any]:
        for item in self.list_terms():
            if item["item_id"] == item_id:
                return item
        raise ValueError("没有找到这个术语")

    def record(self, item: dict[str, Any], *, status: str) -> str:
        return self.learning_store.upsert(
            item_type="term",
            display_form=item["term"],
            part_of_speech="term",
            meaning_en=item["meaning_en"],
            meaning_zh=item["meaning_zh"],
            domain_label=self.domain_label,
            confidence=item["confidence"],
            domain_id=self.domain_id,
            paper_id=item["source_id"],
            source_title=item["source_title"],
            source_url=item["source_url"],
            context=item["context"],
            evidence_context_id=item["evidence_context_id"],
            sense_key=item["sense_key"],
            status=status,
        )
