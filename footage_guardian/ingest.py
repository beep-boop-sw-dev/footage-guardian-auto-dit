from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .manifest import Manifest
from .progress import ByteProgress
from .storage import copy_verified, md5_file, refuse_if_occupied, safe_component, verified_copy_exists


CAMERA_PATTERNS = [
    ("Main Cam", ("PRIVATE/AVCHD", "CONTENTS/CLIP")),
    ("Sony", ("PRIVATE/M4ROOT", "XDROOT")),
    ("Canon", ("CANONMSC", "CONTENTS/CLIPS001", "DCIM/100CANON")),
    ("Main Cam", ("PRIVATE/PANA_GRP", "DCIM/100_PANA")),
    ("Blackmagic", ("BLACKMAGIC",)),
    ("RED", ("RDM",)),
    ("GoPro", ("DCIM/100GOPRO", "DCIM/101GOPRO")),
    ("Drone", ("DCIM/DJI_001", "DCIM/100MEDIA", "MISC/THM")),
]

# Some trees genuinely cannot be told apart, and guessing mixes two cameras into
# one folder. A DJI drone and a DJI Osmo both write DCIM/DJI_001 full of
# DJI_####.MP4, and both can carry a MISC folder of .THM thumbnails - DJI's own
# export guide gives the same "DCIM > DJI 001" path for drones and the whole Osmo
# range. Nothing on the card resolves it, so the guardian asks rather than
# assumes. Checked against DJI documentation 2026-09-11.
AMBIGUOUS_TREES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("DCIM/DJI_001", "DCIM/100MEDIA"), ("Drone", "DJI Osmo")),
]

# When the camera itself is plugged in, macOS reports what it is. That settles
# the drone-versus-Osmo question the cards never could, so it is checked first
# and nothing needs confirming. Matched against the USB product string, which is
# a model name: "Osmo Action 4", "Mavic 3 Pro", "Insta360 X4".
#
# Drones are listed by model because DJI brands its handhelds "Osmo" and its
# aircraft by line. A DJI device matching neither list stays ambiguous rather
# than being guessed at.
DEVICE_NAME_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("DJI Osmo", ("OSMO", "ACTION", "POCKET")),
    ("Drone", ("MAVIC", "PHANTOM", "INSPIRE", "AVATA", "NEO", "FPV",
               "MINI 2", "MINI 3", "MINI 4", "AIR 2", "AIR 3", "AIR S")),
    ("360", ("INSTA360", "ONE X", "ONE R", "ONE RS", "GO 3", "X3", "X4", "X5")),
    ("GoPro", ("GOPRO", "HERO")),
    ("Main Cam", ("LUMIX", "DC-G", "DC-S", "GH5", "GH6", "GH7", "DMC-")),
    ("Sony", ("ILCE", "FX3", "FX6", "FX30", "PXW", "SONY ALPHA")),
    ("Canon", ("CANON", "EOS")),
    ("Blackmagic", ("BLACKMAGIC", "POCKET CINEMA")),
]


def camera_from_device_name(product: str, vendor: str = "") -> str:
    """Name the camera from what the hardware calls itself, or '' if unrecognised.

    Deliberately strict: an unrecognised DJI device returns nothing rather than
    defaulting to Drone, because that guess is exactly what mixes two cameras.
    """
    haystack = f"{product} {vendor}".upper()
    if not haystack.strip():
        return ""
    for camera, needles in DEVICE_NAME_PATTERNS:
        if any(needle in haystack for needle in needles):
            return camera
    return ""


class SourceUnclear(RuntimeError):
    """Raised when a drive is neither plainly an offloaded tree nor plainly a card.

    Filing it either way would be a guess, and a wrong guess here mirrors the
    whole drive into one incorrect folder.
    """

    def __init__(self, root: Path, reason: str) -> None:
        self.root = root
        super().__init__(f"Not sure what {root.name!r} is, so nothing has been copied — {reason}")


class CameraUnconfirmed(RuntimeError):
    """Raised when a card could be one of several cameras and nobody has said which.

    Carries the choices so the window can offer them. Filing footage under the
    wrong camera silently merges two shoots, which is exactly the kind of quiet
    wrong answer this app exists to avoid.
    """

    def __init__(self, card: "CardInfo") -> None:
        self.card = card
        options = " or ".join(card.alternatives)
        super().__init__(
            f"{card.volume_name} could be {options} — the cards are identical in structure. "
            f"Confirm which camera it is before this footage can be filed."
        )


@dataclass(frozen=True)
class CardInfo:
    root: Path
    volume_name: str
    signature: str
    fingerprint: str
    suggested_camera: str
    files: tuple[Path, ...]
    total_bytes: int
    # Populated only when the tree matches more than one camera and nobody has
    # confirmed which. Empty means the guess can be trusted.
    alternatives: tuple[str, ...] = ()

    @property
    def card_label(self) -> str:
        return f"CARD-{self.fingerprint[:10].upper()}"


# New folders are written M-D-YY. The colon spelling is accepted because that is
# what macOS actually stores when a date is typed into Finder as 7/22/26 — the
# existing archive is full of them, and failing to recognise one would bury an
# organised drive inside a second dated wrapper.
DATED_FOLDER = re.compile(r"^\d{1,2}[-:]\d{1,2}[-:]\d{2}$")


def looks_offloaded(root: Path) -> bool:
    """True when a drive already carries the M-D-YY convention at its top level.

    Such a drive is mirrored verbatim. Anything else is treated as a raw card and
    given a dated wrapper so its files cannot collide with the next card's.
    """
    try:
        return any(DATED_FOLDER.match(item.name) for item in root.iterdir() if item.is_dir())
    except OSError:
        return False


# The complete vocabulary a camera uses at the top of its own storage. Cameras
# are boring and repetitive here; working drives are not, because people name
# folders after projects. That difference is the only dependable way to tell a
# card from a drive full of other work.
CARD_TOP_LEVEL = {
    "DCIM", "PRIVATE", "MISC", "CLIPS", "AVCHD", "BDMV", "CANONMSC", "XDROOT",
    "M4ROOT", "PANA_GRP", "CONTENTS", "BLACKMAGIC", "RDM", "DJI_001", "CAMERA01",
    "PANORAMA", "HYPERLAPSE", "TIMELAPSE", "SYSTEM", "GOPRO", "LOST.DIR",
}


# Volumes macOS mounts for its own purposes. None of them is ever camera media,
# and walking one is not merely wasted work: /Volumes/Macintosh HD is the boot
# drive, and a recursive scan of it does not finish.
SYSTEM_VOLUME_NAMES = {
    "data", "preboot", "recovery", "update", "vm", "xarts", "iscpreboot", "hardware",
}


def boot_device() -> int | None:
    """The device id of the startup disk, or None if it cannot be read."""
    try:
        return os.stat("/").st_dev
    except OSError:
        return None


def is_system_volume(path: Path, boot: int | None) -> bool:
    """True for anything macOS mounted for itself rather than for the user.

    The name check catches the APFS helper volumes and Apple's own mounts. The
    device check catches the startup disk under whatever name it carries, which
    a name list cannot promise to know.
    """
    name = path.name
    if name.startswith(".") or name.startswith("com.apple."):
        return True
    if name.lower() in SYSTEM_VOLUME_NAMES:
        return True
    if boot is None:
        return False
    try:
        return path.stat().st_dev == boot
    except OSError:
        return True


@dataclass(frozen=True)
class DeviceIdentity:
    """What macOS knows about the hardware behind a mounted volume.

    A DJI drone and a DJI Osmo write identical folder trees, so the card itself
    can never tell them apart. Plugged in as devices rather than as loose cards
    they are different pieces of USB hardware, and macOS reports the product
    string — which is the one signal that does distinguish them.
    """
    volume_name: str = ""
    media_name: str = ""       # e.g. "Osmo Action 4 Media" — the device's own name
    protocol: str = ""         # "USB" for a plugged-in camera, "Apple Fabric" internally
    usb_product: str = ""
    usb_vendor: str = ""

    @property
    def described(self) -> str:
        parts = [p for p in (self.usb_product, self.media_name, self.protocol) if p]
        return " / ".join(parts) or "not reported"

    @property
    def is_removable_device(self) -> bool:
        return self.protocol.upper() == "USB"


def device_identity(mount_point: Path) -> DeviceIdentity:
    """Ask macOS what hardware a volume is sitting on. Never raises."""
    fields: dict[str, str] = {}
    try:
        result = subprocess.run(["diskutil", "info", str(mount_point)],
                                capture_output=True, text=True, timeout=20)
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()
    except (OSError, subprocess.SubprocessError):
        return DeviceIdentity(volume_name=mount_point.name)

    identifier = fields.get("device identifier", "")
    usb_product = usb_vendor = ""
    if identifier:
        # Correlate the BSD disk back to a USB device to recover its product name.
        base = re.sub(r"s\d+.*$", "", identifier)
        try:
            usb = subprocess.run(["system_profiler", "SPUSBDataType", "-json"],
                                 capture_output=True, text=True, timeout=30)
            usb_product, usb_vendor = _find_usb_device(json.loads(usb.stdout), base)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    return DeviceIdentity(
        volume_name=fields.get("volume name", mount_point.name),
        media_name=fields.get("device / media name", ""),
        protocol=fields.get("protocol", ""),
        usb_product=usb_product,
        usb_vendor=usb_vendor,
    )


def _find_usb_device(payload: dict, bsd_name: str) -> tuple[str, str]:
    """Walk the USB tree looking for whichever device owns this disk."""
    def walk(node: dict) -> tuple[str, str] | None:
        media = node.get("Media") or []
        if any(entry.get("bsd_name") == bsd_name for entry in media if isinstance(entry, dict)):
            return node.get("_name", ""), node.get("manufacturer", "")
        if node.get("bsd_name") == bsd_name:
            return node.get("_name", ""), node.get("manufacturer", "")
        for child in node.get("_items", []):
            found = walk(child)
            if found:
                return found
        return None

    for controller in payload.get("SPUSBDataType", []):
        found = walk(controller)
        if found:
            return found
    return "", ""


@dataclass(frozen=True)
class SourceKind:
    kind: str     # "offloaded" | "card" | "unclear"
    reason: str = ""


def classify_source(root: Path) -> SourceKind:
    """Decide what kind of drive this is, or refuse to decide.

    Everything downstream depends on getting this right: an offloaded tree is
    mirrored verbatim, a card is wrapped under a dated folder. Guessing wrong on
    a working drive mirrors terabytes into one nonsense folder, so anything that
    is not plainly one or the other is refused and handed back to the human.
    """
    try:
        entries = [item for item in root.iterdir() if not item.name.startswith(".")]
    except OSError as exc:
        return SourceKind("unclear", f"the drive could not be read ({exc})")

    directories = [item for item in entries if item.is_dir()]
    if any(DATED_FOLDER.match(item.name) for item in directories):
        return SourceKind("offloaded")

    # A dated tree hiding one level down means this is an archive, not a card -
    # but only the human knows whether to mirror from here or from inside it.
    for item in directories:
        try:
            nested = [child.name for child in item.iterdir()
                      if child.is_dir() and DATED_FOLDER.match(child.name)]
        except OSError:
            continue
        if nested:
            return SourceKind("unclear", (
                f"dated folders were found inside '{item.name}' rather than at the top "
                f"of the drive (for example '{nested[0]}'). If '{item.name}' is the "
                f"archive, add that folder as the source instead of the whole drive."
            ))

    unexpected = [item.name for item in directories if item.name.upper() not in CARD_TOP_LEVEL]
    if unexpected:
        listed = ", ".join(f"'{name}'" for name in sorted(unexpected)[:4])
        more = f" and {len(unexpected) - 4} more" if len(unexpected) > 4 else ""
        return SourceKind("unclear", (
            f"this does not look like a camera card: it holds {listed}{more} at its top "
            f"level, which no camera creates. If it is a working drive, point the "
            f"guardian at the dated folder inside it instead."
        ))

    return SourceKind("card")


def date_from_footage(files: tuple[Path, ...]) -> str:
    """Date the card by its earliest clip, so a card read after midnight still files correctly."""
    earliest = None
    for path in files:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if earliest is None or stamp < earliest:
            earliest = stamp
    moment = datetime.fromtimestamp(earliest if earliest else datetime.now().timestamp())
    return f"{moment.month}-{moment.day}-{moment.strftime('%y')}"


def normalize_offload_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value.strip(), "%m-%d-%y")
    except ValueError as exc:
        raise RuntimeError("Offload date must use M-D-YY, for example 8-10-26") from exc
    return f"{parsed.month}-{parsed.day}-{parsed.strftime('%y')}"


def visible_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for directory, names, filenames in os.walk(root):
        names[:] = [name for name in names if not name.startswith(".") and name not in {"System Volume Information"}]
        for filename in filenames:
            if not filename.startswith(".") and not filename.endswith(".footage-guardian-part"):
                result.append(Path(directory) / filename)
    return sorted(result, key=lambda item: item.relative_to(root).as_posix().lower())


def inspect_card(root: Path, manifest: Manifest | None = None) -> CardInfo:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("The camera card is not available")
    files = visible_files(root)
    if not files:
        raise RuntimeError("No visible files were found on this card")
    relative_upper = [path.relative_to(root).as_posix().upper() for path in files]
    folder_markers: set[str] = set()
    for item in relative_upper:
        parts = item.split("/")[:-1]
        for depth in range(1, min(len(parts), 3) + 1):
            folder_markers.add("/".join(parts[:depth]))
    signature = "+".join(sorted(folder_markers)[:40]) or root.name.upper()
    suggested = "Unknown camera"
    if any(Path(item).suffix.lower() in {".insv", ".insp"} for item in relative_upper):
        suggested = "360"
    for camera, patterns in CAMERA_PATTERNS:
        if suggested == "Unknown camera" and any(any(item.startswith(pattern) for item in relative_upper) for pattern in patterns):
            suggested = camera
            break
    alternatives: tuple[str, ...] = ()
    for patterns, options in AMBIGUOUS_TREES:
        if any(any(item.startswith(pattern) for item in relative_upper) for pattern in patterns):
            alternatives = options
            break

    # The camera itself, plugged in, names itself - and that outranks anything the
    # folders imply. This is what makes a drone and an Osmo distinguishable at
    # all, so when it answers there is nothing left to confirm.
    identity = device_identity(root)
    from_hardware = camera_from_device_name(identity.usb_product or identity.media_name,
                                            identity.usb_vendor)
    if from_hardware:
        suggested, alternatives = from_hardware, ()

    digest = hashlib.sha256()
    total = 0
    for path in files:
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0" + str(stat.st_size).encode() + b"\0")
        total += stat.st_size
    fingerprint = digest.hexdigest()

    if manifest:
        # A confirmation for this physical card always wins, and settles the
        # ambiguity. The signature lookup deliberately cannot: an Osmo and a drone
        # share one signature, so learning from it would mislabel the other camera.
        confirmed = manifest.confirmed_card_camera(fingerprint)
        if confirmed:
            suggested, alternatives = confirmed, ()
        elif not alternatives:
            with manifest.connect() as db:
                learned = db.execute("SELECT camera_name FROM camera_signatures WHERE signature=?",
                                     (signature,)).fetchone()
            if learned:
                suggested = learned["camera_name"]

    return CardInfo(root, root.name, signature, fingerprint, suggested, tuple(files), total, alternatives)


class CardIngester:
    def __init__(self, manifest: Manifest):
        self.manifest = manifest

    @staticmethod
    def mounted_cards(volumes_root: Path = Path("/Volumes")) -> list[Path]:
        """Every drive a human plugged in, and nothing macOS mounted for itself."""
        if not volumes_root.is_dir():
            return []
        # Only the real /Volumes carries the boot drive; a temporary test root
        # sits on it, so asking there would filter everything away.
        boot = boot_device() if volumes_root == Path("/Volumes") else None
        candidates = []
        for item in volumes_root.iterdir():
            try:
                if not item.is_dir() or is_system_volume(item, boot):
                    continue
            except OSError:
                continue
            candidates.append(item)
        return sorted(candidates, key=lambda p: p.name.lower())

    def prior_ingest(self, fingerprint: str):
        with self.manifest.connect() as db:
            return db.execute("SELECT * FROM card_ingests WHERE card_fingerprint=?", (fingerprint,)).fetchone()

    def offload(self, card: CardInfo, destination_root: Path, offload_date: str,
                camera_name: str, card_slot: str = "",
                progress: Callable[[int, int, str], None] | None = None) -> Path:
        destination_root = destination_root.expanduser().resolve()
        if not destination_root.is_dir():
            raise RuntimeError("Connect and choose the destination SSD first")
        camera_name = safe_component(camera_name)
        if camera_name == "Unnamed" or camera_name == "Unknown camera":
            raise RuntimeError("Confirm which camera this card belongs to")
        dated_folder = normalize_offload_date(offload_date)
        if camera_name == "Main Cam":
            if card_slot not in {"card 1", "card 2"}:
                raise RuntimeError("Choose card 1 or card 2 for Main Cam")
            destination = destination_root / dated_folder / camera_name / card_slot
        else:
            destination = destination_root / dated_folder / camera_name
        prior = self.prior_ingest(card.fingerprint)
        if prior and prior["state"] == "VERIFIED":
            raise RuntimeError(f"This exact card was already verified at {prior['destination_path']}")
        with self.manifest.connect() as db:
            db.execute("""INSERT INTO camera_signatures(signature, camera_name) VALUES (?, ?)
                ON CONFLICT(signature) DO UPDATE SET camera_name=excluded.camera_name, updated_at=CURRENT_TIMESTAMP""",
                (card.signature, camera_name))
            db.execute("""INSERT INTO card_ingests
                (card_fingerprint, volume_name, camera_name, destination_path, file_count, total_bytes, state, detail)
                VALUES (?, ?, ?, ?, ?, ?, 'TRANSFERRING', 'Verified copy in progress')
                ON CONFLICT(card_fingerprint) DO UPDATE SET camera_name=excluded.camera_name,
                destination_path=excluded.destination_path, state='TRANSFERRING', detail=excluded.detail,
                updated_at=CURRENT_TIMESTAMP""",
                (card.fingerprint, card.volume_name, camera_name, str(destination), len(card.files), card.total_bytes))
        # Three passes over every byte: hash the source, copy it, re-read
        # the copy to verify it. Weighting by that keeps the bar counting
        # footage rather than disk reads, and keeps it moving inside a
        # single 18GB clip instead of once per file.
        bar = ByteProgress(card.total_bytes, passes=3, report=progress)
        for index, source in enumerate(card.files, 1):
            relative = source.relative_to(card.root)
            target = destination / relative
            bar.label(f"{index}/{len(card.files)}  {relative.as_posix()}")
            source_hash = md5_file(source, bar.add)
            if not verified_copy_exists(target, source.stat().st_size, source_hash):
                refuse_if_occupied(target)
                copy_verified(source, target, source_hash, bar.add)
        bar.finished(f"{len(card.files)} files verified")
        verified = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.manifest.connect() as db:
            db.execute("""UPDATE card_ingests SET state='VERIFIED', detail='Every file size and MD5 verified',
                verified_at=?, updated_at=CURRENT_TIMESTAMP WHERE card_fingerprint=?""", (verified, card.fingerprint))
        self.manifest.event("INFO", f"Card offload verified: {card.card_label} to {destination}")
        return destination

    @staticmethod
    def eject(card_root: Path) -> None:
        resolved = card_root.expanduser().resolve()
        volumes = Path("/Volumes").resolve()
        try:
            relative = resolved.relative_to(volumes)
        except ValueError as exc:
            raise RuntimeError("Only a mounted volume under /Volumes can be ejected") from exc
        if len(relative.parts) != 1:
            raise RuntimeError("Could not identify the card's mounted volume")
        result = subprocess.run(["diskutil", "eject", str(resolved)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "macOS could not eject the card")


def describe_mounted_devices(ssd: Path | None = None,
                             manifest: Manifest | None = None,
                             volumes_root: Path = Path("/Volumes")) -> list[tuple[str, str, str]]:
    """One row per plugged-in drive: (volume, detected as, note).

    Deliberately cheap before it is thorough. `classify_source` reads only the
    top two levels of a drive, so a working drive or an already-organised
    archive is described without ever being walked. Only something that already
    looks like a camera card earns the full inspection, which walks every file
    and asks macOS about the hardware behind the volume.
    """
    rows: list[tuple[str, str, str]] = []
    for volume in CardIngester.mounted_cards(volumes_root):
        if ssd is not None:
            try:
                if volume.resolve() == ssd.resolve():
                    continue
            except OSError:
                pass
        kind = classify_source(volume)
        if kind.kind == "offloaded":
            rows.append((volume.name, "—", "already organised by day — not a camera card"))
            continue
        if kind.kind == "unclear":
            rows.append((volume.name, "—", kind.reason))
            continue
        try:
            card = inspect_card(volume, manifest)
            camera, note = card.suggested_camera, f"{len(card.files):,} files"
            if card.alternatives:
                camera = "?"
                note = ("could be " + " or ".join(card.alternatives) +
                        " — plug the camera in rather than its card")
        except RuntimeError as exc:
            camera, note = "—", str(exc)
        rows.append((volume.name, camera, note))
    return rows
