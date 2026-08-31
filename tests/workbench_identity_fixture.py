#!/usr/bin/env python3
"""Minimal subprocess fixture for ResearchRamp launcher lifecycle tests."""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


IDENTITY_PATH = "/api/identity"
SERVICE = "researchramp-workbench"
IDENTITY_VERSION = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--fail-message")
    parser.add_argument("--exit-code", type=int, default=23)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.fail_message is not None:
        print(args.fail_message, flush=True)
        raise SystemExit(args.exit_code)
    if args.delay:
        time.sleep(args.delay)

    identity = {
        "service": SERVICE,
        "identity_version": IDENTITY_VERSION,
        "registry": str(args.registry.expanduser().resolve()),
        "instance_id": args.instance_id,
        "domain_ids": ["domain-a"],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path != IDENTITY_PATH:
                self.send_error(404)
                return
            payload = json.dumps(identity).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
