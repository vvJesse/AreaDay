#!/usr/bin/env python3
"""Use the licensed remote vocabulary predictor without uploading corpus text."""

from __future__ import annotations

import csv
import hashlib
import http.client
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

from researchramp_license import (
    DEFAULT_PRODUCTION_ACTIVATION_SERVER,
    production_license_path,
)


MAX_CALIBRATION_RESPONSE_BYTES = 8_000_000
REMOTE_SESSION_SCHEMA_VERSION = 2
WORD_STATISTIC_FIELDS = (
    "lemma",
    "part_of_speech",
    "total_count",
    "document_count",
    "document_share",
    "zipf",
    "cefr_level",
    "exam_tags",
)


class CalibrationServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvalidCalibrationData(ValueError):
    """The local calibration files cannot be loaded as one completed result."""


RECOVERY_NOTICE = "本地校准数据无法读取，已清除。请重新回答 30 道题。"


def load_completed_calibration(
    state_path: Path,
    result_path: Path,
    export_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the fields used by the app, without byte-level consistency checks."""

    if not all(path.is_file() for path in (state_path, result_path, export_path)):
        raise InvalidCalibrationData("calibration output files are incomplete")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        with export_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not {"lemma", "classification"}.issubset(reader.fieldnames or []):
                raise InvalidCalibrationData(
                    "personalized vocabulary columns are invalid"
                )
            list(reader)
    except InvalidCalibrationData:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        csv.Error,
    ) as error:
        raise InvalidCalibrationData(
            "calibration output files cannot be loaded"
        ) from error

    answers = state.get("answers") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or not isinstance(result, dict)
        or not isinstance(answers, list)
        or len(answers) != 30
        or not isinstance(result.get("counts"), dict)
        or not isinstance(result.get("threshold"), dict)
        or not isinstance(result.get("importance"), dict)
    ):
        raise InvalidCalibrationData("completed calibration data has an invalid format")
    return state, result


def clear_calibration_outputs(
    state_path: Path,
    result_path: Path,
    export_path: Path,
) -> None:
    """Remove only product-generated calibration outputs."""

    for path in (state_path, result_path, export_path):
        path.unlink(missing_ok=True)


def serialize_word_statistics(word: Any) -> dict[str, Any]:
    return {
        "lemma": str(word.lemma),
        "part_of_speech": str(word.part_of_speech),
        "total_count": int(word.total_count),
        "document_count": int(word.document_count),
        "document_share": float(word.document_share),
        "zipf": float(word.zipf),
        "cefr_level": word.cefr_level,
        "exam_tags": list(word.exam_tags),
    }


def vocabulary_snapshot_sha256(words: list[Any]) -> str:
    snapshot = json.dumps(
        [
            serialize_word_statistics(word)
            for word in sorted(words, key=lambda item: item.lemma)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


class RemoteCalibrationClient:
    def __init__(
        self,
        endpoint: str = DEFAULT_PRODUCTION_ACTIVATION_SERVER,
        *,
        timeout: float = 20.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise CalibrationServiceError(
                "calibration_server_invalid",
                "The vocabulary prediction server address is invalid.",
            )
        self.endpoint = endpoint.rstrip("/")
        self.parsed_endpoint = parsed
        self.timeout = timeout

    def request(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action not in {
            "preflight",
            "start",
            "state",
            "answer",
            "threshold",
            "reset",
        }:
            raise ValueError(f"Unknown calibration action: {action}")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection_type = (
            http.client.HTTPSConnection
            if self.parsed_endpoint.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            self.parsed_endpoint.hostname,
            self.parsed_endpoint.port,
            timeout=self.timeout,
        )
        try:
            connection.request(
                "POST",
                f"/v1/calibrations/{action}",
                body=encoded,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            response_bytes = response.read(MAX_CALIBRATION_RESPONSE_BYTES + 1)
        except (http.client.HTTPException, TimeoutError, OSError) as error:
            raise CalibrationServiceError(
                "calibration_service_unavailable",
                "The vocabulary prediction service could not be reached.",
            ) from error
        finally:
            connection.close()
        if len(response_bytes) > MAX_CALIBRATION_RESPONSE_BYTES:
            raise CalibrationServiceError(
                "calibration_response_invalid",
                "The vocabulary prediction service returned an oversized response.",
            )
        try:
            body = json.loads(response_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CalibrationServiceError(
                "calibration_response_invalid",
                "The vocabulary prediction service returned invalid JSON.",
            ) from error
        if not isinstance(body, dict):
            raise CalibrationServiceError(
                "calibration_response_invalid",
                "The vocabulary prediction service returned an invalid response.",
            )
        if response.status != 200:
            code = body.get("code")
            message = body.get("error")
            if (
                body.get("status") != "calibration_error"
                or not isinstance(code, str)
                or not code.startswith("calibration_")
                or not isinstance(message, str)
                or not message.strip()
            ):
                raise CalibrationServiceError(
                    "calibration_response_invalid",
                    "The vocabulary prediction service returned an invalid error.",
                )
            raise CalibrationServiceError(code, message.strip())
        return body


class RemoteCalibrationSession:
    def __init__(
        self,
        words: list[Any],
        state_path: Path,
        corpus_label: str,
        *,
        result_path: Path | None = None,
        export_path: Path | None = None,
        enforce_snapshot_match: bool = False,
        client: RemoteCalibrationClient | Any | None = None,
        license_path: Path | None = None,
    ) -> None:
        self.words = words
        self.state_path = state_path
        self.result_path = result_path or state_path.with_name(
            "vocabulary-calibration-result.json"
        )
        self.export_path = export_path or state_path.with_name(
            "personalized-vocabulary.tsv"
        )
        self.corpus_label = corpus_label
        self.enforce_snapshot_match = enforce_snapshot_match
        self.client = client or RemoteCalibrationClient()
        self.license_path = license_path or production_license_path()
        self.snapshot = vocabulary_snapshot_sha256(words)
        self.session_id = ""
        self.answers: list[dict[str, Any]] = []
        self._remote_state: dict[str, Any] | None = None
        self.recovery_notice: str | None = None
        if self._load_completed_local_result():
            return
        saved = self._read_saved_state()
        if len(saved.get("answers") or []) >= 30:
            self._recover_local_calibration()
            saved = {}
        self.answers = list(saved.get("answers") or [])
        saved_session = saved.get("remote_session_id")
        if isinstance(saved_session, str) and saved_session:
            response = self._request("state", {"session_id": saved_session})
        else:
            response = self._request(
                "start",
                {
                    "vocabulary_snapshot_sha256": self.snapshot,
                    "words": [serialize_word_statistics(word) for word in words],
                },
            )
        self._accept_response(response)

    def _recover_local_calibration(self) -> None:
        clear_calibration_outputs(self.state_path, self.result_path, self.export_path)
        self.recovery_notice = RECOVERY_NOTICE

    def _license_envelope(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.license_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise CalibrationServiceError(
                "calibration_license_missing",
                "A local activated license is required for vocabulary prediction.",
            ) from error
        if not isinstance(payload, dict):
            raise CalibrationServiceError(
                "calibration_license_invalid",
                "The installed vocabulary prediction license is invalid.",
            )
        return payload

    def _request(self, action: str, values: dict[str, object]) -> dict[str, object]:
        return self.client.request(
            action,
            {"license": self._license_envelope(), **values},
        )

    def _read_saved_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._recover_local_calibration()
            return {}
        if not isinstance(payload, dict) or not isinstance(
            payload.get("answers", []), list
        ):
            self._recover_local_calibration()
            return {}
        return payload

    def _load_completed_local_result(self) -> bool:
        if not (self.result_path.is_file() or self.export_path.is_file()):
            return False
        try:
            state, result = load_completed_calibration(
                self.state_path,
                self.result_path,
                self.export_path,
            )
            if (
                self.enforce_snapshot_match
                and result.get("vocabulary_snapshot_sha256") != self.snapshot
            ):
                raise InvalidCalibrationData("vocabulary snapshot does not match")
            answers = state["answers"]
            self.answers = answers
            self.session_id = str(state.get("remote_session_id") or "completed-local")
            self._remote_state = self._completed_public_state(result)
            return True
        except InvalidCalibrationData:
            self._recover_local_calibration()
            return False

    def _completed_public_state(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "corpus_label": self.corpus_label,
            "answered": 30,
            "question_limit": 30,
            "complete": True,
            "mutation_revision": int(result.get("mutation_revision") or 0),
            "threshold": result.get("threshold") or {},
            "responses": {
                label: sum(
                    answer.get("response") == label for answer in self.answers
                )
                for label in ("known", "unknown", "unsure")
            },
            "result": {
                **result,
                "output_files": {
                    "result": str(self.result_path),
                    "personalized_vocabulary": str(self.export_path),
                },
            },
        }

    def _accept_response(self, response: dict[str, object]) -> None:
        session_id = response.get("session_id")
        remote_snapshot = response.get("vocabulary_snapshot_sha256")
        calibration = response.get("calibration")
        if (
            not isinstance(session_id, str)
            or not session_id
            or remote_snapshot != self.snapshot
            or not isinstance(calibration, dict)
            or calibration.get("question_limit") != 30
            or not isinstance(calibration.get("complete"), bool)
        ):
            raise CalibrationServiceError(
                "calibration_response_invalid",
                "The vocabulary prediction service returned an invalid response.",
            )
        self.session_id = session_id
        self._remote_state = {"corpus_label": self.corpus_label, **calibration}
        if self.recovery_notice:
            self._remote_state["recovery_notice"] = self.recovery_notice
        if calibration["complete"]:
            self._write_final_outputs(calibration)
        else:
            self._save_state()

    def _save_state(self) -> None:
        payload = {
            "schema_version": REMOTE_SESSION_SCHEMA_VERSION,
            "remote_session_id": self.session_id,
            "vocabulary_snapshot_sha256": self.snapshot,
            "answers": self.answers,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    @staticmethod
    def _export_tsv(rows: list[dict[str, Any]]) -> str:
        fields = [
            "lemma",
            "part_of_speech",
            "probability_known",
            "classification",
            "total_count",
            "document_count",
            "zipf",
            "frequency_prior_probability",
            "cefr_level",
            "cefr_adjustment",
            "exam_tags",
            "exam_adjustment",
            "education_adjustment",
            "direct_response",
            "importance_tier",
            "important_boundary_protected",
            "selected_threshold",
        ]
        lines = ["\t".join(fields)]
        for row in rows:
            values = []
            for field in fields:
                value = row.get(field)
                if field == "exam_tags":
                    value = " ".join(value or [])
                elif isinstance(value, bool):
                    value = str(value).lower()
                elif value is None:
                    value = ""
                elif field in {"probability_known", "frequency_prior_probability"}:
                    value = f"{float(value):.6f}"
                elif field in {
                    "cefr_adjustment",
                    "exam_adjustment",
                    "education_adjustment",
                    "selected_threshold",
                }:
                    value = f"{float(value):.2f}"
                elif field == "zipf":
                    value = f"{float(value):.3f}"
                values.append(str(value))
            lines.append("\t".join(values))
        return "\n".join(lines) + "\n"

    def _write_final_outputs(self, calibration: dict[str, Any]) -> None:
        raw_result = calibration.get("result")
        if not isinstance(raw_result, dict) or not isinstance(raw_result.get("rows"), list):
            raise CalibrationServiceError(
                "calibration_response_invalid",
                "The vocabulary prediction service returned an incomplete result.",
            )
        result = dict(raw_result)
        rows = result.pop("rows")
        remote_answers = result.pop("answers", None)
        if not isinstance(remote_answers, list) or len(remote_answers) != 30:
            raise CalibrationServiceError(
                "calibration_response_invalid",
                "The vocabulary prediction service returned incomplete answers.",
            )
        self.answers = remote_answers
        result["completed_at"] = time.time()
        result["answers"] = self.answers
        result["vocabulary_snapshot_sha256"] = self.snapshot
        result["mutation_revision"] = int(calibration.get("mutation_revision") or 0)
        export_content = self._export_tsv(rows)
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        result_temporary = self.result_path.with_suffix(self.result_path.suffix + ".tmp")
        export_temporary = self.export_path.with_suffix(self.export_path.suffix + ".tmp")
        result_temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        export_temporary.write_text(export_content, encoding="utf-8")
        os.replace(export_temporary, self.export_path)
        os.replace(result_temporary, self.result_path)
        self._save_state()
        self._remote_state = self._completed_public_state(result)

    def public_state(self) -> dict[str, Any]:
        if self._remote_state is None:
            raise RuntimeError("Remote calibration state is unavailable")
        return self._remote_state

    def answer(self, lemma: str, response: str) -> None:
        result = self._request(
            "answer",
            {
                "session_id": self.session_id,
                "lemma": lemma,
                "response": response,
            },
        )
        self.answers.append({"lemma": lemma, "response": response})
        self._accept_response(result)

    def set_threshold_percent(
        self,
        threshold_percent: int,
        mutation_revision: int | None = None,
    ) -> None:
        revision = mutation_revision or int(
            (self._remote_state or {}).get("mutation_revision") or 0
        ) + 1
        self._accept_response(
            self._request(
                "threshold",
                {
                    "session_id": self.session_id,
                    "threshold_percent": threshold_percent,
                    "mutation_revision": revision,
                },
            )
        )

    def reset(self, mutation_revision: int | None = None) -> None:
        revision = mutation_revision or int(
            (self._remote_state or {}).get("mutation_revision") or 0
        ) + 1
        response = self._request(
            "reset",
            {"session_id": self.session_id, "mutation_revision": revision},
        )
        self.answers = []
        self.result_path.unlink(missing_ok=True)
        self.export_path.unlink(missing_ok=True)
        self._accept_response(response)

    def persisted_export_tsv(self) -> str:
        if not self.export_path.is_file():
            raise RuntimeError("The personalized vocabulary export is not ready")
        return self.export_path.read_text(encoding="utf-8")

    def personal_vocabulary_mastery(
        self,
        mastered_word_forms: set[str],
    ) -> dict[str, Any] | None:
        if not self.export_path.is_file():
            return None
        try:
            reader = csv.DictReader(
                self.export_path.read_text(encoding="utf-8").splitlines(),
                delimiter="\t",
            )
            rows = list(reader)
        except (OSError, csv.Error):
            return None
        groups = []
        for key, label, tiers in (
            ("priority", "重要与核心词", {"A", "B"}),
            ("other", "其他词", {"C", "D"}),
        ):
            selected = [
                row
                for row in rows
                if row.get("importance_tier") in tiers
                and row.get("classification") != "likely_known"
            ]
            mastered = sum(
                row.get("classification") == "important_boundary"
                or str(row.get("lemma") or "").casefold() in mastered_word_forms
                for row in selected
            )
            total = len(selected)
            groups.append(
                {
                    "key": key,
                    "label": label,
                    "tiers": sorted(tiers),
                    "mastered_count": mastered,
                    "total_count": total,
                    "mastery_percent": round(mastered / total * 100, 1)
                    if total
                    else 0.0,
                }
            )
        return {
            "basis": "personal_vocabulary_calibrated_and_confirmed_mastery",
            "groups": groups,
        }
