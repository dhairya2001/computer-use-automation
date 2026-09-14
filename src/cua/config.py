"""Small configuration + wiring helpers shared by the CLI and the demo."""
from __future__ import annotations

import os

from .observability.logging import RunLogger, new_run_id
from .safety.redaction import Redactor
from .schema import CapabilityArtifact, TargetBinding

DEFAULT_BASE_URL = os.environ.get("CUA_MOCKAPP_URL", "http://127.0.0.1:5000")
EVIDENCE_ROOT = os.environ.get("CUA_EVIDENCE_ROOT", "evidence")
ARTIFACTS_ROOT = os.environ.get("CUA_ARTIFACTS_ROOT", "artifacts")


def build_target(app_id: str, base_url: str, tenant_id: str | None = None,
                 app_version: str | None = None) -> TargetBinding:
    return TargetBinding(app_id=app_id, base_url=base_url, tenant_id=tenant_id,
                         app_version=app_version)


def make_logger(prefix: str, secret_values: list[str] | None = None) -> RunLogger:
    return RunLogger(EVIDENCE_ROOT, new_run_id(prefix), Redactor(secret_values or []))


def save_artifact(artifact: CapabilityArtifact, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(artifact.to_json())
    return path


def load_artifact(path: str) -> CapabilityArtifact:
    with open(path, "r", encoding="utf-8") as fh:
        return CapabilityArtifact.from_json(fh.read())
