from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from continuous_state import ContinuousStore  # noqa: E402
from domain_registry import DomainRegistry  # noqa: E402
from vocabulary_cards import card_id  # noqa: E402


SPEC = importlib.util.spec_from_file_location(
    "researchramp_app_domain_tests",
    ROOT / "app" / "server.py",
)
APP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)

FIXTURE = ROOT / "tests" / "fixtures" / "weekly-brief.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_word(index: int) -> object:
    return APP.Word(
        lemma=f"sharedword{index}",
        part_of_speech="noun",
        total_count=40 - index,
        document_count=max(2, 20 - index // 2),
        document_share=0.2,
        zipf=4.0,
        cefr_level=None,
        exam_tags=(),
    )


class NoNetworkClient:
    def request(self, _action: str, _payload: dict) -> dict:
        raise AssertionError("completed domain fixtures must remain offline")


def create_completed_workspace(root: Path, domain_id: str, label: str) -> tuple:
    workspace = root / domain_id
    analysis = workspace / "analysis"
    analysis.mkdir(parents=True)
    (workspace / "research-profile-input.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profile_id": domain_id,
                "title": label,
                "confirmed": True,
            }
        ),
        encoding="utf-8",
    )
    vocabulary = analysis / "vocabulary-map.tsv"
    vocabulary.write_text(
        "lemma\tpart_of_speech\ttotal_count\tdocument_count\tdocument_share\n"
        + "".join(
            f"sharedword{index}\tnoun\t{40-index}\t{max(2, 20-index//2)}\t0.2\n"
            for index in range(30)
        ),
        encoding="utf-8",
    )
    (analysis / "papers.jsonl").write_text("", encoding="utf-8")
    card_rows = [
        {
            "card_id": card_id(f"sharedword{index}", "noun"),
            "sense_key": card_id(f"sharedword{index}", "noun"),
            "lemma": f"sharedword{index}",
            "part_of_speech": "noun",
            "meaning_en": "Synthetic word definition.",
            "meaning_zh": "合成词汇释义。",
            "meaning_origin": "agent",
            "source_paper_id": "W1",
            "source_title": "Synthetic source",
            "source_url": "https://example.invalid/source",
            "context": "Synthetic source context.",
            "total_count": 40 - index,
            "document_count": max(2, 20 - index // 2),
            "document_share": 0.2,
        }
        for index in range(30)
    ]
    card_rows.extend(
        [
            {
                "card_id": card_id(lemma, "noun"),
                "sense_key": card_id(lemma, "noun"),
                "lemma": lemma,
                "part_of_speech": "noun",
                "meaning_en": meaning_en,
                "meaning_zh": meaning_zh,
                "meaning_origin": "agent",
                "source_paper_id": "W1",
                "source_title": "Synthetic source",
                "source_url": "https://example.invalid/source",
                "context": f"Synthetic source context for {lemma}.",
                "total_count": 10,
                "document_count": 2,
                "document_share": 0.2,
            }
            for lemma, meaning_en, meaning_zh in (
                ("alpha", "A synthetic term used in the Alpha test domain.", "测试领域 Alpha 中使用的合成术语"),
                ("isolation", "The separation of distinct data spaces.", "不同数据空间彼此分离"),
                ("provenance", "A verifiable link between content and its source.", "内容及其来源之间可验证的对应关系"),
            )
        ]
    )
    (analysis / "vocabulary-card-catalog.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in card_rows), encoding="utf-8"
    )
    (analysis / "first-terminology-map.jsonl").write_text("", encoding="utf-8")
    (analysis / "terminology-explanations.json").write_text("{}\n", encoding="utf-8")
    (analysis / "host-review-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reviewer": "current-host-agent",
                "review_passes": 1,
                "terminology_candidate_count": 0,
                "selected_terminology_count": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    words = [make_word(index) for index in range(30)]
    state = analysis / "vocabulary-calibration-session.json"
    answers = [
        {
            "lemma": item.lemma,
            "response": "known" if index % 2 else "unknown",
        }
        for index, item in enumerate(words)
    ]
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "remote_session_id": f"{domain_id}-completed-fixture",
                "answers": answers,
            }
        ),
        encoding="utf-8",
    )
    export = analysis / "personalized-vocabulary.tsv"
    export.write_text(
        "lemma\tclassification\n"
        + "".join(
            f"{item.lemma}\t{'likely_known' if index % 2 else 'likely_unknown'}\n"
            for index, item in enumerate(words)
        ),
        encoding="utf-8",
    )
    result = {
        "counts": {
            "total": 30,
            "likely_known": 15,
            "uncertain": 0,
            "likely_unknown": 15,
            "remaining_after_conservative_exclusion": 15,
            "important_boundary_protected": 0,
        },
        "threshold": {"selected_percent": 90},
        "importance": {"tiers": []},
        "known_boundary": [],
        "remaining_boundary": [],
        "answers": answers,
        "personalized_vocabulary_sha256": hashlib.sha256(
            export.read_bytes()
        ).hexdigest(),
    }
    (analysis / "vocabulary-calibration-result.json").write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    session = APP.RemoteCalibrationSession(
        words,
        state,
        label,
        client=NoNetworkClient(),
        license_path=analysis / "missing-license.rrlicense",
    )
    store = ContinuousStore(workspace, domain_id=domain_id, display_name=label)
    brief = json.loads(FIXTURE.read_text(encoding="utf-8"))
    brief["headline"] = f"TEST FIXTURE — {label} brief"
    brief["items"][0]["title"] = f"TEST FIXTURE — {label} paper"
    brief["items"][0]["source_url"] = (
        f"https://example.invalid/researchramp/{domain_id}/paper"
    )
    store.import_brief(brief)
    return workspace, session, store


class DomainIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.alpha = create_completed_workspace(self.root, "alpha", "Alpha Research")
        self.beta = create_completed_workspace(self.root, "beta", "Beta Research")
        self.registry_path = self.root / "library" / "domains.json"
        self.registry = DomainRegistry(self.registry_path)
        alpha_registration = self.registry.register(
            self.alpha[0], display_name="Alpha Research", domain_id="alpha"
        )
        beta_registration = self.registry.register(
            self.beta[0], display_name="Beta Research", domain_id="beta"
        )
        self.runtime = APP.AppRuntime(
            [
                APP.DomainContext(
                    "alpha", "Alpha Research", self.alpha[0], self.alpha[1], self.alpha[2]
                ),
                APP.DomainContext(
                    "beta", "Beta Research", self.beta[0], self.beta[1], self.beta[2]
                ),
            ],
            initial_domain_id=beta_registration.domain_id,
            initial_view="vocabulary",
            registry=self.registry,
        )
        self.assertEqual(alpha_registration.domain_id, "alpha")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_contains_only_explicit_workspaces_and_never_merges_them(self) -> None:
        reloaded = DomainRegistry(self.registry_path)
        self.assertEqual([item.domain_id for item in reloaded.domains], ["alpha", "beta"])
        self.assertEqual(reloaded.active_domain_id, "beta")
        self.assertNotEqual(reloaded.get("alpha").workspace, reloaded.get("beta").workspace)
        with self.assertRaisesRegex(ValueError, "another workspace"):
            reloaded.register(self.beta[0], domain_id="alpha")

    def test_actions_in_alpha_leave_beta_and_initial_snapshot_untouched(self) -> None:
        alpha_result = self.alpha[0] / "analysis" / "vocabulary-calibration-result.json"
        alpha_export = self.alpha[0] / "analysis" / "personalized-vocabulary.tsv"
        beta_result = self.beta[0] / "analysis" / "vocabulary-calibration-result.json"
        beta_export = self.beta[0] / "analysis" / "personalized-vocabulary.tsv"
        alpha_before = (digest(alpha_result), digest(alpha_export))
        beta_before = (digest(beta_result), digest(beta_export))

        alpha_store = self.runtime.context("alpha").continuous_store
        beta_store = self.runtime.context("beta").continuous_store
        assert alpha_store is not None and beta_store is not None
        paper = alpha_store.start_preheat("fixture-paper-alpha")
        alpha_word = next(
            item for item in paper["vocabulary"] if item["lemma"] == "alpha"
        )
        alpha_store.mark_item_mastered(alpha_word["item_id"])
        alpha_store.save_settings(
            {
                "weekly_brief": {"enabled": True, "weekday": 3, "time": "08:30"},
                "daily_review": {"enabled": True, "time": "18:30"},
            }
        )

        self.assertEqual((digest(alpha_result), digest(alpha_export)), alpha_before)
        self.assertEqual((digest(beta_result), digest(beta_export)), beta_before)
        self.assertEqual(beta_store.summary()["learning_count"], 0)
        self.assertEqual(beta_store.get_settings()["weekly_brief"]["time"], "09:00")
        self.assertNotEqual(
            alpha_store.automation_handoff()["weekly_brief"]["automation_key"],
            beta_store.automation_handoff()["weekly_brief"]["automation_key"],
        )

        self.assertEqual((digest(beta_result), digest(beta_export)), beta_before)

    def test_app_state_switches_all_domain_scoped_views_together(self) -> None:
        alpha = self.runtime.app_state("alpha")
        beta = self.runtime.app_state("beta")
        self.assertEqual(alpha["domain_id"], "alpha")
        self.assertEqual(beta["domain_id"], "beta")
        self.assertIn("Alpha Research", alpha["briefs"][0]["items"][0]["title"])
        self.assertIn("Beta Research", beta["briefs"][0]["items"][0]["title"])
        self.assertTrue(alpha["domain_switching"])
        self.assertEqual(len(alpha["domains"]), 2)
        self.assertEqual(alpha["terminology"], {"count": 0, "terms": []})
        self.assertEqual(beta["terminology"], {"count": 0, "terms": []})

    def test_standalone_content_fingerprint_changes_when_file_is_replaced(self) -> None:
        vocabulary = self.root / "standalone.tsv"
        vocabulary.write_text(
            "lemma\tpart_of_speech\ttotal_count\tdocument_count\tdocument_share\n"
            "alpha\tnoun\t2\t2\t1.0\n",
            encoding="utf-8",
        )
        args = SimpleNamespace(
            corpus=None, vocabulary=vocabulary, state=None, label=None
        )
        first_state = APP.resolve_calibration_paths(args)[1]
        vocabulary.write_text(
            "lemma\tpart_of_speech\ttotal_count\tdocument_count\tdocument_share\n"
            "beta\tnoun\t2\t2\t1.0\n",
            encoding="utf-8",
        )
        second_state = APP.resolve_calibration_paths(args)[1]
        self.assertNotEqual(first_state, second_state)


class DomainRoutingHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        alpha = create_completed_workspace(root, "alpha", "Alpha HTTP")
        beta = create_completed_workspace(root, "beta", "Beta HTTP")
        runtime = APP.AppRuntime(
            [
                APP.DomainContext("alpha", "Alpha HTTP", alpha[0], alpha[1], alpha[2]),
                APP.DomainContext("beta", "Beta HTTP", beta[0], beta[1], beta[2]),
            ],
            initial_domain_id="alpha",
            initial_view="vocabulary",
        )

        class Handler(APP.AppHandler):
            def __init__(
                self,
                method: str,
                path: str,
                body: dict | None,
                headers: dict[str, str],
            ):
                self.command = method
                self.path = path
                encoded = json.dumps(body or {}).encode("utf-8") if method == "POST" else b""
                self.rfile = io.BytesIO(encoded)
                self.wfile = io.BytesIO()
                self.headers = Message()
                self.headers["Content-Length"] = str(len(encoded))
                for key, value in headers.items():
                    self.headers[key] = value
                self.response_status = None

            def send_response(self, code: int, message: str | None = None) -> None:
                self.response_status = code

            def send_header(self, keyword: str, value: str) -> None:
                return

            def end_headers(self) -> None:
                return

        Handler.runtime = runtime
        Handler.static_dir = ROOT / "app" / "static"
        Handler.exit_on_settings_save = False
        self.runtime = runtime
        self.Handler = Handler

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        handler = self.Handler(method, path, body, request_headers)
        if method == "POST":
            handler.do_POST()
        else:
            handler.do_GET()
        data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        return int(handler.response_status), data

    def test_scoped_requests_require_and_echo_the_exact_domain(self) -> None:
        status, alpha = self.request("GET", "/api/app-state?domain_id=alpha")
        self.assertEqual(status, 200)
        self.assertEqual(alpha["domain_id"], "alpha")
        self.assertEqual(alpha["api_version"], APP.APP_API_VERSION)

        status, _ = self.request(
            "POST",
            "/api/preheat/start",
            {"paper_id": "fixture-paper-alpha"},
            {"X-ResearchRamp-API-Version": str(APP.APP_API_VERSION)},
        )
        self.assertEqual(status, 400)

        status, result = self.request(
            "POST",
            "/api/preheat/start?domain_id=alpha",
            {"paper_id": "fixture-paper-alpha"},
            {
                "X-ResearchRamp-API-Version": str(APP.APP_API_VERSION),
                "X-ResearchRamp-Domain": "alpha",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["domain_id"], "alpha")
        self.assertEqual(self.runtime.context("beta").continuous_store.summary()["learning_count"], 0)

        status, _ = self.request(
            "POST",
            "/api/vocabulary/known?domain_id=alpha",
            {"lemma": "alpha"},
            {
                "X-ResearchRamp-API-Version": str(APP.APP_API_VERSION),
                "X-ResearchRamp-Domain": "beta",
            },
        )
        self.assertEqual(status, 400)

        status, _ = self.request(
            "POST",
            "/api/vocabulary/known?domain_id=unknown",
            {"lemma": "alpha"},
            {
                "X-ResearchRamp-API-Version": str(APP.APP_API_VERSION),
                "X-ResearchRamp-Domain": "unknown",
            },
        )
        self.assertEqual(status, 400)

    def test_old_frontend_cannot_mutate_new_backend(self) -> None:
        beta_store = self.runtime.context("beta").continuous_store
        before = beta_store.summary()
        status, data = self.request(
            "POST",
            "/api/vocabulary/known?domain_id=beta",
            {"lemma": "alpha"},
            {"X-ResearchRamp-Domain": "beta"},
        )
        self.assertEqual(status, 409)
        self.assertIn("版本不一致", data["error"])
        self.assertEqual(beta_store.summary(), before)

    def test_new_word_routes_use_precalibrated_cards_without_a_preheat_action(self) -> None:
        headers = {
            "X-ResearchRamp-API-Version": str(APP.APP_API_VERSION),
            "X-ResearchRamp-Domain": "alpha",
        }
        status, candidates = self.request(
            "GET", "/api/learning/new-words?domain_id=alpha&limit=5", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(candidates["domain_id"], "alpha")
        self.assertGreater(candidates["count"], 0)
        card = candidates["cards"][0]
        self.assertEqual(card["classification"], "likely_unknown")
        self.assertTrue(card["meaning_zh"])

        status, result = self.request(
            "POST",
            "/api/learning/new-word-status?domain_id=alpha",
            {"card_id": card["card_id"], "status": "learning"},
            headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["item_id"])
        self.assertEqual(result["continuous"]["learning_count"], 1)


if __name__ == "__main__":
    unittest.main()
