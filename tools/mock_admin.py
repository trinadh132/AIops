"""Client for the mock service's /admin/failures API. Demo/dev only: it's how
an MCP client can *cause* an incident on purpose. Clearing failures is
deliberately not exposed here; that goes through the remediation queue and
approval flow like any other fix."""

import requests

from tools.runbooks import normalize_failure_type

TIMEOUT_SECONDS = 5


def list_failures(base_url: str) -> dict:
    response = requests.get(f"{base_url.rstrip('/')}/admin/failures", timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def inject_failure(base_url: str, failure_type: str) -> dict:
    mode = normalize_failure_type(failure_type).lower()
    response = requests.post(f"{base_url.rstrip('/')}/admin/failures/{mode}/activate", timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()
