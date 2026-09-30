import os
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import tkinter as tk
from tkinter import ttk

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xbox_game_prep_gui as gui
import xbox_hdd_prep as engine


class GuiSupportTests(unittest.TestCase):
    @staticmethod
    def destroy_tk_root(root):
        for timer in root.tk.call("after", "info"):
            root.tk.call("after", "cancel", timer)
        root.destroy()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_friendly_types_distinguish_extracted_console_generations(self):
        xbox_360 = self.root / "Xbox 360 Game"
        xbox_360.mkdir()
        (xbox_360 / "default.xex").write_bytes(b"fixture")
        original = self.root / "Original Xbox Game"
        original.mkdir()
        (original / "default.xbe").write_bytes(b"fixture")

        self.assertEqual(
            gui.friendly_game_type(xbox_360, "extracted game"),
            "Extracted Xbox 360 game",
        )
        self.assertEqual(
            gui.friendly_game_type(original, "extracted game"),
            "Original Xbox game",
        )

    def test_content_package_type_is_read_from_header(self):
        package = self.root / "XBLA Package"
        data = bytearray(0x400)
        data[:4] = b"LIVE"
        data[0x344:0x348] = bytes.fromhex("000D0000")
        data[0x360:0x364] = bytes.fromhex("58410AE9")
        package.write_bytes(data)

        self.assertEqual(
            gui.friendly_game_type(package, "Xbox content package"),
            "Xbox Live Arcade",
        )

    def test_scan_summary_counts_each_detected_type(self):
        games = [
            gui.DetectedGame(self.root / "one", "disc image", "Disc image"),
            gui.DetectedGame(self.root / "two", "disc image", "Disc image"),
            gui.DetectedGame(
                self.root / "three",
                "Xbox content package",
                "Xbox Live Arcade",
            ),
        ]
        self.assertEqual(
            gui.summarize_games(games),
            [("Disc image", 2), ("Xbox Live Arcade", 1)],
        )

    def test_gui_recorder_forwards_structured_events(self):
        events = []
        recorder = gui.GuiRunRecorder(self.root, events.append)
        recorder.record("input_stage", input="game.iso", stage="source_preparation")
        recorder.finalize(0)

        self.assertEqual(events[0]["event"], "run_started")
        self.assertTrue(any(event["event"] == "input_stage" for event in events))
        self.assertEqual(events[-1]["event"], "run_finished")

    def test_cancellation_check_uses_gui_event(self):
        previous = engine._CANCEL_EVENT
        cancel = threading.Event()
        cancel.set()
        engine._CANCEL_EVENT = cancel
        try:
            with self.assertRaises(engine.UserCancelled):
                engine.check_cancelled()
        finally:
            engine._CANCEL_EVENT = previous

    def test_failure_labels_explain_space_and_conflict(self):
        self.assertEqual(
            gui.failure_label({"code": "destination_no_space"}),
            "Not enough space",
        )
        self.assertEqual(
            gui.failure_detail({"code": "destination_no_space", "details": {
                "estimated_bytes": 2048, "free_bytes": 1024,
            }}),
            "Needs about 2.00 KiB; 1.00 KiB was free.",
        )
        self.assertEqual(
            gui.failure_label({"code": "destination_conflict_hash"}),
            "File conflict",
        )

    def test_appearance_preferences_survive_restart_and_preserve_other_settings(self):
        path = self.root / "gui-settings.json"
        self.assertFalse(gui.load_dark_mode(path))
        path.write_text('{"another_setting": 12}', encoding="utf-8")
        gui.save_dark_mode(path, True)
        self.assertTrue(gui.load_dark_mode(path))
        self.assertEqual(json.loads(path.read_text())["another_setting"], 12)
        gui.save_dark_mode(path, False)
        self.assertFalse(gui.load_dark_mode(path))
        self.assertEqual(list(self.root.glob(".gui-settings-*")), [])

    def test_damaged_preferences_and_interrupted_save_are_safe(self):
        path = self.root / "gui-settings.json"
        for content in ('{broken', '[]', '{"dark_mode": "false"}', ''):
            path.write_text(content, encoding="utf-8")
            self.assertFalse(gui.load_dark_mode(path))
        gui.save_dark_mode(path, True)
        with patch.object(gui.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                gui.save_dark_mode(path, False)
        self.assertTrue(gui.load_dark_mode(path))
        self.assertEqual(list(self.root.glob(".gui-settings-*")), [])

    @unittest.skipUnless(os.environ.get("XBOX_GUI_LAYOUT_TESTS") == "1",
                         "Set XBOX_GUI_LAYOUT_TESTS=1 for the live theme check")
    def test_live_theme_switch_preserves_transfer_state_and_themes_open_dialogs(self):
        gui.save_dark_mode(self.root / "gui-settings.json", True)
        with patch.object(gui, "available_destination_drives", return_value=[]), \
                patch.object(engine, "ROOT", self.root):
            root = tk.Tk()
            self.addCleanup(self.destroy_tk_root, root)
            root.geometry("1240x850+5000+5000")
            app = gui.GamePrepApp(root)
        self.assertTrue(app.dark_mode.get())
        self.assertEqual(root.cget("background"), gui.DARK_PALETTE["BG"])
        games = [gui.DetectedGame(self.root / f"Game {number}", "disc image",
                                  "Disc image", estimated_bytes=1024)
                 for number in range(12)]
        app._finish_scan(self.root, games)
        app._append_activity("A test activity entry")
        app.show_activity_window()
        app.activity_popup.geometry("820x550+5000+5000")
        options = gui.OptionsDialog(app)
        options.geometry("+5000+5000")
        options.destroy()
        app._show_run_summary(title="Test summary", headline="One game failed",
                              description="Theme fixture", tone="error", transferred=1,
                              skipped=0, failed=1,
                              failures=[("A game", "Not enough space", "Needs more room.")])
        summary = next(child for child in root.winfo_children()
                       if isinstance(child, tk.Toplevel) and child.title() == "Test summary")
        summary.geometry("760x650+5000+5000")
        root.update()
        app.game_canvas.yview_moveto(1.0)
        root.update()
        before_scroll = app.game_canvas.yview()
        before_selection = app.selected_paths.copy()
        before_rows = app.rows_by_path.copy()
        app.running = True
        app._set_game_checkboxes_enabled(False)
        app._set_progress(42)
        app.status_dot.configure(foreground=app.AMBER)
        app.capacity_label.configure(foreground=app.RED)

        for enabled, palette in ((False, gui.LIGHT_PALETTE), (True, gui.DARK_PALETTE)):
            app.dark_mode.set(enabled)
            app.change_appearance()
            root.update()
            self.assertEqual(app.selected_paths, before_selection)
            self.assertEqual(app.rows_by_path, before_rows)
            self.assertEqual(app.game_canvas.yview(), before_scroll)
            self.assertTrue(app.running)
            self.assertEqual(app.progress_value.get(), 42)
            self.assertEqual(app.status_dot.cget("foreground"), palette["AMBER"])
            self.assertEqual(str(app.capacity_label.cget("foreground")), palette["RED"])
            self.assertEqual(app.activity.cget("background"), palette["ACTIVITY"])
            self.assertEqual(app.activity_popup_text.cget("background"), palette["SURFACE"])
            self.assertIn("A test activity entry", app.activity.get("1.0", "end"))
            self.assertEqual(summary.cget("background"), palette["BG"])
            style = ttk.Style(root)
            self.assertEqual(style.lookup("Menu.TMenubutton", "background"), palette["BG"])
            self.assertFalse(root.cget("menu"))
            self.assertEqual(style.lookup("TEntry", "fieldbackground"), palette["INPUT"])
            self.assertEqual(style.lookup("TCombobox", "fieldbackground", ("readonly",)),
                             palette["INPUT"])
            self.assertEqual(style.lookup("TCheckbutton", "indicatorbackground", ("disabled",)),
                             palette["DISABLED"])
            self.assertEqual(style.lookup("Problem.GameList.TLabel", "foreground"), palette["RED"])
            popdown = root.tk.call("ttk::combobox::PopdownWindow", str(app.destination_combo))
            self.assertEqual(str(root.tk.call(f"{popdown}.f.l", "cget", "-background")), palette["INPUT"])
            self.assertEqual(gui.load_dark_mode(app.settings_path), enabled)

        with patch.object(gui, "save_dark_mode", side_effect=OSError("read-only")):
            app.change_appearance()
        self.assertIn("setting could not be saved", app.activity_lines[-1])

    @unittest.skipUnless(os.environ.get("XBOX_GUI_LAYOUT_TESTS") == "1",
                         "Set XBOX_GUI_LAYOUT_TESTS=1 for the off-screen Tk layout check")
    def test_header_selection_tracks_partial_selection_and_ignores_unready_games(self):
        with patch.object(gui, "available_destination_drives", return_value=[]), \
                patch.object(engine, "ROOT", self.root):
            root = tk.Tk()
            self.addCleanup(self.destroy_tk_root, root)
            root.geometry("1240x850+5000+5000")
            app = gui.GamePrepApp(root)
        self.assertTrue(app.select_all_checkbox.instate(["disabled"]))
        games = [gui.DetectedGame(self.root / "First", "disc image", "Disc image", estimated_bytes=1024),
                 gui.DetectedGame(self.root / "Second", "disc image", "Disc image", estimated_bytes=1024),
                 gui.DetectedGame(self.root / "Broken", "disc image", "Disc image", status="Scan issue")]
        app._finish_scan(self.root, games)
        root.update()
        ready_keys = {gui.normal_path(game.path) for game in games[:2]}
        first = app.rows_by_path[gui.normal_path(games[0].path)]
        self.assertTrue(app.select_all_checkbox.instate(["selected", "!alternate", "!disabled"]))
        self.assertEqual(app.game_header.grid_bbox(0, 0)[2], first.frame.grid_bbox(0, 0)[2])
        first.checkbox.invoke()
        self.assertTrue(app.select_all_checkbox.instate(["alternate", "!selected"]))
        app.select_all_checkbox.invoke()
        self.assertEqual(app.selected_paths, ready_keys)
        self.assertTrue(app.select_all_checkbox.instate(["selected", "!alternate"]))
        app.select_all_checkbox.invoke()
        self.assertFalse(app.selected_paths)
        self.assertTrue(app.clear_selection_button.instate(["disabled"]))
        first.checkbox.invoke()
        app.clear_selection_button.invoke()
        self.assertFalse(app.selected_paths)
        self.assertTrue(app.select_all_checkbox.instate(["!selected", "!alternate"]))
        self.assertIn("0 B", app.selection_total.get())
        app.select_all_checkbox.invoke()
        for busy_flag in ("scanning", "running"):
            setattr(app, busy_flag, True)
            app._set_game_checkboxes_enabled(False)
            self.assertTrue(app.select_all_checkbox.instate(["disabled"]))
            self.assertTrue(app.clear_selection_button.instate(["disabled"]))
            app.select_all_checkbox.invoke()
            app.clear_selection_button.invoke()
            self.assertEqual(app.selected_paths, ready_keys)
            setattr(app, busy_flag, False)
            app._set_game_checkboxes_enabled(True)
        for enabled, palette in ((True, gui.DARK_PALETTE), (False, gui.LIGHT_PALETTE)):
            app.dark_mode.set(enabled)
            app.change_appearance()
            style = ttk.Style(root)
            self.assertEqual(style.lookup("Header.TCheckbutton", "background", ("active",)), palette["HEADER"])
            self.assertEqual(style.lookup("Link.TButton", "background", ("disabled",)), palette["SURFACE"])
        app._finish_scan(self.root, games[2:])
        self.assertTrue(app.select_all_checkbox.instate(["disabled", "!selected", "!alternate"]))

    @unittest.skipUnless(os.environ.get("XBOX_GUI_LAYOUT_TESTS") == "1",
                         "Set XBOX_GUI_LAYOUT_TESTS=1 for the off-screen Tk layout check")
    def test_game_list_has_usable_height_and_scrolls(self):
        with patch.object(gui, "available_destination_drives", return_value=[]):
            root = tk.Tk()
            self.addCleanup(self.destroy_tk_root, root)
            root.geometry("1240x850+5000+5000")
            app = gui.GamePrepApp(root)
        source = self.root / "games"
        source.mkdir()
        games = [gui.DetectedGame(source / f"Game {number:02}", "disc image",
                                  "Disc image", estimated_bytes=1024)
                 for number in range(12)]
        app._finish_scan(source, games)
        root.update()

        self.assertGreaterEqual(app.game_canvas.winfo_height(), 240)
        self.assertLess(app.game_canvas.yview()[1], 1.0)
        app.game_canvas.yview_moveto(1.0)
        root.update()
        self.assertGreater(app.game_canvas.yview()[0], 0.0)
        self.assertLess(app.page_canvas.yview()[1], 1.0)


if __name__ == "__main__":
    unittest.main()
