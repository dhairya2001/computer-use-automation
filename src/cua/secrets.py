"""Secret resolution for replay inputs.

Secrets (inputs marked `secret`) must NOT be passed on the command line -- a
`--param passcode=...` lands in shell history and is visible in `ps`. Instead each
secret input is resolved at replay time, in this order:

  1. a `vault://<key>` reference (from --param or the env var below), dereferenced
     through a secret manager (a mock resolver here; point it at Vault/ASM/etc.),
  2. the env var `CUA_SECRET_<name>` (its value may itself be a vault:// reference),
  3. an interactive stdin prompt (getpass), when a TTY is available.

Passing the raw literal via --param still works for convenience but prints a
warning, because it is the insecure path.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from typing import Callable, Optional


class SecretError(Exception):
    """A secret could not be resolved."""


def _resolve_vault(ref: str) -> str:
    """Mock secret-manager dereference for `vault://<key>`.

    Resolution order for the key: env `CUA_VAULT_<KEY>` (slashes/dashes -> _,
    upper-cased), then a JSON file at `CUA_VAULT_FILE`. A real deployment swaps
    this for its actual secret manager client.
    """
    key = ref[len("vault://"):].strip("/")
    env_key = "CUA_VAULT_" + key.replace("/", "_").replace("-", "_").upper()
    if os.environ.get(env_key):
        return os.environ[env_key]
    vault_file = os.environ.get("CUA_VAULT_FILE")
    if vault_file:
        try:
            with open(vault_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if key in data:
                return str(data[key])
        except (OSError, json.JSONDecodeError):
            pass
    raise SecretError(f"Could not resolve vault reference '{ref}'.")


def resolve_secret(name: str, provided: Optional[str] = None, *,
                   allow_prompt: bool = True,
                   warn: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> str:
    """Resolve one secret input by name. Raises SecretError if it cannot."""
    # 1. explicit vault reference passed for this input
    if provided and provided.startswith("vault://"):
        return _resolve_vault(provided)

    # 2. environment variable (may itself be a vault reference)
    env_val = os.environ.get(f"CUA_SECRET_{name}")
    if env_val:
        return _resolve_vault(env_val) if env_val.startswith("vault://") else env_val

    # 3. raw literal passed on the CLI -- allowed, but insecure
    if provided is not None:
        warn(f"WARNING: secret '{name}' was passed on the command line; it may be "
             f"visible in shell history and `ps`. Prefer CUA_SECRET_{name} or a "
             f"vault:// reference.")
        return provided

    # 4. interactive prompt
    if allow_prompt and sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(f"Enter secret '{name}': ")

    raise SecretError(
        f"Secret '{name}' not provided. Set CUA_SECRET_{name}, pass a vault:// "
        f"reference, or run interactively so it can be prompted."
    )
