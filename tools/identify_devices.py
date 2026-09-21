#!/usr/bin/env python3
"""Describe every camera, card and device currently mounted, so detection can be
built against the real thing instead of guesswork.

Kevin runs this once with everything plugged in and sends back the report. It is
strictly read-only: it opens nothing, copies nothing, and never writes to a
mounted volume. The report lists folder shapes, file extensions and a couple of
example filenames per device - enough to identify a camera, and no footage.

    python3 tools/identify_devices.py

The report is written next to this script as device-report.txt and also printed.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from footage_guardian.ingest import device_identity, inspect_card  # noqa: E402

SKIP = {"Macintosh HD", "Data", "com.apple.TimeMachine.localsnapshots", ".timemachine"}
MAX_DEPTH = 4


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000:
            return f"{n:,.1f} {unit}"
        n /= 1000
    return f"{n:,.1f} PB"


def describe(volume: Path, out: list[str]) -> None:
    out.append("")
    out.append("=" * 68)
    out.append(f"VOLUME NAME: {volume.name!r}")
    out.append("=" * 68)

    # The hardware behind the volume. This is the only thing that can separate a
    # DJI drone from a DJI Osmo, because their cards are identical - but it only
    # exists when the camera itself is plugged in, not a card in a reader.
    identity = device_identity(volume)
    out.append("  HARDWARE macOS REPORTS:")
    out.append(f"    protocol     : {identity.protocol or '(none)'}")
    out.append(f"    media name   : {identity.media_name or '(none)'}")
    out.append(f"    USB product  : {identity.usb_product or '(none)'}")
    out.append(f"    USB maker    : {identity.usb_vendor or '(none)'}")
    if identity.is_removable_device and (identity.usb_product or identity.media_name):
        out.append("    -> the camera itself is plugged in and names itself. Good.")
    elif identity.protocol.upper() == "USB":
        out.append("    -> plugged in over USB but not naming itself; likely a card reader.")
    out.append("")

    folders: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    examples: dict[str, str] = {}
    total_bytes = 0
    count = 0
    earliest = latest = None
    unreadable = 0

    for dirpath, dirnames, filenames in os.walk(volume):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in
                       {"System Volume Information", ".Spotlight-V100", ".fseventsd"}]
        for name in filenames:
            if name.startswith("."):
                continue
            full = Path(dirpath) / name
            try:
                stat = full.stat()
            except OSError:
                unreadable += 1
                continue
            relative = full.relative_to(volume)
            shape = "/".join(relative.parts[:-1][:MAX_DEPTH]) or "(top level)"
            folders[shape] += 1
            suffix = full.suffix.upper() or "(no extension)"
            extensions[suffix] += 1
            examples.setdefault(suffix, name)
            total_bytes += stat.st_size
            count += 1
            stamp = stat.st_mtime
            earliest = stamp if earliest is None else min(earliest, stamp)
            latest = stamp if latest is None else max(latest, stamp)

    if not count:
        out.append("  No readable files found. If this is a phone or a pair of glasses,")
        out.append("  macOS is probably not mounting it as a disk at all - see the note")
        out.append("  at the end of this report.")
        return

    out.append(f"  files: {count:,}    total: {human(total_bytes)}")
    if earliest and latest:
        out.append(f"  oldest file: {datetime.fromtimestamp(earliest):%Y-%m-%d %H:%M}")
        out.append(f"  newest file: {datetime.fromtimestamp(latest):%Y-%m-%d %H:%M}")
    if unreadable:
        out.append(f"  unreadable entries skipped: {unreadable}")

    out.append("")
    out.append("  FOLDER SHAPE (this is what identifies the camera):")
    for shape, n in folders.most_common(14):
        out.append(f"    {n:>6} files   {shape}")
    if len(folders) > 14:
        out.append(f"    ... and {len(folders) - 14} more folders")

    out.append("")
    out.append("  FILE TYPES:")
    for suffix, n in extensions.most_common(12):
        out.append(f"    {n:>6}  {suffix:<16} e.g. {examples[suffix]}")

    out.append("")
    try:
        card = inspect_card(volume)
        out.append(f"  APP CURRENTLY THINKS: {card.suggested_camera}")
        if card.alternatives:
            out.append(f"  *** CANNOT DECIDE between: {', '.join(card.alternatives)}")
            out.append("      Kevin: please write below which camera this actually is.")
        out.append(f"  folder signature: {card.signature[:110]}")
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never abort
        out.append(f"  APP COULD NOT READ THIS AS A CARD: {exc}")

    out.append("")
    out.append("  WHICH DEVICE IS THIS? ______________________________________")


def main() -> int:
    volumes_root = Path("/Volumes")
    if not volumes_root.is_dir():
        print("No /Volumes folder - is this macOS?")
        return 2

    volumes = [v for v in sorted(volumes_root.iterdir(), key=lambda p: p.name.lower())
               if v.is_dir() and v.name not in SKIP and not v.name.startswith(".")]

    out: list[str] = []
    out.append("FOOTAGE GUARDIAN - DEVICE IDENTIFICATION REPORT")
    out.append(f"Generated {datetime.now():%Y-%m-%d %H:%M} on {os.uname().nodename}")
    out.append("")
    out.append("Read-only. Nothing on any device was opened, copied or changed.")
    out.append(f"Found {len(volumes)} mounted volume(s).")

    for volume in volumes:
        describe(volume, out)

    out.append("")
    out.append("=" * 68)
    out.append("DEVICES THAT WILL NOT APPEAR ABOVE")
    out.append("=" * 68)
    out.append("  iPhone / iPad - macOS does not mount these as disks. Their footage")
    out.append("    has to come through Photos, Image Capture, or AirDrop first.")
    out.append("  Meta Ray-Ban glasses - these sync through the Meta AI app, so the")
    out.append("    files arrive in Downloads rather than on a mounted volume.")
    out.append("  Any camera that needs a mode change - some cameras only appear as a")
    out.append("    disk once switched to 'USB mass storage' rather than 'MTP' or")
    out.append("    'charge only'. If a device is missing, check its screen.")
    out.append("")
    out.append("Please fill in the 'WHICH DEVICE IS THIS?' line for each volume and")
    out.append("send this file back.")

    text = "\n".join(out) + "\n"
    print(text)
    target = Path(__file__).resolve().parent / "device-report.txt"
    target.write_text(text, encoding="utf-8")
    print(f"Saved to: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
