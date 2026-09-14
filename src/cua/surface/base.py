"""Surface abstraction: the boundary between perception/action and the flow.

The recorded flow (the artifact) is expressed in terms of *actions* and
*locators*, never in terms of Playwright, a DOM, or pixel coordinates. A concrete
Surface knows how to:

    * perceive the current state in a model-readable way (`observe`),
    * resolve a schema `Locator` to a real control and act on it,
    * evaluate a `Checkpoint`.

Today there is one implementation, `PlaywrightWebSurface`. A `LegacyWebSurface`,
a `DesktopAXSurface` (accessibility tree), or a `ScreenshotSurface` (vision +
coordinates) would implement this same interface, and neither the artifact schema
nor the replay engine would change. That is the seam Section 3.7 asks about.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..schema import Checkpoint, Locator


class SurfaceError(Exception):
    """Base class for surface-level problems."""


class ElementNotResolved(SurfaceError):
    """No selector in a Locator (primary or fallback) matched a unique control."""

    def __init__(self, locator: Locator, detail: str = ""):
        self.locator = locator
        super().__init__(f"Could not resolve control '{locator.description}'. {detail}".strip())


@dataclass
class ObservedField:
    """A form control surfaced to the agent, described in human-meaningful terms.

    On legacy surfaces the control usually has no id and no accessible name, so we
    also surface its `name` attribute and the nearest visible label text -- the
    same cues a human operator uses.
    """
    tag: str
    field_type: Optional[str]
    name_attr: Optional[str]
    nearby_label: Optional[str]
    role: Optional[str] = None
    accessible_name: Optional[str] = None


@dataclass
class Observation:
    """A surface-agnostic snapshot the discovery agent reasons over.

    Deliberately NOT a screenshot or raw HTML: it is the accessibility view plus
    the cues a human uses. This is what keeps the agent honest about targeting on
    surfaces with no clean DOM.
    """
    url: str
    title: str
    aria_tree: str                       # accessibility tree (role/name hierarchy)
    fields: list[ObservedField] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)   # accessible names
    links: list[str] = field(default_factory=list)     # visible link text
    text_excerpt: str = ""               # trimmed visible text (for detecting messages)

    def render_for_llm(self, max_chars: int = 3500) -> str:
        parts = [
            f"URL: {self.url}",
            f"TITLE: {self.title}",
            "",
            "ACCESSIBILITY TREE:",
            self.aria_tree.strip(),
            "",
            "FORM FIELDS (legacy cues -- may have no accessible name):",
        ]
        if self.fields:
            for f in self.fields:
                parts.append(
                    f"  - tag={f.tag} type={f.field_type} name_attr={f.name_attr!r} "
                    f"nearby_label={f.nearby_label!r}"
                )
        else:
            parts.append("  (none)")
        parts.append("")
        parts.append(f"BUTTONS: {self.buttons}")
        parts.append(f"LINKS: {self.links}")
        parts.append("")
        parts.append("VISIBLE TEXT (excerpt):")
        parts.append(self.text_excerpt.strip())
        blob = "\n".join(parts)
        return blob[:max_chars]


class Surface(ABC):
    """The perceive/act interface every surface implements."""

    # --- lifecycle ---
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    # --- perception ---
    @abstractmethod
    def observe(self) -> Observation: ...

    @abstractmethod
    def current_url(self) -> str: ...

    @abstractmethod
    def check(self, checkpoint: Checkpoint) -> bool:
        """Return True if the checkpoint currently holds. Never raises for 'false'."""

    @abstractmethod
    def is_present(self, locator: Locator) -> bool:
        """True if the locator resolves to a control right now (no waiting)."""

    # --- action ---
    @abstractmethod
    def goto(self, url: str) -> None: ...

    @abstractmethod
    def click(self, locator: Locator) -> None: ...

    @abstractmethod
    def fill(self, locator: Locator, value: str) -> None: ...

    @abstractmethod
    def select(self, locator: Locator, value: str) -> None: ...

    @abstractmethod
    def press(self, key: str, locator: Optional[Locator] = None) -> None: ...

    @abstractmethod
    def extract(self, locator: Locator, attr: Optional[str] = None) -> str:
        """Read visible text (or an attribute) from the located control."""

    @abstractmethod
    def wait_for(self, checkpoint: Checkpoint, timeout_ms: int) -> bool:
        """Wait until the checkpoint holds or timeout. Return whether it held."""

    # --- evidence ---
    @abstractmethod
    def screenshot(self, path: str) -> Optional[str]:
        """Capture a screenshot to `path`; return the path or None if unsupported."""

    @abstractmethod
    def snapshot_html(self, path: str) -> Optional[str]:
        """Dump a DOM/state snapshot to `path` for debugging; return path or None."""
