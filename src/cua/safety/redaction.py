"""Redaction of secrets and regulated data.

Two layers:
  1. Pattern-based: obvious sensitive shapes (SSN, long card-like digit runs,
     bearer tokens, api keys, emails) are masked wherever they appear.
  2. Value-based: concrete values of inputs marked `secret` (and any explicitly
     registered secret) are scrubbed verbatim -- this catches secrets that have no
     recognizable shape (a passcode like "demo").

Everything written to logs, evidence, and artifact echoes passes through here.
The artifact itself never stores concrete secret values -- only {{param}} refs.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "***REDACTED***"

_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                     # SSN
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),                    # card-like digit runs
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),                 # api-key-ish
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+\b", re.I),       # bearer token
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),  # email
]


class Redactor:
    def __init__(self, secret_values: list[str] | None = None):
        # Longest-first so overlapping secrets are masked greedily.
        self._secrets = sorted({s for s in (secret_values or []) if s}, key=len, reverse=True)

    def add_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        out = text
        for s in self._secrets:
            if s:
                out = out.replace(s, MASK)
        for pat in _PATTERNS:
            out = pat.sub(MASK, out)
        return out

    def redact_obj(self, obj: Any) -> Any:
        """Recursively redact strings inside dicts/lists."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj
