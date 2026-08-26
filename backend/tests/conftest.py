"""Project-level conftest — shared test utilities only.

Do NOT put connector-specific fixtures here; they belong in
``tests/connectors/conftest.py``.
"""

import pytest

import modulo.core.ssrf as _ssrf


@pytest.fixture(autouse=True)
def _allow_test_hostnames(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise real DNS in the SSRF guard for the whole backend suite.

    The SSRF guard (``modulo.core.ssrf``) performs real DNS resolution, which is
    unavailable in CI sandboxes. Every backend test mocks the HTTP layer (respx
    or a patched ``AsyncClient``) against ``example.com`` / ``localhost`` hosts,
    so the guard fails closed on absent DNS and breaks the suite.

    Stubbing ``ssrf._resolve_all_sync`` / ``_resolve_all_async`` makes any
    hostname validate as a public address. This only affects the *validation*
    pre-check: production call sites use ``validate_outbound_url`` (no connection
    pinning), so no test will actually connect to the stubbed address. Literal
    private/loopback IPs remain blocked via a separate DNS-independent code
    path, so the control is not weakened. ``tests/unit/core/test_ssrf.py``
    exercises the REAL resolver (including DNS timeouts and resolution failures),
    so the stub is deliberately skipped there — that file patches the resolver
    itself where it needs a fake answer, and relies on the genuine
    ``loop.getaddrinfo`` path for the timeout/failure assertions.
    """
    # The SSRF unit suite validates the real resolver; never stub it there, or
    # the fail-closed timeout/failure assertions would be silently defeated.
    if "test_ssrf" in getattr(request.node.path, "name", ""):
        return

    monkeypatch.setattr(_ssrf, "_resolve_all_sync", lambda host: ["8.8.8.8"])

    async def _fake_resolve(_host: str) -> list[str]:  # pragma: no cover - test shim
        return ["8.8.8.8"]

    monkeypatch.setattr(_ssrf, "_resolve_all_async", _fake_resolve)


pytest_plugins = ["tests.quarantine_plugin"]
