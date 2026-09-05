"""Small, stable identity contract shared by the workbench and its launcher."""

from __future__ import annotations


WORKBENCH_SERVICE = "researchramp-workbench"
WORKBENCH_IDENTITY_VERSION = 2
WORKBENCH_IDENTITY_PATH = "/api/identity"
WORKBENCH_ACTIVITY_PATH = "/api/activity"
WORKBENCH_SHUTDOWN_PATH = "/api/shutdown"
DEFAULT_WORKBENCH_IDLE_TIMEOUT_SECONDS = 3_600
