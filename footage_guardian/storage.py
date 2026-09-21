from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


CHUNK = 8 * 1024 * 1024


def safe_component(value: str) -> str:
    """Reduce a name to something that cannot escape its folder."""
    value = value.strip().replace("/", "-").replace(":", "-")
    return value or "Unnamed"


def md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def remote_hash(entry: dict, algorithm: str = "md5") -> str:
    """Read a hash out of an rclone lsjson entry whatever case the key uses.

    Real rclone emits lowercase "md5". Looking only for "MD5" found nothing, so
    every cloud check silently downgraded to size-only — including the ones
    guarding local deletion. Match case-insensitively so that cannot recur.
    """
    hashes = entry.get("Hashes") or {}
    for key, value in hashes.items():
        if key.lower() == algorithm.lower():
            return (value or "").strip().lower()
    return ""


def verified_copy_exists(destination: Path, size: int, expected_md5: str) -> bool:
    """True when destination already holds exactly this content."""
    return destination.is_file() and destination.stat().st_size == size and md5_file(destination) == expected_md5


def refuse_if_occupied(destination: Path) -> None:
    """Never overwrite footage already filed under this name.

    Callers check verified_copy_exists first, so anything still sitting here is
    different content. Two clips claiming one path is a naming collision a human
    has to resolve; silently replacing one of them would destroy the only backup.
    """
    if destination.exists():
        raise RuntimeError(
            f"Different footage is already backed up at {destination}. "
            "Two files claim the same path; resolve this before continuing."
        )


def copy_verified(source: Path, destination: Path, expected_md5: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".footage-guardian-part")
    if partial.exists():
        partial.unlink()
    try:
        with source.open("rb") as src, partial.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, partial)
        if partial.stat().st_size != source.stat().st_size or md5_file(partial) != expected_md5:
            raise IOError("Backup verification failed")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


AUTH_HINTS = ("empty token", "oauth", "token expired", "invalid_grant", "unauthenticated", "reconnect")
OFFLINE_HINTS = ("no such host", "connection refused", "dial tcp", "i/o timeout",
                 "network is unreachable", "no route to host")
LEVEL_PREFIX = re.compile(r"^\d{4}/\d{2}/\d{2} [\d:]+ (?:CRITICAL|ERROR|FATAL|Failed):\s*")


def readable_rclone_error(output: str, remote: str) -> str:
    """Turn an rclone dump into one line a videographer can act on.

    Kevin reads this in the DETAIL column mid-shoot. It has to say what is wrong
    and what to do, not what rclone's internals were doing at the time.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    # Deprecation notices are noise; they are never why an upload failed, so a
    # run that produced nothing else has genuinely explained nothing.
    meaningful = [line for line in lines if "NOTICE:" not in line]
    haystack = " ".join(meaningful).lower()
    account = remote.split(":")[0] or "gdrive"

    if any(hint in haystack for hint in AUTH_HINTS):
        return f"Google Drive sign-in has expired. In Terminal run: rclone config reconnect {account}:"
    if any(hint in haystack for hint in OFFLINE_HINTS):
        return "Cannot reach Google Drive right now. It will keep trying."
    if "quota" in haystack or "insufficient" in haystack:
        return "Google Drive is out of space. Free some up and it will try again."
    if not meaningful:
        return "Google Drive upload failed without explanation."
    return LEVEL_PREFIX.sub("", meaningful[-1])[:300]


class Rclone:
    def __init__(self, executable: str = "rclone"):
        self.executable = executable

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def reachable(self, remote: str) -> str:
        """Empty string when the remote answers, otherwise why it does not.

        Checked once before a run so a dead sign-in fails immediately instead of
        once per file.
        """
        account = remote.split(":")[0] + ":"
        result = subprocess.run([self.executable, "about", account], capture_output=True, text=True)
        return "" if result.returncode == 0 else readable_rclone_error(result.stderr or result.stdout, remote)

    def upload(self, source: Path, remote_path: str) -> None:
        command = [
            self.executable, "copyto", str(source), remote_path,
            "--checksum", "--drive-chunk-size", "64M", "--retries", "10",
            "--low-level-retries", "20", "--retries-sleep", "10s",
            "--timeout", "5m", "--contimeout", "30s", "--stats", "15s",
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(readable_rclone_error(result.stderr or result.stdout, remote_path))

    def verify(self, remote_path: str, size: int, md5: str, require_checksum: bool = False) -> str:
        """Confirm the remote object really holds this content.

        Set require_checksum when a matching size is not good enough — before
        deleting a local backup, a size match alone proves nothing about bytes.
        """
        result = subprocess.run(
            [self.executable, "lsjson", remote_path, "--files-only", "--hash"],
            capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError(readable_rclone_error(result.stderr or result.stdout, remote_path))
        entries = json.loads(result.stdout)
        if len(entries) != 1 or int(entries[0].get("Size", -1)) != size:
            raise RuntimeError("Cloud size verification failed")
        remote_md5 = remote_hash(entries[0])
        if remote_md5 and remote_md5 != md5.lower():
            raise RuntimeError("Cloud checksum verification failed")
        if not remote_md5:
            if require_checksum:
                raise RuntimeError(
                    "Google Drive did not report a checksum for this file, so its contents "
                    "cannot be confirmed. Refusing to clear the local backup."
                )
            return "remote size"
        return "MD5 checksum and size"
