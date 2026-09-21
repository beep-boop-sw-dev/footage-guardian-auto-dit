from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .config import Config, Source
from .ingest import (
    DATED_FOLDER,
    CameraUnconfirmed,
    SourceUnclear,
    classify_source,
    date_from_footage,
    inspect_card,
)
from .manifest import Manifest
from .storage import (
    Rclone,
    copy_verified,
    md5_file,
    refuse_if_occupied,
    safe_component,
    verified_copy_exists,
)


StatusCallback = Callable[[str], None]


class Guardian:
    def __init__(self, config: Config, manifest: Manifest, log: logging.Logger,
                 cloud: Rclone | None = None, status: StatusCallback | None = None):
        self.config = config
        self.manifest = manifest
        self.log = log
        self.cloud = cloud or Rclone()
        self.status = status or (lambda _: None)
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def scan_once(self) -> None:
        """Offload, back up and sync are three deliberate steps Kevin triggers.

        There was once a loop that did all of it continuously in the background.
        It was removed on 2026-09-15: two ways of doing the same job is how people
        end up trusting the wrong one, and a DIT wants to say when a card is read,
        not discover it happened.
        """
        self.status("SCANNING")
        # One clear warning beats the same failure repeated per file. The scan
        # still runs: local backups are worth making even with Drive unreachable.
        if self.cloud.available():
            problem = self.cloud.reachable(self.config.google_destination)
            if problem:
                self._event("WARNING", problem)
        for source in self.config.sources:
            if self.stop_event.is_set():
                break
            self._scan_source(source)
        self.status("READY")

    def _scan_source(self, source: Source) -> None:
        root = Path(source.path).expanduser()
        if not root.is_dir():
            self._event("WARNING", f"Source drive unavailable: {source.name} ({root})")
            return
        try:
            paths = list(self._media_files(root))
        except OSError as exc:
            self._event("ERROR", f"Could not scan {source.name}: {exc}")
            return
        try:
            prefix = self._archive_prefix(root)
        except (CameraUnconfirmed, SourceUnclear) as exc:
            # Nothing is copied for this drive until the human settles it. The
            # footage stays where it is, untouched, which is the safe place for it.
            self._event("ERROR", str(exc))
            return
        except OSError as exc:
            self._event("ERROR", f"Could not identify {source.name}: {exc}")
            return
        for path in paths:
            if self.stop_event.is_set():
                return
            file_id: int | None = None
            attempts = 0
            try:
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
                file_id = self.manifest.discover(source.name, str(path), relative, stat.st_size, stat.st_mtime_ns)
                age = time.time() - stat.st_mtime
                if age < self.config.stable_seconds:
                    continue
                row = self.manifest.get(file_id)
                attempts = int(row["attempts"])
                if row["state"] in {"SAFE", "CLEAR TO REMOVE", "CLOUD SAFE"} and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                    continue
                self._protect(file_id, path, prefix, relative)
            except (OSError, RuntimeError) as exc:
                if file_id is not None:
                    self.manifest.update(file_id, "ERROR", str(exc), attempts=attempts + 1)
                self._event("ERROR", f"{path}: {exc}")

    def _archive_prefix(self, root: Path) -> Path:
        """Work out where this drive's files belong inside the archive.

        A drive that already carries the M-D-YY convention is mirrored verbatim. A
        raw camera card gets a dated wrapper instead, because a card's own tree
        carries no date or camera name and the next card reuses the same
        filenames. The answer is worked out once and remembered, so repeated scans
        never refile footage, and a card remounted at a different path keeps the
        slot it was already given.
        """
        stored = self.manifest.source_label(str(root))
        if stored is not None:
            return Path(stored["archive_prefix"])

        verdict = classify_source(root)
        if verdict.kind == "offloaded":
            self.manifest.save_source_label(str(root), "", "")
            return Path()
        if verdict.kind == "unclear":
            raise SourceUnclear(root, verdict.reason)

        try:
            card = inspect_card(root, self.manifest)
        except CameraUnconfirmed:
            raise
        except RuntimeError:
            return Path()  # Empty or unreadable; nothing to file yet.

        # Refuse rather than risk: two cameras writing the same tree means a guess
        # would silently merge them into one folder, and nothing downstream would
        # ever reveal it. Nothing is copied until Kevin says which camera this is.
        if card.alternatives:
            raise CameraUnconfirmed(card)

        seen = self.manifest.source_label(str(root), card.fingerprint)
        if seen is not None:
            self.manifest.save_source_label(str(root), card.fingerprint, seen["archive_prefix"],
                                            seen["camera_name"], seen["offload_date"], seen["card_slot"])
            return Path(seen["archive_prefix"])

        camera = card.suggested_camera
        if camera == "Unknown camera":
            camera = root.name
            self._event("WARNING", f"Could not identify the camera on {root.name}; filing it under '{camera}'")
        camera = safe_component(camera)
        offload_date = date_from_footage(card.files)

        # Nothing on a second card says it is the second card, so the card's own
        # fingerprint decides: a card already filed keeps its slot, a genuinely
        # new one takes the next free slot for that day.
        already_filed = self.manifest.other_cards_on(offload_date, camera, card.fingerprint)
        slot = f"card {already_filed + 1}" if camera == "Main Cam" or already_filed else ""
        prefix = Path(offload_date) / camera / slot if slot else Path(offload_date) / camera

        self.manifest.save_source_label(str(root), card.fingerprint, prefix.as_posix(),
                                        camera, offload_date, slot)
        self._event("INFO", f"Filing {root.name} as {prefix.as_posix()}")
        return prefix

    def _media_files(self, root: Path):
        extensions = {item.lower() for item in self.config.video_extensions}
        for directory, names, filenames in os.walk(root):
            names[:] = [name for name in names if not name.startswith(".") and name != ".Trashes"]
            for filename in filenames:
                if filename.startswith(".") or filename.endswith(".footage-guardian-part"):
                    continue
                path = Path(directory) / filename
                if not extensions or path.suffix.lower() in extensions:
                    yield path

    def _protect(self, file_id: int, source: Path, prefix: Path, relative: str) -> None:
        row = self.manifest.get(file_id)
        digest = row["md5"] or md5_file(source)
        duplicate = self.manifest.duplicate_of(file_id, source.stat().st_size, digest)
        duplicate_note = f" (duplicate content of {duplicate['relative_path']})" if duplicate else ""
        self.manifest.update(file_id, "UPLOADING", "Preparing local backup and cloud copy" + duplicate_note, md5=digest)

        # The backup drive and Google Drive mirror the source tree exactly, under
        # the dated wrapper a raw card was given. What Kevin sees on the SSD is
        # what he sees in Drive.
        archive_relative = prefix / relative
        size = source.stat().st_size

        backup_ok = False
        backup_detail = ""
        backup_root = Path(self.config.backup_path).expanduser() if self.config.backup_path else None
        if backup_root and backup_root.is_dir():
            destination = backup_root / archive_relative
            if not verified_copy_exists(destination, size, digest):
                refuse_if_occupied(destination)
                copy_verified(source, destination, digest)
            backup_ok = True
            self.manifest.update(file_id, "UPLOADING", "Local backup verified; uploading", backup_path=str(destination))
        else:
            backup_detail = "Backup drive is unplugged or not configured"

        remote = self.config.google_destination.rstrip("/") + "/" + PurePosixPath(archive_relative.as_posix()).as_posix()
        claimant = self.manifest.remote_claimed_by(file_id, remote, digest)
        if claimant:
            raise RuntimeError(
                f"Different footage is already uploaded to {remote} (from {claimant['source_path']}); "
                "resolve this before continuing"
            )
        if not self.cloud.available():
            raise RuntimeError("rclone is not installed; local backup remains intact")
        self.cloud.upload(source, remote)
        self.cloud.verify(remote, size, digest)
        if backup_ok:
            self.manifest.update(file_id, "SAFE", "Two local copies and verified Google Drive copy" + duplicate_note, remote_path=remote)
        else:
            self.manifest.update(file_id, "MISSING BACKUP", backup_detail + "; cloud copy verified" + duplicate_note, remote_path=remote)
        self._event("INFO", f"Protected: {source.name}")

    def days_on(self, source_root: Path) -> list[str]:
        """The dated shoot folders sitting on the SSD, newest first."""
        try:
            days = [item.name for item in source_root.iterdir()
                    if item.is_dir() and DATED_FOLDER.match(item.name)]
        except OSError:
            return []
        return sorted(days, reverse=True)

    def backup_day(self, source_root: Path, day: str,
                   progress: Callable[[int, int, str], None] | None = None) -> dict:
        """Duplicate one shoot day from the SSD onto every backup drive.

        This is the second stage of Kevin's workflow: everything is offloaded to
        the SSD first, then both hard drives are plugged in and the day is copied
        to each. Every file is verified by size and MD5 on arrival, and a file
        already present with matching contents is left alone so the job can be
        stopped and resumed. The SSD is only ever read.
        """
        report = lambda *args: progress(*args) if progress else None  # noqa: E731
        day_folder = source_root / day
        if not day_folder.is_dir():
            raise RuntimeError(f"There is no folder named {day!r} on {source_root.name}")

        roots = self.config.backup_roots()
        if not roots:
            raise RuntimeError("No backup drives are configured yet")
        missing = [str(root) for root in roots if not root.is_dir()]
        if missing:
            raise RuntimeError("These backup drives are not plugged in: " + ", ".join(missing))

        files = sorted(p for p in day_folder.rglob("*")
                       if p.is_file() and not p.name.startswith(".")
                       and not p.name.endswith(".footage-guardian-part"))
        if not files:
            raise RuntimeError(f"{day} holds no files yet — has everything finished offloading?")

        total = len(files) * len(roots)
        done = copied = already = 0
        failures: list[str] = []

        for source in files:
            relative = source.relative_to(source_root)
            try:
                size = source.stat().st_size
                digest = md5_file(source)
            except OSError as exc:
                failures.append(f"{relative}: could not be read ({exc})")
                done += len(roots)
                continue
            for root in roots:
                done += 1
                destination = root / relative
                report(done, total, f"{root.name}: {relative.name}")
                try:
                    if verified_copy_exists(destination, size, digest):
                        already += 1
                        continue
                    refuse_if_occupied(destination)
                    copy_verified(source, destination, digest)
                    copied += 1
                except (OSError, RuntimeError) as exc:
                    failures.append(f"{relative} -> {root.name}: {exc}")

        summary = {
            "day": day, "files": len(files), "drives": len(roots),
            "copied": copied, "already_there": already, "failures": failures,
        }
        if failures:
            self._event("ERROR", f"{day}: {len(failures)} file(s) could not be backed up")
        else:
            self._event("INFO", f"{day} backed up to {len(roots)} drive(s): "
                                f"{copied} copied, {already} already present")
        return summary

    def sync_day(self, source_root: Path, day: str,
                 progress: Callable[[int, int, str], None] | None = None) -> dict:
        """Upload one shoot day from the SSD main drive to Google Drive.

        The third stage, run once the day is safely on both backup HDDs. Drive
        mirrors the SSD folder for folder, every upload is checked against the
        remote MD5, and a file already up there with matching contents is skipped
        so the job can be stopped and resumed. The SSD is only ever read.
        """
        report = lambda *args: progress(*args) if progress else None  # noqa: E731
        day_folder = source_root / day
        if not day_folder.is_dir():
            raise RuntimeError(f"There is no folder named {day!r} on {source_root.name}")
        if not self.config.google_destination.strip():
            raise RuntimeError("No Google Drive folder is configured yet")
        if not self.cloud.available():
            raise RuntimeError("rclone is not installed, so Google Drive cannot be reached")
        problem = self.cloud.reachable(self.config.google_destination)
        if problem:
            raise RuntimeError(problem)

        files = sorted(p for p in day_folder.rglob("*")
                       if p.is_file() and not p.name.startswith(".")
                       and not p.name.endswith(".footage-guardian-part"))
        if not files:
            raise RuntimeError(f"{day} holds no files yet")

        base = self.config.google_destination.rstrip("/")
        uploaded = already = 0
        failures: list[str] = []

        for index, source in enumerate(files, 1):
            relative = source.relative_to(source_root)
            remote = f"{base}/{PurePosixPath(relative.as_posix())}"
            report(index, len(files), relative.name)
            file_id: int | None = None
            try:
                stat = source.stat()
                digest = md5_file(source)
                file_id = self.manifest.discover("SSD main drive", str(source),
                                                 relative.as_posix(), stat.st_size, stat.st_mtime_ns)
                # Never overwrite someone else's footage sitting at this path.
                claimant = self.manifest.remote_claimed_by(file_id, remote, digest)
                if claimant:
                    raise RuntimeError(
                        f"different footage is already uploaded here (from {claimant['source_path']})")
                try:
                    self.cloud.verify(remote, stat.st_size, digest, require_checksum=True)
                    already += 1
                    self.manifest.update(file_id, "SAFE", "Already in Google Drive, checksum matched",
                                         md5=digest, remote_path=remote)
                    continue
                except RuntimeError:
                    pass  # Not up there yet, or not matching - upload it.
                self.manifest.update(file_id, "UPLOADING", "Uploading to Google Drive", md5=digest)
                self.cloud.upload(source, remote)
                method = self.cloud.verify(remote, stat.st_size, digest, require_checksum=True)
                self.manifest.update(file_id, "SAFE", f"Google Drive copy verified by {method}",
                                     remote_path=remote)
                uploaded += 1
            except (OSError, RuntimeError) as exc:
                failures.append(f"{relative}: {exc}")
                if file_id is not None:
                    self.manifest.update(file_id, "ERROR", str(exc))

        summary = {"day": day, "files": len(files), "uploaded": uploaded,
                   "already_there": already, "failures": failures}
        if failures:
            self._event("ERROR", f"{day}: {len(failures)} file(s) did not reach Google Drive")
        else:
            self._event("INFO", f"{day} is in Google Drive: {uploaded} uploaded, {already} already there")
        return summary

    def verify_for_clearance(self, file_id: int) -> str:
        """Revalidate both identity and the remote object before allowing removal."""
        row = self.manifest.get(file_id)
        if row["state"] not in {"SAFE", "CLEAR TO REMOVE"}:
            raise RuntimeError("Only a SAFE local backup can be cleared")
        if not row["backup_path"] or not row["remote_path"] or not row["md5"]:
            raise RuntimeError("This file does not have complete backup records")
        backup = self._validated_backup_path(row)
        if not backup.is_file():
            raise RuntimeError("The recorded local backup is no longer present")
        if backup.stat().st_size != row["size"] or md5_file(backup) != row["md5"]:
            raise RuntimeError("The local backup no longer matches its recorded checksum")
        if not self.cloud.available():
            raise RuntimeError("rclone is not installed, so Google Drive cannot be checked")
        method = self.cloud.verify(row["remote_path"], row["size"], row["md5"], require_checksum=True)
        checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        detail = f"Google Drive re-verified using {method or 'checksum/size'} at {checked}"
        self.manifest.update(file_id, "CLEAR TO REMOVE", detail, clearance_at=checked)
        self._event("INFO", f"Cleared for local removal: {row['relative_path']}")
        return detail

    def remove_local_backup(self, file_id: int) -> None:
        """Permanently remove only a configured backup copy after a fresh cloud check."""
        row = self.manifest.get(file_id)
        if row["state"] != "CLEAR TO REMOVE":
            raise RuntimeError("Verify this file on Google Drive before removing it")
        backup = self._validated_backup_path(row)
        # Re-check the remote at the last possible moment; a stale clearance is not enough.
        self.cloud.verify(row["remote_path"], row["size"], row["md5"], require_checksum=True)
        if not backup.is_file():
            raise RuntimeError("The local backup is already missing")
        if backup.stat().st_size != row["size"] or md5_file(backup) != row["md5"]:
            raise RuntimeError("Local file identity changed; removal refused")
        backup.unlink()
        removed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.manifest.update(file_id, "CLOUD SAFE", f"Local backup removed by user at {removed}; source untouched",
                             backup_path=None, backup_removed_at=removed)
        self._event("WARNING", f"User removed verified local backup: {row['relative_path']}")

    def _validated_backup_path(self, row) -> Path:
        if not self.config.backup_path:
            raise RuntimeError("No backup drive is configured")
        root = Path(self.config.backup_path).expanduser().resolve()
        backup = Path(row["backup_path"]).expanduser().resolve()
        source = Path(row["source_path"]).expanduser().resolve()
        try:
            backup.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("Removal refused: recorded file is outside the configured backup drive") from exc
        if backup == source:
            raise RuntimeError("Removal refused: backup record points to the source footage")
        return backup

    def _event(self, level: str, message: str) -> None:
        getattr(self.log, level.lower(), self.log.info)(message)
        self.manifest.event(level, message)
