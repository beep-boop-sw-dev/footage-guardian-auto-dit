"""Stage three across every folder: the drives into one master Drive folder.

Three layers, because the fake-rclone lesson in CLAUDE.md applies here:

1. A stand-in cloud backed by a local folder, for every rule.
2. The parsers, fed lines captured from real rclone 1.75 on 2026-10-08.
3. The real Rclone class and the real rclone binary, uploading into a
   local folder through rclone's ":local:" remote — real listing, real
   --ignore-existing, real MD5s — skipped only if rclone is not installed.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from footage_guardian.cloud_sync import cloud_rows, cloud_summary, run_sync, scan_cloud
from footage_guardian.config import Config, Source
from footage_guardian.engine import Guardian
from footage_guardian.manifest import Manifest
from footage_guardian.storage import Rclone, json_log_errors, parse_stats_bytes, parse_tree

# Captured from real rclone 1.75 (copyto --use-json-log --stats 1s), trimmed
# of fields nothing reads. Invented file name.
REAL_STATS = [
    '{"time":"2026-10-08T14:16:30.1-07:00","level":"notice","msg":"\\nTransferred: ...","stats":{"bytes":8417280,"checks":0,"elapsedTime":1.0,"errors":0,"eta":null,"speed":0,"totalBytes":30000000,"totalTransfers":1,"transfers":0,"transferring":[{"bytes":8417280,"name":"Story of us.MOV","size":30000000}]},"source":"accounting/stats.go:551"}',
    '{"time":"2026-10-08T14:16:31.1-07:00","level":"notice","msg":"\\nTransferred: ...","stats":{"bytes":16805888,"checks":0,"elapsedTime":2.0,"errors":0,"eta":1,"speed":8416440.45,"totalBytes":30000000,"totalTransfers":1,"transfers":0},"source":"accounting/stats.go:551"}',
    '{"time":"2026-10-08T14:16:33.1-07:00","level":"notice","msg":"\\nTransferred: ...","stats":{"bytes":30000000,"checks":0,"elapsedTime":3.6,"errors":0,"eta":0,"speed":8398048.6,"totalBytes":30000000,"totalTransfers":1,"transfers":1},"source":"accounting/stats.go:551"}',
]
REAL_ERROR = '{"time":"2026-10-08T14:23:46.47-07:00","level":"error","msg":"error reading source root directory: directory not found","object":"Local file system at /nonexistent/file","objectType":"*local.Fs","source":"march/march.go:568"}'
# Real Google Drive lsjson -R --files-only --hash entry (path invented).
REAL_DRIVE_ENTRY = ('[{"Path":"7:22:26/Main Cam/Disk 1/P1000001.MOV","Name":"P1000001.MOV","Size":1096144887,'
                    '"MimeType":"video/quicktime","ModTime":"2026-07-22T23:29:16.000Z","IsDir":false,'
                    '"Hashes":{"md5":"36c7b2e37b9123447525c2bdadb66fca","sha1":"b12a82d0ce76d6a78b2fbdbd67f575f5dc07c5bc"}}]')


def write(root: Path, relative: str, data: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class FolderCloud:
    """A Google Drive stand-in: a local folder, behaving like the real wrapper."""

    def __init__(self, drive: Path, free: int | None = 10 ** 12):
        self.drive, self.free = drive, free
        self.uploads: list[str] = []
        self.duplicates: dict[str, int] = {}

    def _path(self, remote: str) -> Path:
        return self.drive / remote.split(":", 1)[1].lstrip("/").split("/", 1)[1]

    def available(self): return True
    def reachable(self, remote): return ""

    def list_tree(self, remote):
        if not self.drive.is_dir():
            return None
        found = {p.relative_to(self.drive).as_posix(): [(p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest())]
                 for p in self.drive.rglob("*") if p.is_file()}
        for relative, count in self.duplicates.items():
            found[relative] = found[relative] * count
        return found

    def free_space(self, remote): return self.free

    def upload(self, source: Path, remote: str, on_bytes=None):
        target = self._path(remote)
        self.uploads.append(remote)
        if target.exists():          # --ignore-existing
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if on_bytes:
            on_bytes(target.stat().st_size, "upload")

    def verify(self, remote, size, md5, require_checksum=False):
        target = self._path(remote)
        if not target.is_file() or target.stat().st_size != size:
            raise RuntimeError("Cloud size verification failed")
        if hashlib.md5(target.read_bytes()).hexdigest() != md5:
            raise RuntimeError("Cloud checksum verification failed")
        return "MD5 checksum and size"


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ssd, self.one, self.two = (self.root / n for n in ("SSD", "HDD1", "HDD2"))
        for drive in (self.ssd, self.one, self.two):
            drive.mkdir()
        self.drive = self.root / "Drive" / "Master"
        self.drive.mkdir(parents=True)
        self.cloud = FolderCloud(self.drive)
        self.base = "gdrive:Master"

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self):
        return scan_cloud(self.cloud, self.base, self.ssd, [self.one, self.two])

    def test_everything_on_any_drive_reaches_drive_and_is_verified(self):
        write(self.ssd, "10-8-26/360/A.insv", b"new" * 400)
        write(self.one, "8-21-26/Main Cam/B.MOV", b"older, cleared from the SSD" * 400)
        write(self.two, "8-21-26/Main Cam/B.MOV", b"older, cleared from the SSD" * 400)
        before = {d: tree(d) for d in (self.ssd, self.one, self.two)}
        summary = run_sync(self.plan(), self.cloud)
        self.assertEqual(summary["failures"], [])
        self.assertEqual(summary["uploaded"], 2)
        self.assertEqual(set(tree(self.drive)), {"10-8-26/360/A.insv", "8-21-26/Main Cam/B.MOV"})
        self.assertEqual({d: tree(d) for d in (self.ssd, self.one, self.two)}, before,
                         "local drives are only ever read")

    def test_the_ssd_is_read_in_preference_to_an_hdd(self):
        write(self.ssd, "10-8-26/A.MOV", b"a" * 100)
        write(self.one, "10-8-26/A.MOV", b"a" * 100)
        self.assertEqual(self.plan().jobs[0].source, self.ssd / "10-8-26/A.MOV")

    def test_what_is_already_in_drive_is_not_uploaded_again(self):
        write(self.ssd, "7:22:26/A.MOV", b"july" * 100)
        write(self.drive, "7:22:26/A.MOV", b"july" * 100)
        plan = self.plan()
        self.assertEqual(plan.jobs, [])
        self.assertEqual(cloud_rows(plan)[0][-1], "✓ In Google Drive")
        self.assertIn("Google Drive holds everything", cloud_summary(plan))

    def test_a_different_file_already_in_drive_is_never_replaced(self):
        write(self.ssd, "10-8-26/A.MOV", b"the real clip" * 100)
        write(self.drive, "10-8-26/A.MOV", b"something else")
        plan = self.plan()
        self.assertEqual(plan.jobs, [])
        self.assertEqual(len(plan.conflicts), 1)
        run_sync(plan, self.cloud)
        self.assertEqual((self.drive / "10-8-26/A.MOV").read_bytes(), b"something else")

    def test_a_file_that_arrives_in_drive_after_the_scan_is_not_overwritten(self):
        write(self.ssd, "10-8-26/A.MOV", b"ours" * 100)
        plan = self.plan()
        write(self.drive, "10-8-26/A.MOV", b"theirs" * 100)      # lands meanwhile
        summary = run_sync(plan, self.cloud)
        self.assertEqual((self.drive / "10-8-26/A.MOV").read_bytes(), b"theirs" * 100)
        self.assertEqual(len(summary["failures"]), 1, "reported, not passed off as uploaded")

    def test_a_file_drive_holds_twice_is_reported_not_guessed(self):
        write(self.ssd, "10-8-26/A.MOV", b"a" * 100)
        write(self.drive, "10-8-26/A.MOV", b"a" * 100)
        self.cloud.duplicates["10-8-26/A.MOV"] = 2
        plan = self.plan()
        self.assertIn("Google Drive holds it 2 times", plan.conflicts[0])

    def test_sizes_that_differ_between_local_drives_are_not_uploaded(self):
        write(self.one, "10-8-26/A.MOV", b"a" * 100)
        write(self.two, "10-8-26/A.MOV", b"a" * 99)
        plan = self.plan()
        self.assertEqual(plan.jobs, [])
        self.assertIn("sizes differ between your drives", plan.conflicts[0])

    def test_a_mistyped_folder_is_refused_not_created(self):
        shutil.rmtree(self.drive)
        write(self.ssd, "10-8-26/A.MOV", b"a" * 100)
        plan = self.plan()
        self.assertIn("There is no folder called 'Master'", plan.blocker())
        with self.assertRaises(RuntimeError):
            run_sync(plan, self.cloud)
        self.assertFalse(self.drive.exists())

    def test_no_folder_set_says_where_to_set_it(self):
        plan = scan_cloud(self.cloud, "", self.ssd, [self.one, self.two])
        self.assertIn("Drives tab", plan.blocker())

    def test_newest_folders_go_first_and_what_does_not_fit_waits(self):
        write(self.ssd, "10-8-26/A.MOV", b"a" * 3000)
        write(self.ssd, "9-1-26/B.MOV", b"b" * 3000)
        write(self.one, "7:22:26/C.MOV", b"c" * 3000)
        self.cloud.free = 6500 + 5 * 1000 ** 3            # room for two, plus the margin
        plan = self.plan()
        self.assertEqual([j.relative.split("/")[0] for j in plan.jobs], ["10-8-26", "9-1-26"])
        self.assertEqual([j.relative.split("/")[0] for j in plan.overflow], ["7:22:26"])
        self.assertIn("will not fit in Google Drive", cloud_summary(plan))
        rows = {r[0]: r for r in cloud_rows(plan)}
        self.assertTrue(rows["7:22:26"][-1].startswith("Won't fit"))
        summary = run_sync(plan, self.cloud)
        self.assertEqual(summary["uploaded"], 2)
        self.assertFalse((self.drive / "7:22:26").exists())

    def test_a_full_drive_refuses_before_starting(self):
        write(self.ssd, "10-8-26/A.MOV", b"a" * 3000)
        self.cloud.free = 1000
        plan = self.plan()
        self.assertIn("Google Drive is full", plan.blocker())
        with self.assertRaises(RuntimeError):
            run_sync(plan, self.cloud)

    def test_stopping_stops_between_files(self):
        for n in range(5):
            write(self.ssd, f"10-8-26/{n}.MOV", bytes([n]) * 2000)
        stop = threading.Event()
        seen = []

        def progress(done, total, text):
            seen.append(done)
            if len(seen) == 2:
                stop.set()

        summary = run_sync(self.plan(), self.cloud, progress=progress, stop=stop)
        self.assertTrue(summary["stopped"])
        self.assertLess(summary["uploaded"], 5)
        self.assertEqual(run_sync(self.plan(), self.cloud)["failures"], [])
        self.assertEqual(len(tree(self.drive)), 5)

    def test_progress_is_in_bytes_and_ends_at_the_total(self):
        write(self.ssd, "10-8-26/A.MOV", b"a" * 5000)
        write(self.one, "1-1-25/B.MOV", b"b" * 3000)
        reports = []
        run_sync(self.plan(), self.cloud, progress=lambda d, t, x: reports.append((d, t)))
        self.assertEqual(reports[-1], (8000, 8000))

    def test_uploads_are_recorded_for_space_recovery(self):
        write(self.ssd, "10-8-26/A.MOV", b"a" * 100)
        manifest = Manifest(self.root / "m.db")
        run_sync(self.plan(), self.cloud, manifest)
        self.assertEqual(manifest.synced_under("10-8-26"), 1)

    def test_folders_only_in_drive_are_shown_and_left(self):
        write(self.drive, "6-1-26/A.MOV", b"only up there")
        plan = self.plan()
        self.assertEqual(cloud_rows(plan)[0][-1], "✓ Only in Google Drive (not on these drives)")
        self.assertEqual(plan.jobs, [])

    def test_the_engine_rescans_and_uploads(self):
        write(self.ssd, "10-8-26/A.MOV", b"a" * 100)
        config = Config(sources=[Source("SSD", str(self.ssd))],
                        backup_paths=[str(self.one), str(self.two)], google_destination=self.base)
        guardian = Guardian(config, Manifest(self.root / "m.db"), logging.getLogger("t"), cloud=self.cloud)
        write(self.ssd, "10-8-26/B.MOV", b"b" * 100)
        self.assertEqual(guardian.sync_everything(self.ssd)["uploaded"], 2)

    def test_a_big_upload_suggests_ethernet(self):
        write(self.ssd, "10-8-26/A.MOV", b"a")
        plan = self.plan()
        plan.jobs[0].size = 50 * 1000 ** 3
        self.assertIn("Ethernet", cloud_summary(plan))


class RealRcloneShapeTests(unittest.TestCase):
    def test_stats_lines_give_bytes_sent(self):
        self.assertEqual([parse_stats_bytes(line) for line in REAL_STATS], [8417280, 16805888, 30000000])

    def test_non_stats_lines_give_nothing(self):
        self.assertIsNone(parse_stats_bytes(REAL_ERROR))
        self.assertIsNone(parse_stats_bytes("2026/10/08 14:16:44 NOTICE: gdrive: client_id"))

    def test_errors_are_pulled_out_of_the_json_log(self):
        self.assertEqual(json_log_errors(REAL_STATS + [REAL_ERROR]),
                         "error reading source root directory: directory not found")

    def test_a_real_drive_listing_parses(self):
        parsed = parse_tree(REAL_DRIVE_ENTRY)
        self.assertEqual(parsed, {"7:22:26/Main Cam/Disk 1/P1000001.MOV":
                                  [(1096144887, "36c7b2e37b9123447525c2bdadb66fca")]})


@unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
class RealRcloneTests(unittest.TestCase):
    """The real wrapper and the real binary, into a local folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ssd, self.one = self.root / "SSD", self.root / "HDD1"
        self.ssd.mkdir()
        self.one.mkdir()
        self.drive = self.root / "Drive"
        self.drive.mkdir()
        self.base = f":local:{self.drive}"
        self.cloud = Rclone()

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_real_run_uploads_verifies_and_reports_bytes(self):
        write(self.ssd, "10-8-26/Main Cam/Story of us.MOV", b"x" * 3_000_000)
        write(self.one, "8-21-26/360/VID.insv", b"y" * 1_000_000)
        plan = scan_cloud(self.cloud, self.base, self.ssd, [self.one])
        self.assertEqual(plan.blocker(), "")
        self.assertEqual(len(plan.jobs), 2)
        reports = []
        summary = run_sync(plan, self.cloud, progress=lambda d, t, x: reports.append(d))
        self.assertEqual(summary["failures"], [])
        self.assertEqual(tree(self.drive)["10-8-26/Main Cam/Story of us.MOV"], b"x" * 3_000_000)
        self.assertEqual(reports[-1], 4_000_000)
        self.assertEqual(scan_cloud(self.cloud, self.base, self.ssd, [self.one]).jobs, [])

    def test_the_real_upload_never_replaces_a_different_file(self):
        write(self.ssd, "10-8-26/A.MOV", b"ours" * 1000)
        write(self.drive, "10-8-26/A.MOV", b"the only offsite copy")
        with self.assertRaises(RuntimeError):
            self.cloud.upload(self.ssd / "10-8-26/A.MOV", f"{self.base}/10-8-26/A.MOV")
            self.cloud.verify(f"{self.base}/10-8-26/A.MOV", 4000,
                              hashlib.md5(b"ours" * 1000).hexdigest(), require_checksum=True)
        self.assertEqual((self.drive / "10-8-26/A.MOV").read_bytes(), b"the only offsite copy")

    def test_a_missing_folder_is_none_not_an_empty_listing(self):
        self.assertIsNone(self.cloud.list_tree(f":local:{self.root / 'No such folder'}"))
        self.assertEqual(self.cloud.list_tree(self.base), {})


if __name__ == "__main__":
    unittest.main()
