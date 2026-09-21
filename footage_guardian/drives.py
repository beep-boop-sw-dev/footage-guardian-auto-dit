"""Working out which plugged-in drive is which.

The Drives tab was three empty text boxes and a Choose… button, which
asks the operator to type paths on a shoot day. Everything needed to
fill them in was already here — `looks_offloaded` reads a drive cheaply,
`device_identity` asks macOS about the hardware — it was just never
pointed at the question.

This proposes; it never decides. A saved setting is kept whenever the
drive behind it is still plugged in, every mounted volume stays offered
in the dropdown, and a wrong guess is one click to correct. Detection
silently rewriting where footage gets written is exactly the kind of
confident guess the safety model forbids.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .ingest import boot_device, classify_source, is_system_volume, looks_offloaded

# What the operator actually calls these drives, per the project notes:
# "SSD main drive", "Back up HDD 1", "Back up HDD 2". People rename
# drives, so a name is strong evidence and never proof.
SSD_NAME = re.compile(r"\b(ssd|main)\b", re.I)
BACKUP_NAME = re.compile(r"\b(back\s*-?\s*up|backup|hdd)\b", re.I)
BACKUP_NUMBER = re.compile(r"(\d+)\s*$")


def is_solid_state(mount_point: Path) -> bool | None:
    """True, False, or None when macOS will not say.

    None matters: an unknown drive must not be guessed at as rotational
    and quietly proposed as a backup target.
    """
    try:
        result = subprocess.run(["diskutil", "info", str(mount_point)],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "solid state":
            answer = value.strip().lower()
            if answer.startswith("yes"):
                return True
            if answer.startswith("no"):
                return False
    return None


def mounted_volumes(volumes_root: Path = Path("/Volumes")) -> list[Path]:
    """Every drive a human plugged in, and nothing macOS mounted for itself."""
    if not volumes_root.is_dir():
        return []
    # Only the real /Volumes carries the boot drive; a temporary test root
    # sits on it, so asking there would filter everything away.
    boot = boot_device() if volumes_root == Path("/Volumes") else None
    found = []
    for item in volumes_root.iterdir():
        try:
            if not item.is_dir() or is_system_volume(item, boot):
                continue
        except OSError:
            continue
        found.append(item)
    return sorted(found, key=lambda p: p.name.lower())


@dataclass
class DriveGuess:
    """One proposed answer, and why — so the operator can judge it."""
    path: Path | None = None
    reason: str = ""
    confident: bool = False

    @property
    def value(self) -> str:
        return str(self.path) if self.path else ""


@dataclass
class DrivePlan:
    ssd: DriveGuess = field(default_factory=DriveGuess)
    backups: list[DriveGuess] = field(default_factory=list)
    #: Everything mounted, so a wrong guess is one click to fix.
    volumes: list[Path] = field(default_factory=list)
    #: Configured drives that are not plugged in right now.
    missing: list[str] = field(default_factory=list)


def _name_score(path: Path, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(path.name))


def _is_camera_card(path: Path) -> bool:
    """A card is footage waiting to be rescued, never somewhere to write it."""
    try:
        return classify_source(path).kind == "card"
    except Exception:  # noqa: BLE001 - a drive we cannot read is not a target either
        return True


def propose_drives(configured_ssd: str = "",
                   configured_backups: tuple[str, ...] = (),
                   volumes_root: Path = Path("/Volumes"),
                   solid_state: Callable[[Path], bool | None] = is_solid_state,
                   backup_slots: int = 2) -> DrivePlan:
    """Suggest which mounted volume is the SSD and which are the backups.

    In order of how much it is worth trusting:

    1. A saved setting whose drive is plugged in. Already confirmed once,
       so nothing here second-guesses it.
    2. The drive's name. The operator names these deliberately.
    3. What it looks like: a drive already carrying dated folders is one
       of these three, and macOS can usually say whether it is solid
       state.

    Anything left over is offered but not proposed.
    """
    volumes = mounted_volumes(volumes_root)
    by_path = {str(v.resolve() if v.exists() else v): v for v in volumes}
    plan = DrivePlan(volumes=volumes)
    taken: set[Path] = set()

    def claim(path: Path | None, reason: str, confident: bool) -> DriveGuess:
        if path is not None:
            taken.add(path)
        return DriveGuess(path=path, reason=reason, confident=confident)

    def still_mounted(configured: str) -> Path | None:
        if not configured:
            return None
        candidate = Path(configured).expanduser()
        try:
            resolved = str(candidate.resolve())
        except OSError:
            resolved = str(candidate)
        found = by_path.get(resolved)
        if found is not None and found not in taken:
            return found
        # A configured path can be a folder on a drive rather than the
        # drive itself; accept it if it is simply there.
        if candidate.is_dir() and candidate not in taken:
            return candidate
        return None

    # 1 — keep what is already set, when it is plugged in.
    kept = still_mounted(configured_ssd)
    if kept is not None:
        plan.ssd = claim(kept, "already set, and plugged in", True)
    elif configured_ssd:
        plan.missing.append(f"SSD main drive ({configured_ssd})")

    for index in range(backup_slots):
        configured = configured_backups[index] if index < len(configured_backups) else ""
        kept = still_mounted(configured)
        if kept is not None:
            plan.backups.append(claim(kept, "already set, and plugged in", True))
        else:
            plan.backups.append(DriveGuess())
            if configured:
                plan.missing.append(f"Back up HDD {index + 1} ({configured})")

    # 2 — the operator's own naming.
    if plan.ssd.path is None:
        named = [v for v in volumes if v not in taken and _name_score(v, SSD_NAME)]
        if len(named) == 1:
            plan.ssd = claim(named[0], f"named {named[0].name!r}", True)

    named_backups = sorted(
        (v for v in volumes if v not in taken and _name_score(v, BACKUP_NAME)),
        key=lambda v: (int(m.group(1)) if (m := BACKUP_NUMBER.search(v.name)) else 99, v.name.lower()),
    )
    for slot, guess in enumerate(plan.backups):
        if guess.path is None and named_backups:
            chosen = named_backups.pop(0)
            plan.backups[slot] = claim(chosen, f"named {chosen.name!r}", True)

    # 3 — shape and hardware, for anything still unfilled.
    #
    # Only drives already carrying dated folders are considered. A
    # freshly formatted backup drive is therefore not detected and has
    # to be picked from the list, which is the right way round to fail:
    # the alternative proposes writing footage onto whatever else is
    # plugged in. A real Mac here had a camera card and an unrelated
    # client's work drive mounted at the same time.
    remaining = [v for v in volumes if v not in taken and not _is_camera_card(v)]
    organised = [v for v in remaining if looks_offloaded(v)]
    if organised:
        pool = organised
        states = {v: solid_state(v) for v in pool}

        if plan.ssd.path is None:
            flash = [v for v in pool if states.get(v) is True]
            if len(flash) == 1:
                why = "the only solid-state drive plugged in"
                if flash[0] in organised:
                    why += " that already holds dated folders"
                plan.ssd = claim(flash[0], why, False)

        spinning = [v for v in pool if v not in taken and states.get(v) is False]
        for slot, guess in enumerate(plan.backups):
            if guess.path is None and spinning:
                chosen = spinning.pop(0)
                plan.backups[slot] = claim(
                    chosen,
                    "a hard drive already holding dated folders" if chosen in organised
                    else "a hard drive, not solid state",
                    False,
                )

    return plan
