from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


APP_DIR = Path.home() / "Library" / "Application Support" / "Footage Guardian Auto DIT"


@dataclass
class Source:
    name: str
    path: str


@dataclass
class Config:
    sources: list[Source] = field(default_factory=list)
    # Kevin backs a shoot up to two hard drives, not one. backup_path is the older
    # single-drive setting and is still honoured so an existing config keeps
    # working; backup_paths supersedes it.
    backup_path: str = ""
    backup_paths: list[str] = field(default_factory=list)
    google_destination: str = "gdrive:Footage Guardian Auto DIT"
    scan_interval_seconds: int = 30
    stable_seconds: int = 30
    video_extensions: list[str] = field(default_factory=lambda: [
        ".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".braw", ".r3d",
        ".crm", ".ari", ".wav", ".mp3", ".jpg", ".jpeg", ".png", ".xml",
        ".xmp", ".srt", ".thm", ".lrv",
    ])

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["sources"] = [Source(**item) for item in raw.get("sources", [])]
        # Ignore settings written by an older version rather than refusing to start.
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})

    def backup_roots(self) -> list[Path]:
        """Every configured backup drive, newest setting first, oldest honoured."""
        raw = list(self.backup_paths) if self.backup_paths else []
        if self.backup_path and self.backup_path not in raw:
            raw.append(self.backup_path)
        roots: list[Path] = []
        for item in raw:
            if not item.strip():
                continue
            resolved = Path(item).expanduser()
            if resolved not in roots:
                roots.append(resolved)
        return roots

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temporary.replace(path)


def default_paths() -> tuple[Path, Path, Path]:
    return APP_DIR / "config.json", APP_DIR / "manifest.sqlite3", APP_DIR / "guardian.log"

