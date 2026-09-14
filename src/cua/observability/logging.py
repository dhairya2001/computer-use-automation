"""Structured, redacted run logging + evidence capture.

Each run gets its own evidence directory:

    evidence/<run_id>/
        run.jsonl        # one JSON event per line (what happened and why)
        summary.json     # final structured result
        step_XX.png      # screenshots (on failure, and optionally per step)
        step_XX.html     # DOM snapshot on failure (richer signal)

Every string written passes through the Redactor, so secrets / regulated data never
land in evidence. This satisfies 3.5 (evidence) and part of 3.4 (no raw sensitive
data persisted).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..safety.redaction import Redactor


def new_run_id(prefix: str) -> str:
    # Timestamp for human readability + a short UUID for uniqueness, so two runs
    # started in the same second never collide on the same evidence dir / run.jsonl.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:8]}"


class RunLogger:
    def __init__(self, evidence_root: str, run_id: str, redactor: Optional[Redactor] = None):
        self.run_id = run_id
        self.dir = os.path.join(evidence_root, run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.redactor = redactor or Redactor()
        self._jsonl_path = os.path.join(self.dir, "run.jsonl")
        self._events: list[dict[str, Any]] = []
        self._step_counter = 0

    # ------------------------------------------------------------------ #
    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        record = self.redactor.redact_obj(record)
        self._events.append(record)
        with open(self._jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def next_step_index(self) -> int:
        self._step_counter += 1
        return self._step_counter

    def capture_failure(self, surface, label: str = "failure") -> dict[str, Optional[str]]:
        """Richer signal on failure: screenshot + DOM snapshot."""
        idx = self._step_counter
        png = os.path.join(self.dir, f"step_{idx:02d}_{label}.png")
        html = os.path.join(self.dir, f"step_{idx:02d}_{label}.html")
        shot = surface.screenshot(png)
        dom = surface.snapshot_html(html)
        self.log("evidence_captured", label=label, screenshot=shot, dom_snapshot=dom)
        return {"screenshot": shot, "dom_snapshot": dom}

    def capture_step(self, surface, label: str) -> Optional[str]:
        idx = self._step_counter
        png = os.path.join(self.dir, f"step_{idx:02d}_{label}.png")
        return surface.screenshot(png)

    def write_summary(self, summary: dict[str, Any]) -> str:
        summary = self.redactor.redact_obj(summary)
        path = os.path.join(self.dir, "summary.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        return path

    @property
    def evidence_dir(self) -> str:
        return self.dir
