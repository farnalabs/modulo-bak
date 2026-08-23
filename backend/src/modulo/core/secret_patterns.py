"""Canonical secret-format VALUE redaction patterns (single source of truth).

The secret-VALUE redaction knowledge — the gitleaks-style
``SECRET_VALUE_PATTERNS`` list and the shared ``AWS_ACCESS_KEY_PATTERN`` /
``GITHUB_PAT_PATTERN`` raw patterns — is defined ONCE here so the AWS-key and
fine-grained-GitHub-PAT coverage is never duplicated or drifted across
redaction sites. Agent-output masking (:mod:`modulo.api.routes.runs`) and the
API-layer :mod:`modulo.api.middleware.sensitive_mask` import these names
directly.

Key-NAME masking (matching on config keys such as ``token`` / ``password``)
remains a separate concern owned by each read surface (``error_codes.py``,
``node_runner.py``, ``soc2.py``), because those sites mask by key rather than
by secret value and some run under import-linter contracts that this leaf
module must not pull in.

This module lives in ``modulo.core`` (a leaf with no ``modulo.*`` imports) so
that core redaction sites can use it without violating the
``core-does-not-import-api`` architecture contract. The API-layer
:mod:`modulo.api.middleware.sensitive_mask` re-exports ``SENSITIVE_VALUE_MASK``
so callers in the API layer keep importing from the documented location.
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

# Connection strings with inline credentials: scheme://user:PASSWORD@host.
# The password group is greedy (``.*``) so it consumes every character up to the
# FINAL ``@`` (the host separator) — including passwords that themselves contain
# ``@`` / ``:`` / ``/``. This masks the whole secret, not just the first chunk.
CONNECTION_STRING_PATTERN = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^\s:/@]+:)(.*)(@)")
# Private key blocks (multiline, any flavour). Uses a nested quantifier, so it
# is fed bounded slices of :data:`SECRET_VALUE_REDACT_CHAR_CAP` at masking time.
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |)PRIVATE KEY-----[^-]*"
    r"(?:-[^-]*)*-----END (?:RSA |EC |OPENSSH |DSA |PGP |)PRIVATE KEY-----",
    re.DOTALL,
)

# Patterns that use nested quantifiers and must be fed bounded slices (ReDoS
# guard). Every other pattern is a flat char-class pattern, safe to run over the
# entire input string.
_BOUNDED_REDACT_PATTERNS: frozenset[re.Pattern[str]] = frozenset({CONNECTION_STRING_PATTERN, PRIVATE_KEY_PATTERN})

# For each bounded pattern, the literal that begins a secret (``anchor``), the
# literal that must appear within the same bounded window for a match to be
# possible (``close``), and how many chars BEFORE the anchor must be included in
# the window so the regex can match the secret's full prefix (e.g. the scheme
# before ``://``). We anchor each ReDoS-bounded window at the secret's START
# rather than at fixed 5000-char boundaries: a private-key / connection string
# that begins in one fixed slice and ends in the next would be contained by
# neither slice (and so leak unmasked). Anchoring at the start guarantees the
# secret's whole body falls inside one bounded window.
_BOUNDED_WINDOW_HINTS: dict[re.Pattern[str], tuple[str, str, int]] = {
    PRIVATE_KEY_PATTERN: ("-----BEGIN ", "-----END ", 0),
    CONNECTION_STRING_PATTERN: ("://", "@", 40),
}

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
    (PRIVATE_KEY_PATTERN, SENSITIVE_VALUE_MASK),
    # Connection strings carrying inline credentials: scheme://user:PASSWORD@host
    (
        CONNECTION_STRING_PATTERN,
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
    without a matching secret pattern is returned unchanged — and crucially,
    non-secret content is NEVER truncated (an earlier 5000-char cap silently
    dropped long benign strings such as LLM answers, code blocks or log tails).

    ReDoS defense: flat char-class patterns run over the whole string; the two
    patterns with nested quantifiers (private-key block, connection-string) are
    fed bounded slices of :data:`SECRET_VALUE_REDACT_CHAR_CAP` chars and the
    slices rejoined — so the returned string is always the full input with only
    secret matches replaced. Non-str input passes through unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    # Flat patterns: safe to run over the whole string (no nested quantifiers).
    masked = text
    for pattern, replacement in SECRET_VALUE_PATTERNS:
        if pattern in _BOUNDED_REDACT_PATTERNS:
            continue
        masked = pattern.sub(replacement, masked)
    # Bounded patterns: feed each a ReDoS-bounded window anchored at the secret's
    # start so secrets straddling a fixed slice boundary are still caught, while
    # non-secret content is preserved in full (no truncation).
    for pattern, replacement in SECRET_VALUE_PATTERNS:
        if pattern not in _BOUNDED_REDACT_PATTERNS:
            continue
        masked = _mask_bounded_pattern(masked, pattern, replacement)
    return masked


def _mask_bounded_pattern(text: str, pattern: re.Pattern[str], replacement: Any) -> str:
    """Mask *pattern* (a nested-quantifier pattern) without ReDoS or truncation.

    The pattern is only ever fed windows of at most
    :data:`SECRET_VALUE_REDACT_CHAR_CAP` characters, which bounds the ReDoS
    surface. Each window is anchored at a literal that begins a potential
    secret (e.g. ``-----BEGIN `` for private keys) so a secret whose body crosses
    a fixed 5000-char slice boundary is still wholly contained in one window and
    gets masked. A short string with no anchor is masked in one pass.
    """
    if len(text) <= SECRET_VALUE_REDACT_CHAR_CAP:
        return pattern.sub(replacement, text)
    anchor, close, prefix = _BOUNDED_WINDOW_HINTS[pattern]
    result: list[str] = []
    last = 0
    pos = 0
    while True:
        off = text.find(anchor, pos)
        if off == -1:
            break
        pos = off + 1
        # Include ``prefix`` chars before the anchor so the regex can match the
        # secret's leading context (e.g. the scheme before ``://``).
        window_start = max(0, off - prefix)
        window = text[window_start : window_start + SECRET_VALUE_REDACT_CHAR_CAP]
        # No possible match in this window — skip the (bounded) regex attempt.
        if close not in window:
            continue
        for match in pattern.finditer(window):
            start = window_start + match.start()
            end = window_start + match.end()
            if start < last:
                # Already covered by a previous replacement; avoid double-masking.
                continue
            result.append(text[last:start])
            result.append(replacement(match) if callable(replacement) else replacement)
            last = end
    result.append(text[last:])
    return "".join(result)
