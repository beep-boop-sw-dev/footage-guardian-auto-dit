from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    md5 TEXT,
    backup_path TEXT,
    remote_path TEXT,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    clearance_at TEXT,
    backup_removed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS files_identity ON files(size, md5);
CREATE INDEX IF NOT EXISTS files_state ON files(state);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS card_ingests (
    id INTEGER PRIMARY KEY,
    card_fingerprint TEXT NOT NULL UNIQUE,
    volume_name TEXT NOT NULL,
    camera_name TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS source_labels (
    source_path TEXT PRIMARY KEY,
    card_fingerprint TEXT,
    archive_prefix TEXT NOT NULL,
    camera_name TEXT NOT NULL DEFAULT '',
    offload_date TEXT NOT NULL DEFAULT '',
    card_slot TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS source_labels_fingerprint ON source_labels(card_fingerprint);
CREATE TABLE IF NOT EXISTS camera_signatures (
    signature TEXT PRIMARY KEY,
    camera_name TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Keyed on the card's own fingerprint, not its folder signature. A DJI drone and
-- a DJI Osmo write identical trees, so they share a signature; only the card's
-- contents tell them apart. Kevin's answer is recorded per physical card.
CREATE TABLE IF NOT EXISTS card_cameras (
    card_fingerprint TEXT PRIMARY KEY,
    camera_name TEXT NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS meta_imports (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    md5 TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    state TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(size, md5, destination_path)
);
"""


class Manifest:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(files)")}
            if "clearance_at" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN clearance_at TEXT")
            if "backup_removed_at" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN backup_removed_at TEXT")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def discover(self, source_name: str, source_path: str, relative_path: str,
                 size: int, mtime_ns: int) -> int:
        with self.connect() as db:
            row = db.execute("SELECT id, size, mtime_ns FROM files WHERE source_path=?", (source_path,)).fetchone()
            if row:
                if row["size"] != size or row["mtime_ns"] != mtime_ns:
                    db.execute("""UPDATE files SET size=?, mtime_ns=?, md5=NULL, state='WAITING',
                                detail='File changed; waiting until stable', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                               (size, mtime_ns, row["id"]))
                return int(row["id"])
            cursor = db.execute("""INSERT INTO files
                (source_name, source_path, relative_path, size, mtime_ns, state, detail)
                VALUES (?, ?, ?, ?, ?, 'WAITING', 'Waiting until file is stable')""",
                (source_name, source_path, relative_path, size, mtime_ns))
            return int(cursor.lastrowid)

    def update(self, file_id: int, state: str, detail: str = "", **values: object) -> None:
        allowed = {"md5", "backup_path", "remote_path", "attempts", "clearance_at", "backup_removed_at"}
        assignments = ["state=?", "detail=?", "updated_at=CURRENT_TIMESTAMP"]
        params: list[object] = [state, detail]
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"Invalid manifest field: {key}")
            assignments.append(f"{key}=?")
            params.append(value)
        params.append(file_id)
        with self.connect() as db:
            db.execute(f"UPDATE files SET {', '.join(assignments)} WHERE id=?", params)

    def get(self, file_id: int) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise KeyError(file_id)
        return row

    def rows(self, limit: int = 1000) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute("SELECT * FROM files ORDER BY updated_at DESC LIMIT ?", (limit,)))

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute("SELECT state, COUNT(*) AS count FROM files GROUP BY state").fetchall()
        return {row["state"]: row["count"] for row in rows}

    def duplicate_of(self, file_id: int, size: int, md5: str) -> sqlite3.Row | None:
        """Find identical content at another path without conflating the archive paths."""
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM files WHERE id<>? AND size=? AND md5=? ORDER BY id LIMIT 1",
                (file_id, size, md5),
            ).fetchone()

    def remote_claimed_by(self, file_id: int, remote_path: str, md5: str) -> sqlite3.Row | None:
        """Find another file already uploaded to this remote path with different content."""
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM files WHERE id<>? AND remote_path=? AND md5 IS NOT NULL AND md5<>? ORDER BY id LIMIT 1",
                (file_id, remote_path, md5),
            ).fetchone()

    def synced_under(self, day: str) -> int:
        """How many files of this shoot day are verified in Google Drive.

        Counted from the remote path rather than the local one, because that is
        the thing actually proven to exist in Drive.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM files WHERE state IN ('SAFE','CLEAR TO REMOVE','CLOUD SAFE') "
                "AND remote_path LIKE ?", (f"%/{day}/%",)).fetchone()
        return int(row["n"]) if row else 0

    def confirmed_card_camera(self, fingerprint: str) -> str:
        """The camera Kevin confirmed for this physical card, or '' if never asked."""
        with self.connect() as db:
            row = db.execute("SELECT camera_name FROM card_cameras WHERE card_fingerprint=?",
                             (fingerprint,)).fetchone()
        return row["camera_name"] if row else ""

    def confirm_card_camera(self, fingerprint: str, camera_name: str) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO card_cameras(card_fingerprint, camera_name) VALUES (?, ?)
                ON CONFLICT(card_fingerprint) DO UPDATE SET camera_name=excluded.camera_name,
                confirmed_at=CURRENT_TIMESTAMP""", (fingerprint, camera_name))

    def source_label(self, source_path: str, fingerprint: str | None = None) -> sqlite3.Row | None:
        """Recall how this drive was filed, by path or by the card's own fingerprint.

        The fingerprint lookup is what lets the same card remounted at a different
        path keep the slot it was already given.
        """
        with self.connect() as db:
            row = db.execute("SELECT * FROM source_labels WHERE source_path=?", (source_path,)).fetchone()
            if row or not fingerprint:
                return row
            return db.execute(
                "SELECT * FROM source_labels WHERE card_fingerprint=? ORDER BY created_at LIMIT 1",
                (fingerprint,),
            ).fetchone()

    def save_source_label(self, source_path: str, fingerprint: str, archive_prefix: str,
                          camera_name: str = "", offload_date: str = "", card_slot: str = "") -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO source_labels
                (source_path, card_fingerprint, archive_prefix, camera_name, offload_date, card_slot)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET card_fingerprint=excluded.card_fingerprint,
                archive_prefix=excluded.archive_prefix, camera_name=excluded.camera_name,
                offload_date=excluded.offload_date, card_slot=excluded.card_slot""",
                (source_path, fingerprint, archive_prefix, camera_name, offload_date, card_slot))

    def other_cards_on(self, offload_date: str, camera_name: str, fingerprint: str) -> int:
        """How many different cards are already filed under this date and camera."""
        with self.connect() as db:
            row = db.execute(
                """SELECT COUNT(DISTINCT card_fingerprint) AS count FROM source_labels
                   WHERE offload_date=? AND camera_name=? AND card_fingerprint<>?""",
                (offload_date, camera_name, fingerprint),
            ).fetchone()
        return int(row["count"])

    def event(self, level: str, message: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(level, message) VALUES (?, ?)", (level, message))
