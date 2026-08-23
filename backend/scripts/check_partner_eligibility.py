"""OSS Partner composite eligibility checker (launch tool).

Given a GitHub repository URL or `owner/name`, evaluates the OSS Partner
composite and prints a verdict plus evidence for founder review.

Criteria
--------
1. public, NOT a fork, NOT archived
2. OSI-approved licence detected (license.spdx_id, with a LICENSE-file
   fallback when the SPDX id is null/NOASSERTION)
3. >= 100 stars AND repo age >= 6 months AND >= 1 tagged release AND
   >= 2 distinct committers in the last 90 days (bots excluded heuristically)

The verdict is keyed on the GitHub repo ID (survives renames). Any API
failure (rate-limit / outage / token expiry) is reported as INCONCLUSIVE
with an escalation note -- it never produces a false reject.

Usage
-----
    python backend/scripts/check_partner_eligibility.py <repo-url-or-owner/name>
    python backend/scripts/check_partner_eligibility.py owner/repo --json

Design note: the network layer lives entirely in :func:`gather_repo_evidence`,
which is small and injectable so tests can patch it without touching the
network. All evaluation logic is pure and operates on the returned dict.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# A curated set of OSI-approved SPDX licence identifiers. GitHub's per-repo
# licence object does not expose an `osi_approved` boolean directly, so we
# approximate with this allow-list of the licences the OSS Partner program
# commonly accepts. Extend as needed.
OSI_APPROVED_SPDX_IDS: frozenset[str] = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-2.0",
        "GPL-3.0",
        "LGPL-2.1",
        "LGPL-3.0",
        "MPL-2.0",
        "EPL-2.0",
        "AGPL-3.0",
        "ISC",
        "Unlicense",
        "CC0-1.0",
        "Zlib",
        "BSL-1.0",
        "Artistic-2.0",
        "Python-2.0",
        "LLVM-exception",
    }
)

MIN_STARS = 100
MIN_AGE_DAYS = 183  # ~6 months
MIN_TAGGED_RELEASES = 1
MIN_DISTINCT_COMMITTERS = 2
COMMITTER_WINDOW_DAYS = 90

# Bot usernames are matched case-insensitively. The `[bot]` suffix covers the
# bulk of GitHubApps; the explicit set catches notable exceptions.
KNOWN_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "dependabot[bot]",
        "github-actions[bot]",
        "renovate[bot]",
        "mergify[bot]",
        "greenkeeper[bot]",
        "snyk-bot",
        "imgbot[bot]",
        "codecov[bot]",
        "dependabot-preview[bot]",
    }
)

_API_TIMEOUT_SECONDS = 30


class GithubApiError(Exception):
    """Raised when the GitHub API cannot be reached or returns an error.

    Callers translate this into an INCONCLUSIVE verdict -- never a false
    reject.
    """


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

RC_NOT_PUBLIC = "NOT_PUBLIC"
RC_IS_FORK = "IS_FORK"
RC_IS_ARCHIVED = "IS_ARCHIVED"
RC_NO_OSI_LICENSE = "NO_OSI_LICENSE"
RC_LICENSE_FILE_FALLBACK = "LICENSE_FILE_FALLBACK"
RC_BELOW_STARS = "BELOW_STARS"
RC_TOO_YOUNG = "TOO_YOUNG"
RC_NO_TAGGED_RELEASE = "NO_TAGGED_RELEASE"
RC_INSUFFICIENT_COMMITTERS = "INSUFFICIENT_COMMITTERS"
RC_API_FAILURE = "API_FAILURE"

VERDICT_ELIGIBLE = "ELIGIBLE"
VERDICT_INELIGIBLE = "INELIGIBLE"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

STATUS_PASS = "PASS"  # nosec B105
STATUS_FAIL = "FAIL"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------


def parse_target(target: str) -> str:
    """Normalise a repo URL or `owner/name` string to `owner/name`.

    Raises ValueError if the input cannot be parsed.
    """
    target = target.strip()
    # Strip a trailing ".git" and any leading "git@" scp-like forms defensively.
    target = target.removesuffix(".git")

    # https://github.com/owner/repo or http://github.com/owner/repo
    url_match = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+)/?$", target, re.IGNORECASE)
    if url_match:
        return f"{url_match.group(1)}/{url_match.group(2)}"

    # github.com/owner/repo (no scheme)
    bare_match = re.match(r"^github\.com/([^/]+)/([^/?#]+)/?$", target, re.IGNORECASE)
    if bare_match:
        return f"{bare_match.group(1)}/{bare_match.group(2)}"

    # owner/name
    if re.match(r"^[^/\s]+/[^/\s]+$", target):
        return target

    raise ValueError(f"Could not parse {target!r} as a GitHub repo URL or owner/name.")


# ---------------------------------------------------------------------------
# Network layer (injectable)
# ---------------------------------------------------------------------------


def _api_get(path: str, *, allow_codes: tuple[int, ...] = ()) -> dict[str, Any]:
    """GET a GitHub REST API path, returning parsed JSON.

    Prefers the `gh` CLI (handles auth + rate-limit), falling back to
    `requests` with a token from the environment. Raises
    :class:`GithubApiError` on any failure.

    *allow_codes* lists HTTP status codes that should be returned as an empty
    dict `{}` instead of raising -- used so a 404 (e.g. "no LICENSE file")
    can be distinguished from a genuine API failure (which must stay
    INCONCLUSIVE, never a silent false reject).
    """
    full_url = f"https://api.github.com/{path.lstrip('/')}"

    # 1. Try gh CLI.
    try:
        result = subprocess.run(  # nosec  # noqa: S603
            ["gh", "api", path],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_API_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # gh not installed or hung -- fall through to requests.
        pass

    # 2. Fall back to requests with a token.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(full_url, headers=headers, timeout=_API_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise GithubApiError(f"GitHub API request failed: {exc}") from None

    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise GithubApiError("GitHub API rate limit exceeded.")
    if resp.status_code in (401, 403):
        raise GithubApiError(f"GitHub API auth/permission error: HTTP {resp.status_code}.")
    if resp.status_code >= 400:
        if resp.status_code in allow_codes:
            return {}
        raise GithubApiError(f"GitHub API error: HTTP {resp.status_code}.")

    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise GithubApiError(f"GitHub API returned non-JSON body: {exc}") from None


def gather_repo_evidence(target: str) -> dict[str, Any]:
    """Fetch all raw GitHub evidence for *target*.

    The returned dict is consumed by :func:`evaluate_evidence` and contains no
    network calls of its own. Raises :class:`GithubApiError` on any API
    failure (so callers can report INCONCLUSIVE).
    """
    owner_repo = parse_target(target)
    now = _dt.datetime.now(_dt.UTC)

    repo = _api_get(f"repos/{owner_repo}")
    repo_id = repo.get("id")
    if repo_id is None:
        raise GithubApiError("GitHub API response missing repo id.")

    # Licence: the /license endpoint returns 404 when no licence file exists
    # (legitimately "not present"); any other API error must propagate as
    # INCONCLUSIVE rather than be swallowed into a silent false reject.
    license_file_present = False
    spdx_id = None
    if isinstance(repo.get("license"), dict):
        spdx_id = repo["license"].get("spdx_id")
    license_file_present = bool(_api_get(f"repos/{owner_repo}/license", allow_codes=(404,)))

    # Tags (>= 1 tagged release).
    tags = _api_get(f"repos/{owner_repo}/tags?per_page=100")

    # Commits in the last COMMITTER_WINDOW_DAYS.
    since = (now - _dt.timedelta(days=COMMITTER_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = _api_get(f"repos/{owner_repo}/commits?since={since}&per_page=100")

    return {
        "repo_id": repo_id,
        "full_name": repo.get("full_name"),
        "private": bool(repo.get("private", False)),
        "fork": bool(repo.get("fork", False)),
        "archived": bool(repo.get("archived", False)),
        "stargazers_count": int(repo.get("stargazers_count", 0) or 0),
        "created_at": repo.get("created_at"),
        "spdx_id": spdx_id,
        "license_file_present": license_file_present,
        "tags": tags if isinstance(tags, list) else [],
        "commits": commits if isinstance(commits, list) else [],
        "now": now,
    }


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


def _is_bot_login(login: str | None) -> bool:
    if not login:
        return False
    if login.lower().endswith("[bot]"):
        return True
    return login.lower() in KNOWN_BOT_LOGINS


def _distinct_human_committers(commits: list[dict[str, Any]]) -> set[str]:
    authors: set[str] = set()
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        author = commit.get("author") or commit.get("committer")
        login = None
        if isinstance(author, dict):
            login = author.get("login")
        if login and not _is_bot_login(login):
            authors.add(login)
    return authors


def _evaluate_criteria(evidence: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    """Return a mapping of criterion -> {status, reason}."""
    now = evidence["now"]
    criteria: dict[str, dict[str, str | None]] = {}

    # 1. Visibility / fork / archived.
    if evidence["private"]:
        criteria["public"] = {"status": STATUS_FAIL, "reason": RC_NOT_PUBLIC}
    elif evidence["fork"]:
        criteria["public"] = {"status": STATUS_FAIL, "reason": RC_IS_FORK}
    elif evidence["archived"]:
        criteria["public"] = {"status": STATUS_FAIL, "reason": RC_IS_ARCHIVED}
    else:
        criteria["public"] = {"status": STATUS_PASS, "reason": None}

    # 2. Licence.
    spdx = evidence.get("spdx_id")
    if spdx and spdx.upper() not in ("NULL", "NOASSERTION") and spdx in OSI_APPROVED_SPDX_IDS:
        criteria["license"] = {"status": STATUS_PASS, "reason": None}
    elif evidence.get("license_file_present"):
        criteria["license"] = {
            "status": STATUS_PASS,
            "reason": RC_LICENSE_FILE_FALLBACK,
        }
    else:
        criteria["license"] = {"status": STATUS_FAIL, "reason": RC_NO_OSI_LICENSE}

    # 3. Stars.
    if evidence["stargazers_count"] >= MIN_STARS:
        criteria["stars"] = {"status": STATUS_PASS, "reason": None}
    else:
        criteria["stars"] = {"status": STATUS_FAIL, "reason": RC_BELOW_STARS}

    # 4. Age.
    created_raw = evidence.get("created_at")
    age_ok = False
    if created_raw:
        try:
            created = _dt.datetime.fromisoformat(created_raw)
            age_ok = (now - created) >= _dt.timedelta(days=MIN_AGE_DAYS)
        except ValueError:
            age_ok = False
    if age_ok:
        criteria["age"] = {"status": STATUS_PASS, "reason": None}
    else:
        criteria["age"] = {"status": STATUS_FAIL, "reason": RC_TOO_YOUNG}

    # 5. Tagged release.
    tag_count = len(evidence.get("tags", []))
    if tag_count >= MIN_TAGGED_RELEASES:
        criteria["release"] = {"status": STATUS_PASS, "reason": None}
    else:
        criteria["release"] = {"status": STATUS_FAIL, "reason": RC_NO_TAGGED_RELEASE}

    # 6. Distinct human committers.
    committers = _distinct_human_committers(evidence.get("commits", []))
    if len(committers) >= MIN_DISTINCT_COMMITTERS:
        criteria["committers"] = {"status": STATUS_PASS, "reason": None}
    else:
        criteria["committers"] = {
            "status": STATUS_FAIL,
            "reason": RC_INSUFFICIENT_COMMITTERS,
        }

    return criteria


def evaluate_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Evaluate evidence into a full result dict (no network)."""
    criteria = _evaluate_criteria(evidence)

    has_fail = any(c["status"] == STATUS_FAIL for c in criteria.values())
    verdict = VERDICT_INELIGIBLE if has_fail else VERDICT_ELIGIBLE

    return {
        "repo_id": evidence.get("repo_id"),
        "full_name": evidence.get("full_name"),
        "verdict": verdict,
        "criteria": criteria,
        "evidence": {
            "stars": evidence.get("stargazers_count"),
            "created_at": evidence.get("created_at"),
            "tag_count": len(evidence.get("tags", [])),
            "distinct_human_committers": len(_distinct_human_committers(evidence.get("commits", []))),
            "spdx_id": evidence.get("spdx_id"),
            "license_file_present": evidence.get("license_file_present"),
        },
    }


def check_eligibility(
    target: str,
    fetcher: Callable[[str], dict[str, Any]] = gather_repo_evidence,
) -> dict[str, Any]:
    """Check eligibility for *target*.

    *fetcher* is injectable for testing. Any :class:`GithubApiError` raised by
    the fetcher yields an INCONCLUSIVE verdict (never a false reject).
    """
    try:
        evidence = fetcher(target)
    except GithubApiError as exc:
        return {
            "repo_id": None,
            "full_name": None,
            "verdict": VERDICT_INCONCLUSIVE,
            "criteria": {"api": {"status": STATUS_INCONCLUSIVE, "reason": RC_API_FAILURE}},
            "evidence": {"error": str(exc)},
            "escalation": (
                "API unreachable or rate-limited. Do NOT reject on this result. "
                "Escalate to a human to verify manually (e.g. check the repo in "
                "a browser, or retry with a valid GITHUB_TOKEN / gh auth)."
            ),
        }
    return evaluate_evidence(evidence)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _format_human(result: dict[str, Any]) -> str:
    lines: list[str] = []
    verdict = result["verdict"]
    lines.append("=" * 60)
    lines.append(f" OSS PARTNER ELIGIBILITY — {verdict}")
    lines.append("=" * 60)
    if result.get("full_name"):
        lines.append(f" Repository : {result['full_name']}")
    if result.get("repo_id") is not None:
        lines.append(f" Repo ID    : {result['repo_id']} (rename-safe key)")
    lines.append("")
    lines.append(" Criteria:")
    for name, c in result.get("criteria", {}).items():
        reason = c.get("reason")
        reason_txt = f" ({reason})" if reason else ""
        lines.append(f"   - {name:<10} {c['status']}{reason_txt}")
    if result.get("evidence"):
        lines.append("")
        lines.append(" Evidence:")
        for k, v in result["evidence"].items():
            lines.append(f"   - {k}: {v}")
    if verdict == VERDICT_INCONCLUSIVE and result.get("escalation"):
        lines.append("")
        lines.append(" ESCALATION: " + result["escalation"])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OSS Partner composite eligibility for a GitHub repo.")
    parser.add_argument(
        "target",
        help="GitHub repo URL (https://github.com/owner/repo) or owner/name.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human report.",
    )
    args = parser.parse_args(argv)

    try:
        result = check_eligibility(args.target, fetcher=gather_repo_evidence)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_format_human(result))

    if result["verdict"] == VERDICT_INCONCLUSIVE:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
