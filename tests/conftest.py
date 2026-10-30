"""Shared fixtures: external-tool absence is pinned, not assumed.

egress-plane and acl-guard-audit resolve via flag > env var > PATH (KTD3);
pointing the env vars at a nonexistent path keeps absence-of-tool tests
and the e2e goldens independent of whatever happens to be on PATH. Tests
that set these vars themselves still override. PATH itself is untouched:
git, go, and cargo must stay reachable for repo fixtures and helpers.
"""

import pytest

_SENTINEL = "/nonexistent/rsd-tool-sentinel"


@pytest.fixture(autouse=True)
def _external_tools_absent(monkeypatch):
    monkeypatch.setenv("RSD_EGRESS_PLANE", _SENTINEL)
    monkeypatch.setenv("RSD_ACL_GUARD_AUDIT", _SENTINEL)
