#!/usr/bin/env python3
"""Put a Footage Guardian app in Applications, so Kevin clicks an icon
instead of opening a folder and double-clicking a .command file.

Run once, from the Footage Guardian folder:

    python3 tools/install_app.py

The app carries its own copy of the code, inside the app. After `git pull`,
run this again to put the new code in it:

    git pull && python3 tools/install_app.py

Why a copy, rather than an app that runs this folder: this folder lives on
the Desktop, and macOS gives every app its own permission to read the
Desktop. Terminal has it, which is why the .command file works. A freshly
made app does not, and on Kevin's Mac (2026-10-08) it was refused without
a prompt — the app quit the instant it was opened, with no window and no
message. Run from Terminal, the very same app opened fine. An app that only
reads inside itself needs no such permission.

Why it is built here rather than shipped: an app downloaded from the
internet is quarantined, and macOS 15 makes unsigned quarantined apps a trip
through System Settings to open. One written on this Mac is not quarantined.

Kept to Python 3.9 on purpose: `python3` in Terminal may still be Apple's,
and that is fine for this — it only writes files, it never draws a window.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import plistlib
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER_NAME = "Footage Guardian Auto DIT.command"
PACKAGE = "footage_guardian"
APP_NAME = "Footage Guardian"
BUNDLE_ID = "local.footage-guardian.auto-dit"


class InstallRefused(Exception):
    """Something is in the way that this script did not put there."""


def default_destination() -> Path:
    """/Applications if this account can write there, else ~/Applications.

    Either shows up in Launchpad and Spotlight. No sudo, no password.
    """
    system = Path("/Applications")
    if os.access(system, os.W_OK):
        return system
    return Path.home() / "Applications"


def info_plist() -> dict:
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        # Shown in the macOS prompt the first time it reads a camera card.
        # Answering "Don't Allow" leaves it unable to see any card at all,
        # so the prompt itself says what the access is for.
        "NSRemovableVolumesUsageDescription": (
            "Footage Guardian reads camera cards to copy them onto your drives. "
            "It never changes or deletes anything on a card."
        ),
    }


def executable_script() -> str:
    return """#!/bin/zsh
# Written by tools/install_app.py. Runs the copy of Footage Guardian kept
# inside this app — never the folder on the Desktop, which macOS will not
# let a new app read.
launcher="${0:A:h}/../Resources/Footage Guardian Auto DIT.command"
# Python would otherwise write cache files into the app itself.
export PYTHONDONTWRITEBYTECODE=1
/bin/zsh "$launcher"
code=$?
# 126/127: the launcher could not even be read or run, so it had no chance
# to explain itself. Everything later reports through its own dialog.
if (( code == 126 || code == 127 )); then
  osascript -e 'on run argv' \\
            -e 'display dialog (item 1 of argv) with title "Footage Guardian" buttons {"OK"} default button 1 with icon caution' \\
            -e 'end run' "Footage Guardian could not start. Your footage and drives were not changed.

Open Terminal and paste:

    cd ~/Desktop/\\"Footage Guardian\\" && python3 tools/install_app.py

Then open Footage Guardian again. If that does not fix it, send Stuart a photo of this message." >/dev/null 2>&1
fi
exit $code
"""


def source_version(source: Path) -> str:
    """The commit the copy was taken from, so 'which version is he on' has an answer."""
    try:
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


def bundle_identifier(app: Path) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return None


def build_app(destination: Path, source: Path = REPO) -> Path:
    """Write the app into `destination` and return its path.

    Copies the launcher and the footage_guardian package out of `source`.
    Replaces an earlier copy of this same app. Refuses to touch anything
    else with the same name — that is somebody else's app.
    """
    launcher = source / LAUNCHER_NAME
    package = source / PACKAGE
    if not launcher.is_file() or not (package / "__init__.py").is_file():
        raise InstallRefused(f"This does not look like the Footage Guardian folder: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    app = destination / f"{APP_NAME}.app"
    if app.exists() and bundle_identifier(app) != BUNDLE_ID:
        raise InstallRefused(
            f"There is already a different app called {app.name} in {destination}. "
            "It was left alone. Rename or remove it, then run this again."
        )

    # Build beside the target and swap it in, so a half-written app never
    # sits where Kevin would click it.
    staging = Path(tempfile.mkdtemp(prefix=".footage-guardian-", dir=destination))
    try:
        built = staging / app.name
        macos = built / "Contents" / "MacOS"
        resources = built / "Contents" / "Resources"
        macos.mkdir(parents=True)
        resources.mkdir()
        info = info_plist()
        info["CFBundleVersion"] = source_version(source)
        with open(built / "Contents" / "Info.plist", "wb") as handle:
            plistlib.dump(info, handle)
        (built / "Contents" / "PkgInfo").write_text("APPL????")
        shutil.copy2(launcher, resources / LAUNCHER_NAME)
        shutil.copytree(package, resources / PACKAGE,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        script = macos / APP_NAME
        script.write_text(executable_script())
        script.chmod(0o755)
        if app.exists():
            shutil.rmtree(app)
        built.rename(app)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Add Footage Guardian to Applications")
    parser.add_argument("--destination", type=Path, default=None,
                        help="folder to put the app in (default: Applications)")
    args = parser.parse_args()
    destination = args.destination or default_destination()
    try:
        app = build_app(destination)
    except (InstallRefused, OSError) as exc:
        print(f"\nFootage Guardian was not installed.\n\n{exc}\n")
        return 1
    print(f"""
Done. Footage Guardian is installed at:

    {app}

To open it: press Cmd+Space, type Footage Guardian, press Enter.

To keep it in the Dock: open your Applications folder and drag
Footage Guardian onto the Dock. (While it runs, the Dock may also show a
Python rocket icon — that is the same app, and is not the one to keep.)

The first time it reads a camera, macOS asks whether it may access files
on a removable volume. Click Allow, or it cannot see any card.

After every update, run this again so the app gets the new version:

    cd ~/Desktop/"Footage Guardian" && git pull && python3 tools/install_app.py
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
