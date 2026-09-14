"""LLM provider seam.

The agent loop depends only on `LLMProvider.next_action(...)`. Two implementations:

  * AnthropicProvider -- a real Claude-driven run (the discovery path).
  * MockProvider      -- a scripted sequence of actions, for offline tests and for
                         exercising the full pipeline without an API key.

Keeping this a narrow interface is what makes the discovery path swappable and the
rest of the system testable without spending tokens.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from .prompts import SYSTEM_PROMPT

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response."""
    text = text.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            raise ValueError(f"No JSON object found in model response: {text[:200]!r}")
        return json.loads(m.group(0))


def anthropic_complete(system: str, messages: list[dict[str, str]],
                       model: Optional[str] = None, max_tokens: int = 1024) -> str:
    """One-shot Anthropic call, SDK if installed else dependency-free HTTPS.

    Shared by the discovery provider and the post-run verifier. Requires
    ANTHROPIC_API_KEY (deterministic replay never calls this)."""
    model = model or os.environ.get("CUA_MODEL", "claude-sonnet-4-5")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set (needed only for LLM calls).")
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(model=model, max_tokens=max_tokens,
                                       system=system, messages=messages)
        return "".join(b.text for b in resp.content if b.type == "text")
    except ImportError:
        import urllib.request
        payload = json.dumps({"model": model, "max_tokens": max_tokens,
                              "system": system, "messages": messages}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=payload, method="POST",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


class LLMProvider(ABC):
    system_prompt: str = SYSTEM_PROMPT

    @abstractmethod
    def next_action(self, goal: str, transcript: list[dict[str, str]]) -> dict[str, Any]:
        """Given the goal and the running transcript (list of {role, content}),
        return the next action as a dict following the protocol in prompts.py."""

    def model_name(self) -> Optional[str]:
        return None


class AnthropicProvider(LLMProvider):
    """Real discovery via Anthropic's Messages API.

    Uses the `anthropic` SDK when it is installed; otherwise falls back to a
    dependency-free HTTPS call to the Messages API (only an API key is required).
    Either way, replay -- which never needs an LLM -- has zero dependency on this.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, model: Optional[str] = None, max_tokens: int = 1500):
        self.model = model or os.environ.get("CUA_MODEL", "claude-sonnet-4-5")
        self.max_tokens = max_tokens

    def model_name(self) -> Optional[str]:
        return self.model

    def _messages(self, goal: str, transcript: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{"role": "user", "content": f"GOAL: {goal}"}, *transcript]

    def next_action(self, goal: str, transcript: list[dict[str, str]]) -> dict[str, Any]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set (needed only for discovery).")
        messages = self._messages(goal, transcript)
        try:
            import anthropic  # optional
            client = anthropic.Anthropic()
            resp = client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=self.system_prompt, messages=messages,
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        except ImportError:
            text = self._http_call(messages)
        return _extract_json(text)

    def _http_call(self, messages: list[dict[str, str]]) -> str:
        """SDK-free POST to the Messages API using only the stdlib."""
        import json as _json
        import urllib.request

        payload = _json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system_prompt,
            "messages": messages,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.API_URL, data=payload, method="POST",
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": self.API_VERSION,
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


class MockProvider(LLMProvider):
    """Returns a pre-scripted list of action dicts, in order.

    Used to drive the full discovery pipeline deterministically in tests and in an
    offline demo. It ignores the observations (a real model would not).
    """

    def __init__(self, script: list[dict[str, Any]], model_label: str = "mock-provider"):
        self._script = list(script)
        self._i = 0
        self._model_label = model_label

    def model_name(self) -> Optional[str]:
        return self._model_label

    def next_action(self, goal: str, transcript: list[dict[str, str]]) -> dict[str, Any]:
        if self._i >= len(self._script):
            return {"action": "give_up", "reason": "mock script exhausted"}
        action = self._script[self._i]
        self._i += 1
        return action
