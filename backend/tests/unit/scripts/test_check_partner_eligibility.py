"""Unit tests for the OSS Partner eligibility checker.

The GitHub network layer (``gather_repo_evidence``) is patched so no network
calls are made -- every scenario is driven from a synthetic evidence dict.
"""

from __future__ import annotations

import datetime as _dt
import json

import check_partner_eligibility as mod
import pytest
from check_partner_eligibility import GithubApiError


def _base_evidence(now: _dt.datetime) -> dict:
    """A fully-qualifying repo as of *now*."""
    return {
        "repo_id": 123456,
        "full_name": "acme/widgets",
        "private": False,
        "fork": False,
        "archived": False,
        "stargazers_count": 1500,
        "created_at": (now - _dt.timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spdx_id": "MIT",
        "license_file_present": True,
        "tags": [{"name": "v1.0.0"}],
        "commits": [
            {"author": {"login": "alice"}},
            {"author": {"login": "bob"}},
            {"author": {"login": "dependabot[bot]"}},
        ],
        "now": now,
    }


@pytest.fixture()
def now() -> _dt.datetime:
    return _dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=_dt.UTC)


def test_fully_qualifying_repo_is_eligible(now: _dt.datetime) -> None:
    result = mod.evaluate_evidence(_base_evidence(now))
    assert result["verdict"] == mod.VERDICT_ELIGIBLE
    assert result["repo_id"] == 123456
    assert all(c["status"] == mod.STATUS_PASS for c in result["criteria"].values())


def test_fork_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["fork"] = True
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["public"]["reason"] == mod.RC_IS_FORK


def test_archived_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["archived"] = True
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["public"]["reason"] == mod.RC_IS_ARCHIVED


def test_private_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["private"] = True
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["public"]["reason"] == mod.RC_NOT_PUBLIC


def test_below_stars_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["stargazers_count"] = 42
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["stars"]["reason"] == mod.RC_BELOW_STARS


def test_too_young_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["created_at"] = (now - _dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["age"]["reason"] == mod.RC_TOO_YOUNG


def test_no_tagged_release_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["tags"] = []
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["release"]["reason"] == mod.RC_NO_TAGGED_RELEASE


def test_insufficient_committers_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    # Only one human committer (the other entries are bots).
    ev["commits"] = [
        {"author": {"login": "alice"}},
        {"author": {"login": "dependabot[bot]"}},
        {"committer": {"login": "renovate[bot]"}},
    ]
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["committers"]["reason"] == mod.RC_INSUFFICIENT_COMMITTERS


def test_license_file_fallback_passes(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["spdx_id"] = "NOASSERTION"
    # licence file present -> fallback pass.
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["license"]["status"] == mod.STATUS_PASS
    assert result["criteria"]["license"]["reason"] == mod.RC_LICENSE_FILE_FALLBACK


def test_no_osi_license_fails(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["spdx_id"] = "NOASSERTION"
    ev["license_file_present"] = False
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["license"]["status"] == mod.STATUS_FAIL
    assert result["criteria"]["license"]["reason"] == mod.RC_NO_OSI_LICENSE


def test_api_failure_is_inconclusive(monkeypatch, now: _dt.datetime) -> None:
    def _raise(target: str) -> dict:
        raise GithubApiError("rate limit")

    monkeypatch.setattr(mod, "gather_repo_evidence", _raise)
    result = mod.check_eligibility("acme/widgets")
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE
    assert "escalation" in result


def test_json_output_shape(
    now: _dt.datetime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev = _base_evidence(now)

    def _fake(target: str) -> dict:
        return ev

    monkeypatch.setattr(mod, "gather_repo_evidence", _fake)
    mod.main(["acme/widgets", "--json"])

    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert parsed["verdict"] == mod.VERDICT_ELIGIBLE
    for key in ("repo_id", "full_name", "verdict", "criteria", "evidence"):
        assert key in parsed
    assert "public" in parsed["criteria"]
    assert "license" in parsed["criteria"]


def test_parse_target_variants() -> None:
    assert mod.parse_target("https://github.com/foo/bar") == "foo/bar"
    assert mod.parse_target("http://github.com/foo/bar/") == "foo/bar"
    assert mod.parse_target("github.com/foo/bar") == "foo/bar"
    assert mod.parse_target("foo/bar") == "foo/bar"
    assert mod.parse_target("foo/bar.git") == "foo/bar"
    with pytest.raises(ValueError):
        mod.parse_target("not-a-repo")
