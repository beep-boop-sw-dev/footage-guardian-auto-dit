#!/usr/bin/env python3
"""Put a Footage Guardian app in Applications, so Kevin clicks an icon
instead of opening a folder and double-clicking a .command file.

Run once, from the Footage Guardian folder:

    python3 tools/install_app.py

The app holds no code. It runs "Footage Guardian Auto DIT.command" in this
folder, so `git pull` keeps updating the real thing and the app never needs
reinstalling. Move or rename this folder and the app says so and asks for
this to be run again.

Why it is built here rather than shipped: an app downloaded from the
internet is quarantined, and macOS 15 makes unsigned quarantined apps a trip
through System Settings to open. One written on this Mac is not quarantined.

Kept to Python 3.9 on purpose: `python3` in Terminal may still be Apple's,
and that is fine for this — it only writes files, it never draws a window.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "Footage Guardian Auto DIT.command"
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


def executable_script(launcher: Path) -> str:
    target = shlex.quote(str(launcher))
    return f"""#!/bin/zsh
# Written by tools/install_app.py. Holds no code: it runs the launcher in
# the Footage Guardian folder, so updating that folder updates the app.
launcher={target}
if [[ ! -f "$launcher" ]]; then
  osascript -e 'on run argv' \\
            -e 'display dialog (item 1 of argv) with title "Footage Guardian" buttons {{"OK"}} default button 1 with icon caution' \\
            -e 'end run' "Footage Guardian cannot find its folder. It was here:

$launcher

If the folder was moved or renamed, put it back. Otherwise send Stuart a photo of this message. Your footage and drives were not changed." >/dev/null 2>&1
  exit 1
fi
exec /bin/zsh "$launcher"
"""


def bundle_identifier(app: Path) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return None


def build_app(destination: Path, launcher: Path = LAUNCHER) -> Path:
    """Write the app into `destination` and return its path.

    Replaces an earlier copy of this same app. Refuses to touch anything
    else with the same name — that is somebody else's app.
    """
    if not launcher.is_file():
        raise InstallRefused(f"The launcher is missing: {launcher}")
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
        macos.mkdir(parents=True)
        with open(built / "Contents" / "Info.plist", "wb") as handle:
            plistlib.dump(info_plist(), handle)
        (built / "Contents" / "PkgInfo").write_text("APPL????")
        script = macos / APP_NAME
        script.write_text(executable_script(launcher.resolve()))
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

This folder must stay where it is: {REPO}
The app runs the copy in here, so moving it breaks the app.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
