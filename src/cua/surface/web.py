"""Playwright web Surface implementation.

Locator resolution is the heart of determinism. A schema `Locator` is an ordered
list of strategies; we try each in turn and use the first that resolves to exactly
one control. Accessibility/role/label/text strategies are tried before brittle
CSS/XPath, so replay prefers human-meaningful targeting and only falls back to
positional selectors when it must.
"""
from __future__ import annotations

from typing import Optional

from playwright.sync_api import (
    Locator as PWLocator,
    Page,
    sync_playwright,
)

from ..schema import Checkpoint, CheckpointKind, Locator, LocatorStrategy, Selector
from .base import ElementNotResolved, Observation, ObservedField, Surface


class PlaywrightWebSurface(Surface):
    def __init__(self, headless: bool = True, default_timeout_ms: int = 10_000):
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Optional[Page] = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context()
        self._context.set_default_timeout(self.default_timeout_ms)
        self.page = self._context.new_page()

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # selector -> Playwright locator
    # ------------------------------------------------------------------ #
    def _build(self, sel: Selector) -> PWLocator:
        p = self.page
        s = sel.strategy
        if s == LocatorStrategy.ROLE:
            return p.get_by_role(sel.role, name=sel.value, exact=sel.exact)  # type: ignore[arg-type]
        if s == LocatorStrategy.LABEL:
            return p.get_by_label(sel.value, exact=sel.exact)
        if s == LocatorStrategy.PLACEHOLDER:
            return p.get_by_placeholder(sel.value, exact=sel.exact)
        if s == LocatorStrategy.TEXT:
            return p.get_by_text(sel.value, exact=sel.exact)
        if s == LocatorStrategy.ALT_TEXT:
            return p.get_by_alt_text(sel.value, exact=sel.exact)
        if s == LocatorStrategy.TITLE:
            return p.get_by_title(sel.value, exact=sel.exact)
        if s == LocatorStrategy.NAME_ATTR:
            return p.locator(f'[name="{sel.value}"]')
        if s == LocatorStrategy.CSS:
            return p.locator(sel.value)
        if s == LocatorStrategy.XPATH:
            return p.locator(f"xpath={sel.value}")
        raise ElementNotResolved  # unreachable

    def _resolve(self, locator: Locator, require_visible: bool = True) -> tuple[PWLocator, Selector]:
        """Return the first selector (primary then fallbacks) that matches uniquely."""
        last_detail = ""
        for sel in locator.all_selectors():
            try:
                pw = self._build(sel)
                count = pw.count()
            except Exception as e:  # malformed selector, etc.
                last_detail = f"selector {sel.strategy}:{sel.value!r} error: {e}"
                continue
            if count == 0:
                last_detail = f"selector {sel.strategy}:{sel.value!r} matched 0 elements"
                continue
            target = pw.first if count > 1 else pw
            if require_visible:
                try:
                    if not target.is_visible():
                        last_detail = f"selector {sel.strategy}:{sel.value!r} matched but not visible"
                        continue
                except Exception:
                    pass
            return target, sel
        raise ElementNotResolved(locator, last_detail)

    # ------------------------------------------------------------------ #
    # perception
    # ------------------------------------------------------------------ #
    def current_url(self) -> str:
        return self.page.url if self.page else ""

    def observe(self) -> Observation:
        p = self.page
        try:
            aria = p.locator("body").aria_snapshot()
        except Exception:
            aria = "(aria snapshot unavailable)"

        # Legacy field cues: name attr + nearest visible label text.
        fields_raw = p.evaluate(
            """() => {
                const out = [];
                const els = document.querySelectorAll('input, select, textarea');
                for (const el of els) {
                    if (el.type === 'hidden') continue;
                    // nearest visible label cue: preceding cell text, label, or aria-label
                    let cue = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
                    if (!cue) {
                        const td = el.closest('td');
                        if (td && td.previousElementSibling) cue = td.previousElementSibling.innerText.trim();
                    }
                    out.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.getAttribute('type'),
                        name: el.getAttribute('name'),
                        cue: (cue || '').slice(0, 60)
                    });
                }
                return out;
            }"""
        )
        fields = [
            ObservedField(
                tag=f["tag"], field_type=f.get("type"), name_attr=f.get("name"),
                nearby_label=f.get("cue") or None,
            )
            for f in fields_raw
        ]

        buttons = self._safe_names("button") + self._safe_input_submits()
        links = self._safe_names("link")

        try:
            text = p.locator("body").inner_text()
        except Exception:
            text = ""
        text_excerpt = " ".join(text.split())[:1200]

        return Observation(
            url=p.url,
            title=(p.title() or ""),
            aria_tree=aria,
            fields=fields,
            buttons=sorted(set(buttons)),
            links=sorted(set(links)),
            text_excerpt=text_excerpt,
        )

    def _safe_names(self, role: str) -> list[str]:
        try:
            loc = self.page.get_by_role(role)
            n = min(loc.count(), 30)
            names = []
            for i in range(n):
                try:
                    t = (loc.nth(i).inner_text() or "").strip()
                    if t:
                        names.append(" ".join(t.split())[:60])
                except Exception:
                    continue
            return names
        except Exception:
            return []

    def _safe_input_submits(self) -> list[str]:
        try:
            return self.page.eval_on_selector_all(
                "input[type=submit], input[type=button]",
                "els => els.map(e => e.value).filter(Boolean)",
            )
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # checkpoints
    # ------------------------------------------------------------------ #
    def check(self, checkpoint: Checkpoint) -> bool:
        p = self.page
        k = checkpoint.kind
        v = checkpoint.value
        try:
            if k == CheckpointKind.URL_CONTAINS:
                return v in p.url
            if k == CheckpointKind.TEXT_PRESENT:
                return p.get_by_text(v).count() > 0
            if k == CheckpointKind.TEXT_ABSENT:
                return p.get_by_text(v).count() == 0
            if k == CheckpointKind.ELEMENT_VISIBLE:
                return p.locator(v).first.is_visible()
        except Exception:
            return False
        return False

    def wait_for(self, checkpoint: Checkpoint, timeout_ms: int) -> bool:
        """Poll the checkpoint until it holds or timeout. Handles transient slowness."""
        import time

        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if self.check(checkpoint):
                return True
            self.page.wait_for_timeout(200)
        return self.check(checkpoint)

    def is_present(self, locator: Locator) -> bool:
        try:
            self._resolve(locator)
            return True
        except ElementNotResolved:
            return False

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")

    def click(self, locator: Locator) -> None:
        target, _ = self._resolve(locator)
        target.click()

    def fill(self, locator: Locator, value: str) -> None:
        target, _ = self._resolve(locator)
        target.fill(value)

    def select(self, locator: Locator, value: str) -> None:
        target, _ = self._resolve(locator)
        target.select_option(label=value)

    def press(self, key: str, locator: Optional[Locator] = None) -> None:
        if locator is not None:
            target, _ = self._resolve(locator)
            target.press(key)
        else:
            self.page.keyboard.press(key)

    def extract(self, locator: Locator, attr: Optional[str] = None) -> str:
        target, _ = self._resolve(locator, require_visible=False)
        if attr:
            val = target.get_attribute(attr)
            return (val or "").strip()
        return (target.inner_text() or "").strip()

    # ------------------------------------------------------------------ #
    # evidence
    # ------------------------------------------------------------------ #
    def screenshot(self, path: str) -> Optional[str]:
        try:
            self.page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return None

    def snapshot_html(self, path: str) -> Optional[str]:
        try:
            html = self.page.content()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            return path
        except Exception:
            return None
