"""Stage two across every day at once: the SSD onto both backup HDDs, and
the two HDDs into mirrors of each other.

The Back up tab used to ask which shoot day to copy. Kevin wants to plug
both HDDs in and be told what is not backed up yet, then press one button.

Why this is more than "copy the SSD across": the SSD is 4TB and each HDD
is 8TB. Older days are cleared off the SSD to make room and live only on
the HDDs, so the SSD is the source for what it holds but never the list
of what should exist. Every file in a dated folder on any of the three
drives counts. Each file is copied to whichever HDD lacks it, from the
SSD when the SSD has it and otherwise from the HDD that does.

What it never does:

- Write to the SSD. It is only ever read.
- Delete or replace anything. A file is copied to a drive only when that
  drive has nothing at that path; if two drives hold different sizes
  under one path, neither is touched and the file is reported.
- Touch anything outside a top-level dated folder. A real drive was
  once found with its archive nested inside a project folder; this
  reports such folders as not handled rather than guessing at them.

The scan compares paths and sizes, not checksums: reading 4TB on every
look would take hours. Everything it copies is hashed at the source and
re-read at the destination by copy_verified, as every copy is. The cost:
a file damaged on one HDD *after* it was copied, without its size
changing, still reads as "on both". Catching that needs a full read of
both HDDs — a deliberate deep check, not something to do on every look.
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .ingest import DATED_FOLDER
from .progress import ByteProgress, human_bytes
from .storage import copy_verified, md5_file, refuse_if_occupied, verified_copy_exists

PART_SUFFIX = ".footage-guardian-part"
# Room left on a drive after the copy, so a full disk never truncates the
# last file and macOS keeps space for its own bookkeeping.
FREE_SPACE_MARGIN = 2 * 1000 ** 3


def day_sort_key(day: str) -> tuple[int, int, int]:
    """M-D-YY or M:D:YY as (year, month, day), so 12-1-25 sorts before 1-5-26."""
    month, dom, year = (int(part) for part in day.replace(":", "-").split("-"))
    return year, month, dom


@dataclass
class CopyJob:
    relative: str            # "9-15-26/Main Cam/Disk 1/DCIM/…/P1000001.MOV"
    source: Path             # absolute path on the drive it is read from
    size: int
    destinations: list[Path]  # backup roots that lack it


@dataclass
class DayStatus:
    day: str
    on_ssd: bool
    files: int                       # distinct files across all drives
    size: int
    held: dict[str, int]             # drive name -> files it holds at the right size
    missing_files: int               # file copies still to make, across HDDs
    missing_bytes: int
    conflicts: list[str]

    @property
    def complete(self) -> bool:
        return self.missing_files == 0 and not self.conflicts


@dataclass
class BackupPlan:
    ssd: Path | None
    hdds: list[Path]
    names: dict[Path, str]
    days: list[DayStatus] = field(default_factory=list)
    jobs: list[CopyJob] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    needed: dict[Path, int] = field(default_factory=dict)
    free: dict[Path, int] = field(default_factory=dict)
    missing_drives: list[str] = field(default_factory=list)
    unhandled: dict[str, list[str]] = field(default_factory=dict)
    problem: str = ""

    @property
    def copies(self) -> int:
        return sum(len(job.destinations) for job in self.jobs)

    @property
    def bytes_to_copy(self) -> int:
        return sum(job.size * len(job.destinations) for job in self.jobs)

    def blocker(self) -> str:
        """Why the button cannot run, in words for Kevin, or "" if it can."""
        if self.problem:
            return self.problem
        if not self.hdds:
            return "Set Back up HDD 1 and Back up HDD 2 on the Drives tab first."
        if self.missing_drives:
            return (f"Plug in {' and '.join(self.missing_drives)}. Nothing is copied "
                    f"until every backup HDD is connected, so they stay mirrors.")
        for root, need in self.needed.items():
            have = self.free.get(root, 0)
            if need + FREE_SPACE_MARGIN > have:
                return (f"{self.names[root]} does not have room: it needs "
                        f"{human_bytes(need)} and has {human_bytes(have)} free. "
                        f"Nothing was copied. Free up space on it, then check again.")
        return ""


def _same_drive(first: Path, second: Path) -> bool:
    """One folder, one inside the other, or two mount points of one disk."""
    a, b = first.resolve(), second.resolve()
    if a == b or a in b.parents or b in a.parents:
        return True
    try:
        # Only meaningful for real volumes: two folders on one disk are
        # normal, two *mount points* sharing a device are not.
        return os.path.ismount(a) and os.path.ismount(b) and a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def _index(root: Path) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Every file under each top-level dated folder, as day -> {relative: size}.

    Also returns the top-level folders that are not dated, so the tab can
    say plainly that they are left alone.
    """
    days: dict[str, dict[str, int]] = {}
    other: list[str] = []
    for item in sorted(root.iterdir(), key=lambda p: p.name):
        if item.name.startswith(".") or not item.is_dir():
            continue
        if not DATED_FOLDER.match(item.name):
            other.append(item.name)
            continue
        files: dict[str, int] = {}
        for folder, dirnames, filenames in os.walk(item):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith(".") or name.endswith(PART_SUFFIX):
                    continue
                path = Path(folder) / name
                try:
                    files[path.relative_to(root).as_posix()] = path.stat().st_size
                except OSError:
                    continue
        days[item.name] = files
    return days, other


def scan_backups(ssd: Path | None, hdds: list[Path],
                 names: dict[Path, str] | None = None) -> BackupPlan:
    """Work out what is on which drive and what needs copying where."""
    names = dict(names or {})
    for number, root in enumerate(hdds, start=1):
        names.setdefault(root, f"Back up HDD {number}")
    if ssd is not None:
        names.setdefault(ssd, "SSD main drive")
    plan = BackupPlan(ssd=ssd, hdds=list(hdds), names=names)

    present = [root for root in hdds if root.is_dir()]
    plan.missing_drives = [names[root] for root in hdds if not root.is_dir()]
    readable = ([ssd] if ssd is not None and ssd.is_dir() else []) + present

    # Two settings naming one disk would make a "mirror" of a drive onto
    # itself, and the HDDs would quietly be one copy, not two.
    for i, first in enumerate(readable):
        for second in readable[i + 1:]:
            if _same_drive(first, second):
                plan.problem = (f"{names[first]} and {names[second]} are set to the same drive. "
                                "Set each one to a different disk on the Drives tab.")
                return plan

    indexes: dict[Path, dict[str, dict[str, int]]] = {}
    for root in readable:
        try:
            indexes[root], other = _index(root)
        except OSError as exc:
            plan.problem = f"{names[root]} could not be read: {exc}"
            return plan
        if other:
            plan.unhandled[names[root]] = other

    all_days = sorted({day for index in indexes.values() for day in index},
                      key=day_sort_key, reverse=True)
    ssd_index = indexes.get(ssd, {}) if ssd is not None else {}

    for root in present:
        plan.needed[root] = 0
        try:
            plan.free[root] = shutil.disk_usage(root).free
        except OSError:
            plan.free[root] = 0

    ssd_jobs: list[CopyJob] = []
    hdd_jobs: list[CopyJob] = []
    for day in all_days:
        holders: dict[str, dict[Path, int]] = {}
        for root, index in indexes.items():
            for relative, size in index.get(day, {}).items():
                holders.setdefault(relative, {})[root] = size
        status = DayStatus(day=day, on_ssd=day in ssd_index, files=len(holders), size=0,
                           held={names[root]: 0 for root in readable},
                           missing_files=0, missing_bytes=0, conflicts=[])
        for relative in sorted(holders):
            sizes = holders[relative]
            if len(set(sizes.values())) > 1:
                detail = ", ".join(f"{names[root]} {human_bytes(size)}"
                                   for root, size in sizes.items())
                status.conflicts.append(f"{relative} — sizes differ ({detail})")
                continue
            size = next(iter(sizes.values()))
            status.size += size
            for root in sizes:
                status.held[names[root]] += 1
            lacking = [root for root in present if root not in sizes]
            if not lacking:
                continue
            # The SSD is read in preference: it is the fastest drive, and
            # reading it spares the HDD that already holds the file.
            source_root = ssd if ssd in sizes else next(r for r in hdds if r in sizes)
            job = CopyJob(relative, source_root / relative, size, lacking)
            (ssd_jobs if source_root == ssd else hdd_jobs).append(job)
            status.missing_files += len(lacking)
            status.missing_bytes += size * len(lacking)
            for root in lacking:
                plan.needed[root] += size
        plan.conflicts.extend(status.conflicts)
        plan.days.append(status)

    # Footage that so far exists only on the SSD is the most exposed, so it
    # goes first, newest day first. Evening up the old archive comes after.
    plan.jobs = ssd_jobs + hdd_jobs
    return plan


def run_backup(plan: BackupPlan, progress: Callable[[int, int, str], None] | None = None,
               stop: threading.Event | None = None) -> dict:
    """Make every copy the plan calls for, verified, then report what happened.

    Refuses outright, copying nothing, if the plan has a blocker. Checks
    `stop` between files, so stopping never leaves a half-written file.
    """
    blocker = plan.blocker()
    if blocker:
        raise RuntimeError(blocker)
    for job in plan.jobs:
        for root in job.destinations:
            # Belt and braces: the plan only ever names HDDs as destinations.
            if root not in plan.hdds or root == plan.ssd:
                raise RuntimeError(f"Refusing to write to {root}: it is not a backup HDD.")

    total = plan.bytes_to_copy
    bar = ByteProgress(total, passes=3, report=progress)
    copied = already = 0
    copied_bytes = 0
    failures: list[str] = []
    stopped = False

    for job in plan.jobs:
        if stop is not None and stop.is_set():
            stopped = True
            break
        count = len(job.destinations)
        bar.label(job.relative)
        try:
            # One read of the source serves every destination, so it
            # counts once per destination towards the three passes.
            digest = md5_file(job.source, lambda n, phase: bar.add(n * count, phase))
        except OSError as exc:
            failures.append(f"{job.relative}: could not be read ({exc})")
            continue
        for root in job.destinations:
            destination = root / job.relative
            bar.label(f"{plan.names[root]}: {job.relative}")
            try:
                if verified_copy_exists(destination, job.size, digest):
                    # Arrived since the scan — another run, or by hand.
                    already += 1
                    bar.add(job.size * 2, "skipped")
                    continue
                refuse_if_occupied(destination)
                copy_verified(job.source, destination, digest, bar.add)
                copied += 1
                copied_bytes += job.size
            except (OSError, RuntimeError) as exc:
                failures.append(f"{job.relative} → {plan.names[root]}: {exc}")
    if not stopped:
        bar.finished("Done")
    return {"copied": copied, "already_there": already, "bytes": copied_bytes,
            "failures": failures, "conflicts": list(plan.conflicts), "stopped": stopped}


# ------------------------------------------------------------ for the window
# Plain functions so what Kevin reads can be tested without a screen.

def plan_columns(plan: BackupPlan) -> list[str]:
    drives = ([plan.ssd] if plan.ssd is not None else []) + plan.hdds
    return ["Shoot day"] + [plan.names[root] for root in drives] + ["Status"]


def plan_rows(plan: BackupPlan) -> list[tuple[str, ...]]:
    drives = ([plan.ssd] if plan.ssd is not None else []) + plan.hdds
    rows = []
    for status in plan.days:
        cells = [status.day]
        for root in drives:
            name = plan.names[root]
            if name not in status.held:
                cells.append("not plugged in")
                continue
            held = status.held[name]
            if held == 0:
                cells.append("—")
            elif held >= status.files - len(status.conflicts):
                cells.append(f"{held:,} file" + ("" if held == 1 else "s"))
            else:
                cells.append(f"{held:,} of {status.files - len(status.conflicts):,}")
        if status.conflicts:
            state = f"⚠ {len(status.conflicts)} file(s) differ between drives — not copied"
        elif status.complete:
            state = "✓ On both HDDs" if status.on_ssd else "✓ On both HDDs (cleared from SSD)"
        else:
            copies = "copy" if status.missing_files == 1 else "copies"
            state = f"Needs {status.missing_files:,} {copies}, {human_bytes(status.missing_bytes)}"
            if not status.on_ssd:
                state += " — older footage, HDD to HDD"
        cells.append(state)
        rows.append(tuple(cells))
    return rows


def plan_summary(plan: BackupPlan) -> str:
    blocker = plan.blocker()
    lines: list[str] = []
    waiting = [day for day in plan.days if day.missing_files]
    if blocker:
        lines.append(blocker)
    elif not plan.days:
        lines.append("No dated shoot folders on any drive yet.")
    elif not waiting:
        lines.append(f"Both HDDs hold everything — {len(plan.days):,} shoot days checked.")
    else:
        lines.append(f"{len(waiting):,} shoot day(s) need backing up: {plan.copies:,} file copies, "
                     f"{human_bytes(plan.bytes_to_copy)}. Footage only on the SSD goes first.")
    if plan.free and not plan.problem:
        lines.append("Free space: " + ", ".join(
            f"{plan.names[root]} {human_bytes(free)}" for root, free in plan.free.items()))
    if plan.conflicts:
        lines.append(f"{len(plan.conflicts)} file(s) have different sizes on different drives. "
                     "They are left exactly as they are — tell Stuart before anything is done about them.")
    for name, folders in plan.unhandled.items():
        shown = ", ".join(folders[:4]) + (f" and {len(folders) - 4} more" if len(folders) > 4 else "")
        lines.append(f"Left alone on {name} (not a dated shoot folder): {shown}")
    return "\n".join(lines)
