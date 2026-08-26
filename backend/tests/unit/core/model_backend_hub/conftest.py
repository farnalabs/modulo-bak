"""Test-only shim: let connector/model/BDD suites skip real DNS in SSRF checks.

The SSRF guard (``modulo.core.ssrf``) performs real DNS resolution. In CI
sandboxes there is no network, and every connector / model-backend / BDD test
mocks the HTTP layer (respx or a patched ``AsyncClient``) against ``example.com``
or ``localhost`` hosts. We make any hostname validate as a public address so the
guard no longer fails closed on absent DNS.

Literal private/loopback IPs are still enforced by a separate code path that
does NOT consult DNS, so this shim cannot weaken that control. The dedicated
``tests/unit/core/test_ssrf.py`` suite exercises the real resolver and is not
covered by this fixture.
"""

import pytest

import modulo.core.ssrf as _ssrf


@pytest.fixture(autouse=True)
def _allow_test_hostnames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ssrf, "_resolve_all_sync", lambda host: ["8.8.8.8"])

    async def _fake_resolve(_host: str) -> list[str]:  # pragma: no cover - test shim
        return ["8.8.8.8"]

    monkeypatch.setattr(_ssrf, "_resolve_all_async", _fake_resolve)
