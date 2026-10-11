"""Local Megas browser capability for the Community MCP.

Available by default whenever an MCP client starts this package; browser
sessions are task-scoped to the current stdio process. Browser automation is
not silently started during pip installation.
"""
from __future__ import annotations

import atexit
from threading import RLock
from typing import Any
from urllib.parse import urlparse

MAX_PAGE_TEXT = 8000


class BrowserSession:
    """Use the existing Playwright engine, not a custom browser."""

    def __init__(self) -> None:
        self.lock = RLock()
        self.playwright: Any = None
        self.browser: Any = None
        self.page: Any = None
        atexit.register(self.close)

    @staticmethod
    def tools() -> list[dict[str, Any]]:
        def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
            return {"type": "object", "additionalProperties": False,
                    "properties": properties, "required": required or []}
        string = {"type": "string", "minLength": 1, "maxLength": 4096}
        return [
            {"name": "browser_status", "description": "Inspect default-enabled browser capability",
             "inputSchema": schema({})},
            {"name": "browser_start", "description": "Start an isolated browser for this local MCP session",
             "inputSchema": schema({"headless": {"type": "boolean"}})},
            {"name": "browser_open", "description": "Open a requested HTTP(S) page in the current browser",
             "inputSchema": schema({"url": string}, ["url"])},
            {"name": "browser_observe", "description": "Read bounded visible browser page text, title and URL",
             "inputSchema": schema({})},
            {"name": "browser_click", "description": "Click an explicitly selected CSS element",
             "inputSchema": schema({"selector": string}, ["selector"])},
            {"name": "browser_fill", "description": "Fill a selected field without storing its value in logs",
             "inputSchema": schema({"selector": string, "value": string}, ["selector", "value"])},
            {"name": "browser_close", "description": "Close the local browser session",
             "inputSchema": schema({})},
        ]

    def status(self) -> dict[str, Any]:
        try:
            import playwright.sync_api  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        return {
            "enabled": True,
            "playwright_installed": installed,
            "started": self.page is not None,
            "transport": "local_stdio",
            "setup_hint": "python -m playwright install chromium",
        }

    def start(self, *, headless: bool = True) -> dict[str, Any]:
        with self.lock:
            if self.page is None:
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError as exc:
                    raise RuntimeError("Playwright dependency missing") from exc
                driver = sync_playwright().start()
                try:
                    browser = driver.chromium.launch(headless=headless)
                    page = browser.new_page(accept_downloads=False)
                except Exception as exc:
                    driver.stop()
                    raise RuntimeError("Chromium unavailable; run python -m playwright install chromium") from exc
                self.playwright, self.browser, self.page = driver, browser, page
            return {"started": True, "headless": headless}

    def _page(self) -> Any:
        if self.page is None:
            raise RuntimeError("Browser has not started; invoke browser_start")
        return self.page

    def open(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Only HTTP(S) browser URLs are supported")
        with self.lock:
            page = self._page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return {"url": page.url, "title": page.title()[:500]}

    def observe(self) -> dict[str, Any]:
        with self.lock:
            page = self._page()
            return {"url": page.url, "title": page.title()[:500],
                    "visible_text": page.locator("body").inner_text(timeout=10000)[:MAX_PAGE_TEXT]}

    def click(self, selector: str) -> dict[str, Any]:
        with self.lock:
            page = self._page()
            page.locator(selector).first.click(timeout=10000)
            return {"clicked": True, "url": page.url}

    def fill(self, selector: str, value: str) -> dict[str, Any]:
        with self.lock:
            self._page().locator(selector).first.fill(value, timeout=10000)
            return {"filled": True}

    def close(self) -> dict[str, Any]:
        with self.lock:
            if self.browser is not None:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if self.playwright is not None:
                try:
                    self.playwright.stop()
                except Exception:
                    pass
            self.page = self.browser = self.playwright = None
            return {"closed": True}

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if name == "browser_status" and not args:
                return self.status()
            if name == "browser_start" and set(args) <= {"headless"} and isinstance(args.get("headless", True), bool):
                return self.start(headless=args.get("headless", True))
            if name == "browser_open" and set(args) == {"url"} and isinstance(args["url"], str):
                return self.open(args["url"])
            if name == "browser_observe" and not args:
                return self.observe()
            if name == "browser_click" and set(args) == {"selector"} and isinstance(args["selector"], str):
                return self.click(args["selector"])
            if name == "browser_fill" and set(args) == {"selector", "value"} and all(isinstance(x, str) for x in args.values()):
                return self.fill(args["selector"], args["value"])
            if name == "browser_close" and not args:
                return self.close()
            raise ValueError("unknown browser tool or invalid input")


browser = BrowserSession()
