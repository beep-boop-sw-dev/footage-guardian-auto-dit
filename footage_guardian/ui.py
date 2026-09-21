from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import Config, Source
from .drives import propose_drives
from .engine import Guardian
from .manifest import Manifest
from .ingest import (
    CAMERA_NAMES,
    CardInfo,
    CardIngester,
    describe_mounted_devices,
    inspect_card,
)
from .meta_import import MetaCandidate, MetaImporter, find_recent_meta


COLORS = {"SAFE": "#16803a", "CLEAR TO REMOVE": "#6b4ea0", "CLOUD SAFE": "#1769aa", "UPLOADING": "#1769aa", "MISSING BACKUP": "#b25c00", "ERROR": "#b42318", "WAITING": "#666666"}


STAGE_DONE, STAGE_PART, STAGE_NONE = "✓", "…", "—"


def status_snapshot(root: Path | None, day: str, roots: list[Path],
                    manifest: Manifest) -> dict:
    """Everything the three stage lines need, counted without touching a widget.

    Kept apart from the window on purpose: this is the slow half, it runs in a
    worker thread, and being a plain function it can be tested without a screen.
    """
    if root is None:
        return {"message": "Set the SSD main drive on the Drives tab to begin.",
                "backup_state": "No SSD main drive is set yet.",
                "sync_state": "No SSD main drive is set yet."}
    if not day:
        return {"message": f"No dated folders on {root.name} yet — copy some footage first.",
                "backup_state": "Nothing has been copied to the SSD yet.",
                "sync_state": "Nothing has been copied to the SSD yet."}

    files = [path for path in (root / day).rglob("*")
             if path.is_file() and not path.name.startswith(".")]
    size = sum(path.stat().st_size for path in files) if files else 0

    counts: list[int] = []
    missing: list[str] = []
    for drive in roots:
        if drive.is_dir():
            counts.append(sum(1 for path in (drive / day).rglob("*")
                              if path.is_file() and not path.name.startswith(".")))
        else:
            missing.append(drive.name)

    if not roots:
        backup_state = "Set both backup HDDs on the Drives tab first."
    elif missing:
        backup_state = (f"Plug in {', '.join(missing)} — nothing will be copied "
                        f"until both are connected.")
    elif not files:
        backup_state = f"{day} has no files on the SSD yet."
    else:
        backup_state = (f"Ready: {len(files):,} files from {day} will be copied "
                        f"onto {len(roots)} HDD(s).")

    return {"day": day, "files": len(files), "size": size, "roots": len(roots),
            "counts": counts, "missing": missing,
            "synced": manifest.synced_under(day), "backup_state": backup_state}


class App(tk.Tk):
    """One window, three stages, in the order a shoot actually happens.

    Everything lands on the SSD main drive first, then goes to both backup HDDs,
    then to Google Drive. Each stage is a button Kevin presses when he is ready.
    There is deliberately no background mode: a DIT wants to say when a card is
    read, not discover that it happened.
    """

    def __init__(self, config_path: Path, manifest_path: Path, log_path: Path,
                 probe_hardware: bool = True):
        super().__init__()
        self.title("Footage Guardian Auto DIT")
        self.geometry("1000x700")
        self.minsize(880, 620)
        self.config_path = config_path
        self.config_data = Config.load(config_path)
        self.manifest = Manifest(manifest_path)
        self.log = logging.getLogger("footage_guardian")
        self.log.setLevel(logging.INFO)
        handler = logging.FileHandler(log_path)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(handler)
        self.busy = False
        # Both scans run in workers; these stop a slow drive from stacking up a
        # queue of duplicate scans behind it, one per tick.
        self._device_scan = False
        self._status_scan = False
        self._device_rescan = False
        self._device_signature: tuple[str, ...] | None = None
        # Tk may only be touched from the thread running the main loop, so the
        # scans post their results here and the main thread collects them.
        self._results: queue.Queue[tuple[str, object]] = queue.Queue()
        self.day = tk.StringVar(value="")
        self._build()
        self.refresh_days()
        # Straight after the window is up, not during _build: detection
        # shells out to diskutil once per mounted volume and the window
        # should be on screen first. Blanks only, so a saved setting is
        # never overwritten without the operator asking.
        #
        # probe_hardware exists for the tests. Asking the machine what is
        # plugged in makes a test depend on what happens to be plugged
        # into it, which is both slow and not reproducible — the window
        # should be provable on a Mac with nothing attached.
        if probe_hardware:
            self.after(200, lambda: self.detect_drives(fill_blanks_only=True))
        self.after(1500, self._tick)
        self.after(150, self._pump)
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ---------------------------------------------------------------- building

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(18, 14, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Footage Guardian", font=("Helvetica", 22, "bold")).pack(side="left")
        ttk.Label(header, text="Shoot day:").pack(side="left", padx=(24, 6))
        self.day_picker = ttk.Combobox(header, textvariable=self.day, state="readonly", width=14)
        self.day_picker.pack(side="left")
        self.day_picker.bind("<<ComboboxSelected>>", lambda _event: self.refresh_status())
        ttk.Button(header, text="Refresh", command=self.refresh_days).pack(side="left", padx=8)

        board = ttk.Frame(self, padding=(18, 0, 18, 10))
        board.pack(fill="x")
        self.stage_labels: dict[str, ttk.Label] = {}
        for key, title in (("ssd", "1  On the SSD main drive"),
                           ("backup", "2  On both backup HDDs"),
                           ("drive", "3  In Google Drive")):
            row = ttk.Frame(board)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=title, width=28, font=("Helvetica", 12, "bold")).pack(side="left")
            label = ttk.Label(row, text="—", font=("Helvetica", 12))
            label.pack(side="left")
            self.stage_labels[key] = label

        self.banner = ttk.Label(self, text="", anchor="center", font=("Helvetica", 12, "bold"))
        self.banner.pack(fill="x", padx=18, pady=(0, 8))

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        self._build_copy_tab()
        self._build_backup_tab()
        self._build_sync_tab()
        self._build_settings_tab()

        bottom = ttk.Frame(self, padding=(18, 0, 18, 14))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x")
        self.progress_text = ttk.Label(bottom, text="", font=("Helvetica", 11))
        self.progress_text.pack(anchor="w", pady=(4, 0))

    def _build_copy_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(tab, text="  1 · Copy footage  ")
        ttk.Label(tab, wraplength=880, justify="left", text=(
            "Plug in every camera, then copy each one onto the SSD main drive. They all go into "
            "one folder named for the day, with a folder per camera inside it.\n\n"
            "Plug the cameras in themselves rather than putting their cards in a reader — the "
            "drone and the Osmo write identical cards, and only the camera can say which it is."
        )).pack(anchor="w")
        ttk.Button(tab, text="Copy a camera onto the SSD…", command=self.open_offload).pack(anchor="w", pady=(14, 4))
        ttk.Button(tab, text="Import Meta glasses from Downloads…", command=self.open_meta).pack(anchor="w")

        ttk.Separator(tab).pack(fill="x", pady=14)
        ttk.Label(tab, text="Cameras plugged in now", font=("Helvetica", 13, "bold")).pack(anchor="w")
        self.devices = ttk.Treeview(tab, columns=("volume", "camera", "note"), show="headings", height=7)
        for column, title, width in (("volume", "VOLUME", 200), ("camera", "DETECTED AS", 160), ("note", "", 460)):
            self.devices.heading(column, text=title)
            self.devices.column(column, width=width)
        self.devices.pack(fill="both", expand=True, pady=(6, 0))

    def _build_backup_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(tab, text="  2 · Back up to HDDs  ")
        ttk.Label(tab, wraplength=880, justify="left", text=(
            "Once every camera has been copied onto the SSD main drive, plug in both backup "
            "HDDs and copy the whole day onto each. Every file is checked by size and checksum "
            "as it lands. Running it again only copies what is missing."
        )).pack(anchor="w")
        self.backup_state = ttk.Label(tab, text="", wraplength=880, justify="left",
                                      font=("Helvetica", 12))
        self.backup_state.pack(anchor="w", pady=(12, 10))
        self.backup_button = ttk.Button(
            tab, text="All cameras are on the SSD — back this day up to both HDDs",
            command=self.start_backup)
        self.backup_button.pack(anchor="w")
        self.backup_result = ttk.Label(tab, text="", wraplength=880, justify="left")
        self.backup_result.pack(anchor="w", pady=(14, 0))

    def _build_sync_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(tab, text="  3 · Sync to Google Drive  ")
        ttk.Label(tab, wraplength=880, justify="left", text=(
            "With the day safely on both backup HDDs, upload it to Google Drive. Drive ends up "
            "mirroring the SSD folder for folder, and every upload is checked against Google's "
            "own checksum. Stopping and restarting is safe — it never re-uploads a file."
        )).pack(anchor="w")
        self.sync_state = ttk.Label(tab, text="", wraplength=880, justify="left", font=("Helvetica", 12))
        self.sync_state.pack(anchor="w", pady=(12, 10))
        self.sync_button = ttk.Button(tab, text="Upload this day to Google Drive", command=self.start_sync)
        self.sync_button.pack(anchor="w")
        self.sync_result = ttk.Label(tab, text="", wraplength=880, justify="left")
        self.sync_result.pack(anchor="w", pady=(14, 0))

        ttk.Separator(tab).pack(fill="x", pady=16)
        ttk.Label(tab, wraplength=880, justify="left", text=(
            "Once a day is verified in Google Drive you can recover space by removing its local "
            "backup copies. That is the only part of this app that deletes anything, and it never "
            "touches a camera or the SSD."
        )).pack(anchor="w")
        ttk.Button(tab, text="Recover space…", command=self.open_recovery).pack(anchor="w", pady=(10, 0))

    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(tab, text="  Drives  ")
        ttk.Label(tab, wraplength=860, justify="left", text=(
            "Plug in the SSD and both backup HDDs and these fill themselves in. Check "
            "them and press Confirm. If one is wrong, pick the right drive from its "
            "list — every drive you have plugged in is in there."
        )).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))

        roots = self.config_data.backup_roots()
        self.ssd_var, self.ssd_note, self.ssd_box = self._drive_row(
            tab, "SSD main drive",
            self.config_data.sources[0].path if self.config_data.sources else "", 1)
        self.hdd1_var, self.hdd1_note, self.hdd1_box = self._drive_row(
            tab, "Back up HDD 1", str(roots[0]) if roots else "", 2)
        self.hdd2_var, self.hdd2_note, self.hdd2_box = self._drive_row(
            tab, "Back up HDD 2", str(roots[1]) if len(roots) > 1 else "", 3)
        self.remote_var = self._path_row(tab, "Google Drive folder",
                                         self.config_data.google_destination, 4, browse=False)
        tab.columnconfigure(1, weight=1)

        self.drive_missing = ttk.Label(tab, text="", wraplength=860, justify="left",
                                       font=("Helvetica", 11))
        self.drive_missing.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=6, column=0, columnspan=3, sticky="w", pady=14)
        ttk.Button(actions, text="Confirm these drives", command=self.save).pack(side="left")
        self.detect_button = ttk.Button(actions, text="Look again", command=self.detect_drives)
        self.detect_button.pack(side="left", padx=8)
        self.drive_saved = ttk.Label(actions, text="")
        self.drive_saved.pack(side="left", padx=10)

    def _drive_row(self, parent, label: str, value: str, row: int):
        """A drive slot: what we think it is, with every other drive one click away."""
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w", pady=6)
        variable = tk.StringVar(value=value)
        box = ttk.Combobox(parent, textvariable=variable)
        box.grid(row=row, column=1, sticky="ew", pady=6)
        note = ttk.Label(parent, text="", width=34, foreground="#555")
        note.grid(row=row, column=2, sticky="w", padx=8)
        return variable, note, box

    def detect_drives(self, fill_blanks_only: bool = False) -> None:
        """Ask macOS what is plugged in, off the main thread.

        `diskutil info` is a subprocess per volume and can take a moment,
        which would freeze the window if it ran here.
        """
        self.detect_button.configure(state="disabled")

        configured_ssd = self.ssd_var.get().strip()
        configured_backups = (self.hdd1_var.get().strip(), self.hdd2_var.get().strip())

        def worker() -> None:
            try:
                plan = propose_drives(configured_ssd, configured_backups)
            except Exception:  # noqa: BLE001 - detection must never take the app down
                plan = None
            self._post(lambda: self._drives_proposed(plan, fill_blanks_only))

        threading.Thread(target=worker, daemon=True).start()

    def _drives_proposed(self, plan, fill_blanks_only: bool) -> None:
        if not self._alive():
            return
        self.detect_button.configure(state="normal")
        if plan is None:
            self.drive_missing.configure(text="Could not read the drives just now. Try Look again.")
            return

        choices = [str(path) for path in plan.volumes]
        slots = ((self.ssd_var, self.ssd_note, self.ssd_box, plan.ssd),
                 (self.hdd1_var, self.hdd1_note, self.hdd1_box,
                  plan.backups[0] if plan.backups else None),
                 (self.hdd2_var, self.hdd2_note, self.hdd2_box,
                  plan.backups[1] if len(plan.backups) > 1 else None))

        for variable, note, box, guess in slots:
            box.configure(values=choices)
            if guess is None or guess.path is None:
                note.configure(text="" if variable.get() else "not found — choose it here")
                continue
            # Never overwrite something already filled in unless the
            # operator asked us to look again. This decides where
            # footage gets written.
            if variable.get().strip() and fill_blanks_only:
                continue
            variable.set(guess.value)
            note.configure(text=guess.reason if guess.confident else f"best guess — {guess.reason}")

        self.drive_missing.configure(
            text=("Not plugged in: " + "; ".join(plan.missing)) if plan.missing else "")

    def _path_row(self, parent, label: str, value: str, row: int, browse: bool = True) -> tk.StringVar:
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w", pady=6)
        variable = tk.StringVar(value=value)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
        if browse:
            ttk.Button(parent, text="Choose…",
                       command=lambda v=variable: self.choose(v)).grid(row=row, column=2, padx=8)
        return variable

    # ----------------------------------------------------------------- state

    def choose(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(initialdir="/Volumes", mustexist=True, parent=self)
        if selected:
            variable.set(selected)

    def save(self) -> None:
        ssd = self.ssd_var.get().strip()
        self.config_data.sources = [Source("SSD main drive", ssd)] if ssd else []
        self.config_data.backup_paths = [p for p in (self.hdd1_var.get().strip(),
                                                     self.hdd2_var.get().strip()) if p]
        self.config_data.backup_path = ""
        self.config_data.google_destination = self.remote_var.get().strip()
        self.config_data.save(self.config_path)
        self.refresh_days()
        # The SSD is kept out of the plugged-in list, so naming a different one
        # changes what belongs there.
        self.refresh_devices(force=True)
        # Inline rather than a modal: "Confirm" followed by an OK box is
        # two clicks for one decision, and this is the tab he passes
        # through on the way to work.
        self.drive_saved.configure(text="Saved.")
        self.after(4000, lambda: self.drive_saved.configure(text=""))

    def ssd_root(self) -> Path | None:
        if not self.config_data.sources:
            return None
        root = Path(self.config_data.sources[0].path).expanduser()
        return root if root.is_dir() else None

    def guardian(self) -> Guardian:
        return Guardian(self.config_data, self.manifest, self.log)

    def refresh_days(self) -> None:
        root = self.ssd_root()
        days = self.guardian().days_on(root) if root else []
        self.day_picker.configure(values=days)
        if days and self.day.get() not in days:
            self.day.set(days[0])
        elif not days:
            self.day.set("")
        self.refresh_status()

    def refresh_status(self) -> None:
        """Recount the day, off the thread that draws the window.

        Counting walks the day's folder on the SSD and on both HDDs. On external
        drives that takes seconds, so the counting happens in a worker and only
        the finished numbers come back here to be displayed.
        """
        if self._status_scan:
            return
        self._status_scan = True
        root, day, roots = self.ssd_root(), self.day.get(), self.config_data.backup_roots()

        def worker() -> None:
            try:
                snapshot = status_snapshot(root, day, roots, self.manifest)
            except Exception as exc:  # a count must never take the window with it
                snapshot = {"message": f"The drives could not be read: {exc}",
                            "backup_state": "Check the drives on the Drives tab.",
                            "sync_state": "Check the drives on the Drives tab."}
            self._results.put(("status", snapshot))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, snapshot: dict) -> None:
        self._status_scan = False
        if "message" in snapshot:
            self.banner.configure(text=snapshot["message"])
            for label in self.stage_labels.values():
                label.configure(text=STAGE_NONE)
            self.backup_state.configure(text=snapshot["backup_state"])
            self.sync_state.configure(text=snapshot["sync_state"])
            return

        files, day = snapshot["files"], snapshot["day"]
        self.stage_labels["ssd"].configure(text=f"{STAGE_DONE if files else STAGE_NONE}   "
                                                f"{files:,} files, {_human(snapshot['size'])}")

        counts, missing = snapshot["counts"], snapshot["missing"]
        if not snapshot["roots"]:
            backup_text, backup_mark = "no backup HDDs set", STAGE_NONE
        elif missing:
            backup_text, backup_mark = f"not plugged in: {', '.join(missing)}", STAGE_NONE
        elif counts and files and all(count >= files for count in counts):
            backup_text, backup_mark = f"both HDDs hold all {files:,} files", STAGE_DONE
        else:
            backup_text = " and ".join(f"{count:,}" for count in counts) + f" of {files:,} files"
            backup_mark = STAGE_PART if any(counts) else STAGE_NONE
        self.stage_labels["backup"].configure(text=f"{backup_mark}   {backup_text}")
        self.backup_state.configure(text=snapshot["backup_state"])

        synced = snapshot["synced"]
        if not files:
            drive_mark, drive_text = STAGE_NONE, "nothing to upload"
        elif synced >= files:
            drive_mark, drive_text = STAGE_DONE, f"all {files:,} files verified in Drive"
        elif synced:
            drive_mark, drive_text = STAGE_PART, f"{synced:,} of {files:,} files"
        else:
            drive_mark, drive_text = STAGE_NONE, "not started"
        self.stage_labels["drive"].configure(text=f"{drive_mark}   {drive_text}")
        self.sync_state.configure(
            text=f"{day}: {files:,} files on the SSD, {synced:,} verified in Google Drive."
            if files else "Nothing to upload for this day.")
        if not self.busy:
            self.banner.configure(text="")

    def _drain_results(self) -> None:
        """Apply whatever the background scans have finished, on this thread.

        Tk may only be touched from the thread running the main loop. Calling
        self.after() from inside a worker happens to work most of the time,
        which is worse than not working at all, so results come back through a
        queue and are applied here.
        """
        while True:
            try:
                kind, payload = self._results.get_nowait()
            except queue.Empty:
                return
            if kind == "devices":
                self._devices_ready(payload)
            elif kind == "status":
                self._apply_status(payload)

    def _post(self, callback) -> None:
        """Hand work back to the Tk thread, tolerating a closed window.

        A background scan can outlive the window that started it — close
        the app a second after opening it and the thread is still
        running. Tk may only be touched from the main loop, and touching
        a destroyed one raises from inside the worker where nothing is
        watching.
        """
        try:
            if self.winfo_exists():
                self.after(0, callback)
        except tk.TclError:
            pass

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _pump(self) -> None:
        if not self._alive():
            return
        self._drain_results()
        self.after(150, self._pump)

    def _tick(self) -> None:
        if not self._alive():
            return
        if not self.busy:
            self.refresh_devices()
            self.refresh_status()
        self.after(4000, self._tick)

    def refresh_devices(self, force: bool = False) -> None:
        """Re-read the plugged-in drives, off the thread that draws the window.

        Describing a drive can walk every file on it and ask macOS about the
        hardware behind it, which takes seconds. Doing that here on a four-second
        timer is what used to freeze the window before it ever painted.

        Nothing about a drive changes while it stays plugged in, so the scan only
        repeats when the set of mounted volumes actually changes.
        """
        if self._device_scan:
            # Saving a new SSD while a scan is running still has to be honoured;
            # dropping it would leave the new SSD listed as something to offload.
            self._device_rescan = self._device_rescan or force
            return
        signature = tuple(path.name for path in CardIngester.mounted_cards())
        if signature == self._device_signature and not force:
            return
        self._device_signature = signature
        self._device_scan = True
        ssd = self.ssd_root()

        def worker() -> None:
            try:
                rows = describe_mounted_devices(ssd, self.manifest)
            except Exception as exc:  # a scan must never take the window with it
                rows = [("—", "—", f"The drives could not be read: {exc}")]
            self._results.put(("devices", rows))

        threading.Thread(target=worker, daemon=True).start()

    def _devices_ready(self, rows: list[tuple[str, str, str]]) -> None:
        self._device_scan = False
        for item in self.devices.get_children():
            self.devices.delete(item)
        for row in rows:
            self.devices.insert("", "end", values=row)
        if self._device_rescan:
            self._device_rescan = False
            self.refresh_devices(force=True)

    # ------------------------------------------------------------ the stages

    def start_backup(self) -> None:
        self._run_stage("backup", self.backup_button, self.backup_result,
                        lambda guardian, root, day: guardian.backup_day(root, day, self._progress),
                        lambda s: (f"Copied {s['copied']:,} files onto {s['drives']} HDD(s); "
                                   f"{s['already_there']:,} were already there."))

    def start_sync(self) -> None:
        self._run_stage("sync", self.sync_button, self.sync_result,
                        lambda guardian, root, day: guardian.sync_day(root, day, self._progress),
                        lambda s: (f"Uploaded {s['uploaded']:,} files; "
                                   f"{s['already_there']:,} were already in Drive."))

    def _run_stage(self, name: str, button: ttk.Button, result: ttk.Label, work, describe) -> None:
        root, day = self.ssd_root(), self.day.get()
        if root is None or not day:
            messagebox.showinfo("Nothing selected", "Choose a shoot day first.", parent=self)
            return
        self.busy = True
        button.configure(state="disabled")
        result.configure(text="")
        self.banner.configure(text=f"Working on {day}…")

        def worker() -> None:
            try:
                summary = work(self.guardian(), root, day)
                self.after(0, lambda: self._stage_done(button, result, summary, describe))
            except Exception as exc:  # noqa: BLE001 - shown to the user, never a trace
                self.after(0, lambda: self._stage_failed(button, result, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _stage_done(self, button: ttk.Button, result: ttk.Label, summary: dict, describe) -> None:
        self.busy = False
        button.configure(state="normal")
        self.progress.configure(value=0)
        self.progress_text.configure(text="")
        failures = summary.get("failures", [])
        if failures:
            self.banner.configure(text=f"{len(failures)} file(s) had a problem")
            shown = "\n".join(f"  • {item}" for item in failures[:6])
            more = f"\n  … and {len(failures) - 6} more" if len(failures) > 6 else ""
            result.configure(text=f"{describe(summary)}\n\nThese did not complete:\n{shown}{more}")
        else:
            self.banner.configure(text="Done")
            result.configure(text=describe(summary) + "\nEvery file was verified.")
        self.refresh_status()

    def _stage_failed(self, button: ttk.Button, result: ttk.Label, detail: str) -> None:
        self.busy = False
        button.configure(state="normal")
        self.progress.configure(value=0)
        self.progress_text.configure(text="")
        self.banner.configure(text="Stopped")
        result.configure(text=detail)

    def _progress(self, done: int, total: int, text: str) -> None:
        percent = (done / total * 100) if total else 0
        self.after(0, lambda: (self.progress.configure(value=percent),
                               self.progress_text.configure(text=f"{done:,} of {total:,} — {text}")))

    # ----------------------------------------------------------------- dialogs

    def open_recovery(self) -> None:
        if self._busy_warning():
            return
        SpaceRecovery(self, self.guardian(), self.manifest)

    def open_offload(self) -> None:
        if self._busy_warning():
            return
        CardOffload(self, self.config_data, self.manifest)

    def open_meta(self) -> None:
        if self._busy_warning():
            return
        MetaGlassesImport(self, self.config_data, self.manifest)

    def _busy_warning(self) -> bool:
        if self.busy:
            messagebox.showinfo("One job at a time",
                                "Wait for the current copy to finish first.", parent=self)
        return self.busy

    def close(self) -> None:
        if self.busy and not messagebox.askyesno(
                "Still working", "A copy is still running. Quit anyway?", parent=self):
            return
        self.destroy()


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000:
            return f"{n:,.1f} {unit}"
        n /= 1000
    return f"{n:,.1f} PB"


class SpaceRecovery(tk.Toplevel):
    def __init__(self, parent: App, guardian: Guardian, manifest: Manifest):
        super().__init__(parent)
        self.guardian = guardian
        self.manifest = manifest
        self.title("Recover Local Space")
        self.geometry("820x480")
        ttk.Label(self, text="Recover Local Space", font=("Helvetica", 20, "bold")).pack(anchor="w", padx=18, pady=(18, 4))
        ttk.Label(self, text="Select one backup. First verify it on Google Drive, then remove only that local backup copy.", wraplength=760).pack(anchor="w", padx=18, pady=(0, 12))
        self.table = ttk.Treeview(self, columns=("status", "file", "size"), show="headings", selectmode="browse")
        self.table.heading("status", text="STATUS")
        self.table.heading("file", text="FILE")
        self.table.heading("size", text="SIZE")
        self.table.column("status", width=150, stretch=False)
        self.table.column("file", width=500)
        self.table.column("size", width=110, stretch=False)
        self.table.pack(fill="both", expand=True, padx=18)
        buttons = ttk.Frame(self, padding=18)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Verify on Google Drive", command=self.verify).pack(side="left")
        ttk.Button(buttons, text="Remove Verified Local Backup…", command=self.remove).pack(side="left", padx=8)
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.refresh_rows()

    def refresh_rows(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        for row in self.manifest.rows(5000):
            if row["state"] in {"SAFE", "CLEAR TO REMOVE"} and row["backup_path"]:
                size = f"{row['size'] / (1024 ** 3):.2f} GB"
                self.table.insert("", "end", iid=str(row["id"]), values=(row["state"], row["relative_path"], size))

    def selected_id(self) -> int | None:
        selected = self.table.selection()
        if not selected:
            messagebox.showinfo("Choose a file", "Select one local backup first.", parent=self)
            return None
        return int(selected[0])

    def verify(self) -> None:
        file_id = self.selected_id()
        if file_id is None:
            return
        try:
            detail = self.guardian.verify_for_clearance(file_id)
            messagebox.showinfo("Verified", detail + "\n\nThis backup is now clear to remove.", parent=self)
        except Exception as exc:
            messagebox.showerror("Not cleared", str(exc), parent=self)
        self.refresh_rows()

    def remove(self) -> None:
        file_id = self.selected_id()
        if file_id is None:
            return
        row = self.manifest.get(file_id)
        if row["state"] != "CLEAR TO REMOVE":
            messagebox.showwarning("Verify first", "This backup must be freshly verified on Google Drive first.", parent=self)
            return
        question = (f"Permanently remove this local BACKUP copy?\n\n{row['relative_path']}\n\n"
                    "Google Drive will be checked again. The camera/source file will not be touched. This immediately frees disk space and cannot be undone here.")
        if not messagebox.askyesno("Remove local backup?", question, icon="warning", parent=self):
            return
        try:
            self.guardian.remove_local_backup(file_id)
            messagebox.showinfo("Space recovered", "The verified local backup was removed. The source footage was untouched.", parent=self)
        except Exception as exc:
            messagebox.showerror("Removal refused", str(exc), parent=self)
        self.refresh_rows()


class CardOffload(tk.Toplevel):
    def __init__(self, parent: App, config: Config, manifest: Manifest):
        super().__init__(parent)
        self.config_data = config
        self.ingester = CardIngester(manifest)
        self.card: CardInfo | None = None
        self.destination: Path | None = None
        self.title("Guided Camera Card Offload")
        self.geometry("760x520")
        self.resizable(True, False)
        ttk.Label(self, text="Guided Camera Card Offload", font=("Helvetica", 20, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
        ttk.Label(self, text="1. Plug in one camera card and the destination SSD.  2. Confirm the camera.  3. Transfer and verify.  4. Eject.", wraplength=710).pack(anchor="w", padx=20, pady=(0, 14))

        form = ttk.Frame(self, padding=(20, 0))
        form.pack(fill="x")
        ttk.Label(form, text="Inserted card").grid(row=0, column=0, sticky="w", pady=5)
        volume_paths = [str(path) for path in self.ingester.mounted_cards()]
        self.card_path = tk.StringVar(value=volume_paths[0] if len(volume_paths) == 1 else "")
        self.card_picker = ttk.Combobox(form, textvariable=self.card_path, values=volume_paths)
        self.card_picker.grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Button(form, text="Choose…", command=self.choose_card).grid(row=0, column=2)
        ttk.Button(form, text="Detect Card", command=self.detect).grid(row=1, column=1, sticky="w", padx=10, pady=5)
        ttk.Label(form, text="Camera name").grid(row=2, column=0, sticky="w", pady=5)
        self.camera = tk.StringVar()
        self.camera_picker = ttk.Combobox(form, textvariable=self.camera,
                                          values=CAMERA_NAMES, state="readonly")
        self.camera_picker.grid(row=2, column=1, sticky="ew", padx=10)
        self.camera_picker.bind("<<ComboboxSelected>>", lambda _: self._camera_changed())
        ttk.Label(form, text="Main Cam card").grid(row=3, column=0, sticky="w", pady=5)
        self.card_slot = tk.StringVar(value="card 1")
        self.slot_picker = ttk.Combobox(form, textvariable=self.card_slot, values=("card 1", "card 2"), state="readonly")
        self.slot_picker.grid(row=3, column=1, sticky="ew", padx=10)
        ttk.Label(form, text="Offload date").grid(row=4, column=0, sticky="w", pady=5)
        now = datetime.now()
        self.offload_date = tk.StringVar(value=f"{now.month}-{now.day}-{now.strftime('%y')}")
        ttk.Entry(form, textvariable=self.offload_date).grid(row=4, column=1, sticky="ew", padx=10)
        ttk.Label(form, text="Destination SSD").grid(row=5, column=0, sticky="w", pady=5)
        self.ssd = tk.StringVar(value=config.backup_path)
        ttk.Entry(form, textvariable=self.ssd).grid(row=5, column=1, sticky="ew", padx=10)
        ttk.Button(form, text="Choose…", command=self.choose_ssd).grid(row=5, column=2)
        form.columnconfigure(1, weight=1)

        self.card_summary = ttk.Label(self, text="Waiting for card detection", wraplength=710)
        self.card_summary.pack(anchor="w", padx=20, pady=16)
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=20)
        self.progress_text = ttk.Label(self, text="")
        self.progress_text.pack(anchor="w", padx=20, pady=(5, 14))
        buttons = ttk.Frame(self, padding=20)
        buttons.pack(fill="x")
        self.transfer_button = ttk.Button(buttons, text="Transfer and Verify", command=self.start_transfer, state="disabled")
        self.transfer_button.pack(side="left")
        self.eject_button = ttk.Button(buttons, text="Eject Card", command=self.eject, state="disabled")
        self.eject_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Done", command=self.destroy).pack(side="right")

    def choose_card(self) -> None:
        selected = filedialog.askdirectory(initialdir="/Volumes", mustexist=True, parent=self)
        if selected:
            self.card_path.set(selected)

    def choose_ssd(self) -> None:
        selected = filedialog.askdirectory(initialdir="/Volumes", mustexist=True, parent=self)
        if selected:
            self.ssd.set(selected)

    def detect(self) -> None:
        try:
            self.card = inspect_card(Path(self.card_path.get()), self.ingester.manifest)
            prior = self.ingester.prior_ingest(self.card.fingerprint)
            prior_text = f" Already verified at: {prior['destination_path']}" if prior and prior["state"] == "VERIFIED" else ""
            # Keep whatever was worked out. This used to blank anything
            # outside a three-item list, so a correctly detected Osmo
            # left him with an empty dropdown and no way to say what it
            # was.
            self.camera.set(self.card.suggested_camera
                            if self.card.suggested_camera in CAMERA_NAMES else "")
            self._camera_changed()
            self.card_summary.configure(text=(f"Detected {self.card.volume_name}: {len(self.card.files)} files, "
                f"{self.card.total_bytes / (1024 ** 3):.2f} GB. Card ID {self.card.card_label}. "
                f"Suggested camera: {self.card.suggested_camera}.{prior_text}"))
            self.transfer_button.configure(state="normal" if not prior or prior["state"] != "VERIFIED" else "disabled")
        except Exception as exc:
            self.card = None
            self.transfer_button.configure(state="disabled")
            messagebox.showerror("Card not detected", str(exc), parent=self)

    def start_transfer(self) -> None:
        if not self.card:
            return
        if not self.camera.get().strip():
            messagebox.showinfo("Confirm the camera", "Choose Main Cam, 360, or Drone.", parent=self)
            return
        if not self.ssd.get().strip():
            messagebox.showinfo("Choose the SSD", "Choose the destination SSD first.", parent=self)
            return
        self.transfer_button.configure(state="disabled")
        self.card_picker.configure(state="disabled")
        threading.Thread(target=self._transfer_worker, daemon=True).start()

    def _transfer_worker(self) -> None:
        assert self.card
        try:
            self.destination = self.ingester.offload(
                self.card, Path(self.ssd.get()), self.offload_date.get().strip(),
                self.camera.get().strip(), self.card_slot.get(), progress=self._progress,
            )
            self.after(0, self._transfer_complete)
        except Exception as exc:
            self.after(0, lambda: self._transfer_failed(str(exc)))

    def _progress(self, completed: int, total: int, text: str) -> None:
        percent = completed * 100 / total if total else 100
        self.after(0, lambda: (self.progress.configure(value=percent), self.progress_text.configure(text=text)))

    def _transfer_complete(self) -> None:
        self.progress.configure(value=100)
        self.progress_text.configure(text=f"VERIFIED — every file copied and checked at {self.destination}")
        self.eject_button.configure(state="normal")
        messagebox.showinfo("Transfer verified", "Every file was copied and checksum-verified. The card is now safe to eject from this app.", parent=self)

    def _transfer_failed(self, detail: str) -> None:
        self.progress_text.configure(text="Transfer stopped — source card was not changed")
        self.transfer_button.configure(state="normal")
        self.card_picker.configure(state="normal")
        messagebox.showerror("Transfer not complete", detail, parent=self)

    def eject(self) -> None:
        if not self.card or not messagebox.askyesno("Eject card?", "The transfer is verified. Eject this camera card now?", parent=self):
            return
        try:
            self.ingester.eject(self.card.root)
            self.eject_button.configure(state="disabled")
            self.progress_text.configure(text="EJECTED — insert the next card, choose it, and click Detect Card")
            messagebox.showinfo("Card ejected", "You may remove the card and insert the next one.", parent=self)
            self.card = None
            self.card_picker.configure(state="normal")
            self.card_picker.configure(values=[str(path) for path in self.ingester.mounted_cards()])
        except Exception as exc:
            messagebox.showerror("Could not eject", str(exc), parent=self)

    def _camera_changed(self) -> None:
        self.slot_picker.configure(state="readonly" if self.camera.get() == "Main Cam" else "disabled")


class MetaGlassesImport(tk.Toplevel):
    def __init__(self, parent: App, config: Config, manifest: Manifest):
        super().__init__(parent)
        self.importer = MetaImporter(manifest)
        self.title("Import Meta Glasses")
        self.geometry("760x540")
        ttk.Label(self, text="Import Meta Glasses", font=("Helvetica", 20, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
        ttk.Label(self, text="Review recently AirDropped videos and photos. Select only files from the Meta glasses; Downloads originals will remain untouched.", wraplength=710).pack(anchor="w", padx=20, pady=(0, 12))
        form = ttk.Frame(self, padding=(20, 0))
        form.pack(fill="x")
        self.downloads = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.ssd = tk.StringVar(value=config.backup_path)
        now = datetime.now()
        self.offload_date = tk.StringVar(value=f"{now.month}-{now.day}-{now.strftime('%y')}")
        for row, (label, variable, choose) in enumerate((("Downloads", self.downloads, self.choose_downloads), ("Destination SSD", self.ssd, self.choose_ssd))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=10)
            ttk.Button(form, text="Choose…", command=choose).grid(row=row, column=2)
        ttk.Label(form, text="Offload date").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.offload_date).grid(row=2, column=1, sticky="ew", padx=10)
        ttk.Button(form, text="Search last 7 days", command=self.search).grid(row=3, column=1, sticky="w", padx=10, pady=6)
        form.columnconfigure(1, weight=1)
        self.listbox = tk.Listbox(self, selectmode="extended", height=12)
        self.listbox.pack(fill="both", expand=True, padx=20, pady=10)
        self.candidates: list[MetaCandidate] = []
        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.pack(fill="x", padx=20)
        self.status = ttk.Label(self, text="Search, review, then select the Meta glasses files.")
        self.status.pack(anchor="w", padx=20, pady=5)
        buttons = ttk.Frame(self, padding=20)
        buttons.pack(fill="x")
        self.import_button = ttk.Button(buttons, text="Copy and Verify Selected", command=self.start_import, state="disabled")
        self.import_button.pack(side="left")
        ttk.Button(buttons, text="Done", command=self.destroy).pack(side="right")
        self.search()

    def choose_downloads(self) -> None:
        selected = filedialog.askdirectory(mustexist=True, parent=self)
        if selected: self.downloads.set(selected)

    def choose_ssd(self) -> None:
        selected = filedialog.askdirectory(initialdir="/Volumes", mustexist=True, parent=self)
        if selected: self.ssd.set(selected)

    def search(self) -> None:
        try:
            self.candidates = find_recent_meta(Path(self.downloads.get()), 7)
            self.listbox.delete(0, "end")
            for item in self.candidates:
                stamp = datetime.fromtimestamp(item.modified).strftime("%b %d %I:%M %p")
                self.listbox.insert("end", f"{stamp}   {item.size / (1024 ** 2):.1f} MB   {item.path.name}")
            self.import_button.configure(state="normal" if self.candidates else "disabled")
            self.status.configure(text=f"Found {len(self.candidates)} recent media files. Select only Meta glasses footage.")
        except Exception as exc:
            messagebox.showerror("Could not search Downloads", str(exc), parent=self)

    def start_import(self) -> None:
        selected = [self.candidates[index] for index in self.listbox.curselection()]
        if not selected:
            messagebox.showinfo("Select files", "Select the Meta glasses files to copy.", parent=self)
            return
        if not messagebox.askyesno("Import selected files?", f"Copy and verify {len(selected)} selected files? Downloads originals will not be removed.", parent=self):
            return
        self.import_button.configure(state="disabled")
        threading.Thread(target=self._worker, args=(selected,), daemon=True).start()

    def _worker(self, selected: list[MetaCandidate]) -> None:
        try:
            destination = self.importer.import_files(selected, Path(self.ssd.get()), self.offload_date.get(), self._progress)
            self.after(0, lambda: self._complete(str(destination)))
        except Exception as exc:
            self.after(0, lambda: self._failed(str(exc)))

    def _progress(self, completed: int, total: int, text: str) -> None:
        percent = completed * 100 / total if total else 100
        self.after(0, lambda: (self.progress.configure(value=percent), self.status.configure(text=text)))

    def _complete(self, destination: str) -> None:
        self.progress.configure(value=100)
        self.status.configure(text=f"VERIFIED — copied to {destination}")
        messagebox.showinfo("Meta glasses import verified", "All selected files were copied and checksum-verified. Downloads originals remain untouched.", parent=self)

    def _failed(self, detail: str) -> None:
        self.import_button.configure(state="normal")
        self.status.configure(text="Import stopped; Downloads originals were untouched.")
        messagebox.showerror("Import not complete", detail, parent=self)
