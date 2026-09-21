from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ingest import normalize_offload_date
from .manifest import Manifest
from .storage import copy_verified, md5_file


META_EXTENSIONS = {".mp4", ".mov", ".m4v", ".hevc", ".jpg", ".jpeg"}


@dataclass(frozen=True)
class MetaCandidate:
    path: Path
    size: int
    modified: float


def find_recent_meta(downloads: Path, days: int = 7) -> list[MetaCandidate]:
    downloads = downloads.expanduser()
    if not downloads.is_dir():
        raise RuntimeError("Downloads folder is not available")
    cutoff = time.time() - max(1, days) * 86400
    candidates = []
    for path in downloads.iterdir():
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file() and path.suffix.lower() in META_EXTENSIONS and stat.st_mtime >= cutoff:
            candidates.append(MetaCandidate(path, stat.st_size, stat.st_mtime))
    return sorted(candidates, key=lambda item: item.modified, reverse=True)


class MetaImporter:
    def __init__(self, manifest: Manifest):
        self.manifest = manifest

    def import_files(self, candidates: list[MetaCandidate], destination_root: Path, offload_date: str,
                     progress: Callable[[int, int, str], None] | None = None) -> Path:
        destination_root = destination_root.expanduser().resolve()
        if not destination_root.is_dir():
            raise RuntimeError("Connect and choose the destination SSD first")
        destination = destination_root / normalize_offload_date(offload_date) / "meta glasses"
        total = sum(item.size for item in candidates)
        completed = 0
        for index, item in enumerate(candidates, 1):
            digest = md5_file(item.path)
            target = self._safe_target(destination, item.path.name, item.size, digest)
            if not target.exists():
                copy_verified(item.path, target, digest)
            with self.manifest.connect() as db:
                db.execute("""INSERT OR IGNORE INTO meta_imports
                    (source_path, size, md5, destination_path, state) VALUES (?, ?, ?, ?, 'VERIFIED')""",
                    (str(item.path), item.size, digest, str(target)))
            completed += item.size
            if progress:
                progress(completed, total, f"{index}/{len(candidates)}  {item.path.name}")
        self.manifest.event("INFO", f"Meta glasses import verified: {len(candidates)} files to {destination}")
        return destination

    @staticmethod
    def _safe_target(folder: Path, filename: str, size: int, digest: str) -> Path:
        target = folder / filename
        if not target.exists():
            return target
        if target.is_file() and target.stat().st_size == size and md5_file(target) == digest:
            return target
        counter = 2
        while True:
            candidate = folder / f"{target.stem}-{counter}{target.suffix}"
            if not candidate.exists():
                return candidate
            if candidate.is_file() and candidate.stat().st_size == size and md5_file(candidate) == digest:
                return candidate
            counter += 1
