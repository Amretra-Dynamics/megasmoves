from __future__ import annotations

from typing import Any


MIN_UI_SCALE = 1.0
MAX_UI_SCALE = 2.0
UI_SCALE_STEP = 0.25


def _is_stale_widget_callback_error(exc_type: type[BaseException], exc: BaseException) -> bool:
    """Return True only for benign Tk callbacks queued after a view was destroyed.

    Tk can deliver a selection/after callback after navigation has already destroyed
    the originating widget. That is a UI lifecycle race, not an operator/runtime
    failure and must not interrupt the user with a modal error dialog.
    """

    if exc_type.__name__ != "TclError":
        return False
    message = str(exc).lower()
    return "bad window path name" in message or "invalid command name" in message


def install_desktop_runtime(root: Any, app: Any, messagebox: Any) -> dict[str, Any]:
    """Install desktop-wide safety and keyboard accessibility behavior.

    The function intentionally accepts duck-typed objects so the contract is
    headlessly testable without creating a Tk display.
    """

    base_scaling = float(root.tk.call("tk", "scaling"))
    state = {"scale": 1.0, "base_scaling": base_scaling}

    for pattern, value in (
        ("*Button.takeFocus", 1),
        ("*TButton.takeFocus", 1),
        ("*TCombobox.takeFocus", 1),
        ("*Treeview.takeFocus", 1),
        ("*Entry.takeFocus", 1),
        ("*Text.takeFocus", 1),
        ("*Button.highlightThickness", 2),
    ):
        root.option_add(pattern, value)

    def callback_exception(exc_type: type[BaseException], exc: BaseException, _traceback: Any) -> None:
        stale_widget = _is_stale_widget_callback_error(exc_type, exc)
        try:
            app.controller.core.store.record(
                "desktop.callback.stale_widget" if stale_widget else "desktop.callback.failed",
                exception_type=exc_type.__name__,
                surface=getattr(app, "current_surface", "unknown"),
            )
        except Exception:
            pass

        if stale_widget:
            try:
                app.status_var.set("View refreshed")
            except Exception:
                pass
            return

        try:
            app.status_var.set(f"Action failed — {exc_type.__name__}")
        except Exception:
            pass
        messagebox.showerror("Megas", f"{exc_type.__name__}: {exc}", parent=root)

    root.report_callback_exception = callback_exception

    def refresh(_event: Any = None) -> str:
        app.show_surface(app.current_surface)
        return "break"

    def close_modal(_event: Any = None) -> str:
        focused = root.focus_get()
        if focused is None:
            return "break"
        top = focused.winfo_toplevel()
        if top is not root:
            try:
                top.destroy()
            except Exception:
                pass
        return "break"

    def move_surface(delta: int) -> str:
        from .desktop import NAVIGATION

        current = app.current_surface
        try:
            index = NAVIGATION.index(current)
        except ValueError:
            index = 0
        app.show_surface(NAVIGATION[(index + delta) % len(NAVIGATION)])
        return "break"

    def apply_scale(value: float) -> str:
        scale = max(MIN_UI_SCALE, min(MAX_UI_SCALE, round(value, 2)))
        state["scale"] = scale
        root.tk.call("tk", "scaling", state["base_scaling"] * scale)
        try:
            app.status_var.set(f"UI scale {int(scale * 100)}% — local core")
        except Exception:
            pass
        app.show_surface(app.current_surface)
        return "break"

    root.bind_all("<F5>", refresh)
    root.bind_all("<Control-r>", refresh)
    root.bind_all("<Control-R>", refresh)
    root.bind_all("<Escape>", close_modal)
    root.bind_all("<Control-Prior>", lambda _event: move_surface(-1))
    root.bind_all("<Control-Next>", lambda _event: move_surface(1))
    root.bind_all("<Control-plus>", lambda _event: apply_scale(state["scale"] + UI_SCALE_STEP))
    root.bind_all("<Control-equal>", lambda _event: apply_scale(state["scale"] + UI_SCALE_STEP))
    root.bind_all("<Control-minus>", lambda _event: apply_scale(state["scale"] - UI_SCALE_STEP))
    root.bind_all("<Control-Shift-Key-0>", lambda _event: apply_scale(1.0))

    return state


def main() -> None:
    """Launch the legacy Tk developer shell.

    The production Windows product UI is WinUI 3. This entry remains only as a
    development/compatibility fallback around the same capability core.
    """

    try:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import messagebox
    except ImportError as exc:
        raise RuntimeError(
            "Megas Desktop requires the Python Tk desktop runtime on this machine"
        ) from exc

    tk.font = tkfont  # type: ignore[attr-defined]

    from .desktop_product import MegasMovesProductApp

    root = tk.Tk()
    app = MegasMovesProductApp(root)
    install_desktop_runtime(root, app, messagebox)
    root.mainloop()


__all__ = [
    "MAX_UI_SCALE",
    "MIN_UI_SCALE",
    "UI_SCALE_STEP",
    "_is_stale_widget_callback_error",
    "install_desktop_runtime",
    "main",
]
