"""End-to-end dry run against real rclone and real Google Drive.

The unit tests use temp folders and a fake rclone, so they prove the logic and
nothing about the integration. This exercises the real thing: real uploads, real
remote hashes, real card structures.

Everything it touches is disposable and quarantined:

  * a synthetic camera card built in a scratch folder, never your footage
  * its own config and manifest, never ~/Library/Application Support/...
  * a Drive folder whose name must contain DRY-RUN, never your archive

Run it from the project root:

    python3 tools/dry_run.py                 # build, upload, verify, clean up
    python3 tools/dry_run.py --keep-remote   # leave the Drive folder to inspect
    python3 tools/dry_run.py --cleanup       # just purge the Drive folder
    python3 tools/dry_run.py --card /Volumes/UNTITLED   # use a real card instead
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from footage_guardian.config import Config, Source
from footage_guardian.engine import Guardian
from footage_guardian.ingest import inspect_card
from footage_guardian.manifest import Manifest
from footage_guardian.storage import Rclone, md5_file, remote_hash

DEFAULT_REMOTE = "gdrive:FG-Auto-DIT-DRY-RUN"
GUARD = "DRY-RUN"

PASS, FAIL, INFO = "  PASS  ", "  FAIL  ", "        "


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, message: str) -> bool:
        print(f"{PASS if ok else FAIL}{message}")
        if not ok:
            self.failures += 1
        return ok

    def note(self, message: str) -> None:
        print(f"{INFO}{message}")


def require_disposable(remote: str) -> None:
    """Refuse to operate on anything not obviously a throwaway folder."""
    if GUARD not in remote.upper():
        raise SystemExit(
            f"Refusing to use {remote!r}: the dry-run destination must contain "
            f"{GUARD!r} so it can never be a real archive folder."
        )


def purge_remote(remote: str) -> None:
    require_disposable(remote)
    subprocess.run(["rclone", "purge", remote], capture_output=True, text=True)


def build_card(root: Path, folders: tuple[str, ...], clips: dict[str, bytes]) -> Path:
    for name, data in clips.items():
        target = root.joinpath(*folders, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def build_synthetic_cards(scratch: Path) -> list[Path]:
    """Three cards that between them exercise the paths most likely to break."""
    avchd = ("PRIVATE", "AVCHD", "BDMV", "STREAM")
    # Two sequential Main Cam cards deliberately reusing filenames.
    first = build_card(scratch / "CARD_A", avchd,
                       {"00000.MTS": b"main cam card one clip one" * 4096,
                        "00001.MTS": b"main cam card one clip two" * 4096})
    second = build_card(scratch / "CARD_B", avchd,
                        {"00000.MTS": b"main cam card two clip one" * 4096})
    drone = build_card(scratch / "CARD_DRONE", ("DCIM", "DJI_001"),
                       {"DJI_0001.MP4": b"drone footage" * 4096})
    return [first, second, drone]


def remote_index(remote: str) -> dict[str, dict]:
    result = subprocess.run(
        ["rclone", "lsjson", remote, "--recursive", "--files-only", "--hash"],
        capture_output=True, text=True,
    )
    if result.returncode:
        return {}
    return {item["Path"]: item for item in json.loads(result.stdout)}


def fingerprint_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): md5_file(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def run(remote: str, card_override: str | None, keep_remote: bool) -> int:
    report = Report()
    require_disposable(remote)

    cloud = Rclone()
    if not report.check(cloud.available(), "rclone is installed"):
        return 1
    remotes = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout.split()
    configured = remote.split(":")[0] + ":"
    if not report.check(configured in remotes, f"rclone remote {configured} is configured"):
        report.note(f"Found: {', '.join(remotes) or 'none'}. Run 'rclone config' first.")
        return 1
    # A configured remote is not a working one. Fail here rather than once per file.
    problem = cloud.reachable(remote)
    if not report.check(not problem, f"Google Drive answers on {configured}"):
        report.note(problem)
        return 1

    scratch = Path(tempfile.mkdtemp(prefix="fg-dry-run-"))
    report.note(f"Scratch folder: {scratch}")
    report.note(f"Drive folder:   {remote}")
    print()

    try:
        if card_override:
            cards = [Path(card_override).expanduser().resolve()]
            report.check(cards[0].is_dir(), f"card is mounted at {cards[0]}")
            report.note("Using a real card. It is opened read-only and never written to.")
        else:
            cards = build_synthetic_cards(scratch / "cards")
            report.note(f"Built {len(cards)} synthetic cards (two Main Cam, one Drone)")

        before = {card: fingerprint_tree(card) for card in cards}
        total = sum(len(items) for items in before.values())
        report.note(f"{total} files to protect")
        print()

        report.note("Clearing any leftovers from a previous run…")
        purge_remote(remote)

        backup = scratch / "BACKUP"
        backup.mkdir()
        config = Config(
            sources=[Source(f"Card {index}", str(card)) for index, card in enumerate(cards, 1)],
            backup_path=str(backup),
            google_destination=remote,
            stable_seconds=0,
        )
        manifest = Manifest(scratch / "manifest.sqlite3")
        logging.basicConfig(level=logging.INFO, format="        %(message)s")

        guardian = Guardian(config, manifest, logging.getLogger("dry_run"), cloud=cloud)

        # A DJI card could be the drone or the Osmo; the tree cannot tell them
        # apart. Prove the guardian refuses to guess, then answer it, exactly as
        # Kevin would in the window.
        undecided = [card for card in cards if inspect_card(card).alternatives]
        if undecided:
            guardian.scan_once()
            # The other cards file normally on this pass; only the undecided one
            # must be held back, with its footage still sitting on the card.
            held = {str(card) for card in undecided}
            leaked = [row["relative_path"] for row in manifest.rows()
                      if any(row["source_path"].startswith(path) for path in held)]
            report.check(not leaked, "an unconfirmed DJI card is filed nowhere until answered")
            for path in leaked[:3]:
                report.note(f"leaked: {path}")
            for card in undecided:
                info = inspect_card(card)
                report.note(f"Confirming {card.name} as 'Drone' (offered: {', '.join(info.alternatives)})")
                manifest.confirm_card_camera(info.fingerprint, "Drone")

        print("        Uploading to Google Drive — this is the slow part…\n")
        guardian.scan_once()
        print()

        rows = manifest.rows()
        states = {row["state"] for row in rows}
        report.check(states == {"SAFE"}, f"every file reached SAFE (saw: {', '.join(sorted(states)) or 'nothing'})")
        for row in rows:
            if row["state"] != "SAFE":
                report.note(f"{row['state']}: {row['relative_path']} — {row['detail']}")

        # The one rule that matters most: sources come back untouched.
        for card in cards:
            report.check(before[card] == fingerprint_tree(card), f"source untouched: {card.name}")

        backup_tree = fingerprint_tree(backup)
        report.check(len(backup_tree) == total, f"backup drive holds all {total} files")

        remote_files = remote_index(remote)
        report.check(len(remote_files) == total, f"Google Drive holds all {total} files")

        # Compare against the hash Drive actually reports. Never fall back to the
        # local digest: that turns a missing remote hash into a check of a value
        # against itself, which passes even when Drive holds the wrong bytes.
        mismatches = [path for path, digest in backup_tree.items()
                      if path not in remote_files
                      or remote_hash(remote_files[path]) != digest]
        report.check(not mismatches, "every Drive copy matches the backup by MD5")
        for path in mismatches[:5]:
            reported = remote_hash(remote_files.get(path, {})) if path in remote_files else "absent from Drive"
            report.note(f"mismatch: {path} — Drive reported {reported or 'no MD5 at all'}")

        report.check(backup_tree.keys() == remote_files.keys(),
                     "Drive tree and backup tree are identical folder-for-folder")

        if not card_override:
            slots = sorted({path.split("/")[2] for path in backup_tree if "Main Cam" in path})
            report.check(slots == ["card 1", "card 2"],
                         f"sequential Main Cam cards got separate slots (saw: {slots})")

        print("\n        Archive tree produced:")
        for path in sorted(backup_tree):
            print(f"          {path}")

    finally:
        if not keep_remote:
            purge_remote(remote)
        else:
            print(f"\n        Left in Drive for inspection: {remote}")
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    if report.failures:
        print(f"        {report.failures} check(s) FAILED — do not trust this with real footage yet.")
        return 1
    print("        All checks passed against real Google Drive.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help=f"disposable Drive folder (default {DEFAULT_REMOTE})")
    parser.add_argument("--card", help="use a real mounted card instead of synthetic ones")
    parser.add_argument("--keep-remote", action="store_true", help="leave the Drive folder behind to inspect")
    parser.add_argument("--cleanup", action="store_true", help="purge the Drive folder and exit")
    args = parser.parse_args()

    if args.cleanup:
        purge_remote(args.remote)
        print(f"Purged {args.remote}")
        return 0
    return run(args.remote, args.card, args.keep_remote)


if __name__ == "__main__":
    raise SystemExit(main())
