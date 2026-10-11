from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from .discovery import DiscoveryEngine, DiscoveryResult
from .model import MCPProfile, MCPServer
from .profile_requirements import parse_requirement_pairs
from .service import MegasCoreService


NAVIGATION = (
    "Machine",
    "Servers",
    "Capabilities",
    "Permissions",
    "Sessions",
    "Tool Console",
    "Profiles",
    "Diagnostics",
    "Secrets",
    "History",
    "Settings / Export",
    "Devices / Sync",
)


class DesktopController:
    """Headless controller used by the desktop UI and desktop contract tests."""

    def __init__(self, core: MegasCoreService | None = None) -> None:
        self.core = core or MegasCoreService()
        self._discovery_result: DiscoveryResult | None = None

    def machine(self) -> dict[str, Any]:
        return self.core.health_snapshot()

    def servers(self) -> list[dict[str, Any]]:
        return self.core.list_servers()

    def add_server(
        self,
        *,
        name: str,
        transport: str,
        target: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        transport = transport.strip()
        if transport == "stdio":
            server = MCPServer(
                name=name,
                transport=transport,
                command=target,
                args=list(args or []),
                cwd=cwd or None,
                description=description,
            )
        else:
            server = MCPServer(
                name=name,
                transport=transport,
                url=target,
                description=description,
            )
        return self.core.register_server(server)

    def discover(
        self,
        paths: list[str] | None = None,
        *,
        include_common: bool = True,
    ) -> dict[str, Any]:
        engine = DiscoveryEngine(self.core.store)
        result = engine.scan(paths or [], include_common=include_common)
        self._discovery_result = result
        return {
            "scanned_files": result.scanned_files,
            "candidates": engine.preview(result),
            "errors": [row.to_dict() for row in result.errors],
        }

    def accept_discovered(self, fingerprint: str) -> dict[str, Any]:
        if self._discovery_result is None:
            raise RuntimeError("run discovery before importing a discovered server")
        engine = DiscoveryEngine(self.core.store)
        preview = engine.preview(self._discovery_result)
        selected = next(
            (row for row in preview if row.get("fingerprint") == fingerprint),
            None,
        )
        if selected is None:
            raise KeyError("unknown discovery fingerprint")
        if selected.get("action") == "conflict":
            raise ValueError("resolve discovery source conflict before importing this server")
        server_name = str((selected.get("server") or {}).get("name") or "")
        if selected.get("action") == "update" and server_name:
            try:
                self.core.stop_server(server_name)
            except (KeyError, RuntimeError, OSError):
                pass
        saved = engine.accept(self._discovery_result, fingerprint)
        return self.core.server_view(saved.id)

    def capabilities(self, server_reference: str) -> list[dict[str, Any]]:
        return self.core.capability_history(server_reference)

    def available_tools(self, server_reference: str) -> list[dict[str, Any]]:
        history = self.core.capability_history(server_reference)
        if not history:
            return []
        snapshot = history[0].get("snapshot") or {}
        tools = snapshot.get("tools") or []
        return [dict(tool) for tool in tools if isinstance(tool, dict)]

    def permissions(self, server_reference: str) -> list[dict[str, Any]]:
        grants = {
            row["tool"]: row
            for row in self.core.list_permissions(server_reference)
        }
        return [
            {
                **tool,
                "granted": str(tool.get("name") or "") in grants,
                "grant": grants.get(str(tool.get("name") or "")),
            }
            for tool in self.available_tools(server_reference)
        ]

    def sessions(self) -> list[dict[str, Any]]:
        return self.core.list_sessions()

    def session(self, session_id: str) -> dict[str, Any]:
        return self.core.session_view(session_id)

    def profiles(self) -> list[dict[str, Any]]:
        return self.core.list_profiles()

    def create_profile(
        self,
        *,
        name: str,
        servers: list[str],
        health_policy: str = "required",
        startup_timeout_seconds: float = 8.0,
        description: str = "",
        project_pairs: list[str] | None = None,
        tool_requirements: list[str] | None = None,
        secret_requirements: list[str] | None = None,
    ) -> dict[str, Any]:
        member_ids = [self.core.store.get_server(reference).id for reference in servers]
        project_metadata: dict[str, str] = {}
        for pair in project_pairs or []:
            if "=" not in pair:
                raise ValueError(f"project metadata must use KEY=VALUE: {pair}")
            key, value = pair.split("=", 1)
            key = key.strip()
            if not key:
                raise ValueError("project metadata key must not be empty")
            project_metadata[key] = value
        profile = MCPProfile(
            name=name,
            server_ids=member_ids,
            description=description,
            health_policy=health_policy,
            startup_timeout_seconds=startup_timeout_seconds,
            project_metadata=project_metadata,
            tool_permission_requirements=parse_requirement_pairs(
                self.core.store,
                tool_requirements or [],
                member_server_ids=member_ids,
                kind="tool",
            ),
            secret_binding_requirements=parse_requirement_pairs(
                self.core.store,
                secret_requirements or [],
                member_server_ids=member_ids,
                kind="secret",
            ),
        )
        return self.core.create_profile(profile)

    def diagnostics(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for server in self.core.store.list_servers():
            try:
                result = self.core.doctor(server.id)
            except Exception as exc:
                result = {
                    "healthy": False,
                    "problems": [f"{type(exc).__name__}: {exc}"],
                    "state": "error",
                }
            rows.append(
                {
                    "server_id": server.id,
                    "server": server.name,
                    "transport": server.transport,
                    **result,
                }
            )
        return rows

    def secrets(self) -> list[dict[str, Any]]:
        return self.core.list_secret_bindings()

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.core.history(limit)

    def settings(self) -> dict[str, Any]:
        return {
            **self.core.health_snapshot(),
            "rebinding_requirements": self.core.rebinding_requirements(),
        }

    def devices(self) -> dict[str, Any]:
        return {
            "local_source_of_truth": True,
            "remote_companion_enabled": False,
            "trusted_devices": [],
            "status": (
                "Remote companion is not enabled in this build. "
                "Local MCP operation remains fully available without sign-in or cloud."
            ),
        }


def _tk_modules() -> tuple[Any, Any, Any, Any, Any, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog, ttk
    except ImportError as exc:
        raise RuntimeError(
            "Megas Desktop requires the Python Tk desktop runtime on this machine"
        ) from exc
    return tk, ttk, filedialog, messagebox, simpledialog, tk.font


class MegasDesktopApp:
    """Direct-control desktop UI backed only by MegasCoreService."""

    BG = "#FFFFFF"
    PANEL = "#F6F8FA"
    BORDER = "#D9E1E5"
    TEXT = "#172033"
    MUTED = "#5C6678"
    PRIMARY = "#008C95"
    PRIMARY_DARK = "#006D74"
    SUCCESS = "#127A52"
    WARNING = "#9A6700"
    DANGER = "#B42318"

    def __init__(self, root: Any, controller: DesktopController | None = None) -> None:
        tk, ttk, filedialog, messagebox, simpledialog, _font = _tk_modules()
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.simpledialog = simpledialog
        self.root = root
        self.controller = controller or DesktopController()
        self.current_surface = "Machine"
        self.status_var = tk.StringVar(value="Ready — local core")
        self.surface_var = tk.StringVar(value="Machine")
        self._server_combo_map: dict[str, str] = {}
        self._build_style()
        self._build_shell()
        self.show_surface("Machine")

    def _build_style(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure("Megas.Treeview", background=self.BG, fieldbackground=self.BG, foreground=self.TEXT, rowheight=30)
        style.configure("Megas.Treeview.Heading", background=self.PANEL, foreground=self.TEXT, relief="flat")
        style.map("Megas.Treeview", background=[("selected", "#DDF4F5")], foreground=[("selected", self.TEXT)])
        style.configure("Megas.TCombobox", fieldbackground=self.BG, background=self.BG)

    def _build_shell(self) -> None:
        self.root.title("Megas — Local MCP Workspace")
        self.root.geometry("1320x820")
        self.root.minsize(980, 640)
        self.root.configure(bg=self.BG)

        shell = self.tk.Frame(self.root, bg=self.BG)
        shell.pack(fill="both", expand=True)

        sidebar = self.tk.Frame(shell, bg=self.PANEL, width=224, highlightbackground=self.BORDER, highlightthickness=1)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = self.tk.Frame(sidebar, bg=self.PANEL)
        brand.pack(fill="x", padx=18, pady=(20, 14))
        self.tk.Label(brand, text="MEGAS", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        self.tk.Label(
            brand,
            text="Local MCP workspace",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        self.nav_buttons: dict[str, Any] = {}
        for label in NAVIGATION:
            button = self.tk.Button(
                sidebar,
                text=label,
                anchor="w",
                relief="flat",
                borderwidth=0,
                bg=self.PANEL,
                fg=self.TEXT,
                activebackground="#E7F3F4",
                activeforeground=self.TEXT,
                padx=18,
                pady=8,
                font=("Segoe UI", 10),
                command=lambda value=label: self.show_surface(value),
            )
            button.pack(fill="x", padx=8, pady=1)
            self.nav_buttons[label] = button

        self.tk.Label(
            sidebar,
            text="LOCAL / OFFLINE-FULL",
            bg=self.PANEL,
            fg=self.SUCCESS,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="bottom", anchor="w", padx=18, pady=18)

        main = self.tk.Frame(shell, bg=self.BG)
        main.pack(side="left", fill="both", expand=True)

        header = self.tk.Frame(main, bg=self.BG, height=74, highlightbackground=self.BORDER, highlightthickness=1)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.tk.Label(
            header,
            textvariable=self.surface_var,
            bg=self.BG,
            fg=self.TEXT,
            font=("Segoe UI", 18, "bold"),
        ).pack(side="left", padx=24)
        self.tk.Label(
            header,
            text="No sign-in required",
            bg="#EDF8F3",
            fg=self.SUCCESS,
            padx=10,
            pady=5,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right", padx=24)

        self.content = self.tk.Frame(main, bg=self.BG)
        self.content.pack(fill="both", expand=True, padx=24, pady=20)

        status = self.tk.Frame(main, bg=self.PANEL, height=32, highlightbackground=self.BORDER, highlightthickness=1)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        self.tk.Label(
            status,
            textvariable=self.status_var,
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=14)

    def _clear(self) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()

    def _set_nav(self, surface: str) -> None:
        for name, button in self.nav_buttons.items():
            active = name == surface
            button.configure(
                bg="#DDF4F5" if active else self.PANEL,
                fg=self.PRIMARY_DARK if active else self.TEXT,
                font=("Segoe UI", 10, "bold" if active else "normal"),
            )

    def show_surface(self, surface: str) -> None:
        if surface not in NAVIGATION:
            raise ValueError(f"unknown desktop surface: {surface}")
        self.current_surface = surface
        self.surface_var.set(surface)
        self._set_nav(surface)
        self._clear()
        renderers: dict[str, Callable[[], None]] = {
            "Machine": self._render_machine,
            "Servers": self._render_servers,
            "Capabilities": self._render_capabilities,
            "Permissions": self._render_permissions,
            "Sessions": self._render_sessions,
            "Tool Console": self._render_tool_console,
            "Profiles": self._render_profiles,
            "Diagnostics": self._render_diagnostics,
            "Secrets": self._render_secrets,
            "History": self._render_history,
            "Settings / Export": self._render_settings,
            "Devices / Sync": self._render_devices,
        }
        try:
            renderers[surface]()
            self.status_var.set("Ready — local core")
        except Exception as exc:
            self._render_error(exc)

    def _render_error(self, exc: BaseException) -> None:
        self._clear()
        self.tk.Label(
            self.content,
            text=f"{type(exc).__name__}: {exc}",
            bg=self.BG,
            fg=self.DANGER,
            font=("Segoe UI", 11, "bold"),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=16)
        self.tk.Button(
            self.content,
            text="Retry",
            command=lambda: self.show_surface(self.current_surface),
            bg=self.PRIMARY,
            fg="white",
            relief="flat",
            padx=16,
            pady=8,
        ).pack(anchor="w")
        self.status_var.set("Action failed — retry available")

    def _heading(self, title: str, subtitle: str = "") -> Any:
        frame = self.tk.Frame(self.content, bg=self.BG)
        frame.pack(fill="x", pady=(0, 14))
        self.tk.Label(frame, text=title, bg=self.BG, fg=self.TEXT, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        if subtitle:
            self.tk.Label(
                frame,
                text=subtitle,
                bg=self.BG,
                fg=self.MUTED,
                font=("Segoe UI", 10),
                wraplength=900,
                justify="left",
            ).pack(anchor="w", pady=(4, 0))
        return frame

    def _button(self, parent: Any, text: str, command: Callable[[], None], *, destructive: bool = False) -> Any:
        button = self.tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.DANGER if destructive else self.PRIMARY,
            fg="white",
            activebackground=self.DANGER if destructive else self.PRIMARY_DARK,
            activeforeground="white",
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=7,
            font=("Segoe UI", 9, "bold"),
        )
        button.pack(side="left", padx=(0, 8))
        return button

    def _secondary_button(self, parent: Any, text: str, command: Callable[[], None]) -> Any:
        button = self.tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.BG,
            fg=self.TEXT,
            activebackground=self.PANEL,
            activeforeground=self.TEXT,
            relief="solid",
            borderwidth=1,
            highlightbackground=self.BORDER,
            padx=12,
            pady=6,
            font=("Segoe UI", 9),
        )
        button.pack(side="left", padx=(0, 8))
        return button

    def _tree(self, parent: Any, columns: list[tuple[str, str, int]]) -> Any:
        frame = self.tk.Frame(parent, bg=self.BG)
        frame.pack(fill="both", expand=True)
        keys = [key for key, _label, _width in columns]
        tree = self.ttk.Treeview(frame, columns=keys, show="headings", style="Megas.Treeview")
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=70, anchor="w")
        scrollbar = self.ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return tree

    def _json_text(self, parent: Any, value: Any, *, height: int = 12) -> Any:
        text = self.tk.Text(
            parent,
            height=height,
            bg="#FBFCFD",
            fg=self.TEXT,
            relief="solid",
            borderwidth=1,
            font=("Consolas", 9),
            wrap="word",
        )
        text.insert("1.0", json.dumps(value, indent=2, sort_keys=True, default=str))
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, pady=(10, 0))
        return text

    def _metric_card(self, parent: Any, label: str, value: Any) -> None:
        card = self.tk.Frame(parent, bg=self.BG, highlightbackground=self.BORDER, highlightthickness=1)
        card.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.tk.Label(card, text=str(value), bg=self.BG, fg=self.TEXT, font=("Segoe UI", 20, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        self.tk.Label(card, text=label, bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(0, 12))

    def _background(self, label: str, func: Callable[[], Any], success: Callable[[Any], None]) -> None:
        self.status_var.set(label)

        def worker() -> None:
            try:
                result = func()
            except Exception as exc:
                self.root.after(0, lambda: self._background_error(exc))
                return
            self.root.after(0, lambda: success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _background_error(self, exc: BaseException) -> None:
        self.status_var.set(f"Failed: {type(exc).__name__}")
        self.messagebox.showerror("Megas", f"{type(exc).__name__}: {exc}")

    def _selected_id(self, tree: Any) -> str:
        selection = tree.selection()
        if not selection:
            raise ValueError("select an item first")
        return str(selection[0])

    def _server_choices(self) -> tuple[list[str], dict[str, str]]:
        choices: list[str] = []
        mapping: dict[str, str] = {}
        for row in self.controller.servers():
            label = f"{row['name']}  [{row['transport']}]"
            choices.append(label)
            mapping[label] = str(row["id"])
        return choices, mapping

    def _render_machine(self) -> None:
        data = self.controller.machine()
        self._heading(
            "This machine is the source of truth",
            "Megas local state, MCP runtimes, permissions and history remain available without sign-in or cloud.",
        )
        cards = self.tk.Frame(self.content, bg=self.BG)
        cards.pack(fill="x", pady=(0, 18))
        self._metric_card(cards, "Configured servers", data["configured_servers"])
        self._metric_card(cards, "Running", data["running_servers"])
        self._metric_card(cards, "Tool grants", data["tool_permissions"])
        self._metric_card(cards, "Secret bindings", data["secret_bindings"])

        status = self.tk.Frame(self.content, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        status.pack(fill="x")
        rows = [
            ("Mode", data["mode"]),
            ("Local home", data["home"]),
            ("Active profile", data.get("active_profile") or "None"),
            ("Active session", data.get("active_session_id") or "None"),
            ("Internet required", "No" if not data["network_required"] else "Yes"),
            ("Sign-in required", "No" if not data["sign_in_required"] else "Yes"),
            ("Cloud", data["cloud"]),
        ]
        for label, value in rows:
            row = self.tk.Frame(status, bg=self.PANEL)
            row.pack(fill="x", padx=16, pady=6)
            self.tk.Label(row, text=label, width=20, anchor="w", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9)).pack(side="left")
            self.tk.Label(row, text=str(value), anchor="w", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 10, "bold")).pack(side="left", fill="x", expand=True)

    def _render_servers(self) -> None:
        rows = self.controller.servers()
        self._heading("Server registry", "Register, inspect and control local or remote MCP endpoints. Remote endpoints are not contacted just to display this registry.")
        actions = self.tk.Frame(self.content, bg=self.BG)
        actions.pack(fill="x", pady=(0, 12))
        self._button(actions, "Add server", self._add_server_dialog)
        self._secondary_button(actions, "Discover configs", self._discover_dialog)
        tree = self._tree(
            self.content,
            [
                ("name", "Server", 190),
                ("transport", "Transport", 130),
                ("state", "State", 120),
                ("enabled", "Enabled", 90),
                ("target", "Target", 420),
            ],
        )
        for row in rows:
            target = row.get("command") if row["transport"] == "stdio" else row.get("url")
            tree.insert("", "end", iid=str(row["id"]), values=(row["name"], row["transport"], row["runtime"]["state"], "Yes" if row["enabled"] else "No", target or ""))

        controls = self.tk.Frame(self.content, bg=self.BG)
        controls.pack(fill="x", pady=(10, 0))

        def act(name: str) -> None:
            server_id = self._selected_id(tree)
            operations: dict[str, Callable[[], Any]] = {
                "start": lambda: self.controller.core.start_server(server_id),
                "stop": lambda: self.controller.core.stop_server(server_id),
                "restart": lambda: self.controller.core.restart_server(server_id),
                "enable": lambda: self.controller.core.set_server_enabled(server_id, True),
                "disable": lambda: self.controller.core.set_server_enabled(server_id, False),
            }
            self._background(f"{name.title()} server…", operations[name], lambda _result: self.show_surface("Servers"))

        for name in ("start", "stop", "restart", "enable", "disable"):
            self._secondary_button(controls, name.title(), lambda value=name: act(value))

        def remove() -> None:
            server_id = self._selected_id(tree)
            row = self.controller.core.server_view(server_id)
            if not self.messagebox.askyesno(
                "Remove server",
                f"Remove {row['name']}? Its local registry record, grants and SecretBindings will be deleted.",
            ):
                return
            self.controller.core.remove_server(server_id)
            self.show_surface("Servers")

        self._button(controls, "Remove selected", remove, destructive=True)
        if not rows:
            self.tk.Label(
                self.content,
                text="No MCP servers are registered yet. Add one directly or preview local MCP configuration discovery.",
                bg=self.BG,
                fg=self.MUTED,
                font=("Segoe UI", 10),
            ).pack(anchor="w", pady=12)

    def _add_server_dialog(self) -> None:
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Add MCP server")
        dialog.configure(bg=self.BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("560x470")
        values: dict[str, Any] = {}

        body = self.tk.Frame(dialog, bg=self.BG)
        body.pack(fill="both", expand=True, padx=20, pady=18)
        self.tk.Label(body, text="Add MCP server", bg=self.BG, fg=self.TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(0, 14))

        def entry(label: str) -> Any:
            self.tk.Label(body, text=label, bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 3))
            widget = self.tk.Entry(body, bg=self.BG, fg=self.TEXT, relief="solid", borderwidth=1, font=("Segoe UI", 10))
            widget.pack(fill="x", ipady=5)
            return widget

        name = entry("Server name")
        self.tk.Label(body, text="Transport", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 3))
        transport = self.ttk.Combobox(body, values=("stdio", "streamable-http", "http", "sse"), state="readonly", style="Megas.TCombobox")
        transport.set("stdio")
        transport.pack(fill="x")
        target = entry("Command for stdio, or URL for remote MCP")
        args = entry("stdio arguments separated with | (optional)")
        cwd = entry("Working directory (optional)")
        description = entry("Description (optional)")

        buttons = self.tk.Frame(body, bg=self.BG)
        buttons.pack(fill="x", pady=(18, 0))

        def save() -> None:
            try:
                row = self.controller.add_server(
                    name=name.get().strip(),
                    transport=transport.get().strip(),
                    target=target.get().strip(),
                    args=[value.strip() for value in args.get().split("|") if value.strip()],
                    cwd=cwd.get().strip() or None,
                    description=description.get().strip(),
                )
            except Exception as exc:
                self.messagebox.showerror("Megas", f"{type(exc).__name__}: {exc}", parent=dialog)
                return
            values["saved"] = row
            dialog.destroy()
            self.show_surface("Servers")

        self._button(buttons, "Save server", save)
        self._secondary_button(buttons, "Cancel", dialog.destroy)

    def _discover_dialog(self) -> None:
        chosen = self.filedialog.askdirectory(title="Choose an MCP configuration directory (optional)")
        include_common = self.messagebox.askyesno(
            "MCP discovery",
            "Also scan standard local MCP configuration locations? Discovery only previews changes; nothing is imported silently.",
        )
        paths = [chosen] if chosen else []

        def done(result: dict[str, Any]) -> None:
            window = self.tk.Toplevel(self.root)
            window.title("MCP discovery preview")
            window.geometry("920x560")
            window.configure(bg=self.BG)
            frame = self.tk.Frame(window, bg=self.BG)
            frame.pack(fill="both", expand=True, padx=18, pady=18)
            self.tk.Label(
                frame,
                text=f"Scanned {result['scanned_files']} config file(s). Select one candidate to import.",
                bg=self.BG,
                fg=self.TEXT,
                font=("Segoe UI", 11, "bold"),
            ).pack(anchor="w", pady=(0, 10))
            tree = self._tree(
                frame,
                [
                    ("name", "Server", 180),
                    ("action", "Action", 100),
                    ("source", "Source", 360),
                    ("warnings", "Warnings", 260),
                ],
            )
            for row in result["candidates"]:
                server = row.get("server") or {}
                tree.insert(
                    "",
                    "end",
                    iid=str(row["fingerprint"]),
                    values=(
                        server.get("name") or "",
                        row.get("action") or "",
                        row.get("source_path") or "",
                        "; ".join(row.get("warnings") or []),
                    ),
                )
            if result["errors"]:
                self._json_text(frame, {"scan_errors": result["errors"]}, height=6)
            controls = self.tk.Frame(frame, bg=self.BG)
            controls.pack(fill="x", pady=(10, 0))

            def accept() -> None:
                try:
                    fingerprint = self._selected_id(tree)
                    self.controller.accept_discovered(fingerprint)
                except Exception as exc:
                    self.messagebox.showerror("Megas", f"{type(exc).__name__}: {exc}", parent=window)
                    return
                window.destroy()
                self.show_surface("Servers")

            self._button(controls, "Import selected", accept)
            self._secondary_button(controls, "Close", window.destroy)
            self.status_var.set("Discovery preview ready — no silent import")

        self._background("Scanning local MCP configs…", lambda: self.controller.discover(paths, include_common=include_common), done)

    def _render_capabilities(self) -> None:
        self._heading("Capability inspector", "Probe the MCP initialize handshake and inspect protocol-reported tools, resources and prompts. Upgrade changes invalidate matching stale tool grants.")
        choices, mapping = self._server_choices()
        if not choices:
            self.tk.Label(self.content, text="Register a server before inspecting capabilities.", bg=self.BG, fg=self.MUTED).pack(anchor="w")
            return
        bar = self.tk.Frame(self.content, bg=self.BG)
        bar.pack(fill="x", pady=(0, 12))
        combo = self.ttk.Combobox(bar, values=choices, state="readonly", width=45, style="Megas.TCombobox")
        combo.set(choices[0])
        combo.pack(side="left", padx=(0, 8))
        body = self.tk.Frame(self.content, bg=self.BG)
        body.pack(fill="both", expand=True)

        def load() -> None:
            for child in body.winfo_children():
                child.destroy()
            server_id = mapping[combo.get()]
            history = self.controller.capabilities(server_id)
            if not history:
                self.tk.Label(body, text="No inspected capability snapshot yet. Probe this server to establish real protocol evidence.", bg=self.BG, fg=self.MUTED).pack(anchor="w", pady=12)
                return
            latest = history[0]
            self._json_text(body, latest, height=22)

        def probe() -> None:
            server_id = mapping[combo.get()]
            self._background("Probing MCP capabilities…", lambda: self.controller.core.probe_server(server_id), lambda _result: load())

        self._button(bar, "Probe", probe)
        self._secondary_button(bar, "Refresh", load)
        combo.bind("<<ComboboxSelected>>", lambda _event: load())
        load()

    def _render_permissions(self) -> None:
        self._heading("Permissions", "Tool authority is deny-by-default. Grants are explicit local owner actions and are revoked when matching tool capability semantics change.")
        choices, mapping = self._server_choices()
        if not choices:
            self.tk.Label(self.content, text="Register and probe a server before granting tools.", bg=self.BG, fg=self.MUTED).pack(anchor="w")
            return
        bar = self.tk.Frame(self.content, bg=self.BG)
        bar.pack(fill="x", pady=(0, 12))
        combo = self.ttk.Combobox(bar, values=choices, state="readonly", width=45, style="Megas.TCombobox")
        combo.set(choices[0])
        combo.pack(side="left", padx=(0, 8))
        table_holder = self.tk.Frame(self.content, bg=self.BG)
        table_holder.pack(fill="both", expand=True)
        tree_ref: dict[str, Any] = {}

        def load() -> None:
            for child in table_holder.winfo_children():
                child.destroy()
            server_id = mapping[combo.get()]
            rows = self.controller.permissions(server_id)
            tree = self._tree(
                table_holder,
                [("tool", "Tool", 220), ("granted", "Granted", 100), ("description", "Protocol description", 520)],
            )
            for row in rows:
                name = str(row.get("name") or "")
                tree.insert("", "end", iid=name, values=(name, "Yes" if row["granted"] else "No", row.get("description") or ""))
            tree_ref["tree"] = tree
            if not rows:
                self.tk.Label(table_holder, text="No tools are present in the latest snapshot. Probe the server first.", bg=self.BG, fg=self.MUTED).pack(anchor="w", pady=10)

        def mutate(grant: bool) -> None:
            tree = tree_ref.get("tree")
            if tree is None:
                raise ValueError("no tool list available")
            tool = self._selected_id(tree)
            server_id = mapping[combo.get()]
            if grant:
                self.controller.core.grant_tool(server_id, tool)
            else:
                self.controller.core.revoke_tool(server_id, tool)
            load()

        self._button(bar, "Grant selected", lambda: mutate(True))
        self._secondary_button(bar, "Revoke selected", lambda: mutate(False))
        combo.bind("<<ComboboxSelected>>", lambda _event: load())
        load()

    def _render_sessions(self) -> None:
        self._heading("Sessions", "Inspect persisted request/response/error events with monotonic sequence, correlation IDs, duration, replay provenance and redaction evidence.")
        actions = self.tk.Frame(self.content, bg=self.BG)
        actions.pack(fill="x", pady=(0, 12))
        sessions = self.controller.sessions()
        tree = self._tree(
            self.content,
            [("label", "Session", 220), ("state", "State", 110), ("profile", "Profile ID", 220), ("started", "Started", 240)],
        )
        for row in sessions:
            tree.insert("", "end", iid=str(row["id"]), values=(row.get("label") or row["id"], row["state"], row.get("profile_id") or "", row["started_at"]))

        def start() -> None:
            label = self.simpledialog.askstring("Start session", "Session label (optional):", parent=self.root) or ""
            self.controller.core.start_session(label=label)
            self.show_surface("Sessions")

        def end() -> None:
            session_id = self._selected_id(tree)
            self.controller.core.end_session(session_id)
            self.show_surface("Sessions")

        self._button(actions, "Start session", start)
        self._secondary_button(actions, "End selected", end)

        detail = self.tk.Frame(self.content, bg=self.BG)
        detail.pack(fill="both", expand=True, pady=(10, 0))

        def inspect(_event: Any = None) -> None:
            for child in detail.winfo_children():
                child.destroy()
            try:
                session_id = self._selected_id(tree)
            except ValueError:
                return
            view = self.controller.session(session_id)
            calls = view.get("tool_calls") or []
            call_tree = self._tree(
                detail,
                [("tool", "Tool", 180), ("server", "Server", 160), ("status", "Status", 110), ("duration", "Duration ms", 110), ("started", "Started", 230)],
            )
            for call in calls:
                call_tree.insert("", "end", iid=str(call["id"]), values=(call["tool"], call.get("server") or "", call["status"], call.get("duration_ms") if call.get("duration_ms") is not None else "", call["started_at"]))
            controls = self.tk.Frame(detail, bg=self.BG)
            controls.pack(fill="x", pady=(8, 0))

            def replay() -> None:
                call_id = self._selected_id(call_tree)
                self._background("Replaying tool call…", lambda: self.controller.core.replay_tool_call(call_id), lambda result: self._show_result("Replay result", result))

            self._secondary_button(controls, "Replay selected call", replay)
            self._secondary_button(controls, "Show events", lambda: self._show_result("Session events", view.get("events") or []))

        tree.bind("<<TreeviewSelect>>", inspect)

    def _render_tool_console(self) -> None:
        self._heading("Tool-call console", "Invoke only protocol-reported tools. Arguments are validated by the MCP path and dispatch remains blocked until explicit local permission exists.")
        choices, mapping = self._server_choices()
        if not choices:
            self.tk.Label(self.content, text="Register and probe a server before invoking tools.", bg=self.BG, fg=self.MUTED).pack(anchor="w")
            return
        top = self.tk.Frame(self.content, bg=self.BG)
        top.pack(fill="x")
        server_combo = self.ttk.Combobox(top, values=choices, state="readonly", width=42, style="Megas.TCombobox")
        server_combo.set(choices[0])
        server_combo.pack(side="left", padx=(0, 8))
        tool_combo = self.ttk.Combobox(top, state="readonly", width=34, style="Megas.TCombobox")
        tool_combo.pack(side="left", padx=(0, 8))

        def load_tools() -> None:
            server_id = mapping[server_combo.get()]
            tools = [str(row.get("name") or "") for row in self.controller.available_tools(server_id) if row.get("name")]
            tool_combo.configure(values=tools)
            tool_combo.set(tools[0] if tools else "")

        self._secondary_button(top, "Probe server", lambda: self._background("Probing MCP capabilities…", lambda: self.controller.core.probe_server(mapping[server_combo.get()]), lambda _result: load_tools()))
        server_combo.bind("<<ComboboxSelected>>", lambda _event: load_tools())
        load_tools()

        self.tk.Label(self.content, text="Arguments JSON", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(16, 4))
        args_text = self.tk.Text(self.content, height=10, bg="#FBFCFD", fg=self.TEXT, relief="solid", borderwidth=1, font=("Consolas", 10))
        args_text.insert("1.0", "{}")
        args_text.pack(fill="x")
        output = self.tk.Text(self.content, height=16, bg="#FBFCFD", fg=self.TEXT, relief="solid", borderwidth=1, font=("Consolas", 9))
        output.pack(fill="both", expand=True, pady=(12, 0))

        def invoke() -> None:
            server_id = mapping[server_combo.get()]
            tool = tool_combo.get().strip()
            if not tool:
                raise ValueError("probe the server and choose a tool first")
            arguments = json.loads(args_text.get("1.0", "end").strip() or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")

            def done(result: dict[str, Any]) -> None:
                output.delete("1.0", "end")
                output.insert("1.0", json.dumps(result, indent=2, sort_keys=True, default=str))
                self.status_var.set("Tool call completed")

            self._background("Invoking tool…", lambda: self.controller.core.invoke_tool(server_id, tool, arguments), done)

        buttons = self.tk.Frame(self.content, bg=self.BG)
        buttons.pack(fill="x", pady=(10, 0))
        self._button(buttons, "Invoke tool", invoke)

    def _render_profiles(self) -> None:
        rows = self.controller.profiles()
        self._heading("Profiles", "Profiles group server startup order, health policy, project metadata and explicit authorization/machine-binding requirements.")
        actions = self.tk.Frame(self.content, bg=self.BG)
        actions.pack(fill="x", pady=(0, 12))
        tree = self._tree(
            self.content,
            [("name", "Profile", 180), ("servers", "Servers", 260), ("health", "Health policy", 120), ("ready", "Requirements ready", 140), ("active", "Active", 90)],
        )
        for row in rows:
            tree.insert("", "end", iid=str(row["id"]), values=(row["name"], ", ".join(row.get("servers") or []), row["health_policy"], "Yes" if row["requirements"]["ready"] else "No", "Yes" if row["active"] else "No"))

        self._button(actions, "Create profile", self._create_profile_dialog)
        self._secondary_button(actions, "Activate", lambda: self._profile_action(tree, "activate"))
        self._secondary_button(actions, "Deactivate", lambda: self._profile_action(tree, "deactivate"))
        self._button(actions, "Delete selected", lambda: self._profile_action(tree, "delete"), destructive=True)

        detail = self.tk.Frame(self.content, bg=self.BG)
        detail.pack(fill="both", expand=True, pady=(10, 0))

        def inspect(_event: Any = None) -> None:
            for child in detail.winfo_children():
                child.destroy()
            try:
                profile_id = self._selected_id(tree)
            except ValueError:
                return
            self._json_text(detail, self.controller.core.profile_view(profile_id), height=14)

        tree.bind("<<TreeviewSelect>>", inspect)

    def _profile_action(self, tree: Any, action: str) -> None:
        if action == "deactivate":
            self.controller.core.deactivate_profile()
            self.show_surface("Profiles")
            return
        profile_id = self._selected_id(tree)
        if action == "activate":
            self._background("Activating profile…", lambda: self.controller.core.activate_profile(profile_id), lambda _result: self.show_surface("Profiles"))
            return
        profile = self.controller.core.profile_view(profile_id)
        if not self.messagebox.askyesno("Delete profile", f"Delete profile {profile['name']}? This removes its local workspace definition."):
            return
        self.controller.core.delete_profile(profile_id)
        self.show_surface("Profiles")

    def _create_profile_dialog(self) -> None:
        servers = self.controller.servers()
        if not servers:
            self.messagebox.showinfo("Megas", "Register at least one server before creating a profile.")
            return
        names = [row["name"] for row in servers]
        name = self.simpledialog.askstring("Create profile", "Profile name:", parent=self.root)
        if not name:
            return
        selected = self.simpledialog.askstring(
            "Create profile",
            "Server names in startup order, comma-separated:\nAvailable: " + ", ".join(names),
            parent=self.root,
        )
        if not selected:
            return
        health = self.simpledialog.askstring(
            "Create profile",
            "Health policy: required or best-effort",
            initialvalue="required",
            parent=self.root,
        ) or "required"
        tool_req = self.simpledialog.askstring(
            "Create profile",
            "Required tools as SERVER=TOOL, comma-separated (optional). This does not grant them.",
            parent=self.root,
        ) or ""
        secret_req = self.simpledialog.askstring(
            "Create profile",
            "Required secrets as SERVER=TARGET, comma-separated (optional). This does not bind/release them.",
            parent=self.root,
        ) or ""
        try:
            self.controller.create_profile(
                name=name.strip(),
                servers=[value.strip() for value in selected.split(",") if value.strip()],
                health_policy=health.strip(),
                tool_requirements=[value.strip() for value in tool_req.split(",") if value.strip()],
                secret_requirements=[value.strip() for value in secret_req.split(",") if value.strip()],
            )
        except Exception as exc:
            self.messagebox.showerror("Megas", f"{type(exc).__name__}: {exc}")
            return
        self.show_surface("Profiles")

    def _render_diagnostics(self) -> None:
        self._heading("Diagnostics", "Deterministic local checks report executable, path, environment reference, runtime and configuration problems without model inference.")
        holder = self.tk.Frame(self.content, bg=self.BG)
        holder.pack(fill="both", expand=True)

        def render(rows: list[dict[str, Any]]) -> None:
            for child in holder.winfo_children():
                child.destroy()
            tree = self._tree(
                holder,
                [("server", "Server", 180), ("transport", "Transport", 120), ("healthy", "Healthy", 90), ("state", "State", 120), ("problems", "Problems", 520)],
            )
            for row in rows:
                tree.insert("", "end", iid=str(row["server_id"]), values=(row["server"], row["transport"], "Yes" if row.get("healthy") else "No", row.get("state") or "", "; ".join(str(value) for value in row.get("problems") or [])))
            self.status_var.set("Diagnostics complete")

        self._background("Running deterministic diagnostics…", self.controller.diagnostics, render)

    def _render_secrets(self) -> None:
        rows = self.controller.secrets()
        self._heading("Secret bindings", "Megas stores provider references, not secret values. Runtime release requires a separate explicit local owner grant.")
        actions = self.tk.Frame(self.content, bg=self.BG)
        actions.pack(fill="x", pady=(0, 12))
        tree = self._tree(
            self.content,
            [("server", "Server", 180), ("target", "Target", 180), ("reference", "Machine reference", 220), ("available", "Available", 90), ("release", "Release allowed", 120)],
        )
        for row in rows:
            tree.insert("", "end", iid=str(row["id"]), values=(row["server"], row["target"], row["reference"], "Yes" if row["available"] else "No", "Yes" if row["release_allowed"] else "No"))

        def bind() -> None:
            choices, mapping = self._server_choices()
            stdio = [label for label in choices if "[stdio]" in label]
            if not stdio:
                raise ValueError("register a stdio server before binding an environment secret")
            server_label = self.simpledialog.askstring("Bind secret", "Server name:", parent=self.root)
            if not server_label:
                return
            server = self.controller.core.store.get_server(server_label.strip())
            target = self.simpledialog.askstring("Bind secret", "Target environment name used by the MCP server:", parent=self.root)
            if not target:
                return
            source = self.simpledialog.askstring("Bind secret", "Local machine environment variable containing the value:", parent=self.root)
            if not source:
                return
            self.controller.core.bind_env_secret(server.id, target.strip(), source.strip())
            self.show_surface("Secrets")

        def selected_binding() -> dict[str, Any]:
            binding_id = self._selected_id(tree)
            return next(row for row in rows if str(row["id"]) == binding_id)

        def allow() -> None:
            row = selected_binding()
            self.controller.core.grant_secret_release(row["server_id"], row["target"])
            self.show_surface("Secrets")

        def revoke() -> None:
            row = selected_binding()
            self.controller.core.revoke_secret_release(row["server_id"], row["target"])
            self.show_surface("Secrets")

        def unbind() -> None:
            row = selected_binding()
            if not self.messagebox.askyesno("Unbind secret", f"Remove local SecretBinding {row['server']}/{row['target']} and revoke its release permission?"):
                return
            self.controller.core.unbind_secret(row["server_id"], row["target"])
            self.show_surface("Secrets")

        self._button(actions, "Bind environment reference", bind)
        self._secondary_button(actions, "Allow release", allow)
        self._secondary_button(actions, "Revoke release", revoke)
        self._button(actions, "Unbind selected", unbind, destructive=True)
        if not rows:
            self.tk.Label(self.content, text="No SecretBindings are configured. Imported requirements appear under Settings / Export until rebound.", bg=self.BG, fg=self.MUTED).pack(anchor="w", pady=10)

    def _render_history(self) -> None:
        rows = self.controller.history(250)
        self._heading("History", "Local audit history records registry, permission, capability, profile, session and portability mutations. Secrets are not expected in default history payloads.")
        tree = self._tree(
            self.content,
            [("at", "Time", 230), ("event", "Event", 260), ("details", "Evidence", 560)],
        )
        for index, row in enumerate(rows):
            detail = {key: value for key, value in row.items() if key not in {"at", "timestamp", "event"}}
            tree.insert("", "end", iid=str(index), values=(row.get("at") or row.get("timestamp") or "", row.get("event") or "", json.dumps(detail, sort_keys=True, default=str)))

    def _render_settings(self) -> None:
        data = self.controller.settings()
        self._heading("Settings and portability", "Export/import remains deterministic and local. Tool grants, secret-release grants and secret values are intentionally not portable.")
        info = self.tk.Frame(self.content, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        info.pack(fill="x", pady=(0, 14))
        self.tk.Label(info, text=f"Local home: {data['home']}", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        self.tk.Label(info, text=f"Unresolved imported secret bindings: {len(data['rebinding_requirements'])}", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(0, 12))
        actions = self.tk.Frame(self.content, bg=self.BG)
        actions.pack(fill="x")
        self._button(actions, "Export workspace", self._export_workspace)
        self._secondary_button(actions, "Import dry-run", lambda: self._import_workspace(dry_run=True))
        self._secondary_button(actions, "Import and apply", lambda: self._import_workspace(dry_run=False))
        self._json_text(self.content, {"rebinding_requirements": data["rebinding_requirements"]}, height=13)

    def _export_workspace(self) -> None:
        path = self.filedialog.asksaveasfilename(
            title="Export Megas workspace",
            defaultextension=".json",
            filetypes=(("Megas JSON", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        encrypt = self.messagebox.askyesno("Encrypt export", "Encrypt this portable bundle with a passphrase?")
        passphrase: str | None = None
        if encrypt:
            passphrase = self.simpledialog.askstring("Encrypt export", "Passphrase (minimum 12 characters):", show="*", parent=self.root)
            if passphrase is None:
                return
        try:
            result = self.controller.core.export_to_path(path, passphrase=passphrase)
        except Exception as exc:
            self.messagebox.showerror("Megas", f"{type(exc).__name__}: {exc}")
            return
        self._show_result("Export complete", result)

    def _import_workspace(self, *, dry_run: bool) -> None:
        path = self.filedialog.askopenfilename(
            title="Import Megas workspace",
            filetypes=(("Megas JSON", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        passphrase = self.simpledialog.askstring(
            "Import workspace",
            "Passphrase if this bundle is encrypted. Leave blank for a plain bundle:",
            show="*",
            parent=self.root,
        )
        passphrase = passphrase or None

        def done(result: dict[str, Any]) -> None:
            self._show_result("Import dry-run" if dry_run else "Import complete", result)
            self.show_surface("Settings / Export")

        self._background(
            "Validating portable bundle…" if dry_run else "Applying portable bundle transaction…",
            lambda: self.controller.core.import_from_path(path, passphrase=passphrase, dry_run=dry_run),
            done,
        )

    def _render_devices(self) -> None:
        data = self.controller.devices()
        self._heading("Devices and optional sync", "This surface is intentionally truthful: remote continuity is optional and cannot become a prerequisite for local MCP control.")
        card = self.tk.Frame(self.content, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        card.pack(fill="x")
        self.tk.Label(card, text="Local source of truth", bg=self.PANEL, fg=self.SUCCESS, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(16, 5))
        self.tk.Label(card, text=data["status"], bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 10), wraplength=760, justify="left").pack(anchor="w", padx=16, pady=(0, 16))
        self.tk.Label(
            self.content,
            text="No cloud account, trusted-device enrollment or remote session is created by opening this page.",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=14)

    def _show_result(self, title: str, value: Any) -> None:
        window = self.tk.Toplevel(self.root)
        window.title(title)
        window.geometry("820x560")
        window.configure(bg=self.BG)
        frame = self.tk.Frame(window, bg=self.BG)
        frame.pack(fill="both", expand=True, padx=16, pady=16)
        self.tk.Label(frame, text=title, bg=self.BG, fg=self.TEXT, font=("Segoe UI", 13, "bold")).pack(anchor="w")
        self._json_text(frame, value, height=26)
        controls = self.tk.Frame(frame, bg=self.BG)
        controls.pack(fill="x", pady=(10, 0))
        self._secondary_button(controls, "Close", window.destroy)


def main() -> None:
    tk, _ttk, _filedialog, _messagebox, _simpledialog, _font = _tk_modules()
    root = tk.Tk()
    MegasDesktopApp(root)
    root.mainloop()


__all__ = ["DesktopController", "MegasDesktopApp", "NAVIGATION", "main"]
