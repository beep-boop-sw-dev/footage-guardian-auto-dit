"""Stage three across every folder at once: everything on Kevin's drives
into one master folder in Google Drive.

The Sync tab used to upload one shoot day from the SSD. Drive is meant to
end up as the master copy of everything, and most of the archive is no
longer on the 4TB SSD — it is on the 8TB HDDs. So the source is all three
drives together: each file is read from the SSD if it is there (fastest),
otherwise from whichever HDD holds it.

What it never does:

- Write to any local drive. They are only read.
- Delete or replace anything in Drive. A file is uploaded only to a path
  Drive has nothing at, and rclone is told to leave existing files alone
  even if the listing was out of date. A path whose size differs between
  Drive and the drives, or that Drive holds twice, is reported and left.
- Upload into a folder that does not exist. A mistyped folder name would
  otherwise be created and filled with terabytes.

Drive has a quota, and on the first real look (2026-10-08) it was smaller
than the archive. So uploads go newest folder first and the plan stops
before the quota is reached: what fits is uploaded, the rest is listed as
not fitting, rather than failing partway through a file.

Like the backup, the scan compares paths and sizes. Every upload is then
checked against the MD5 Google reports for it, which is computed by Google
from what it received.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from .backup import MEANINGFUL_SAMPLE, _index, _same_drive, folder_order
from .manifest import Manifest
from .progress import ByteProgress, human_bytes, human_duration
from .storage import md5_file

# Left unused in the Drive account, so a nearly full account never fails
# halfway through the last file.
DRIVE_MARGIN = 5 * 1000 ** 3
# Above this, a reminder that the network is the whole story: a real upload
# ran at 5.6 MB/s on wi-fi and 88 MB/s on Ethernet.
ETHERNET_HINT_BYTES = 20 * 1000 ** 3


@dataclass
class UploadJob:
    relative: str
    source: Path
    source_name: str
    size: int


@dataclass
class FolderStatus:
    folder: str
    files: int                 # distinct files on the local drives
    in_drive: int              # of those, in Drive at the same size
    remote_only: int           # in Drive, on none of the plugged-in drives
    missing_files: int
    missing_bytes: int
    wont_fit: int
    conflicts: list[str]


@dataclass
class CloudPlan:
    base: str
    names: dict[Path, str]
    folders: list[FolderStatus] = field(default_factory=list)
    jobs: list[UploadJob] = field(default_factory=list)       # what fits, in order
    overflow: list[UploadJob] = field(default_factory=list)   # what does not fit
    conflicts: list[str] = field(default_factory=list)
    free: int | None = None
    drives_read: list[str] = field(default_factory=list)
    drives_missing: list[str] = field(default_factory=list)
    problem: str = ""

    @property
    def bytes_to_upload(self) -> int:
        return sum(job.size for job in self.jobs)

    @property
    def overflow_bytes(self) -> int:
        return sum(job.size for job in self.overflow)

    def blocker(self) -> str:
        if self.problem:
            return self.problem
        if not self.drives_read:
            return "Plug in the SSD main drive or a backup HDD — there is nothing to upload from."
        if self.overflow and not self.jobs:
            return (f"Google Drive is full: {human_bytes(self.free or 0)} free, and the next file "
                    "needs more. Nothing was uploaded. Free up space or add storage to the "
                    "Google account, then check again.")
        return ""


def scan_cloud(cloud, base: str, ssd: Path | None, hdds: list[Path]) -> CloudPlan:
    base = base.strip().rstrip("/")
    names: dict[Path, str] = {}
    if ssd is not None:
        names[ssd] = "SSD main drive"
    for number, root in enumerate(hdds, start=1):
        names.setdefault(root, f"Back up HDD {number}")
    plan = CloudPlan(base=base, names=names)

    if not base or ":" not in base:
        plan.problem = ("No Google Drive folder is set. Type it on the Drives tab as "
                        "gdrive:Folder name, then press Confirm these drives.")
        return plan
    if not cloud.available():
        plan.problem = "rclone is not installed, so Google Drive cannot be reached. Tell Stuart."
        return plan

    drives = [root for root in ([ssd] if ssd is not None else []) + hdds]
    readable = [root for root in drives if root.is_dir()]
    plan.drives_missing = [names[root] for root in drives if not root.is_dir()]
    for i, first in enumerate(readable):
        for second in readable[i + 1:]:
            if _same_drive(first, second):
                plan.problem = (f"{names[first]} and {names[second]} are set to the same drive. "
                                "Set each one to a different disk on the Drives tab.")
                return plan
    plan.drives_read = [names[root] for root in readable]

    problem = cloud.reachable(base)
    if problem:
        plan.problem = problem
        return plan
    try:
        remote = cloud.list_tree(base)
    except RuntimeError as exc:
        plan.problem = str(exc)
        return plan
    if remote is None:
        plan.problem = (f"There is no folder called {base.split(':', 1)[1]!r} in Google Drive. "
                        "Check the Google Drive folder on the Drives tab — it has to match the "
                        "folder's name exactly. Nothing was uploaded.")
        return plan
    plan.free = cloud.free_space(base)

    # What is on the plugged-in drives, file by file, and where to read it.
    local: dict[str, dict[str, dict[Path, int]]] = {}
    for root in readable:
        try:
            index, _loose = _index(root)
        except OSError as exc:
            plan.problem = f"{names[root]} could not be read: {exc}"
            return plan
        for folder, files in index.items():
            for relative, size in files.items():
                local.setdefault(folder, {}).setdefault(relative, {})[root] = size

    remote_folders: dict[str, dict[str, list[tuple[int, str]]]] = {}
    for relative, entries in remote.items():
        remote_folders.setdefault(PurePosixPath(relative).parts[0], {})[relative] = entries

    budget = None if plan.free is None else max(0, plan.free - DRIVE_MARGIN)
    for folder in folder_order(set(local) | set(remote_folders)):
        here = local.get(folder, {})
        there = remote_folders.get(folder, {})
        status = FolderStatus(folder=folder, files=len(here), in_drive=0,
                              remote_only=sum(1 for r in there if r not in here),
                              missing_files=0, missing_bytes=0, wont_fit=0, conflicts=[])
        for relative in sorted(here):
            holders = here[relative]
            if len(set(holders.values())) > 1:
                status.conflicts.append(f"{relative} — sizes differ between your drives")
                continue
            size = next(iter(holders.values()))
            entries = there.get(relative, [])
            if len(entries) > 1:
                status.conflicts.append(f"{relative} — Google Drive holds it {len(entries)} times")
                continue
            if entries:
                if entries[0][0] == size:
                    status.in_drive += 1
                else:
                    status.conflicts.append(
                        f"{relative} — a different size is already in Google Drive "
                        f"({human_bytes(entries[0][0])} there, {human_bytes(size)} here)")
                continue
            source_root = ssd if ssd in holders else next(r for r in hdds if r in holders)
            job = UploadJob(relative, source_root / relative, names[source_root], size)
            status.missing_files += 1
            status.missing_bytes += size
            # Newest first until the account is full; once one file does
            # not fit, everything after waits too, so what is uploaded is
            # predictable — whole recent days, not a scatter of small files.
            if budget is not None and (plan.overflow or size > budget):
                plan.overflow.append(job)
                status.wont_fit += 1
                continue
            if budget is not None:
                budget -= size
            plan.jobs.append(job)
        plan.conflicts.extend(status.conflicts)
        plan.folders.append(status)
    return plan


def run_sync(plan: CloudPlan, cloud, manifest: Manifest | None = None,
             progress: Callable[[int, int, str], None] | None = None,
             stop: threading.Event | None = None) -> dict:
    """Upload and verify every job that fits. Stops between files on request."""
    blocker = plan.blocker()
    if blocker:
        raise RuntimeError(blocker)
    started = time.monotonic()
    # Two passes over each file: hashing it here, then sending it.
    bar = ByteProgress(plan.bytes_to_upload, passes=2, report=progress)
    uploaded = 0
    sent_bytes = 0
    failures: list[str] = []
    stopped = False

    for job in plan.jobs:
        if stop is not None and stop.is_set():
            stopped = True
            break
        remote = f"{plan.base}/{job.relative}"
        bar.label(job.relative)
        file_id = None
        try:
            stat = job.source.stat()
            digest = md5_file(job.source, bar.add)
            if manifest is not None:
                file_id = manifest.discover(job.source_name, str(job.source), job.relative,
                                            stat.st_size, stat.st_mtime_ns)
                claimant = manifest.remote_claimed_by(file_id, remote, digest)
                if claimant:
                    raise RuntimeError("different footage is already uploaded here "
                                       f"(from {claimant['source_path']})")
                manifest.update(file_id, "UPLOADING", "Uploading to Google Drive", md5=digest)
            counted = 0

            def on_bytes(delta: int, phase: str) -> None:
                nonlocal counted
                # rclone counts bytes as they leave the Mac and can over-report
                # on retries; never let one file claim more than its size.
                delta = min(delta, job.size - counted)
                if delta > 0:
                    counted += delta
                    bar.add(delta, phase)

            cloud.upload(job.source, remote, on_bytes)
            if counted < job.size:
                bar.add(job.size - counted, "upload")
            method = cloud.verify(remote, job.size, digest, require_checksum=True)
            if manifest is not None and file_id is not None:
                manifest.update(file_id, "SAFE", f"Google Drive copy verified by {method}",
                                remote_path=remote)
            uploaded += 1
            sent_bytes += job.size
        except (OSError, RuntimeError) as exc:
            failures.append(f"{job.relative}: {exc}")
            if manifest is not None and file_id is not None:
                manifest.update(file_id, "ERROR", str(exc))
    if not stopped:
        bar.finished("Done")
    seconds = time.monotonic() - started
    return {"uploaded": uploaded, "bytes": sent_bytes,
            "failures": failures, "conflicts": list(plan.conflicts), "stopped": stopped,
            "seconds": seconds,
            "rate": sent_bytes / seconds if sent_bytes >= MEANINGFUL_SAMPLE and seconds > 0 else 0.0,
            "not_fitting": len(plan.overflow)}


# ------------------------------------------------------------ for the window

def cloud_columns() -> list[str]:
    return ["Folder", "On your drives", "In Google Drive", "Status"]


def cloud_rows(plan: CloudPlan) -> list[tuple[str, ...]]:
    rows = []
    for status in plan.folders:
        if status.files == 0 and status.remote_only == 0:
            state = "Empty folder — nothing to upload"
        elif status.conflicts:
            state = f"⚠ {len(status.conflicts)} file(s) differ — not uploaded"
        elif status.files == 0:
            state = "✓ Only in Google Drive (not on these drives)"
        elif status.missing_files == 0:
            state = "✓ In Google Drive"
        elif status.wont_fit == status.missing_files:
            state = f"Won't fit — {status.missing_files:,} files, {human_bytes(status.missing_bytes)}"
        else:
            state = f"Needs {status.missing_files:,} files, {human_bytes(status.missing_bytes)}"
            if status.wont_fit:
                state += f" ({status.wont_fit:,} won't fit)"
        local = f"{status.files:,} file" + ("" if status.files == 1 else "s") if status.files else "—"
        in_drive = status.in_drive + status.remote_only
        remote = f"{in_drive:,} file" + ("" if in_drive == 1 else "s") if in_drive else "—"
        rows.append((status.folder, local, remote, state))
    return rows


def cloud_summary(plan: CloudPlan, measured_rate: float = 0.0) -> str:
    lines = [f"Uploading to: {plan.base}" if plan.base else ""]
    blocker = plan.blocker()
    waiting = [f for f in plan.folders if f.missing_files]
    if blocker:
        lines.append(blocker)
    elif not plan.jobs and not plan.overflow:
        lines.append(f"Google Drive holds everything on these drives — {len(plan.folders):,} folders checked.")
    else:
        lines.append(f"{len(waiting):,} folder(s) need uploading: {len(plan.jobs):,} files, "
                     f"{human_bytes(plan.bytes_to_upload)}. Newest folders go first.")
        if measured_rate > 0 and plan.jobs:
            lines.append(f"Estimated time: about {human_duration(plan.bytes_to_upload / measured_rate)} "
                         f"(at {human_bytes(measured_rate)}/s, the speed of the last upload — "
                         "it depends almost entirely on the internet connection).")
        elif plan.jobs:
            lines.append("Time left appears once the upload starts — it depends almost entirely "
                         "on the internet connection.")
        if plan.bytes_to_upload >= ETHERNET_HINT_BYTES:
            lines.append("Plug the Mac into Ethernet if you can: on a real upload, wi-fi was 16 times slower.")
    if plan.free is not None and not plan.problem:
        lines.append(f"Google Drive free space: {human_bytes(plan.free)}.")
    if plan.overflow:
        lines.append(f"{len(plan.overflow):,} files ({human_bytes(plan.overflow_bytes)}) will not fit in "
                     "Google Drive. They stay safe on the HDDs. Talk to Stuart about more Google storage.")
    if plan.drives_missing and plan.drives_read:
        lines.append(f"Not plugged in: {', '.join(plan.drives_missing)} — anything only on "
                     f"{'it' if len(plan.drives_missing) == 1 else 'them'} is not counted.")
    if plan.conflicts:
        lines.append(f"{len(plan.conflicts)} file(s) differ between Google Drive and your drives, "
                     "or are in Drive twice. They are left exactly as they are — tell Stuart.")
    return "\n".join(line for line in lines if line)
