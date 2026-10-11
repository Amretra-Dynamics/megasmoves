"""Browser-native execution for the Megas Local Worker."""

from .capability import browser_capability_report, find_browser_executable
from .runtime import BrowserSessionManager

__all__ = [
    "BrowserSessionManager",
    "browser_capability_report",
    "find_browser_executable",
]
