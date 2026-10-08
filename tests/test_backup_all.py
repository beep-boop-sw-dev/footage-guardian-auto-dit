from __future__ import annotations

import logging
import tempfile
import threading
import unittest
from pathlib import Path

from footage_guardian.backup import (
    day_sort_key,
    plan_rows,
    plan_summary,
    run_backup,
    scan_backups,
)
from footage_guardian.config import Config, Source
from footage_guardian.engine import Guardian
from footage_guardian.manifest import Manifest


def write(root: Path, relative: str, data: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file's bytes and mtime — proof a drive was not touched."""
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


class BackupEverythingTests(unittest.TestCase):
    """Stage two across every day: the SSD onto both HDDs, and the HDDs
    into mirrors. The SSD is 4TB and the HDDs 8TB, so older days live only
    on the HDDs and must be left alone — and evened up between them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ssd, self.one, self.two = (self.root / n for n in ("SSD", "HDD1", "HDD2"))
        for drive in (self.ssd, self.one, self.two):
            drive.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self):
        return scan_backups(self.ssd, [self.one, self.two])

    def test_a_new_day_on_the_ssd_lands_on_both_hdds(self):
        write(self.ssd, "10-8-26/Main Cam/Disk 1/P1.MOV", b"clip one" * 500)
        write(self.ssd, "10-8-26/Drone/DCIM/DJI_0001.MP4", b"aerial" * 500)
        before = snapshot(self.ssd)
        summary = run_backup(self.plan())
        self.assertEqual(summary["copied"], 4)
        self.assertEqual(summary["failures"], [])
        for drive in (self.one, self.two):
            self.assertEqual(files(drive), files(self.ssd))
            for relative in files(self.ssd):
                self.assertEqual((drive / relative).read_bytes(), (self.ssd / relative).read_bytes())
        self.assertEqual(snapshot(self.ssd), before, "the SSD is only ever read")

    def test_old_footage_on_one_hdd_is_copied_to_the_other(self):
        write(self.one, "3-2-25/Main Cam/Disk 1/OLD.MOV", b"old shoot" * 500)
        before_one = snapshot(self.one)
        summary = run_backup(self.plan())
        self.assertEqual(summary["copied"], 1)
        self.assertEqual((self.two / "3-2-25/Main Cam/Disk 1/OLD.MOV").read_bytes(), b"old shoot" * 500)
        self.assertEqual(snapshot(self.one), before_one, "the HDD copied from is only read")
        self.assertEqual(files(self.ssd), set(), "old footage is never put back on the SSD")

    def test_old_footage_already_on_both_hdds_is_left_alone(self):
        for drive in (self.one, self.two):
            write(drive, "1-5-25/360/VID.insv", b"archived" * 300)
        before = (snapshot(self.one), snapshot(self.two))
        plan = self.plan()
        self.assertEqual(plan.jobs, [])
        self.assertTrue(plan.days[0].complete)
        run_backup(plan)
        self.assertEqual((snapshot(self.one), snapshot(self.two)), before)

    def test_a_day_partly_cleared_from_the_ssd_is_not_refilled_on_the_ssd(self):
        write(self.ssd, "9-1-26/Main Cam/Disk 1/KEPT.MOV", b"kept" * 300)
        for drive in (self.one, self.two):
            write(drive, "9-1-26/Main Cam/Disk 1/KEPT.MOV", b"kept" * 300)
            write(drive, "9-1-26/Main Cam/Disk 1/CLEARED.MOV", b"cleared" * 300)
        plan = self.plan()
        self.assertEqual(plan.jobs, [])
        run_backup(plan)
        self.assertEqual(files(self.ssd), {"9-1-26/Main Cam/Disk 1/KEPT.MOV"})

    def test_only_the_missing_copy_is_made_and_it_comes_from_the_ssd(self):
        write(self.ssd, "10-1-26/360/A.insv", b"a" * 900)
        write(self.ssd, "10-1-26/360/B.insv", b"b" * 900)
        write(self.one, "10-1-26/360/A.insv", b"a" * 900)
        write(self.one, "10-1-26/360/B.insv", b"b" * 900)
        write(self.two, "10-1-26/360/A.insv", b"a" * 900)
        plan = self.plan()
        self.assertEqual(len(plan.jobs), 1)
        self.assertEqual(plan.jobs[0].destinations, [self.two])
        self.assertEqual(plan.jobs[0].source, self.ssd / "10-1-26/360/B.insv")
        self.assertEqual(run_backup(plan)["copied"], 1)

    def test_files_that_differ_between_drives_are_never_touched(self):
        write(self.ssd, "10-2-26/Drone/DJI_0001.MP4", b"the real clip" * 100)
        write(self.one, "10-2-26/Drone/DJI_0001.MP4", b"something else")
        write(self.ssd, "10-2-26/Drone/DJI_0002.MP4", b"fine" * 100)
        before_one = snapshot(self.one)["10-2-26/Drone/DJI_0001.MP4"]
        plan = self.plan()
        self.assertEqual(len(plan.conflicts), 1)
        self.assertIn("DJI_0001.MP4", plan.conflicts[0])
        summary = run_backup(plan)
        self.assertEqual(snapshot(self.one)["10-2-26/Drone/DJI_0001.MP4"], before_one)
        self.assertFalse((self.two / "10-2-26/Drone/DJI_0001.MP4").exists(),
                         "with two versions about, neither is spread further")
        self.assertTrue((self.two / "10-2-26/Drone/DJI_0002.MP4").exists(), "the rest still goes")
        self.assertEqual(summary["conflicts"], plan.conflicts)

    def test_nothing_is_copied_unless_both_hdds_are_plugged_in(self):
        write(self.ssd, "10-8-26/360/A.insv", b"a" * 100)
        plan = scan_backups(self.ssd, [self.one, self.root / "NOT PLUGGED IN"])
        self.assertIn("Plug in Back up HDD 2", plan.blocker())
        with self.assertRaises(RuntimeError):
            run_backup(plan)
        self.assertEqual(files(self.one), set())

    def test_it_refuses_up_front_when_a_drive_is_too_full(self):
        write(self.ssd, "10-8-26/360/A.insv", b"a" * 100)
        plan = self.plan()
        plan.free[self.two] = 10
        self.assertIn("does not have room", plan.blocker())
        with self.assertRaises(RuntimeError):
            run_backup(plan)
        self.assertEqual(files(self.one), set(), "refused before the first file, not halfway")

    def test_one_drive_set_as_both_hdds_is_refused(self):
        write(self.ssd, "10-8-26/360/A.insv", b"a" * 100)
        plan = scan_backups(self.ssd, [self.one, self.one])
        self.assertIn("same drive", plan.blocker())
        with self.assertRaises(RuntimeError):
            run_backup(plan)

    def test_an_hdd_inside_the_ssd_is_refused(self):
        inside = self.ssd / "backup"
        inside.mkdir()
        plan = scan_backups(self.ssd, [inside, self.two])
        self.assertIn("same drive", plan.blocker())

    def test_stopping_leaves_no_half_files_and_a_rerun_finishes(self):
        for n in range(5):
            write(self.ssd, f"10-8-26/360/{n}.insv", bytes([n]) * 2000)
        stop = threading.Event()
        calls = []

        def progress(done, total, text):
            calls.append(done)
            if len(calls) == 3:
                stop.set()

        summary = run_backup(self.plan(), progress, stop)
        self.assertTrue(summary["stopped"])
        for drive in (self.one, self.two):
            self.assertEqual([p for p in drive.rglob("*.footage-guardian-part")], [])
        self.assertEqual(run_backup(self.plan())["failures"], [])
        self.assertEqual(files(self.one), files(self.ssd))
        self.assertEqual(files(self.two), files(self.ssd))

    def test_running_again_finds_nothing_to_do(self):
        write(self.ssd, "10-8-26/360/A.insv", b"a" * 100)
        write(self.one, "2-2-25/360/OLD.insv", b"o" * 100)
        run_backup(self.plan())
        again = self.plan()
        self.assertEqual(again.jobs, [])
        self.assertTrue(all(day.complete for day in again.days))
        self.assertIn("Both HDDs hold everything", plan_summary(again))

    def test_ssd_footage_goes_before_old_footage_newest_first(self):
        write(self.one, "12-1-25/360/OLD.insv", b"o" * 100)
        write(self.ssd, "9-30-26/360/A.insv", b"a" * 100)
        write(self.ssd, "10-8-26/360/B.insv", b"b" * 100)
        order = [job.relative.split("/")[0] for job in self.plan().jobs]
        self.assertEqual(order, ["10-8-26", "9-30-26", "12-1-25"])

    def test_folders_that_are_not_shoot_days_are_reported_and_left_alone(self):
        write(self.one, "D3 Youtube/7:22:26/Main Cam/A.MOV", b"nested archive")
        plan = self.plan()
        self.assertEqual(plan.unhandled, {"Back up HDD 1": ["D3 Youtube"]})
        self.assertEqual(plan.jobs, [])
        self.assertIn("Left alone on Back up HDD 1", plan_summary(plan))

    def test_colon_dated_folders_from_the_old_archive_count(self):
        write(self.one, "7:22:26/Main Cam/A.MOV", b"finder typed date")
        plan = self.plan()
        self.assertEqual([day.day for day in plan.days], ["7:22:26"])
        run_backup(plan)
        self.assertTrue((self.two / "7:22:26/Main Cam/A.MOV").exists())

    def test_progress_is_in_bytes_and_ends_at_the_total(self):
        write(self.ssd, "10-8-26/360/A.insv", b"a" * 5000)
        write(self.one, "1-1-25/360/B.insv", b"b" * 3000)
        reports = []
        plan = self.plan()
        self.assertEqual(plan.bytes_to_copy, 5000 * 2 + 3000)
        run_backup(plan, lambda done, total, text: reports.append((done, total)))
        self.assertEqual(reports[-1], (13000, 13000))
        self.assertGreater(len({done for done, _ in reports}), 3)

    def test_the_table_reads_plainly(self):
        write(self.ssd, "10-8-26/360/A.insv", b"a" * 100)
        for drive in (self.one, self.two):
            write(drive, "1-5-25/360/OLD.insv", b"o" * 100)
        plan = self.plan()
        rows = {row[0]: row for row in plan_rows(plan)}
        self.assertEqual(rows["10-8-26"][1:4], ("1 file", "—", "—"))
        self.assertTrue(rows["10-8-26"][4].startswith("Needs 2 copies"))
        self.assertEqual(rows["1-5-25"][4], "✓ On both HDDs (cleared from SSD)")

    def test_dates_sort_across_years(self):
        days = ["1-5-26", "12-31-25", "7:22:26", "10-8-26"]
        self.assertEqual(sorted(days, key=day_sort_key),
                         ["12-31-25", "1-5-26", "7:22:26", "10-8-26"])

    def test_the_engine_rescans_before_copying(self):
        write(self.ssd, "10-8-26/360/A.insv", b"a" * 100)
        config = Config(sources=[Source("SSD", str(self.ssd))],
                        backup_paths=[str(self.one), str(self.two)])
        guardian = Guardian(config, Manifest(self.root / "m.db"), logging.getLogger("test"))
        stale = guardian.plan_backups(self.ssd)
        write(self.ssd, "10-8-26/360/B.insv", b"b" * 100)   # arrived after the look
        self.assertEqual(len(stale.jobs), 1)
        summary = guardian.backup_everything(self.ssd)
        self.assertEqual(summary["copied"], 4)


if __name__ == "__main__":
    unittest.main()
