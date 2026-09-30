#!/usr/bin/env python3
"""Native Windows GUI for the Xbox game preparation engine."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import xbox_hdd_prep as engine


APP_NAME = "Xbox Game Prep Tool"
LIGHT_PALETTE = {
    "GREEN": "#13805e", "AMBER": "#a15d08", "RED": "#b42332",
    "BLUE": "#315fbb", "BG": "#f3f6fb", "SURFACE": "#ffffff",
    "TEXT": "#1a2940", "MUTED": "#5d6b7c", "BORDER": "#dce4ee",
    "INPUT": "#fcfdff", "HOVER": "#e7edf7", "PRESSED": "#d9e3f2",
    "DISABLED": "#eef1f5", "PRIMARY_DISABLED": "#aab9d2",
    "PRIMARY_HOVER": "#244f9f", "HEADER": "#eaf0f8",
    "HEADER_TEXT": "#40536d", "TRACK": "#e4ebf5",
    "ACTIVITY": "#f8fafc", "SELECTION": "#cbd9f1",
}
DARK_PALETTE = {
    "GREEN": "#61d6aa", "AMBER": "#f0bb65", "RED": "#ff8d98",
    "BLUE": "#8ab4ff", "BG": "#111720", "SURFACE": "#1b2430",
    "TEXT": "#e8eef7", "MUTED": "#a6b4c6", "BORDER": "#3b4b60",
    "INPUT": "#141d29", "HOVER": "#2b3b50", "PRESSED": "#354a65",
    "DISABLED": "#212b38", "PRIMARY_DISABLED": "#33445c",
    "PRIMARY_HOVER": "#416dc3", "HEADER": "#263347",
    "HEADER_TEXT": "#c5d5ea", "TRACK": "#263244",
    "ACTIVITY": "#151e2a", "SELECTION": "#36557c",
}


def load_dark_mode(path: Path) -> bool:
    """Missing, damaged, or older preferences must never prevent startup."""
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(settings, dict) and settings.get("dark_mode") is True
    except (OSError, ValueError):
        return False


def save_dark_mode(path: Path, enabled: bool) -> None:
    """Replace preferences atomically so an interrupted write is recoverable."""
    settings = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            settings.update(existing)
    except (OSError, ValueError):
        pass
    settings["dark_mode"] = enabled
    fd, temporary = tempfile.mkstemp(prefix=".gui-settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(settings, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)

CONTENT_TYPE_NAMES = {
    "000D0000": "Xbox Live Arcade",
    "00007000": "Games on Demand",
    "00000002": "Downloadable content",
    "00080000": "Xbox demo",
}


@dataclass(frozen=True)
class DetectedGame:
    path: Path
    engine_kind: str
    display_type: str
    status: str = "Ready"
    detail: str = ""
    estimated_bytes: int | None = None


@dataclass
class GameRowWidgets:
    frame: ttk.Frame
    checkbox: ttk.Checkbutton
    selected: tk.BooleanVar
    status: tk.StringVar
    status_label: ttk.Label


@dataclass(frozen=True)
class DriveChoice:
    path: Path
    label: str
    free_bytes: int
    total_bytes: int
    filesystem: str


def normal_path(path: Path | str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


def display_game_name(path: Path) -> str:
    try:
        return engine.safe_game_name(path.name)
    except engine.PrepError:
        return path.stem or path.name


def archive_display_type(path: Path, paths: list[str] | None = None) -> str:
    if paths is None:
        paths = engine.read_archive_paths(path)
    lowered = [item.replace("/", "\\").casefold() for item in paths]
    content_types = {
        content_type
        for content_type in engine.SUPPORTED_CONTENT_TYPES
        if any(f"\\{content_type.casefold()}\\" in f"\\{item}\\" for item in lowered)
    }
    if len(content_types) == 1:
        return CONTENT_TYPE_NAMES[next(iter(content_types))]
    if len(content_types) > 1:
        return "Mixed Xbox content"
    if any(item.endswith("default.xbe") for item in lowered):
        return "Original Xbox game"
    if any(item.endswith("default.xex") for item in lowered):
        return "Extracted Xbox 360 game"
    if any(item.endswith(".iso") for item in lowered):
        return "Disc image"
    if any(item.endswith((".zip", ".7z", ".rar")) for item in lowered):
        return "Nested game archive"
    return "Game archive"


def friendly_game_type(
    path: Path,
    engine_kind: str,
    archive_paths: list[str] | None = None,
) -> str:
    if engine_kind == "archive":
        return archive_display_type(path, archive_paths)
    if engine_kind == "disc image":
        return "Disc image"
    if engine_kind == "Xbox content package":
        inspection = engine.inspect_stfs(path)
        content_type = str(inspection.get("detected_content_type", ""))
        return CONTENT_TYPE_NAMES.get(content_type, "Xbox content package")
    if engine_kind == "extracted game":
        if (path / "default.xbe").is_file():
            return "Original Xbox game"
        return "Extracted Xbox 360 game"
    labels = {
        "Xbox content tree": "Xbox content collection",
        "extracted game collection": "Extracted game collection",
        "disc image collection": "Disc image collection",
    }
    return labels.get(engine_kind, engine_kind.replace("Xbox", "Xbox ").strip())


def scan_source(source: Path) -> list[DetectedGame]:
    games: list[DetectedGame] = []
    for path, kind in engine.list_inputs(source):
        try:
            archive_paths = None
            if kind == "archive":
                archive_paths, estimated_bytes = engine.read_archive_inventory(path)
            else:
                estimated_bytes, _estimate_method = engine.estimate_input_bytes(path, kind)
            games.append(DetectedGame(
                path,
                kind,
                friendly_game_type(path, kind, archive_paths),
                estimated_bytes=estimated_bytes,
            ))
        except (OSError, engine.PrepError) as exc:
            games.append(DetectedGame(
                path,
                kind,
                kind.title(),
                status="Needs attention",
                detail=str(exc),
            ))
    return games


def summarize_games(games: list[DetectedGame]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for game in games:
        counts[game.display_type] = counts.get(game.display_type, 0) + 1
    return sorted(counts.items(), key=lambda item: item[0].casefold())


def failure_label(diagnostic: dict[str, object] | None) -> str:
    """A short, actionable reason that fits in the game-status column."""
    code = str((diagnostic or {}).get("code", ""))
    labels = {
        "destination_no_space": "Not enough space",
        "destination_unavailable": "Drive unavailable",
        "destination_conflict": "File conflict",
        "destination_conflict_size": "File conflict",
        "destination_conflict_hash": "File conflict",
        "copy_hash_mismatch": "Verification failed",
        "copy_size_mismatch": "Verification failed",
        "destination_final_size_mismatch": "Verification failed",
        "transfer_stalled": "Transfer stalled",
        "prepared_source_disappeared": "Source changed",
        "archive_corrupt_or_incomplete": "Archive damaged",
        "source_destination_overlap": "Source/drive overlap",
    }
    if code in labels:
        return labels[code]
    category = str((diagnostic or {}).get("category", ""))
    if category == "source_problem":
        return "Source problem"
    return "Transfer failed"


def failure_detail(diagnostic: dict[str, object] | None) -> str:
    if not diagnostic:
        return "The transfer failed. Check the run report for details."
    message = str(diagnostic.get("message") or "The transfer failed.")
    details = diagnostic.get("details")
    if not isinstance(details, dict):
        return message
    if str(diagnostic.get("code")) != "destination_no_space":
        return message
    needed = details.get("estimated_bytes") or details.get("required_bytes")
    free = details.get("free_bytes")
    if isinstance(needed, int) and isinstance(free, int):
        return f"Needs about {engine.human_size(needed)}; {engine.human_size(free)} was free."
    return message


def _windows_drive_roots() -> list[Path]:
    if os.name != "nt":
        return []
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    roots: list[Path] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
        if drive_type in {2, 3}:  # Removable or fixed.
            roots.append(Path(root))
    return roots


def available_destination_drives() -> list[DriveChoice]:
    choices: list[DriveChoice] = []
    for root in _windows_drive_roots():
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        filesystem = engine.drive_format(root) or "Unknown format"
        label = (
            f"{root}   {engine.human_size(usage.free)} free of "
            f"{engine.human_size(usage.total)}   ({filesystem})"
        )
        choices.append(DriveChoice(root, label, usage.free, usage.total, filesystem))
    return choices


class GuiRunRecorder(engine.RunRecorder):
    def __init__(self, base_dir: Path, event_sink: Callable[[dict[str, object]], None]) -> None:
        self._event_sink = event_sink
        super().__init__(base_dir)

    def record(self, event: str, **fields: object) -> None:
        super().record(event, **fields)
        self._event_sink({"event": event, **fields})


class QueueWriter:
    def __init__(self, sink: queue.Queue[dict[str, object]], stream: str) -> None:
        self.sink = sink
        self.stream = stream
        self.buffer = ""

    def write(self, value: str) -> int:
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                self.sink.put({"kind": "output", "stream": self.stream, "line": line})
        return len(value)

    def flush(self) -> None:
        if self.buffer.strip():
            self.sink.put({
                "kind": "output",
                "stream": self.stream,
                "line": self.buffer.rstrip("\r"),
            })
        self.buffer = ""


class OptionsDialog(tk.Toplevel):
    def __init__(self, parent: "GamePrepApp") -> None:
        super().__init__(parent.root)
        self.parent = parent
        self.title("Options")
        self.resizable(False, False)
        self.transient(parent.root)
        self.grab_set()

        body = ttk.Frame(self, padding=16)
        body.grid(sticky="nsew")
        ttk.Label(body, text="Idle timeout (seconds):").grid(row=0, column=0, sticky="w")
        self.timeout = tk.StringVar(value=str(parent.idle_timeout))
        timeout_entry = ttk.Entry(body, width=12, textvariable=self.timeout)
        timeout_entry.grid(row=0, column=1, padx=(12, 0), sticky="w")

        self.verify = tk.BooleanVar(value=parent.verify_integrity)
        ttk.Checkbutton(
            body,
            text="Deep-check Xbox content packages (recommended)",
            variable=self.verify,
        ).grid(row=1, column=0, columnspan=2, pady=(14, 4), sticky="w")

        ttk.Label(body, text="Temporary work folder (optional):").grid(
            row=2, column=0, pady=(10, 0), sticky="w"
        )
        self.work_dir = tk.StringVar(value=str(parent.work_dir or ""))
        ttk.Entry(body, width=42, textvariable=self.work_dir).grid(
            row=3, column=0, sticky="ew"
        )
        ttk.Button(body, text="Browse…", command=self.choose_work_dir).grid(
            row=3, column=1, padx=(8, 0)
        )
        ttk.Label(
            body,
            text="Leave the work folder blank for an automatic location beside the source.",
            style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=2, pady=(4, 0), sticky="w")

        ttk.Separator(body).grid(row=5, column=0, columnspan=2, sticky="ew", pady=16)
        ttk.Label(body, text="Appearance", style="Section.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(body, text="Dark mode", variable=parent.dark_mode,
                        command=parent.change_appearance).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 4))
        ttk.Label(body, text="Appearance changes apply immediately and are remembered.",
                  style="Muted.TLabel").grid(
            row=8, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=2, pady=(18, 0), sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=self.save).pack(side="right", padx=(0, 8))
        timeout_entry.focus_set()
        self.bind("<Return>", lambda _event: self.save())
        self.bind("<Escape>", lambda _event: self.destroy())
        parent._theme_window(self)

    def choose_work_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a temporary work folder", parent=self)
        if chosen:
            self.work_dir.set(chosen)

    def save(self) -> None:
        try:
            timeout = int(self.timeout.get())
        except ValueError:
            messagebox.showerror("Invalid timeout", "Enter a whole number of seconds.", parent=self)
            return
        if not 30 <= timeout <= 21600:
            messagebox.showerror(
                "Invalid timeout",
                "Choose a timeout between 30 seconds and 6 hours.",
                parent=self,
            )
            return
        self.parent.idle_timeout = timeout
        self.parent.verify_integrity = self.verify.get()
        raw_work_dir = self.work_dir.get().strip().strip('"')
        self.parent.work_dir = Path(raw_work_dir) if raw_work_dir else None
        self.destroy()


class GamePrepApp:
    GREEN = "#13805e"
    AMBER = "#a15d08"
    RED = "#b42332"
    BLUE = "#315fbb"
    BG = "#f3f6fb"
    SURFACE = "#ffffff"
    TEXT = "#1a2940"
    MUTED = "#5d6b7c"
    BORDER = "#dce4ee"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self.games: list[DetectedGame] = []
        self.selected_paths: set[str] = set()
        self.run_games: list[DetectedGame] = []
        self.rows_by_path: dict[str, GameRowWidgets] = {}
        self.drive_choices: dict[str, DriveChoice] = {}
        self.destination_path: Path | None = None
        self.scanned_source: str | None = None
        self.scanning = False
        self.running = False
        self.close_when_finished = False
        self.cancel_event: threading.Event | None = None
        self.idle_timeout = 600
        self.verify_integrity = True
        self.work_dir: Path | None = None
        self.current_input_number = 0
        self.current_input_count = 0
        self.active_row: str | None = None
        self.last_report_path: Path | None = None
        self.last_log_path: Path | None = None
        self.activity_lines: list[str] = []
        self.activity_popup: tk.Toplevel | None = None
        self.activity_popup_text: tk.Text | None = None
        self.completed_inputs: set[str] = set()
        self.failed_game_labels: list[tuple[str, str, str]] = []
        self.game_diagnostics: dict[str, str] = {}
        self.settings_path = engine.ROOT / "gui-settings.json"
        self.dark_mode = tk.BooleanVar(root, value=load_dark_mode(self.settings_path))
        self.palette = dict(DARK_PALETTE if self.dark_mode.get() else LIGHT_PALETTE)
        for role, color in self.palette.items():
            setattr(self, role, color)

        root.title(APP_NAME)
        root.geometry("1240x850")
        root.minsize(1010, 710)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._configure_style()
        self._build_menu()
        self._build_ui()
        self._theme_window(root)
        self.source_var.trace_add("write", self._source_text_changed)
        self.refresh_drives()
        self.root.after(100, self._poll_events)

    def _configure_style(self) -> None:
        self.root.option_add("*Font", ("Segoe UI", 10))
        self.root.configure(background=self.BG)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", background=self.SURFACE, foreground=self.TEXT,
                        bordercolor=self.BORDER, lightcolor=self.BORDER,
                        darkcolor=self.BORDER, focuscolor=self.BLUE,
                        troughcolor=self.TRACK, selectbackground=self.SELECTION,
                        selectforeground=self.TEXT)
        style.map(".", background=[("disabled", self.SURFACE), ("active", self.SURFACE)],
                  foreground=[("disabled", self.MUTED)],
                  selectbackground=[("!focus", self.SELECTION)],
                  selectforeground=[("!focus", self.TEXT)])
        style.configure("TFrame", background=self.SURFACE)
        style.configure("Shell.TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.SURFACE, relief="solid",
                        borderwidth=1, bordercolor=self.BORDER)
        style.configure("TLabel", background=self.SURFACE, foreground=self.TEXT)
        style.configure("Shell.TLabel", background=self.BG, foreground=self.MUTED)
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT,
                        font=("Segoe UI Semibold", 20))
        style.configure("DialogTitle.TLabel", background=self.SURFACE,
                        foreground=self.TEXT, font=("Segoe UI Semibold", 18))
        style.configure("ShellHeading.TLabel", background=self.BG,
                        foreground=self.TEXT, font=("Segoe UI Semibold", 11))
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 12),
                        foreground=self.TEXT)
        style.configure("Heading.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Muted.TLabel", foreground=self.MUTED)
        style.configure("Status.TLabel", background=self.BG,
                        foreground=self.TEXT, font=("Segoe UI Semibold", 10))
        style.configure("TButton", padding=(11, 7), font=("Segoe UI", 10),
                        foreground=self.TEXT, background=self.HEADER,
                        bordercolor=self.BORDER, lightcolor=self.BORDER,
                        darkcolor=self.BORDER)
        style.map("TButton", background=[("disabled", self.DISABLED),
                                          ("pressed", self.PRESSED),
                                          ("active", self.HOVER)],
                  foreground=[("disabled", self.MUTED)],
                  bordercolor=[("focus", self.BLUE), ("active", self.BLUE)],
                  lightcolor=[("pressed", self.BORDER)],
                  darkcolor=[("pressed", self.BORDER)])
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 11),
                        padding=(20, 11), background=LIGHT_PALETTE["BLUE"], foreground="white",
                        borderwidth=0)
        style.map("Primary.TButton", background=[("disabled", self.PRIMARY_DISABLED),
                                                  ("pressed", self.PRIMARY_HOVER),
                                                  ("active", self.PRIMARY_HOVER)],
                  foreground=[("disabled", self.MUTED if self.dark_mode.get() else "#edf1f7")])
        style.configure("GameListHeading.TLabel", font=("Segoe UI Semibold", 9),
                        background=self.HEADER, foreground=self.HEADER_TEXT)
        style.configure("GameListHeader.TFrame", background=self.HEADER)
        style.configure("Header.TCheckbutton", background=self.HEADER,
                        foreground=self.HEADER_TEXT, font=("Segoe UI Semibold", 9))
        style.map("Header.TCheckbutton", background=[("active", self.HEADER),
                                                       ("disabled", self.HEADER)],
                  indicatorbackground=[("disabled", self.DISABLED),
                                        ("alternate", self.SELECTION),
                                        ("selected", self.SELECTION), ("active", self.HOVER)])
        style.configure("Link.TButton", background=self.SURFACE, foreground=self.BLUE,
                        padding=(2, 2), borderwidth=0, relief="flat",
                        font=("Segoe UI", 9, "underline"))
        style.map("Link.TButton", background=[("disabled", self.SURFACE),
                                               ("active", self.SURFACE),
                                               ("pressed", self.SURFACE)],
                  foreground=[("disabled", self.MUTED), ("active", self.BLUE)])
        style.configure("GameList.TLabel", padding=(6, 7))
        style.configure("Problem.GameList.TLabel", foreground=self.RED,
                        padding=(6, 7), font=("Segoe UI Semibold", 9))
        for name in ("TEntry", "TCombobox"):
            style.configure(name, padding=6, fieldbackground=self.INPUT,
                            foreground=self.TEXT, insertcolor=self.TEXT,
                            background=self.HEADER, arrowcolor=self.TEXT,
                            selectbackground=self.SELECTION, selectforeground=self.TEXT)
            style.map(name, fieldbackground=[("disabled", self.DISABLED),
                                              ("readonly", self.INPUT)],
                      foreground=[("disabled", self.MUTED), ("readonly", self.TEXT)],
                      background=[("disabled", self.DISABLED), ("active", self.HOVER)],
                      arrowcolor=[("disabled", self.MUTED)],
                      bordercolor=[("focus", self.BLUE), ("!focus", self.BORDER)],
                      lightcolor=[("focus", self.BLUE), ("!focus", self.BORDER)],
                      darkcolor=[("focus", self.BLUE), ("!focus", self.BORDER)])
        style.configure("TCheckbutton", background=self.SURFACE, foreground=self.TEXT,
                        indicatorbackground=self.INPUT, indicatorforeground=self.TEXT,
                        upperbordercolor=self.BORDER, lowerbordercolor=self.BORDER,
                        bordercolor=self.BORDER,
                        padding=(3, 4))
        style.map("TCheckbutton", background=[("active", self.SURFACE)],
                  foreground=[("disabled", self.MUTED)],
                  indicatorbackground=[("disabled", self.DISABLED),
                                        ("selected", self.SELECTION), ("active", self.HOVER)],
                  indicatorforeground=[("disabled", self.MUTED)])
        style.configure("TMenubutton", padding=(11, 7), background=self.HEADER,
                        foreground=self.TEXT, arrowcolor=self.TEXT)
        style.map("TMenubutton", background=[("disabled", self.DISABLED),
                                              ("active", self.HOVER)],
                  foreground=[("disabled", self.MUTED)],
                  arrowcolor=[("disabled", self.MUTED)])
        style.configure("Menu.TMenubutton", background=self.BG, foreground=self.TEXT,
                        padding=(10, 5), borderwidth=0, relief="flat")
        style.map("Menu.TMenubutton", background=[("active", self.HOVER),
                                                   ("pressed", self.PRESSED)])
        style.configure("TScrollbar", background=self.HEADER, troughcolor=self.BG,
                        arrowcolor=self.MUTED, bordercolor=self.BG,
                        lightcolor=self.HEADER, darkcolor=self.HEADER, gripcount=0)
        style.map("TScrollbar", background=[("pressed", self.PRESSED), ("active", self.HOVER)],
                  arrowcolor=[("active", self.TEXT)])
        style.configure("TSeparator", background=self.BORDER)
        style.configure("TProgressbar", background=self.BLUE, troughcolor=self.TRACK,
                        borderwidth=0, thickness=12)

    def change_appearance(self) -> None:
        previous = self.palette
        self.palette = dict(DARK_PALETTE if self.dark_mode.get() else LIGHT_PALETTE)
        for role, color in self.palette.items():
            setattr(self, role, color)
        self._configure_style()
        self._theme_window(self.root, previous)
        try:
            save_dark_mode(self.settings_path, self.dark_mode.get())
        except OSError:
            self._append_activity("Appearance changed, but the setting could not be saved. "
                                  "Check that the application folder is writable.")

    def _theme_window(self, window: tk.Misc, previous: dict[str, str] | None = None) -> None:
        """Refresh existing widgets in place; never discard transfer or selection state."""
        old_colors = {color: self.palette[role]
                      for role, color in (previous or self.palette).items()}

        def visit(widget: tk.Misc) -> None:
            if isinstance(widget, (tk.Tk, tk.Toplevel)):
                widget.configure(background=self.BG)
                self.root.after_idle(lambda target=widget: self._theme_titlebar(target))
            elif isinstance(widget, tk.Menu):
                widget.configure(background=self.SURFACE, foreground=self.TEXT,
                                 activebackground=self.SELECTION, activeforeground=self.TEXT,
                                 disabledforeground=self.MUTED, selectcolor=self.TEXT,
                                 relief="flat", borderwidth=0, activeborderwidth=0)
            else:
                for option in ("background", "foreground", "highlightbackground",
                               "highlightcolor", "insertbackground", "selectbackground"):
                    if option in widget.keys():
                        current = str(widget.cget(option)).lower()
                        if current in old_colors:
                            widget.configure(**{option: old_colors[current]})
            if isinstance(widget, tk.Text):
                widget.configure(foreground=self.TEXT, insertbackground=self.TEXT,
                                 selectbackground=self.SELECTION, selectforeground=self.TEXT,
                                 highlightbackground=self.BORDER, highlightcolor=self.BLUE,
                                 highlightthickness=1, relief="flat", borderwidth=0)
            if isinstance(widget, ttk.Combobox):
                popdown = widget.tk.call("ttk::combobox::PopdownWindow", str(widget))
                widget.tk.call(f"{popdown}.f.l", "configure",
                               "-background", self.INPUT, "-foreground", self.TEXT,
                               "-selectbackground", self.SELECTION, "-selectforeground", self.TEXT,
                               "-highlightbackground", self.BORDER)
            # Tcl creates private combobox children without Python widget wrappers.
            for child in list(widget.children.values()):
                visit(child)

        visit(window)

    def _theme_titlebar(self, window: tk.Misc) -> None:
        # Windows owns the non-client frame; older systems may not support this hint.
        if os.name != "nt" or not window.winfo_exists():
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            handle = user32.GetParent(window.winfo_id())
            dark = ctypes.c_int(self.dark_mode.get())
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD]
            for attribute in (20, 19):
                if set_attribute(handle, attribute, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                    break
        except (OSError, AttributeError, tk.TclError):
            pass

    @staticmethod
    def _configure_game_columns(widget: ttk.Widget) -> None:
        widget.columnconfigure(0, minsize=65, weight=0)
        widget.columnconfigure(1, minsize=205, weight=5)
        widget.columnconfigure(2, minsize=160, weight=3)
        widget.columnconfigure(3, minsize=105, weight=0)
        widget.columnconfigure(4, minsize=165, weight=0)

    def _on_game_rows_configure(self, _event: tk.Event) -> None:
        bounds = self.game_canvas.bbox("all") or (0, 0, 0, 0)
        self.game_canvas.configure(scrollregion=bounds)

    def _on_game_canvas_configure(self, event: tk.Event) -> None:
        self.game_canvas.itemconfigure(self.game_rows_window, width=event.width)

    def _on_game_list_mousewheel(self, event: tk.Event) -> str:
        steps = int(-event.delta / 120)
        if not steps:
            steps = -1 if event.delta > 0 else 1
        self.game_canvas.yview_scroll(steps, "units")
        return "break"

    def _on_page_mousewheel(self, event: tk.Event) -> str:
        steps = int(-event.delta / 120)
        if not steps:
            steps = -1 if event.delta > 0 else 1
        self.page_canvas.yview_scroll(steps, "units")
        return "break"

    def _on_page_content_configure(self, _event: tk.Event) -> None:
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))

    def _on_page_canvas_configure(self, event: tk.Event) -> None:
        self.page_canvas.itemconfigure(self.page_window, width=event.width)

    def _set_game_checkboxes_enabled(self, enabled: bool) -> None:
        for game in self.games:
            row = self.rows_by_path.get(normal_path(game.path))
            if row:
                state = "normal" if enabled and game.status == "Ready" else "disabled"
                row.checkbox.configure(state=state)
        self._sync_selection_controls()

    def _sync_selection_controls(self) -> None:
        ready = {normal_path(game.path) for game in self.games if game.status == "Ready"}
        selected = ready & self.selected_paths
        self.select_all_var.set(bool(ready) and selected == ready)
        self.select_all_checkbox.state(["alternate" if selected and selected != ready else "!alternate"])
        busy = self.running or self.scanning
        self.select_all_checkbox.state(["disabled" if busy or not ready else "!disabled"])
        self.clear_selection_button.state(["disabled" if busy or not selected else "!disabled"])

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Choose game folder…", command=self.choose_source_folder)
        file_menu.add_command(label="Choose game file…", command=self.choose_source_file)
        file_menu.add_separator()
        file_menu.add_command(label="Open reports folder", command=self.open_reports_folder)
        file_menu.add_command(label="Open diagnostic logs folder", command=self.open_logs_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Rescan source", command=self.start_scan)
        tools_menu.add_command(label="Refresh drives", command=self.refresh_drives)
        tools_menu.add_command(label="Activity history…", command=self.show_activity_window)
        menu.add_cascade(label="Tools", menu=tools_menu)

        options_menu = tk.Menu(menu, tearoff=False)
        options_menu.add_checkbutton(label="Dark mode", variable=self.dark_mode,
                                     command=self.change_appearance)
        options_menu.add_separator()
        options_menu.add_command(label="Transfer settings…", command=self.show_options)
        menu.add_cascade(label="Options", menu=options_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="About", command=self.show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        # A themed menu strip avoids a bright Windows-owned menu bar in dark mode.
        self.root.configure(menu="")
        menu_bar = ttk.Frame(self.root, padding=(12, 3), style="Shell.TFrame")
        menu_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        for column, (name, popup) in enumerate((
            ("File", file_menu), ("Tools", tools_menu),
            ("Options", options_menu), ("Help", help_menu),
        )):
            button = ttk.Menubutton(menu_bar, text=name, menu=popup, underline=0,
                                    takefocus=True, style="Menu.TMenubutton")
            button.grid(row=0, column=column, sticky="w")
            def open_menu(_event: tk.Event, target=button, dropdown=popup) -> str:
                dropdown.tk_popup(target.winfo_rootx(),
                                  target.winfo_rooty() + target.winfo_height())
                return "break"
            self.root.bind(f"<Alt-{name[0].lower()}>", open_menu)

    def _build_ui(self) -> None:
        self.page_canvas = tk.Canvas(self.root, background=self.BG,
                                     highlightthickness=0, borderwidth=0)
        self.page_canvas.grid(row=1, column=0, sticky="nsew")
        page_scroll = ttk.Scrollbar(self.root, orient="vertical",
                                    command=self.page_canvas.yview)
        page_scroll.grid(row=1, column=1, sticky="ns")
        self.page_canvas.configure(yscrollcommand=page_scroll.set)
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)
        container = ttk.Frame(self.page_canvas, padding=(18, 14, 18, 12),
                              style="Shell.TFrame")
        self.page_window = self.page_canvas.create_window(
            (0, 0), window=container, anchor="nw")
        container.bind("<Configure>", self._on_page_content_configure)
        self.page_canvas.bind("<Configure>", self._on_page_canvas_configure)
        self.root.bind("<MouseWheel>", self._on_page_mousewheel)
        container.columnconfigure(0, weight=1)

        header = ttk.Frame(container, style="Shell.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Xbox Game Prep", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(header, text="Prepare games for locally connected Xbox 360 storage",
                  style="Shell.TLabel").grid(row=1, column=0, sticky="w", pady=(1, 0))
        status = ttk.Frame(header, style="Shell.TFrame")
        status.grid(row=0, column=1, rowspan=2, sticky="e")
        self.status_dot = tk.Label(status, text="●", foreground=self.GREEN,
                                   background=self.BG, font=("Segoe UI", 12))
        self.status_dot.pack(side="left")
        self.top_status = tk.StringVar(value="Ready — choose a game or folder")
        ttk.Label(status, textvariable=self.top_status, style="Status.TLabel",
                  wraplength=420, justify="right").pack(
            side="left", padx=(7, 0))

        self._build_source_section(container)
        self._build_destination_section(container)
        self._build_action_section(container)
        self._build_progress_section(container)

        footer = ttk.Frame(container, style="Shell.TFrame")
        footer.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self.footer_status = tk.StringVar(value="No active task")
        ttk.Label(footer, textvariable=self.footer_status, style="Shell.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(footer, text=f"v{engine.VERSION}", style="Shell.TLabel").grid(
            row=0, column=1, sticky="e")

    def _build_source_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, padding=16, style="Card.TFrame")
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text="01  Choose games", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10))

        source_row = ttk.Frame(frame)
        source_row.grid(row=1, column=0, columnspan=2, sticky="ew")
        source_row.columnconfigure(0, weight=1)
        self.source_var = tk.StringVar()
        self.source_entry = ttk.Entry(source_row, textvariable=self.source_var)
        self.source_entry.grid(row=0, column=0, sticky="ew")
        self.source_entry.bind("<Return>", lambda _event: self.start_scan())

        browse_menu = tk.Menu(self.root, tearoff=False)
        browse_menu.add_command(label="Choose a folder…", command=self.choose_source_folder)
        browse_menu.add_command(label="Choose one file…", command=self.choose_source_file)
        self.browse_button = ttk.Menubutton(source_row, text="Browse…", menu=browse_menu, width=12)
        self.browse_button.grid(row=0, column=1, padx=(8, 0))
        self.rescan_button = ttk.Button(source_row, text="Rescan", command=self.start_scan, width=12)
        self.rescan_button.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(
            frame,
            text="Choose a source, then tick the games to transfer. Click a red status for details.",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=2, pady=(6, 10), sticky="w")

        ttk.Label(frame, text="Detected games", style="Heading.TLabel").grid(
            row=3, column=0, sticky="w", pady=(0, 5)
        )
        body = ttk.Frame(frame)
        body.grid(row=4, column=0, columnspan=2, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        table_frame = ttk.Frame(body)
        table_frame.grid(row=0, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.columnconfigure(1, minsize=18)
        table_frame.rowconfigure(1, weight=1)

        self.game_header = ttk.Frame(table_frame)
        self.game_header.grid(row=0, column=0, sticky="ew")
        self._configure_game_columns(self.game_header)
        select_header = ttk.Frame(self.game_header, padding=(2, 0),
                                  style="GameListHeader.TFrame")
        select_header.grid(row=0, column=0, sticky="nsew")
        self.select_all_var = tk.BooleanVar(value=False)
        self.select_all_checkbox = ttk.Checkbutton(
            select_header, text="All", variable=self.select_all_var,
            command=lambda: self._set_all_selected(self.select_all_var.get()),
            style="Header.TCheckbutton", state="disabled")
        self.select_all_checkbox.grid(row=0, column=0, padx=(18, 0), sticky="w")
        for column, title, anchor in (
            (1, "Game", "w"),
            (2, "Type", "w"),
            (3, "Est. size", "e"),
            (4, "Status", "w"),
        ):
            ttk.Label(
                self.game_header,
                text=title,
                anchor=anchor,
                style="GameListHeading.TLabel",
                padding=(6, 7),
                width=1,
            ).grid(row=0, column=column, sticky="ew")

        list_frame = ttk.Frame(table_frame)
        list_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.game_canvas = tk.Canvas(
            list_frame,
            height=250,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            background=self.SURFACE,
        )
        self.game_canvas.grid(row=0, column=0, sticky="nsew")
        self.game_rows = ttk.Frame(self.game_canvas)
        self.game_rows.columnconfigure(0, weight=1)
        self.game_rows_window = self.game_canvas.create_window(
            (0, 0), window=self.game_rows, anchor="nw"
        )
        self.game_rows.bind("<Configure>", self._on_game_rows_configure)
        self.game_rows.bind("<MouseWheel>", self._on_game_list_mousewheel)
        self.game_canvas.bind("<Configure>", self._on_game_canvas_configure)
        self.game_canvas.bind("<MouseWheel>", self._on_game_list_mousewheel)
        list_scroll = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.game_canvas.yview
        )
        list_scroll.grid(row=0, column=1, sticky="ns")
        list_scroll.bind("<MouseWheel>", self._on_game_list_mousewheel)
        self.game_canvas.configure(yscrollcommand=list_scroll.set)

        selection_controls = ttk.Frame(table_frame)
        selection_controls.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.clear_selection_button = ttk.Button(
            selection_controls, text="Clear selection", style="Link.TButton",
            command=lambda: self._set_all_selected(False), state="disabled",
            cursor="hand2")
        self.clear_selection_button.pack(side="right", padx=(12, 0))
        self.selection_total = tk.StringVar(value="Selected: 0 games • 0 B estimated")
        ttk.Label(
            selection_controls,
            textvariable=self.selection_total,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="right", padx=(8, 0))

        sidebar = ttk.Frame(body, padding=(14, 12), style="Card.TFrame")
        sidebar.grid(row=0, column=1, padx=(12, 0), sticky="nsew")
        sidebar.columnconfigure(0, weight=1)
        ttk.Label(sidebar, text="Scan summary", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.summary_total = tk.StringVar(value="No games detected")
        ttk.Label(sidebar, textvariable=self.summary_total, font=("Segoe UI", 13, "bold")).grid(
            row=1, column=0, pady=(8, 10), sticky="w"
        )
        self.summary_breakdown = ttk.Frame(sidebar)
        self.summary_breakdown.grid(row=2, column=0, sticky="new")
        ttk.Separator(sidebar).grid(row=3, column=0, pady=(12, 8), sticky="ew")
        self.scan_state = tk.StringVar(value="Waiting for selection")
        self.scan_state_label = ttk.Label(sidebar, textvariable=self.scan_state)
        self.scan_state_label.grid(row=4, column=0, sticky="w")

        self.scan_activity = tk.StringVar(value="Choose a game source to begin.")
        ttk.Label(frame, textvariable=self.scan_activity, style="Muted.TLabel").grid(
            row=5, column=0, columnspan=2, pady=(9, 0), sticky="w"
        )

    def _build_destination_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, padding=16, style="Card.TFrame")
        frame.grid(row=2, column=0, pady=(11, 0), sticky="ew")
        frame.columnconfigure(0, weight=3)
        frame.columnconfigure(2, weight=2)
        ttk.Label(frame, text="02  Choose Xbox 360 storage", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 9))

        self.destination_var = tk.StringVar()
        self.destination_combo = ttk.Combobox(frame, textvariable=self.destination_var)
        self.destination_combo.grid(row=1, column=0, sticky="ew")
        self.destination_combo.bind("<<ComboboxSelected>>", lambda _event: self.destination_changed())
        self.destination_combo.bind("<Return>", lambda _event: self.destination_changed())
        self.destination_combo.bind("<FocusOut>", lambda _event: self.destination_changed())
        self.refresh_button = ttk.Button(frame, text="Refresh", command=self.refresh_drives, width=12)
        self.refresh_button.grid(row=1, column=1, padx=(8, 12))

        details = ttk.Frame(frame)
        details.grid(row=1, column=2, rowspan=2, sticky="nsew")
        self.drive_name = tk.StringVar(value="—")
        self.drive_free = tk.StringVar(value="—")
        self.drive_destination = tk.StringVar(value="Automatic")
        for row, (label, variable) in enumerate((
            ("Drive:", self.drive_name),
            ("Free space:", self.drive_free),
            ("Destination:", self.drive_destination),
        )):
            ttk.Label(details, text=label).grid(row=row, column=0, sticky="w")
            ttk.Label(details, textvariable=variable).grid(row=row, column=1, padx=(14, 0), sticky="w")

        ttk.Label(
            frame,
            text="Each game will be placed in its correct folder automatically.",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=2, pady=(6, 0), sticky="w")
        self.capacity_note = tk.StringVar(value="Select games and a drive to compare space.")
        self.capacity_label = ttk.Label(frame, textvariable=self.capacity_note,
                                        style="Muted.TLabel")
        self.capacity_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_action_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, padding=16, style="Card.TFrame")
        frame.grid(row=3, column=0, pady=(11, 0), sticky="ew")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="03  Prepare and move", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(frame, text="Games:").grid(row=1, column=0, sticky="w")
        self.action_games = tk.StringVar(value="Not selected")
        ttk.Label(frame, textvariable=self.action_games, font=("Segoe UI", 10, "bold")).grid(
            row=1, column=1, padx=(14, 0), sticky="w"
        )
        ttk.Label(frame, text="Destination:").grid(row=2, column=0, pady=(5, 0), sticky="w")
        self.action_destination = tk.StringVar(value="Not selected")
        ttk.Label(
            frame,
            textvariable=self.action_destination,
            font=("Segoe UI", 10, "bold"),
            width=1,
        ).grid(row=2, column=1, padx=(14, 20), pady=(5, 0), sticky="ew")

        action_buttons = ttk.Frame(frame)
        action_buttons.grid(row=0, column=2, rowspan=3, sticky="e")
        self.start_button = ttk.Button(
            action_buttons,
            text="Prepare & move",
            style="Primary.TButton",
            command=self.start_or_cancel,
            state="disabled",
            width=23,
        )
        self.start_button.pack(fill="x")
        self.options_button = ttk.Button(action_buttons, text="Options…", command=self.show_options)
        self.options_button.pack(fill="x", pady=(6, 0))

    def _build_progress_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, padding=16, style="Card.TFrame")
        frame.grid(row=4, column=0, pady=(11, 0), sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(frame, text="Overall progress", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        progress_row = ttk.Frame(frame)
        progress_row.grid(row=1, column=0, sticky="ew")
        progress_row.columnconfigure(0, weight=1)
        self.progress_value = tk.DoubleVar(value=0)
        ttk.Progressbar(
            progress_row,
            variable=self.progress_value,
            maximum=100,
            mode="determinate",
        ).grid(row=0, column=0, sticky="ew")
        self.progress_text = tk.StringVar(value="0%")
        ttk.Label(progress_row, textvariable=self.progress_text, width=5, anchor="e").grid(
            row=0, column=1, padx=(8, 0)
        )

        activity_header = ttk.Frame(frame)
        activity_header.grid(row=2, column=0, pady=(9, 3), sticky="ew")
        activity_header.columnconfigure(0, weight=1)
        ttk.Label(activity_header, text="Current activity", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            activity_header,
            text="Activity history…",
            command=self.show_activity_window,
        ).grid(
            row=0, column=1, sticky="e"
        )
        self.activity = tk.Text(
            frame,
            height=5,
            wrap="word",
            relief="solid",
            borderwidth=1,
            background=self.ACTIVITY,
            foreground=self.TEXT,
            font=("Segoe UI", 10),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.activity.grid(row=3, column=0, sticky="nsew")
        self._append_activity("Waiting for a game source.")

    def choose_source_folder(self) -> None:
        if self.running:
            return
        chosen = filedialog.askdirectory(title="Choose a game or collection folder")
        if chosen:
            self.source_var.set(chosen)
            self.start_scan()

    def choose_source_file(self) -> None:
        if self.running:
            return
        chosen = filedialog.askopenfilename(
            title="Choose a game file",
            filetypes=(
                ("Supported game files", "*.iso *.zip *.7z *.rar"),
                ("All files", "*.*"),
            ),
        )
        if chosen:
            self.source_var.set(chosen)
            self.start_scan()

    def start_scan(self) -> None:
        if self.running or self.scanning:
            return
        raw = self.source_var.get().strip().strip('"')
        if not raw:
            messagebox.showinfo("Choose games", "Choose one game or a folder to scan first.")
            return
        source = Path(raw)
        if not source.exists():
            messagebox.showerror("Source unavailable", f"The selected source does not exist:\n\n{source}")
            return
        self.scanning = True
        self.scanned_source = None
        self.games.clear()
        self.selected_paths.clear()
        self.rows_by_path.clear()
        self.game_diagnostics.clear()
        for child in self.game_rows.winfo_children():
            child.destroy()
        self._update_selection_total()
        self._render_summary([])
        self.scan_state.set("Scanning…")
        self.scan_activity.set(f"Scanning {source.name or source}…")
        self.top_status.set("Scanning for games…")
        self.footer_status.set("Scanning source")
        self.status_dot.configure(foreground=self.AMBER)
        self.rescan_button.configure(state="disabled")
        self.browse_button.configure(state="disabled")
        self._set_game_checkboxes_enabled(False)
        self._update_ready_state()

        def worker() -> None:
            try:
                games = scan_source(source)
                self.events.put({"kind": "scan_finished", "source": source, "games": games})
            except Exception as exc:
                self.events.put({
                    "kind": "scan_failed",
                    "source": source,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                })

        threading.Thread(target=worker, name="game-scan", daemon=True).start()

    def _finish_scan(self, source: Path, games: list[DetectedGame]) -> None:
        self.scanning = False
        self.games = games
        self.selected_paths = {
            normal_path(game.path) for game in games if game.status == "Ready"
        }
        self.scanned_source = normal_path(source)
        self.rows_by_path.clear()
        self.rescan_button.configure(state="normal")
        self.browse_button.configure(state="normal")

        problem_count = 0
        for index, game in enumerate(games):
            key = normal_path(game.path)
            row_frame = ttk.Frame(self.game_rows, padding=(2, 1))
            self._configure_game_columns(row_frame)
            row_frame.grid(row=index * 2, column=0, sticky="ew")

            selected_var = tk.BooleanVar(value=key in self.selected_paths)
            checkbox = ttk.Checkbutton(
                row_frame,
                variable=selected_var,
                command=lambda path_key=key, value=selected_var: self._set_game_selected(
                    path_key, bool(value.get())
                ),
                state="normal" if game.status == "Ready" else "disabled",
            )
            checkbox.grid(row=0, column=0, padx=(18, 0), sticky="w")
            game_label = ttk.Label(
                row_frame, text=display_game_name(game.path), style="GameList.TLabel",
                anchor="w", width=1,
            )
            game_label.grid(row=0, column=1, sticky="ew")
            ttk.Label(
                row_frame, text=game.display_type, style="GameList.TLabel",
                anchor="w", width=1,
            ).grid(row=0, column=2, sticky="ew")
            ttk.Label(
                row_frame,
                text=(engine.human_size(game.estimated_bytes)
                      if game.estimated_bytes is not None else "Unknown"),
                style="GameList.TLabel",
                anchor="e",
                width=1,
            ).grid(row=0, column=3, sticky="ew")
            status_var = tk.StringVar(
                value="Ready" if game.status == "Ready" else "Scan issue"
            )
            status_label = ttk.Label(
                row_frame,
                textvariable=status_var,
                style=("GameList.TLabel" if game.status == "Ready"
                       else "Problem.GameList.TLabel"),
                anchor="w",
                width=1,
            )
            status_label.grid(row=0, column=4, sticky="ew")
            status_label.bind("<Button-1>", lambda _event, path_key=key: self._show_game_detail(path_key))
            game_label.bind("<Double-Button-1>",
                            lambda _event, path_key=key: self._show_game_detail(path_key))
            separator = ttk.Separator(self.game_rows, orient="horizontal")
            separator.grid(
                row=index * 2 + 1, column=0, sticky="ew"
            )

            self.rows_by_path[key] = GameRowWidgets(
                row_frame, checkbox, selected_var, status_var, status_label
            )
            for widget in (row_frame, *row_frame.winfo_children(), separator):
                widget.bind("<MouseWheel>", self._on_game_list_mousewheel)
            if game.status != "Ready":
                problem_count += 1
                self.game_diagnostics[key] = game.detail
                self._append_activity(f"{game.path.name}: {game.detail}")

        self._render_summary(games)
        ready_count = len(games) - problem_count
        if problem_count:
            self.scan_state.set(f"{problem_count} item(s) need attention")
            self.scan_activity.set(
                f"Scan finished: {ready_count} ready, {problem_count} need attention."
            )
            self.top_status.set(f"Scan finished — {ready_count} of {len(games)} ready")
            self.status_dot.configure(foreground=self.AMBER)
        else:
            self.scan_state.set("✓ Scan complete")
            noun = "game" if len(games) == 1 else "games"
            self.scan_activity.set(f"Scan complete. {len(games)} {noun} detected.")
            self.top_status.set(f"Ready — {len(games)} {noun} detected")
            self.status_dot.configure(foreground=self.GREEN)
        self.action_games.set(f"{ready_count} ready")
        self.footer_status.set(f"{ready_count} game(s) ready")
        self._set_game_checkboxes_enabled(True)
        self._update_selection_total()
        self._append_activity(
            f"Scan complete: {len(games)} detected, {ready_count} ready to prepare."
        )
        self._update_ready_state()

    def _set_game_selected(self, key: str, selected: bool) -> None:
        game = next((item for item in self.games if normal_path(item.path) == key), None)
        if game is None or game.status != "Ready" or self.running or self.scanning:
            row = self.rows_by_path.get(key)
            if row:
                row.selected.set(False)
            return
        if selected:
            self.selected_paths.add(key)
        else:
            self.selected_paths.discard(key)
        row = self.rows_by_path.get(key)
        if row:
            row.selected.set(selected)
        self._update_selection_total()
        selected_games = [
            game for game in self.games if normal_path(game.path) in self.selected_paths
        ]
        self.action_games.set(f"{len(selected_games)} selected")
        self._update_ready_state()

    def _set_all_selected(self, selected: bool) -> None:
        if self.running or self.scanning:
            return
        if selected:
            self.selected_paths = {
                normal_path(game.path) for game in self.games if game.status == "Ready"
            }
        else:
            self.selected_paths.clear()
        for key, row in self.rows_by_path.items():
            row.selected.set(key in self.selected_paths)
        selected_games = [
            game for game in self.games if normal_path(game.path) in self.selected_paths
        ]
        self.action_games.set(f"{len(selected_games)} selected")
        self._update_selection_total()
        self._update_ready_state()

    def _update_selection_total(self) -> None:
        selected = [
            game for game in self.games if normal_path(game.path) in self.selected_paths
        ]
        known_sizes = [game.estimated_bytes for game in selected if game.estimated_bytes is not None]
        total = sum(known_sizes)
        unknown = len(selected) - len(known_sizes)
        if unknown:
            size_text = f"at least {engine.human_size(total)} + {unknown} unknown"
        else:
            size_text = f"{engine.human_size(total)} estimated"
        self.selection_total.set(
            f"Selected: {len(selected)} game(s) • {size_text}"
        )
        self._sync_selection_controls()
        self._update_capacity_note()

    def _update_capacity_note(self) -> None:
        if not self.selected_paths or self.destination_path is None:
            self.capacity_note.set("Select games and a drive to compare space.")
            self.capacity_label.configure(foreground=self.MUTED)
            return
        try:
            free = shutil.disk_usage(self.destination_path).free
        except OSError:
            self.capacity_note.set("Drive space is unavailable. Check the connection.")
            self.capacity_label.configure(foreground=self.RED)
            return
        selected = [game for game in self.games
                    if normal_path(game.path) in self.selected_paths]
        total = sum(game.estimated_bytes or 0 for game in selected)
        unknown = any(game.estimated_bytes is None for game in selected)
        if total > free:
            self.capacity_note.set(
                f"Space warning: {engine.human_size(total)} selected, "
                f"{engine.human_size(free)} free. Games that cannot fit will be skipped."
            )
            self.capacity_label.configure(foreground=self.AMBER)
        else:
            suffix = " (some sizes unknown)" if unknown else ""
            self.capacity_note.set(
                f"{engine.human_size(total)} selected • {engine.human_size(free)} free{suffix}"
            )
            self.capacity_label.configure(foreground=self.MUTED)

    def _show_game_detail(self, key: str) -> None:
        detail = self.game_diagnostics.get(key)
        if detail:
            messagebox.showinfo("Game status", detail, parent=self.root)

    def _source_text_changed(self, *_args: object) -> None:
        if self.running or self.scanning or not self.scanned_source:
            return
        raw = self.source_var.get().strip().strip('"')
        if not raw or normal_path(raw) != self.scanned_source:
            self.action_games.set("Rescan required")
            self.top_status.set("Source changed — rescan to update the game list")
            self.status_dot.configure(foreground=self.AMBER)
        self._update_ready_state()

    def _scan_failed(self, source: Path, error: str) -> None:
        self.scanning = False
        self.rescan_button.configure(state="normal")
        self.browse_button.configure(state="normal")
        self.scan_state.set("Scan failed")
        self.scan_activity.set(error)
        self.top_status.set("Could not scan the selected source")
        self.footer_status.set("No games ready")
        self.status_dot.configure(foreground=self.RED)
        self._append_activity(f"Scan failed for {source}: {error}")
        self._update_ready_state()
        messagebox.showerror("Could not scan games", error)

    def _render_summary(self, games: list[DetectedGame]) -> None:
        for child in self.summary_breakdown.winfo_children():
            child.destroy()
        self.summary_total.set(
            "No games detected" if not games else
            f"{len(games)} {'game' if len(games) == 1 else 'games'} detected"
        )
        for row, (label, count) in enumerate(summarize_games(games)):
            ttk.Label(self.summary_breakdown, text=label).grid(row=row, column=0, sticky="w")
            ttk.Label(
                self.summary_breakdown,
                text=str(count),
                font=("Segoe UI", 10, "bold"),
            ).grid(row=row, column=1, padx=(16, 0), sticky="e")
        problems = sum(game.status != "Ready" for game in games)
        row = len(summarize_games(games))
        ttk.Label(self.summary_breakdown, text="Unsupported items").grid(
            row=row, column=0, pady=(5, 0), sticky="w"
        )
        ttk.Label(
            self.summary_breakdown,
            text=str(problems),
            font=("Segoe UI", 10, "bold"),
        ).grid(row=row, column=1, padx=(16, 0), pady=(5, 0), sticky="e")

    def refresh_drives(self) -> None:
        if self.running:
            return
        current = self.destination_var.get()
        choices = available_destination_drives()
        self.drive_choices = {choice.label: choice for choice in choices}
        self.destination_combo.configure(values=[choice.label for choice in choices])
        if current:
            self.destination_var.set(current)
            self.destination_changed()

    def destination_changed(self) -> None:
        if self.running:
            return
        raw = self.destination_var.get().strip()
        choice = self.drive_choices.get(raw)
        path = choice.path if choice else Path(raw.strip('"')) if raw else None
        self.destination_path = None
        if path is None or not path.is_dir():
            self.drive_name.set("—")
            self.drive_free.set("—")
            self.action_destination.set("Not selected")
            self._update_ready_state()
            return
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            self.drive_name.set("Unavailable")
            self.drive_free.set("—")
            self.action_destination.set("Not selected")
            self._update_ready_state()
            return
        self.destination_path = path.resolve()
        filesystem = choice.filesystem if choice else (engine.drive_format(path) or "Unknown format")
        self.drive_name.set(f"{path.anchor or path} ({filesystem})")
        self.drive_free.set(f"{engine.human_size(usage.free)} of {engine.human_size(usage.total)}")
        self.drive_destination.set("Automatic")
        self.action_destination.set(str(self.destination_path))
        self._update_ready_state()

    def _update_ready_state(self) -> None:
        self._update_capacity_note()
        selected_games = [
            game for game in self.games if normal_path(game.path) in self.selected_paths
        ]
        source_current = bool(
            self.scanned_source
            and self.source_var.get().strip()
            and self.scanned_source == normal_path(self.source_var.get().strip().strip('"'))
        )
        ready = bool(
            selected_games
            and all(game.status == "Ready" for game in selected_games)
            and self.destination_path
            and source_current
            and not self.scanning
            and not self.running
        )
        self.start_button.configure(state="normal" if ready else "disabled")

    def start_or_cancel(self) -> None:
        if self.running:
            self.request_cancel()
        else:
            self.start_run()

    def start_run(self) -> None:
        self.destination_changed()
        if not self.destination_path or not self.games:
            return
        selected_games = [
            game for game in self.games if normal_path(game.path) in self.selected_paths
        ]
        if not selected_games:
            messagebox.showinfo("Choose games", "Select at least one game to prepare and move.")
            return
        if any(game.status != "Ready" for game in selected_games):
            messagebox.showwarning(
                "Resolve scan problems",
                "Unselect items that need attention before preparation can begin.",
            )
            return
        source = Path(self.source_var.get().strip().strip('"')).resolve()
        destination = self.destination_path
        selected_size = sum(
            game.estimated_bytes or 0 for game in selected_games
        )
        unknown_sizes = sum(game.estimated_bytes is None for game in selected_games)
        size_summary = engine.human_size(selected_size)
        if unknown_sizes:
            size_summary += f" plus {unknown_sizes} unknown size(s)"
        confirmed = messagebox.askokcancel(
            "Prepare and move games",
            f"Prepare {len(selected_games)} selected game(s) ({size_summary} estimated) "
            f"and move them to:\n\n{destination}\n\n"
            "Source files will remain unchanged. Existing conflicts will not be overwritten.",
            icon="info",
        )
        if not confirmed:
            return

        self.running = True
        self.run_games = selected_games
        self.completed_inputs.clear()
        self.failed_game_labels.clear()
        self.game_diagnostics.clear()
        self.cancel_event = threading.Event()
        self.current_input_number = 0
        self.current_input_count = len(selected_games)
        self.progress_value.set(0)
        self.progress_text.set("0%")
        self.start_button.configure(text="Cancel transfer", state="normal")
        self.options_button.configure(state="disabled")
        self.source_entry.configure(state="disabled")
        self.browse_button.configure(state="disabled")
        self.rescan_button.configure(state="disabled")
        self.destination_combo.configure(state="disabled")
        self.refresh_button.configure(state="disabled")
        self.top_status.set(f"Preparing 0 of {len(selected_games)} games")
        self.footer_status.set("Task running")
        self.status_dot.configure(foreground=self.BLUE)
        self._set_game_checkboxes_enabled(False)
        for game in selected_games:
            self._set_row_status(normal_path(game.path), "Queued")
        self._append_activity(
            f"Starting: {len(selected_games)} selected game(s) → {destination}"
        )

        options = {
            "source": source,
            "destination": destination,
            "work_dir": self.work_dir,
            "all": False,
            "list": False,
            "selected_inputs": [str(game.path) for game in selected_games],
            "idle_timeout": self.idle_timeout,
            "skip_stfs_integrity": not self.verify_integrity,
        }
        threading.Thread(
            target=self._run_worker,
            args=(options,),
            name="game-preparation",
            daemon=True,
        ).start()

    def _run_worker(self, options: dict[str, object]) -> None:
        recorder: GuiRunRecorder | None = None
        exit_code = 1
        cancelled = False
        previous_recorder = engine._ACTIVE_RECORDER
        previous_cancel = engine._CANCEL_EVENT
        stdout = QueueWriter(self.events, "stdout")
        stderr = QueueWriter(self.events, "stderr")
        try:
            recorder = GuiRunRecorder(
                engine.ROOT,
                lambda payload: self.events.put({"kind": "engine", "payload": payload}),
            )
            engine._ACTIVE_RECORDER = recorder
            engine._CANCEL_EVENT = self.cancel_event
            args = SimpleNamespace(**options)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = engine.run(args, recorder)
        except engine.UserCancelled:
            cancelled = True
            exit_code = 2
            if recorder is not None:
                recorder.mode = "cancelled"
                recorder.current_stage = "cancelled"
                recorder.record("run_cancelled", input=recorder.current_input)
        except Exception as exc:
            if recorder is not None:
                diagnostic = engine.exception_diagnostic(
                    exc,
                    recorder.current_stage,
                    traceback.format_exc(),
                )
                if recorder.current_input:
                    diagnostic["input"] = recorder.current_input
                recorder.set_fatal_error(diagnostic)
            self.events.put({
                "kind": "worker_error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
            exit_code = 1
        finally:
            stdout.flush()
            stderr.flush()
            if recorder is not None:
                try:
                    recorder.finalize(exit_code)
                except Exception as exc:
                    self.events.put({"kind": "worker_error", "error": str(exc)})
                    exit_code = 1
            engine._ACTIVE_RECORDER = previous_recorder
            engine._CANCEL_EVENT = previous_cancel
            self.events.put({
                "kind": "run_finished",
                "exit_code": exit_code,
                "cancelled": cancelled,
                "summary": dict(recorder.summary) if recorder else {},
                "report_path": str(recorder.report_path) if recorder else "",
                "log_path": str(recorder.log_path) if recorder else "",
            })

    def request_cancel(self) -> None:
        if not self.running or self.cancel_event is None or self.cancel_event.is_set():
            return
        if not messagebox.askyesno(
            "Cancel the task?",
            "The current preparation or copy step will stop safely.\n\n"
            "Files already completed will remain on the destination.",
            icon="warning",
        ):
            return
        self.cancel_event.set()
        self.start_button.configure(text="Cancelling…", state="disabled")
        self.top_status.set("Cancelling safely…")
        self.footer_status.set("Cancellation requested")
        self.status_dot.configure(foreground=self.AMBER)
        self._append_activity("Cancellation requested. Cleaning up the current step…")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event.get("kind")
                if kind == "scan_finished":
                    self._finish_scan(event["source"], event["games"])
                elif kind == "scan_failed":
                    self._scan_failed(event["source"], str(event["error"]))
                elif kind == "engine":
                    self._apply_engine_event(event["payload"])
                elif kind == "output":
                    self._apply_output(str(event.get("line", "")), str(event.get("stream", "")))
                elif kind == "worker_error":
                    self._append_activity(f"Application problem: {event.get('error')}")
                elif kind == "run_finished":
                    self._finish_run(event)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _apply_engine_event(self, payload: dict[str, object]) -> None:
        event = payload.get("event")
        input_path = payload.get("input")
        row_key = normal_path(str(input_path)) if input_path else None
        row = row_key if row_key in self.rows_by_path else None
        if event == "input_started":
            self.active_row = row
            self.current_input_number = int(payload.get("input_number", 0) or 0)
            self.current_input_count = int(
                payload.get("input_count", len(self.run_games)) or len(self.run_games)
            )
            name = str(payload.get("name", "game"))
            self._set_row_status(row, "Checking")
            self.top_status.set(
                f"Checking game {self.current_input_number} of {self.current_input_count}: {name}"
            )
            self._append_activity(f"Checking {name}")
            self._set_progress((self.current_input_number - 1) / max(1, self.current_input_count) * 100)
        elif row is None:
            row = self.active_row

        if event == "input_started":
            return
        if event == "input_stage":
            stage = str(payload.get("stage", ""))
            labels = {
                "source_preparation": "Preparing",
                "destination_preflight": "Checking destination",
                "copy_and_verification": "Moving files",
                "input_complete": "Complete",
            }
            label = labels.get(stage)
            if label:
                self._set_row_status(row, label)
                if stage in {"source_preparation", "copy_and_verification"}:
                    self._append_activity(label)
        elif event == "external_tool_started":
            tool = str(payload.get("tool", "preparation tool"))
            self._append_activity(f"Running {tool}…")
        elif event == "external_tool_progress":
            label = str(payload.get("label", "Preparing"))
            size = engine.human_size(int(payload.get("output_bytes", 0) or 0))
            elapsed = int(payload.get("elapsed_seconds", 0) or 0)
            self._set_row_status(row, "Preparing")
            self.footer_status.set(f"{label}: {size} prepared in {elapsed}s")
        elif event == "preparation_notice":
            message = str(payload.get("message", ""))
            if message:
                self._append_activity(f"Note: {message}")
        elif event == "copy_batch_started":
            count = int(payload.get("file_count", 0) or 0)
            self._append_activity(f"Moving {count} files; verification will follow.")
        elif event == "copy_batch_progress":
            self._apply_batch_progress(payload, row)
        elif event == "copy_worker_started":
            number = int(payload.get("file_number", 0) or 0)
            total = int(payload.get("file_count", 0) or 0)
            destination = str(payload.get("relative_destination", ""))
            self._set_row_status(row, f"Moving file {number}/{total}")
            self._append_activity(f"Moving {destination}")
        elif event == "copy_worker_progress":
            self._apply_copy_progress(payload, row)
        elif event == "input_result":
            status = str(payload.get("status", ""))
            labels = {
                "verified": "✓ Complete",
                "skipped_existing": "Already present",
            }
            diagnostic = payload.get("diagnostic")
            diagnostic = diagnostic if isinstance(diagnostic, dict) else None
            label = failure_label(diagnostic) if status == "failed" else labels.get(status, status.title())
            self._set_row_status(row, label, problem=status == "failed")
            if row:
                self.completed_inputs.add(row)
            handled = len(self.completed_inputs)
            self._set_progress(handled / max(1, len(self.run_games)) * 100)
            if status == "failed":
                name = str(payload.get("name") or "Game")
                detail = failure_detail(diagnostic)
                self.failed_game_labels.append((name, label, detail))
                if row:
                    self.game_diagnostics[row] = detail
                self.top_status.set(f"{name}: {label}")
                self.footer_status.set(detail)
                self._append_activity(f"{name} failed — {label}. {detail}")
            else:
                self.top_status.set(f"Processed {handled} of {len(self.run_games)} games")
                name = str(payload.get("name") or "Game")
                self._append_activity(f"{name}: {label}")
            self.active_row = None

    def _apply_batch_progress(self, payload: dict[str, object], row: str | None) -> None:
        phase = str(payload.get("phase", ""))
        transitions = {
            "copy_phase_started": ("Moving files", "Moving files to the drive…"),
            "verification_phase_started": ("Verifying files", "Copy complete. Verifying all files…"),
            "commit_phase_started": ("Finishing", "Verification complete. Finalising files…"),
        }
        if phase in transitions:
            status, message = transitions[phase]
            self._set_row_status(row, status)
            self.top_status.set(status)
            self._append_activity(message)
        elif phase in {"copying", "verifying", "hashing_existing", "committing"}:
            number = int(payload.get("file_number", 0) or 0)
            count = int(payload.get("file_count", 0) or 0)
            label = {
                "copying": "Moving",
                "verifying": "Verifying",
                "hashing_existing": "Checking existing",
                "committing": "Finishing",
            }[phase]
            row_widgets = self.rows_by_path.get(row) if row else None
            if row_widgets:
                row_widgets.status.set(f"{label} {number}/{count}")
            self.footer_status.set(f"{label} file {number} of {count}")
        fraction = float(payload.get("progress", 0) or 0)
        game_fraction = 0.1 + 0.85 * min(1.0, max(0.0, fraction))
        self._set_progress(
            (max(0, self.current_input_number - 1) + game_fraction)
            / max(1, self.current_input_count) * 100
        )

    def _apply_copy_progress(self, payload: dict[str, object], row: str | None) -> None:
        phase = str(payload.get("phase", "working"))
        bytes_done = int(payload.get("bytes", 0) or 0)
        size = int(payload.get("size_bytes", 0) or 0)
        file_number = int(payload.get("file_number", 1) or 1)
        file_count = int(payload.get("file_count", 1) or 1)
        phase_labels = {
            "copying": "Moving",
            "verifying": "Verifying",
            "hashing existing": "Checking existing",
            "verifying existing": "Verifying existing",
            "done": "Verified",
        }
        label = phase_labels.get(phase, phase.replace("_", " ").title())
        self._set_row_status(row, f"{label} {file_number}/{file_count}")
        amount = engine.human_size(bytes_done)
        total = engine.human_size(size) if size else "unknown"
        self.footer_status.set(f"{label}: {amount} of {total}")

        byte_fraction = min(1.0, bytes_done / size) if size else 0.0
        if phase in {"verifying", "verifying existing"}:
            phase_fraction = 0.5 + byte_fraction * 0.5
        elif phase == "done":
            phase_fraction = 1.0
        else:
            phase_fraction = byte_fraction * 0.5
        file_fraction = ((file_number - 1) + phase_fraction) / max(1, file_count)
        game_fraction = 0.2 + file_fraction * 0.75
        overall = (
            (max(0, self.current_input_number - 1) + game_fraction)
            / max(1, self.current_input_count)
            * 100
        )
        self._set_progress(overall)

    def _apply_output(self, line: str, stream: str) -> None:
        # Routine console output duplicates structured events and can contain
        # extractor progress with carriage returns. The run log keeps it.
        if stream == "stderr" and line.startswith("FATAL:"):
            self._append_activity(line.strip())

    def _finish_run(self, event: dict[str, object]) -> None:
        self.running = False
        self.cancel_event = None
        self.last_report_path = Path(str(event["report_path"])) if event.get("report_path") else None
        self.last_log_path = Path(str(event["log_path"])) if event.get("log_path") else None
        self.start_button.configure(text="Prepare & move")
        self.options_button.configure(state="normal")
        self.source_entry.configure(state="normal")
        self.browse_button.configure(state="normal")
        self.rescan_button.configure(state="normal")
        self.destination_combo.configure(state="normal")
        self.refresh_button.configure(state="normal")
        self._set_game_checkboxes_enabled(True)

        summary = event.get("summary") if isinstance(event.get("summary"), dict) else {}
        transferred = int(summary.get("transferred_games", 0) or 0)
        skipped = int(summary.get("skipped_existing", 0) or 0)
        failed = int(summary.get("failed_games", 0) or 0)
        cancelled = bool(event.get("cancelled"))
        if cancelled:
            for game in self.run_games:
                key = normal_path(game.path)
                row = self.rows_by_path.get(key)
                if not row:
                    continue
                if key not in self.completed_inputs:
                    self._set_row_status(key, "Cancelled")
            self.top_status.set("Task cancelled")
            self.footer_status.set("Cancelled safely")
            self.status_dot.configure(foreground=self.AMBER)
            self._append_activity("Task cancelled. Completed files were left in place.")
            if not self.close_when_finished:
                self._show_run_summary(
                    title="Task cancelled",
                    headline="Transfer cancelled",
                    description="The task stopped safely. Completed games remain on the drive.",
                    tone="warning",
                    transferred=transferred,
                    skipped=skipped,
                    failed=failed,
                )
        elif int(event.get("exit_code", 1) or 0) == 0:
            self._set_progress(100)
            self.top_status.set(
                f"Finished — {transferred} transferred, {skipped} already present"
            )
            self.footer_status.set("Task complete")
            self.status_dot.configure(foreground=self.GREEN)
            self._append_activity("All selected games have been processed.")
            if not self.close_when_finished:
                self._show_run_summary(
                    title="Preparation complete",
                    headline="All selected games are ready",
                    description="Every transferred file passed SHA-256 verification.",
                    tone="success",
                    transferred=transferred,
                    skipped=skipped,
                    failed=failed,
                )
        else:
            reasons = {reason for _, reason, _ in self.failed_game_labels}
            reason_text = next(iter(reasons)) if len(reasons) == 1 else "See game statuses"
            self.top_status.set(f"{failed} failed — {reason_text}")
            self.footer_status.set(f"Finished with {failed} failed game(s)")
            self.status_dot.configure(foreground=self.RED)
            self._append_activity(f"Finished: {failed} game(s) failed. {reason_text}.")
            if not self.close_when_finished:
                self._show_run_summary(
                    title="Finished with errors",
                    headline=f"{failed} game(s) need attention",
                    description="Other selected games continued normally.",
                    tone="error",
                    transferred=transferred,
                    skipped=skipped,
                    failed=failed,
                    failures=self.failed_game_labels,
                )
        self._update_ready_state()
        self.run_games = []
        if self.close_when_finished:
            self.root.destroy()

    def _show_run_summary(
        self,
        *,
        title: str,
        headline: str,
        description: str,
        tone: str,
        transferred: int,
        skipped: int,
        failed: int,
        failures: list[tuple[str, str, str]] | None = None,
    ) -> None:
        popup = tk.Toplevel(self.root)
        popup.title(title)
        screen_height = popup.winfo_screenheight()
        popup.geometry(f"760x{min(650, max(480, screen_height - 120))}")
        popup.minsize(600, 440)
        popup.configure(background=self.BG)
        popup.transient(self.root)
        popup.grab_set()
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(0, weight=1)
        popup.protocol("WM_DELETE_WINDOW", popup.destroy)

        tone_color = {
            "success": self.GREEN,
            "warning": self.AMBER,
            "error": self.RED,
        }.get(tone, self.BLUE)

        shell = ttk.Frame(popup, padding=18, style="Shell.TFrame")
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(3, weight=1)

        hero = ttk.Frame(shell, padding=(18, 16), style="Card.TFrame")
        hero.grid(row=0, column=0, sticky="ew")
        hero.columnconfigure(1, weight=1)
        tk.Label(hero, text="!" if tone != "success" else "✓",
                 background=self.SURFACE, foreground=tone_color,
                 font=("Segoe UI Semibold", 27), width=2).grid(
                     row=0, column=0, rowspan=2, padx=(0, 12), sticky="n")
        ttk.Label(hero, text=headline, style="DialogTitle.TLabel",
                  wraplength=620).grid(
            row=0, column=1, sticky="w")
        ttk.Label(hero, text=description, style="Muted.TLabel",
                  wraplength=620).grid(row=1, column=1, sticky="w", pady=(4, 0))

        stats = ttk.Frame(shell, style="Shell.TFrame")
        stats.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        for column, (label, value, color) in enumerate((
            ("Transferred", transferred, self.GREEN),
            ("Already present", skipped, self.BLUE),
            ("Failed", failed, self.RED if failed else self.MUTED),
        )):
            stats.columnconfigure(column, weight=1)
            tile = ttk.Frame(stats, padding=(14, 10), style="Card.TFrame")
            tile.grid(row=0, column=column, sticky="ew",
                      padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
            ttk.Label(tile, text=label, style="Muted.TLabel").pack(anchor="w")
            tk.Label(tile, text=str(value), background=self.SURFACE,
                     foreground=color, font=("Segoe UI Semibold", 18)).pack(anchor="w")

        failures = failures or []
        list_header = ttk.Frame(shell, style="Shell.TFrame")
        list_header.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(
            list_header,
            text=(f"Games needing attention  ·  {len(failures)}" if failures
                  else "Run details"),
            style="ShellHeading.TLabel",
        ).pack(side="left")

        list_area = ttk.Frame(shell, style="Shell.TFrame")
        list_area.grid(row=3, column=0, sticky="nsew")
        list_area.columnconfigure(0, weight=1)
        list_area.rowconfigure(0, weight=1)
        listing = tk.Canvas(list_area, height=250, background=self.BG,
                            highlightthickness=0, borderwidth=0)
        listing.grid(row=0, column=0, sticky="nsew")
        list_scroll = ttk.Scrollbar(list_area, orient="vertical", command=listing.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        listing.configure(yscrollcommand=list_scroll.set)
        def scroll_failure_list(event: tk.Event) -> str:
            steps = int(-event.delta / 120)
            if not steps:
                steps = -1 if event.delta > 0 else 1
            listing.yview_scroll(steps, "units")
            return "break"

        listing.bind("<MouseWheel>", scroll_failure_list)
        list_scroll.bind("<MouseWheel>", scroll_failure_list)
        rows = ttk.Frame(listing, style="Shell.TFrame")
        rows.columnconfigure(0, weight=1)
        rows_window = listing.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda _event: listing.configure(
            scrollregion=listing.bbox("all")))
        listing.bind("<Configure>", lambda event: listing.itemconfigure(
            rows_window, width=event.width))
        if failures:
            for index, (name, reason, detail) in enumerate(failures):
                card = ttk.Frame(rows, padding=(12, 9), style="Card.TFrame")
                card.grid(row=index, column=0, sticky="ew", pady=(0, 7))
                card.columnconfigure(0, weight=1)
                ttk.Label(card, text=name, style="Heading.TLabel",
                          wraplength=650).grid(row=0, column=0, sticky="w")
                ttk.Label(card, text=reason, foreground=tone_color,
                          font=("Segoe UI Semibold", 9)).grid(
                              row=0, column=1, padx=(10, 0), sticky="e")
                ttk.Label(card, text=detail, style="Muted.TLabel",
                          wraplength=650).grid(row=1, column=0, columnspan=2,
                                               sticky="w", pady=(4, 0))
                for child in (card, *card.winfo_children()):
                    child.bind("<MouseWheel>", scroll_failure_list)
        else:
            note = "No failed games were reported."
            if self.last_report_path:
                note = "The run report contains the complete summary."
            ttk.Label(rows, text=note, style="Muted.TLabel", padding=12).grid(
                row=0, column=0, sticky="w")

        footer = ttk.Frame(shell, style="Shell.TFrame")
        footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="Full details are available in the saved report and log.",
                  style="Shell.TLabel", wraplength=340).grid(row=0, column=0, sticky="w")
        actions = ttk.Frame(footer, style="Shell.TFrame")
        actions.grid(row=0, column=1, sticky="e")
        if self.last_report_path:
            ttk.Button(actions, text="Open report",
                       command=lambda: self._open_artifact(self.last_report_path)).pack(
                           side="left", padx=(0, 6))
        if self.last_log_path:
            ttk.Button(actions, text="Open log",
                       command=lambda: self._open_artifact(self.last_log_path)).pack(
                           side="left", padx=(0, 6))
        ttk.Button(actions, text="Close", style="Primary.TButton",
                   command=popup.destroy).pack(side="left")
        self._theme_window(popup)
        popup.update_idletasks()
        popup.focus_set()

    def _open_artifact(self, path: Path) -> None:
        if not path.is_file():
            return
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("Run artifact", str(path), parent=self.root)

    def _set_row_status(self, row: str | None, status: str, problem: bool = False) -> None:
        row_widgets = self.rows_by_path.get(row) if row else None
        if not row_widgets:
            return
        row_widgets.status.set(status)
        row_widgets.status_label.configure(
            style="Problem.GameList.TLabel" if problem else "GameList.TLabel",
            cursor="hand2" if problem else "",
        )
        self._scroll_game_row_into_view(row_widgets.frame)

    def _scroll_game_row_into_view(self, row: ttk.Frame) -> None:
        self.root.update_idletasks()
        row_top = row.winfo_y()
        row_bottom = row_top + row.winfo_height()
        visible_top = self.game_canvas.canvasy(0)
        visible_bottom = visible_top + self.game_canvas.winfo_height()
        if row_top < visible_top or row_bottom > visible_bottom:
            content_height = max(1, self.game_rows.winfo_height())
            target = row_top if row_top < visible_top else row_bottom - self.game_canvas.winfo_height()
            self.game_canvas.yview_moveto(max(0.0, target / content_height))

    def _set_progress(self, value: float) -> None:
        value = max(0.0, min(100.0, value))
        self.progress_value.set(value)
        self.progress_text.set(f"{round(value):d}%")

    def _append_activity(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        entry = f"{datetime.now().strftime('%H:%M:%S')}  {text}"
        self.activity_lines.append(entry)
        if len(self.activity_lines) > 300:
            del self.activity_lines[: len(self.activity_lines) - 300]
        self.activity.configure(state="normal")
        if self.activity.index("end-1c") != "1.0":
            self.activity.insert("end", "\n")
        self.activity.insert("end", entry)
        line_count = int(self.activity.index("end-1c").split(".")[0])
        if line_count > 60:
            self.activity.delete("1.0", f"{line_count - 60}.0")
        self.activity.see("end")
        self.activity.configure(state="disabled")
        widget = self.activity_popup_text
        if widget is None or not widget.winfo_exists():
            return
        widget.configure(state="normal")
        if widget.index("end-1c") != "1.0":
            widget.insert("end", "\n")
        widget.insert("end", entry)
        widget.see("end")
        widget.configure(state="disabled")

    def show_activity_window(self) -> None:
        if self.activity_popup is not None and self.activity_popup.winfo_exists():
            self.activity_popup.deiconify()
            self.activity_popup.lift()
            self.activity_popup.focus_force()
            return

        popup = tk.Toplevel(self.root)
        popup.title(f"Activity history — {APP_NAME}")
        popup.geometry("820x550")
        popup.minsize(620, 360)
        popup.configure(background=self.BG)
        popup.protocol("WM_DELETE_WINDOW", self._close_activity_window)
        popup.rowconfigure(1, weight=1)
        popup.columnconfigure(0, weight=1)

        header = ttk.Frame(popup, padding=(20, 16, 20, 12), style="Shell.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="Activity history",
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Key steps and errors only. Detailed technical logs are saved separately.",
            style="Shell.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Button(header, text="Copy all", command=self.copy_activity).grid(
            row=0, column=1, rowspan=2, padx=(12, 0)
        )

        terminal_frame = ttk.Frame(popup, padding=(20, 0, 20, 10), style="Shell.TFrame")
        terminal_frame.grid(row=1, column=0, sticky="nsew")
        terminal_frame.rowconfigure(0, weight=1)
        terminal_frame.columnconfigure(0, weight=1)
        terminal = tk.Text(
            terminal_frame,
            wrap="word",
            background=self.SURFACE,
            foreground=self.TEXT,
            insertbackground=self.TEXT,
            selectbackground=self.SELECTION,
            font=("Segoe UI", 10),
            relief="solid",
            borderwidth=1,
            padx=14,
            pady=12,
            state="normal",
        )
        vertical = ttk.Scrollbar(terminal_frame, orient="vertical", command=terminal.yview)
        terminal.configure(yscrollcommand=vertical.set)
        terminal.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        if self.activity_lines:
            terminal.insert("1.0", "\n".join(self.activity_lines))
            terminal.see("end")
        terminal.configure(state="disabled")

        footer = ttk.Frame(popup, padding=(20, 0, 20, 16), style="Shell.TFrame")
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(2, weight=1)
        ttk.Button(footer, text="Open reports folder", command=self.open_reports_folder).grid(
            row=0, column=0, sticky="w")
        ttk.Button(footer, text="Open diagnostic logs", command=self.open_logs_folder).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(footer, text="Close", command=self._close_activity_window).grid(
            row=0, column=2, sticky="e"
        )

        self.activity_popup = popup
        self.activity_popup_text = terminal
        self._theme_window(popup)

    def copy_activity(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(self.activity_lines))
        self.root.update_idletasks()

    def _close_activity_window(self) -> None:
        if self.activity_popup is not None and self.activity_popup.winfo_exists():
            self.activity_popup.destroy()
        self.activity_popup = None
        self.activity_popup_text = None

    def show_options(self) -> None:
        if not self.running:
            OptionsDialog(self)

    def open_reports_folder(self) -> None:
        folder = engine.ROOT / "Reports"
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("Reports folder", str(folder))

    def open_logs_folder(self) -> None:
        folder = engine.ROOT / "Logs"
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("Diagnostic logs folder", str(folder))

    def show_about(self) -> None:
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} v{engine.VERSION}\n\n"
            "Detects supported Xbox games, prepares them, and moves each one "
            "to the correct location on locally mounted Xbox 360 storage.\n\n"
            "It does not format or partition drives.",
        )

    def on_close(self) -> None:
        if self.running:
            if messagebox.askyesno(
                "Task in progress",
                "Cancel the current task and close after cleanup finishes?",
                icon="warning",
            ):
                self.close_when_finished = True
                if self.cancel_event is not None:
                    self.cancel_event.set()
                self.start_button.configure(text="Cancelling…", state="disabled")
                self.top_status.set("Cancelling safely…")
            return
        self.root.destroy()


def hide_console_window() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except OSError:
        pass


def write_startup_error(error: BaseException) -> Path | None:
    """Persist failures that happen before the normal run logger exists."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = [engine.ROOT / "Logs"]
    if local_app_data:
        candidates.append(Path(local_app_data) / "XboxGamePrepTool" / "Logs")
    candidates.append(Path(tempfile.gettempdir()) / "XboxGamePrepTool" / "Logs")

    details = "\n".join(
        [
            f"{APP_NAME} v{engine.VERSION} startup failure",
            f"Time (UTC): {datetime.now(timezone.utc).isoformat()}",
            f"Executable: {sys.executable}",
            f"Frozen build: {bool(getattr(sys, 'frozen', False))}",
            f"Bundle path: {getattr(sys, '_MEIPASS', '(not frozen)')}",
            f"TCL_LIBRARY: {os.environ.get('TCL_LIBRARY', '(not set)')}",
            f"TK_LIBRARY: {os.environ.get('TK_LIBRARY', '(not set)')}",
            f"Error: {type(error).__name__}: {error}",
            "",
            traceback.format_exc(),
        ]
    )

    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "gui-startup-error.log"
            path.write_text(details, encoding="utf-8")
            return path
        except OSError:
            continue
    return None


def show_startup_error(error: BaseException, log_path: Path | None) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        location = str(log_path) if log_path else "No log file could be written."
        message = (
            f"{APP_NAME} could not start.\n\n"
            f"{type(error).__name__}: {error}\n\n"
            f"Diagnostic log:\n{location}"
        )
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    except OSError:
        pass


def main() -> int:
    # The frozen GUI executable also acts as the isolated copy worker. Delegate
    # before hiding the console so the parent process can read its JSON output.
    if "--copy-worker" in sys.argv or "--copy-batch-worker" in sys.argv:
        return engine.main()
    startup_check = "--startup-check" in sys.argv
    if not startup_check:
        hide_console_window()
    try:
        root = tk.Tk()
        if startup_check:
            root.withdraw()
        GamePrepApp(root)
        if startup_check:
            root.update_idletasks()
            root.destroy()
            print("GUI startup check passed.")
            return 0
        root.mainloop()
        return 0
    except Exception as error:
        log_path = write_startup_error(error)
        if startup_check:
            print(traceback.format_exc(), file=sys.stderr)
        else:
            show_startup_error(error, log_path)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
