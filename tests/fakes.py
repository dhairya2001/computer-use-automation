"""A scriptable in-memory Surface for fast, deterministic engine tests (no browser)."""
from __future__ import annotations

from typing import Callable, Optional

from cua.schema import Checkpoint, CheckpointKind, Locator
from cua.surface.base import ElementNotResolved, Observation, Surface


class FakeSurface(Surface):
    def __init__(self):
        self.url = ""
        self.texts: set[str] = set()
        self.resolvable: set[str] = set()   # Locator.description values that resolve
        self.extract_values: dict[str, str] = {}
        # description -> callable(self) invoked AFTER a successful action on it
        self.transitions: dict[str, Callable[["FakeSurface"], None]] = {}
        self.actions: list[str] = []

    # lifecycle
    def start(self): ...
    def close(self): ...

    # perception
    def current_url(self) -> str:
        return self.url

    def observe(self) -> Observation:
        return Observation(url=self.url, title="fake", aria_tree="",
                           text_excerpt=" ".join(sorted(self.texts)))

    def check(self, cp: Checkpoint) -> bool:
        if cp.kind == CheckpointKind.URL_CONTAINS:
            return cp.value in self.url
        if cp.kind == CheckpointKind.TEXT_PRESENT:
            return any(cp.value in t for t in self.texts)
        if cp.kind == CheckpointKind.TEXT_ABSENT:
            return not any(cp.value in t for t in self.texts)
        if cp.kind == CheckpointKind.ELEMENT_VISIBLE:
            return cp.value in self.resolvable
        return False

    def wait_for(self, cp: Checkpoint, timeout_ms: int) -> bool:
        return self.check(cp)

    def is_present(self, locator: Locator) -> bool:
        return locator.description in self.resolvable

    # actions
    def _fire(self, locator: Locator):
        if locator.description not in self.resolvable:
            raise ElementNotResolved(locator, "fake: not resolvable")
        self.actions.append(locator.description)
        t = self.transitions.get(locator.description)
        if t:
            t(self)

    def goto(self, url: str):
        self.url = url
        self.actions.append(f"goto:{url}")
        t = self.transitions.get(f"goto:{url}")
        if t:
            t(self)

    def click(self, locator: Locator):
        self._fire(locator)

    def fill(self, locator: Locator, value: str):
        self._fire(locator)

    def select(self, locator: Locator, value: str):
        self._fire(locator)

    def press(self, key: str, locator: Optional[Locator] = None):
        if locator:
            self._fire(locator)

    def extract(self, locator: Locator, attr: Optional[str] = None) -> str:
        if locator.description not in self.resolvable and locator.description not in self.extract_values:
            raise ElementNotResolved(locator, "fake: nothing to extract")
        return self.extract_values.get(locator.description, "")

    # evidence (no-ops)
    def screenshot(self, path: str):
        return None

    def snapshot_html(self, path: str):
        return None
