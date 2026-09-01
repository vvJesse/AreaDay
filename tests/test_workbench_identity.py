from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from domain_registry import DomainRegistry  # noqa: E402


SPEC = importlib.util.spec_from_file_location(
    "researchramp_workbench_identity_tests",
    ROOT / "app" / "server.py",
)
APP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def context(domain_id: str) -> object:
    return APP.DomainContext(
        domain_id=domain_id,
        display_name=f"Domain {domain_id}",
        workspace=None,
        session=object(),
        continuous_store=None,
    )


class WorkbenchIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def library_runtime(self, instance_id: str = "library-instance") -> object:
        registry_path = self.root / "library" / "nested" / ".." / "domains.json"
        registry = DomainRegistry(registry_path)
        return APP.AppRuntime(
            [context("alpha")],
            initial_domain_id="alpha",
            initial_view="vocabulary",
            registry=registry,
            instance_id=instance_id,
        )

    def test_library_identity_is_exact_and_registry_is_canonical(self) -> None:
        runtime = self.library_runtime()

        self.assertEqual(
            runtime.identity(),
            {
                "service": "researchramp-workbench",
                "identity_version": APP.WORKBENCH_IDENTITY_VERSION,
                "registry": str(
                    (self.root / "library" / "domains.json").resolve()
                ),
                "instance_id": "library-instance",
                "domain_ids": ["alpha"],
            },
        )

    def test_standalone_identity_has_no_registry(self) -> None:
        runtime = APP.AppRuntime(
            [context("standalone")],
            initial_domain_id="standalone",
            initial_view="vocabulary",
            standalone=True,
            instance_id="standalone-instance",
        )

        self.assertEqual(
            runtime.identity(),
            {
                "service": "researchramp-workbench",
                "identity_version": APP.WORKBENCH_IDENTITY_VERSION,
                "registry": None,
                "instance_id": "standalone-instance",
                "domain_ids": ["standalone"],
            },
        )

    def test_identity_endpoint_needs_no_domain_and_returns_exact_contract(self) -> None:
        runtime = self.library_runtime(instance_id="http-instance")

        class Handler(APP.AppHandler):
            def __init__(self) -> None:
                self.command = "GET"
                self.path = "/api/identity"
                self.rfile = io.BytesIO()
                self.wfile = io.BytesIO()
                self.headers = Message()
                self.response_status: int | None = None

            def send_response(self, code: int, message: str | None = None) -> None:
                self.response_status = code

            def send_header(self, keyword: str, value: str) -> None:
                return

            def end_headers(self) -> None:
                return

        Handler.runtime = APP.AppRuntime(
            [context("beta"), context("alpha")],
            initial_domain_id="alpha",
            initial_view="vocabulary",
            registry=runtime.registry,
            instance_id="http-instance",
        )
        Handler.static_dir = ROOT / "app" / "static"

        handler = Handler()
        handler.do_GET()

        self.assertEqual(handler.response_status, 200)
        self.assertEqual(
            json.loads(handler.wfile.getvalue().decode("utf-8")),
            {
                "service": "researchramp-workbench",
                "identity_version": APP.WORKBENCH_IDENTITY_VERSION,
                "registry": str(
                    (self.root / "library" / "domains.json").resolve()
                ),
                "instance_id": "http-instance",
                "domain_ids": ["alpha", "beta"],
            },
        )


if __name__ == "__main__":
    unittest.main()
