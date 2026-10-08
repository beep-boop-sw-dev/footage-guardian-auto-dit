from __future__ import annotations

import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk

from footage_guardian import ui
from footage_guardian.ui import App


def tk_available() -> bool:
    try:
        root = tk.Tk()
    except Exception:
        return False
    root.destroy()
    return True


@unittest.skipUnless(tk_available(), "no window server available")
class WindowWiringTests(unittest.TestCase):
    """Proves the window itself works, not just the engine underneath it.

    Every engine call was already tested when this was written, and the app
    still froze on launch: the timer walked every mount under /Volumes — the
    startup disk included — on the thread that draws the window. The gap was
    never the engine, it was the wiring, so these tests drive the window.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # No hardware probing: these prove the window, and what is
        # plugged into the machine running them is not part of that.
        self.app = App(self.root / "config.json",
                       self.root / "manifest.sqlite3",
                       self.root / "guardian.log",
                       probe_hardware=False)
        self.app.withdraw()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.app.destroy)

    def _settle(self, timeout: float = 5.0) -> None:
        """Pump the event loop until the background scans have posted back."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            self.app._drain_results()
            if not self.app._device_scan and not self.app._status_scan:
                return
            time.sleep(0.01)
        self.fail("a background scan never finished")

    def _buttons(self) -> list[ttk.Button]:
        found: list[ttk.Button] = []

        def walk(widget) -> None:
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Button, tk.Button)):
                    found.append(child)
                walk(child)

        walk(self.app)
        return found

    def test_the_timer_returns_immediately(self) -> None:
        """The freeze, stated as a test.

        _tick runs every four seconds on the drawing thread. If the scanning it
        starts happens there too, the window never paints again. Whatever is
        plugged into this Mac, the tick itself must come straight back.
        """
        started = time.monotonic()
        self.app._tick()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0,
                        f"_tick blocked the drawing thread for {elapsed:.1f}s")

    def test_an_unchanged_set_of_drives_is_only_scanned_once(self) -> None:
        """Nothing about a drive changes while it stays plugged in.

        Rescanning on every tick is what made the cost of a slow drive
        unbounded: each four-second tick started work the last had not finished.
        """
        calls = []
        original = ui.describe_mounted_devices
        ui.describe_mounted_devices = lambda *a, **k: calls.append(1) or []
        self.addCleanup(setattr, ui, "describe_mounted_devices", original)

        for _ in range(10):
            self.app.refresh_devices()
            self._settle()
        self.assertEqual(len(calls), 1, f"scanned {len(calls)} times, expected 1")

    def test_naming_a_different_ssd_forces_a_rescan(self) -> None:
        """The SSD is excluded from the list, so changing it changes the list."""
        calls = []
        original = ui.describe_mounted_devices
        ui.describe_mounted_devices = lambda *a, **k: calls.append(1) or []
        self.addCleanup(setattr, ui, "describe_mounted_devices", original)

        self.app.refresh_devices()
        self._settle()
        self.app.refresh_devices(force=True)
        self._settle()
        self.assertEqual(len(calls), 2)

    def test_a_forced_rescan_during_a_scan_is_not_dropped(self) -> None:
        """Saving a new SSD mid-scan must still refresh the list afterwards."""
        calls = []
        original = ui.describe_mounted_devices
        ui.describe_mounted_devices = lambda *a, **k: calls.append(1) or []
        self.addCleanup(setattr, ui, "describe_mounted_devices", original)

        self.app._device_scan = True          # a scan is already in flight
        self.app.refresh_devices(force=True)
        self.assertTrue(self.app._device_rescan, "the forced rescan was dropped")

        self.app._devices_ready([])           # the in-flight scan finishes
        self._settle()
        self.assertEqual(len(calls), 1)

    def test_every_button_is_wired_to_something_callable(self) -> None:
        """A dead button on Tab 2 means a shoot day with no backup."""
        buttons = self._buttons()
        self.assertGreater(len(buttons), 4, "no buttons were found to check")
        for button in buttons:
            label = button.cget("text")
            name = str(button.cget("command"))
            self.assertTrue(name, f"button {label!r} has no command")
            # Tk stores the callback under a generated name; if the binding were
            # dead the name would not resolve to a registered command.
            self.assertIn(name, self.app.tk.call("info", "commands"),
                          f"button {label!r} points at a command that does not exist")

    def test_the_stage_lines_render_before_any_drive_is_set(self) -> None:
        """First launch is unconfigured; it must explain itself, not sit blank."""
        self.app._apply_status({"message": "Set the SSD main drive on the Drives tab to begin.",
                                "backup_state": "No SSD main drive is set yet.",
                                "sync_state": "No SSD main drive is set yet."})
        self.assertIn("Set the SSD main drive", self.app.banner.cget("text"))
        for label in self.app.stage_labels.values():
            self.assertTrue(label.cget("text"))

    def test_device_rows_reach_the_table(self) -> None:
        """The worker posts rows back; they must actually land in the list."""
        self.app._devices_ready([("CAMERA_SD", "GoPro", "412 files")])
        rows = [self.app.devices.item(item)["values"]
                for item in self.app.devices.get_children()]
        self.assertEqual(rows, [["CAMERA_SD", "GoPro", "412 files"]])
        self.assertFalse(self.app._device_scan)

    def test_a_failed_scan_reports_instead_of_disappearing(self) -> None:
        self.app._devices_ready([("—", "—", "The drives could not be read: boom")])
        rows = [self.app.devices.item(item)["values"]
                for item in self.app.devices.get_children()]
        self.assertIn("could not be read", rows[0][2])


    def _plan(self, plugged_in: bool = True):
        from footage_guardian.backup import scan_backups
        ssd, one, two = (self.root / n for n in ("SSD", "HDD1", "HDD2"))
        clip = ssd / "10-8-26" / "360" / "A.insv"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"a" * 100)
        one.mkdir()
        if plugged_in:
            two.mkdir()
        return scan_backups(ssd, [one, two])

    def test_the_backup_table_fills_and_the_button_comes_on(self) -> None:
        self.app._plan_ready(self._plan())
        rows = [self.app.backup_table.item(i)["values"]
                for i in self.app.backup_table.get_children()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(self.app.backup_button.cget("state")), "normal")
        self.assertIn("need backing up", self.app.backup_state.cget("text"))

    def test_a_missing_hdd_keeps_the_backup_button_off(self) -> None:
        self.app._plan_ready(self._plan(plugged_in=False))
        self.assertEqual(str(self.app.backup_button.cget("state")), "disabled")
        self.assertIn("Plug in", self.app.backup_state.cget("text"))

    def test_the_status_timer_does_not_overwrite_the_backup_summary(self) -> None:
        self.app._plan_ready(self._plan())
        self.app._apply_status({"message": "x", "backup_state": "old per-day line",
                                "sync_state": "y"})
        self.assertNotIn("old per-day line", self.app.backup_state.cget("text"))

    def test_a_backup_cannot_start_while_another_job_runs(self) -> None:
        self.app._plan_ready(self._plan())
        self.app.busy = True
        original = ui.messagebox.showinfo
        ui.messagebox.showinfo = lambda *a, **k: None
        try:
            self.app.start_backup()
        finally:
            ui.messagebox.showinfo = original
        self.assertIsNone(self.app._active_guardian)


    def test_backup_progress_reaches_the_tab(self) -> None:
        self.app._show_backup_progress(
            25, 100, "Back up HDD 1: 9:8:26/A.MOV  ·  1.0 TB of 4.0 TB  ·  80.0 MB/s  ·  about 10.4 hr left")
        self.assertEqual(float(self.app.backup_bar.cget("value")), 25.0)
        self.assertIn("25%", self.app.backup_eta.cget("text"))
        self.assertIn("about 10.4 hr left", self.app.backup_eta.cget("text"))
        self.assertIn("A.MOV", self.app.backup_file.cget("text"))


    def _cloud_plan(self, folder_exists: bool = True):
        from footage_guardian.cloud_sync import scan_cloud
        from tests.test_cloud_sync import FolderCloud
        ssd = self.root / "SSD"
        clip = ssd / "10-8-26" / "A.MOV"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"a" * 100)
        drive = self.root / "Drive" / "Master"
        if folder_exists:
            drive.mkdir(parents=True)
        return scan_cloud(FolderCloud(drive), "gdrive:Master", ssd, [])

    def test_the_sync_table_fills_and_the_button_comes_on(self) -> None:
        self.app._cloud_ready(self._cloud_plan())
        rows = [self.app.sync_table.item(i)["values"] for i in self.app.sync_table.get_children()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(self.app.sync_button.cget("state")), "normal")
        self.assertIn("Uploading to: gdrive:Master", self.app.sync_state.cget("text"))

    def test_a_missing_drive_folder_keeps_the_upload_button_off(self) -> None:
        self.app._cloud_ready(self._cloud_plan(folder_exists=False))
        self.assertEqual(str(self.app.sync_button.cget("state")), "disabled")
        self.assertIn("There is no folder called", self.app.sync_state.cget("text"))

    def test_upload_progress_reaches_the_tab(self) -> None:
        self.app._show_sync_progress(
            50, 100, "10-8-26/A.MOV  ·  1.0 GB of 2.0 GB  ·  40.0 MB/s  ·  about 25 sec left")
        self.assertIn("50%", self.app.sync_eta.cget("text"))
        self.assertEqual(self.app.sync_file.cget("text"), "Uploading 10-8-26/A.MOV")


if __name__ == "__main__":
    unittest.main()
