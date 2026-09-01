from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from researchramp_core import OpenAlexClient  # noqa: E402


class JsonResponse(io.BytesIO):
    def __init__(self, value: object) -> None:
        super().__init__(json.dumps(value).encode("utf-8"))

    def __enter__(self) -> JsonResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def response_payload(work_id: str) -> dict[str, object]:
    return {
        "meta": {"count": 1},
        "results": [{"id": f"https://openalex.org/{work_id}"}],
        "group_by": [],
    }


class OpenAlexCacheTests(unittest.TestCase):
    def test_successful_response_is_reused_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = OpenAlexClient(Path(temporary), api_key="first-secret")
            payload = response_payload("W1")
            with patch(
                "researchramp_core.urllib.request.urlopen",
                return_value=JsonResponse(payload),
            ) as urlopen:
                self.assertEqual(client.search("shared query", per_page=10), payload)
                self.assertEqual(client.search("shared query", per_page=10), payload)

            self.assertEqual(urlopen.call_count, 1)
            cache_files = list(Path(temporary).glob("*.json"))
            self.assertEqual(len(cache_files), 1)
            self.assertNotIn("first-secret", cache_files[0].read_text(encoding="utf-8"))

    def test_api_key_does_not_fragment_identical_query_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            first = OpenAlexClient(cache_dir, api_key="first-secret")
            second = OpenAlexClient(cache_dir, api_key="second-secret")
            payload = response_payload("W2")
            with patch(
                "researchramp_core.urllib.request.urlopen",
                return_value=JsonResponse(payload),
            ) as urlopen:
                first.search("same public result", per_page=25)
                self.assertEqual(
                    second.search("same public result", per_page=25),
                    payload,
                )
            self.assertEqual(urlopen.call_count, 1)

    def test_explicit_refresh_replaces_the_cached_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            old_payload = response_payload("W-old")
            new_payload = response_payload("W-new")
            with patch(
                "researchramp_core.urllib.request.urlopen",
                return_value=JsonResponse(old_payload),
            ):
                OpenAlexClient(cache_dir).search("refreshable", per_page=5)
            with patch(
                "researchramp_core.urllib.request.urlopen",
                return_value=JsonResponse(new_payload),
            ) as urlopen:
                result = OpenAlexClient(cache_dir, refresh=True).search(
                    "refreshable", per_page=5
                )

            self.assertEqual(result, new_payload)
            self.assertEqual(urlopen.call_count, 1)
            with patch(
                "researchramp_core.urllib.request.urlopen",
                side_effect=AssertionError("cache miss"),
            ):
                self.assertEqual(
                    OpenAlexClient(cache_dir).search("refreshable", per_page=5),
                    new_payload,
                )

    def test_failed_refresh_keeps_the_last_successful_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            payload = response_payload("W-stable")
            with patch(
                "researchramp_core.urllib.request.urlopen",
                return_value=JsonResponse(payload),
            ):
                OpenAlexClient(cache_dir).search("stable", per_page=5)

            with (
                patch(
                    "researchramp_core.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("offline"),
                ),
                patch("researchramp_core.time.sleep"),
            ):
                with self.assertRaises(RuntimeError):
                    OpenAlexClient(cache_dir, refresh=True).search(
                        "stable", per_page=5
                    )

            with patch(
                "researchramp_core.urllib.request.urlopen",
                side_effect=AssertionError("successful cache was lost"),
            ):
                self.assertEqual(
                    OpenAlexClient(cache_dir).search("stable", per_page=5),
                    payload,
                )


if __name__ == "__main__":
    unittest.main()
