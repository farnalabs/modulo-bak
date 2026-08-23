"""Canonical secret-format redaction patterns (single source of truth).

Every redaction site in the codebase — agent-output masking
(:mod:`modulo.api.routes.runs`), error-text sanitizing
(:mod:`modulo.core.pipeline_engine.error_codes`), raw-output retention
(:mod:`modulo.core.pipeline_engine.node_runner`) and SOC 2 guardrails
(:mod:`modulo.core.guardrails.packs.soc2`) — imports the secret-format
knowledge from HERE so it is never duplicated or drifted.

This module lives in ``modulo.core`` (a leaf with no ``modulo.*`` imports) so
that core redaction sites can use it without violating the
``core-does-not-import-api`` architecture contract. The API-layer
:mod:`modulo.api.middleware.sensitive_mask` re-exports these names so callers
in the API layer keep importing from the documented location.
"""

import re
from typing import Any

# The DOM-safe mask returned for any redacted value.
SENSITIVE_VALUE_MASK = "\u2022\u2022\u2022\u2022\u2022\u2022"

# Hard cap BEFORE any value-pattern regex runs — bounds the ReDoS surface on the
# connection-string / private-key patterns (which use nested quantifiers). Mirrors
# the 5000-char cap in error_codes.py (``runs.error_detail`` is String(5000)), so
# the pattern engine is never fed an unbounded string.
SECRET_VALUE_REDACT_CHAR_CAP = 5000

# Raw compiled secret-format patterns shared with every other redaction site so
# the github_pat_ / AWS key knowledge lives ONLY here.
GITHUB_PAT_PATTERN = re.compile(r"github_pat_[0-9A-Za-z_]{50,}")
AWS_ACCESS_KEY_PATTERN = re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")

# Canonical gitleaks-style secret-VALUE redaction patterns. Each entry is
# ``(compiled_pattern, replacement)`` where ``replacement`` is either a fixed
# string or a callable receiving the match and returning the masked text.
SECRET_VALUE_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # AWS access key ids — classic (AKIA) and temporary/session (ASIA)
    (AWS_ACCESS_KEY_PATTERN, SENSITIVE_VALUE_MASK),
    # Google API key
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), SENSITIVE_VALUE_MASK),
    # GitHub tokens (classic pat/oauth/app/refresh/user-to-server)
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{36,}"), SENSITIVE_VALUE_MASK),
    # GitHub fine-grained PATs (github_pat_<22 chars>_<59 chars> = 50+ after prefix)
    (GITHUB_PAT_PATTERN, SENSITIVE_VALUE_MASK),
    # Slack tokens
    (re.compile(r"xox[baprs]-[0-9A-Za-z-]{8,}"), SENSITIVE_VALUE_MASK),
    # Stripe live secret / restricted keys
    (re.compile(r"(?:sk|rk)_live_[0-9A-Za-z]{16,}"), SENSITIVE_VALUE_MASK),
    # OpenAI / generic sk- prefixed keys
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), SENSITIVE_VALUE_MASK),
    # Anthropic keys
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), SENSITIVE_VALUE_MASK),
    # JSON Web Tokens (three base64url segments)
    (
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        SENSITIVE_VALUE_MASK,
    ),
    # Private key blocks (multiline, any flavour)
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |)PRIVATE KEY-----[^-]*"
            r"(?:-[^-]*)*-----END (?:RSA |EC |OPENSSH |DSA |PGP |)PRIVATE KEY-----",
            re.DOTALL,
        ),
        SENSITIVE_VALUE_MASK,
    ),
    # Connection strings carrying inline credentials: scheme://user:PASSWORD@host
    (
        re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s:/@]+)(@)"),
        lambda m: f"{m.group(1)}{SENSITIVE_VALUE_MASK}{m.group(3)}",
    ),
    # Standalone Bearer tokens in free text
    (
        re.compile(r"(?i)(Bearer\s+)[^\n\"'}\s]+"),
        lambda m: f"{m.group(1)}{SENSITIVE_VALUE_MASK}",
    ),
]


def mask_secret_values_in_text(text: str) -> str:
    """Mask gitleaks-style secret VALUES embedded in *text*.

    Unlike key-name masking, this matches the secret's VALUE content, so it
    catches secrets in free text and under non-sensitive keys alike. Text
    without a matching secret pattern is returned unchanged.

    Input is capped at :data:`SECRET_VALUE_REDACT_CHAR_CAP` code points BEFORE
    any regex runs (ReDoS defense) — bounds the worst case on the
    connection-string / private-key patterns. Non-str input passes through
    unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    capped = text[:SECRET_VALUE_REDACT_CHAR_CAP]
    masked = capped
    for pattern, replacement in SECRET_VALUE_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked
