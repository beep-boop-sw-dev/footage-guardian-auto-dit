from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from footage_guardian.config import Config, Source
from footage_guardian.engine import Guardian, safe_component
from footage_guardian.ingest import date_from_footage
from footage_guardian.manifest import Manifest
from footage_guardian.storage import copy_verified, md5_file


class FakeCloud:
    """A stand-in for rclone that refuses to confirm what it never received.

    An earlier version answered every verify() with success, which let a sync
    conclude that files it had not uploaded were already safely in Drive. A
    double that only ever agrees cannot catch that, so this one keeps a remote
    and answers from it.
    """

    def __init__(self):
        self.uploads = []
        self.verifications = []
        self.checksum_demands = []
        self.remote: dict[str, str] = {}

    def available(self): return True

    def reachable(self, remote): return ""

    def upload(self, source, remote):
        self.uploads.append((source, remote))
        self.remote[remote] = md5_file(Path(source)) if Path(source).is_file() else ""

    def verify(self, remote, size, md5, require_checksum=False):
        self.verifications.append((remote, size, md5))
        self.checksum_demands.append(require_checksum)
        if remote not in self.remote:
            raise RuntimeError("Cloud size verification failed")
        if self.remote[remote] and self.remote[remote] != md5:
            raise RuntimeError("Cloud checksum verification failed")
        return "MD5 checksum and size"


class GuardianTests(unittest.TestCase):
    def test_copy_is_hash_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, destination = root / "a.mov", root / "backup" / "a.mov"
            source.write_bytes(b"footage" * 1000)
            digest = md5_file(source)
            copy_verified(source, destination, digest)
            self.assertEqual(digest, md5_file(destination))
            self.assertFalse(destination.with_name(destination.name + ".footage-guardian-part").exists())

    def test_backup_and_drive_mirror_the_source_tree_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root, backup = root / "SSD", root / "BACKUP"
            clip = source_root / "8-10-26" / "Main Cam" / "card 1" / "PRIVATE" / "AVCHD" / "A001.MP4"
            clip.parent.mkdir(parents=True)
            backup.mkdir()
            clip.write_bytes(b"video data")
            config = Config(sources=[Source("SSD", str(source_root))], backup_path=str(backup),
                            google_destination="gdrive:Archive", stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            cloud = FakeCloud()
            Guardian(config, manifest, logging.getLogger("test"), cloud=cloud).scan_once()
            self.assertEqual("SAFE", manifest.rows()[0]["state"])
            mirrored = "8-10-26/Main Cam/card 1/PRIVATE/AVCHD/A001.MP4"
            self.assertTrue((backup / mirrored).exists())
            self.assertEqual(f"gdrive:Archive/{mirrored}", cloud.uploads[0][1])

    def _main_cam_card(self, root: Path, name: str, clip: str, data: bytes) -> Path:
        card = root / name
        target = card / "PRIVATE" / "AVCHD" / "BDMV" / "STREAM" / clip
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        return card

    def test_raw_card_is_filed_under_date_and_detected_camera(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card = self._main_cam_card(root, "UNTITLED", "00000.MTS", b"todays clip")
            backup = root / "BACKUP"
            backup.mkdir()
            config = Config(sources=[Source("Card", str(card))], backup_path=str(backup),
                            google_destination="gdrive:Archive", stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            cloud = FakeCloud()
            Guardian(config, manifest, logging.getLogger("test"), cloud=cloud).scan_once()
            self.assertEqual("SAFE", manifest.rows()[0]["state"])
            today = date_from_footage((card / "PRIVATE/AVCHD/BDMV/STREAM/00000.MTS",))
            expected = f"{today}/Main Cam/card 1/PRIVATE/AVCHD/BDMV/STREAM/00000.MTS"
            self.assertTrue((backup / expected).exists())
            self.assertEqual(f"gdrive:Archive/{expected}", cloud.uploads[0][1])

    def test_second_card_of_the_day_gets_its_own_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # Sequential cards reuse filenames, so without separate slots they collide.
            first = self._main_cam_card(root, "CARD_A", "00000.MTS", b"first card footage")
            second = self._main_cam_card(root, "CARD_B", "00000.MTS", b"second card footage")
            backup = root / "BACKUP"
            backup.mkdir()
            manifest = Manifest(root / "manifest.db")
            log = logging.getLogger("test")
            for card in (first, second):
                config = Config(sources=[Source("Card", str(card))], backup_path=str(backup), stable_seconds=0)
                Guardian(config, manifest, log, cloud=FakeCloud()).scan_once()
            today = date_from_footage((first / "PRIVATE/AVCHD/BDMV/STREAM/00000.MTS",))
            clip = "PRIVATE/AVCHD/BDMV/STREAM/00000.MTS"
            self.assertEqual(b"first card footage", (backup / today / "Main Cam" / "card 1" / clip).read_bytes())
            self.assertEqual(b"second card footage", (backup / today / "Main Cam" / "card 2" / clip).read_bytes())
            self.assertEqual({"SAFE"}, {row["state"] for row in manifest.rows()})

    def test_rescanning_the_same_card_does_not_refile_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card = self._main_cam_card(root, "UNTITLED", "00000.MTS", b"one clip")
            backup = root / "BACKUP"
            backup.mkdir()
            config = Config(sources=[Source("Card", str(card))], backup_path=str(backup), stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            guardian = Guardian(config, manifest, logging.getLogger("test"), cloud=FakeCloud())
            guardian.scan_once()
            # A second card appearing mid-shoot must not push this one to a new slot.
            self._main_cam_card(root, "OTHER", "00000.MTS", b"different card")
            guardian.scan_once()
            today = date_from_footage((card / "PRIVATE/AVCHD/BDMV/STREAM/00000.MTS",))
            slots = sorted(item.name for item in (backup / today / "Main Cam").iterdir())
            self.assertEqual(["card 1"], slots)

    def test_backup_never_overwrites_different_footage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root, backup = root / "SSD", root / "BACKUP"
            clip = source_root / "8-10-26" / "Drone" / "DJI_0001.MP4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"todays drone footage")
            # An unrelated clip was filed under the same name on a previous day.
            stale = backup / "8-10-26" / "Drone" / "DJI_0001.MP4"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"last months drone footage")
            config = Config(sources=[Source("SSD", str(source_root))], backup_path=str(backup), stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            cloud = FakeCloud()
            Guardian(config, manifest, logging.getLogger("test"), cloud=cloud).scan_once()
            row = manifest.rows()[0]
            self.assertEqual("ERROR", row["state"])
            self.assertEqual(b"last months drone footage", stale.read_bytes())
            self.assertEqual(b"todays drone footage", clip.read_bytes())
            self.assertEqual([], cloud.uploads)

    def test_unplugged_backup_never_deletes_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "CARD"
            source_root.mkdir()
            clip = source_root / "clip.mov"
            clip.write_bytes(b"irreplaceable")
            config = Config(sources=[Source("Camera", str(source_root))], backup_path=str(root / "missing"), stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            Guardian(config, manifest, logging.getLogger("test"), cloud=FakeCloud()).scan_once()
            self.assertEqual("MISSING BACKUP", manifest.rows()[0]["state"])
            self.assertEqual(b"irreplaceable", clip.read_bytes())

    def test_safe_component_blocks_path_separators(self):
        self.assertEqual("Film-2026", safe_component("Film/2026"))

    def test_space_recovery_reverifies_and_never_removes_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root, backup = root / "CARD", root / "BACKUP"
            source_root.mkdir()
            backup.mkdir()
            clip = source_root / "clip.mov"
            clip.write_bytes(b"keep the original")
            config = Config(sources=[Source("Camera", str(source_root))],
                            backup_path=str(backup), stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            cloud = FakeCloud()
            guardian = Guardian(config, manifest, logging.getLogger("test"), cloud=cloud)
            guardian.scan_once()
            row = manifest.rows()[0]
            backup_file = Path(row["backup_path"])
            guardian.verify_for_clearance(row["id"])
            self.assertEqual("CLEAR TO REMOVE", manifest.get(row["id"])["state"])
            guardian.remove_local_backup(row["id"])
            self.assertFalse(backup_file.exists())
            self.assertTrue(clip.exists())
            self.assertEqual("CLOUD SAFE", manifest.get(row["id"])["state"])
            self.assertGreaterEqual(len(cloud.verifications), 3)

    def test_clearing_and_removing_both_demand_a_real_cloud_checksum(self):
        """A size match on Drive must never be enough to justify deleting a backup."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root, backup = root / "CARD", root / "BACKUP"
            source_root.mkdir()
            backup.mkdir()
            (source_root / "clip.mov").write_bytes(b"footage" * 100)
            config = Config(sources=[Source("Camera", str(source_root))], backup_path=str(backup),
                            google_destination="gdrive:Archive", stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            cloud = FakeCloud()
            guardian = Guardian(config, manifest, logging.getLogger("test"), cloud=cloud)
            guardian.scan_once()
            file_id = manifest.rows()[0]["id"]

            cloud.checksum_demands.clear()
            guardian.verify_for_clearance(file_id)
            guardian.remove_local_backup(file_id)
            self.assertEqual([True, True], cloud.checksum_demands)

    def _shoot_day(self, root: Path, day: str = "9-15-26") -> Path:
        """An SSD after a full offload: one dated folder, a folder per device."""
        ssd = root / "SSD"
        clips = {
            f"{day}/Main Cam/Disk 1/DCIM/100_PANA/P1000001.MOV": b"main cam clip" * 400,
            f"{day}/Main Cam/Disk 2/DCIM/100_PANA/P1000002.MOV": b"second card" * 400,
            f"{day}/360/DCIM/Camera01/VID_001.insv": b"threesixty" * 400,
            f"{day}/Drone/DCIM/DJI_001/DJI_0001.MP4": b"aerial" * 400,
            f"{day}/DJI Osmo/DCIM/DJI_001/DJI_0001.MP4": b"handheld, same filename" * 400,
        }
        for relative, data in clips.items():
            target = ssd / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return ssd

    def test_a_shoot_day_is_duplicated_onto_both_backup_drives(self):
        """Stage two of the workflow: everything is on the SSD, both drives are
        plugged in, and the day is copied to each and verified."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            one, two = root / "BACKUP 1", root / "BACKUP 2"
            one.mkdir()
            two.mkdir()
            config = Config(sources=[Source("SSD", str(ssd))],
                            backup_paths=[str(one), str(two)], stable_seconds=0)
            guardian = Guardian(config, Manifest(root / "m.db"), logging.getLogger("test"),
                                cloud=FakeCloud())

            self.assertEqual(["9-15-26"], guardian.days_on(ssd))
            summary = guardian.backup_day(ssd, "9-15-26")

            self.assertEqual([], summary["failures"])
            self.assertEqual(5, summary["files"])
            self.assertEqual(10, summary["copied"])   # five files onto two drives
            for drive in (one, two):
                copied = sorted(p.relative_to(drive).as_posix() for p in drive.rglob("*") if p.is_file())
                self.assertEqual(5, len(copied), drive.name)
                for relative in copied:
                    self.assertEqual((ssd / relative).read_bytes(), (drive / relative).read_bytes())
            # The Drone and the Osmo both hold a DJI_0001.MP4; they must stay apart.
            self.assertTrue((one / "9-15-26/Drone/DCIM/DJI_001/DJI_0001.MP4").exists())
            self.assertTrue((one / "9-15-26/DJI Osmo/DCIM/DJI_001/DJI_0001.MP4").exists())

    def test_backing_up_twice_re_copies_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            one, two = root / "B1", root / "B2"
            one.mkdir()
            two.mkdir()
            config = Config(backup_paths=[str(one), str(two)], stable_seconds=0)
            guardian = Guardian(config, Manifest(root / "m.db"), logging.getLogger("test"),
                                cloud=FakeCloud())
            guardian.backup_day(ssd, "9-15-26")
            again = guardian.backup_day(ssd, "9-15-26")
            self.assertEqual(0, again["copied"])
            self.assertEqual(10, again["already_there"])
            self.assertEqual([], again["failures"])

    def test_backup_refuses_when_a_drive_is_not_plugged_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            one = root / "B1"
            one.mkdir()
            config = Config(backup_paths=[str(one), str(root / "NOT PLUGGED IN")])
            guardian = Guardian(config, Manifest(root / "m.db"), logging.getLogger("test"),
                                cloud=FakeCloud())
            with self.assertRaises(RuntimeError) as caught:
                guardian.backup_day(ssd, "9-15-26")
            self.assertIn("not plugged in", str(caught.exception).lower())
            self.assertEqual([], list(one.rglob("*")), "nothing copied when a drive is missing")

    def test_backup_never_overwrites_different_footage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            one = root / "B1"
            clash = one / "9-15-26/Drone/DCIM/DJI_001/DJI_0001.MP4"
            clash.parent.mkdir(parents=True)
            clash.write_bytes(b"a different clip that must not be lost")
            config = Config(backup_paths=[str(one)])
            guardian = Guardian(config, Manifest(root / "m.db"), logging.getLogger("test"),
                                cloud=FakeCloud())
            summary = guardian.backup_day(ssd, "9-15-26")
            self.assertEqual(1, len(summary["failures"]))
            self.assertEqual(b"a different clip that must not be lost", clash.read_bytes())

    def test_the_older_single_backup_setting_still_works(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            old = root / "OLD BACKUP"
            old.mkdir()
            config = Config(backup_path=str(old))
            self.assertEqual([old], config.backup_roots())
            guardian = Guardian(config, Manifest(root / "m.db"), logging.getLogger("test"),
                                cloud=FakeCloud())
            summary = guardian.backup_day(ssd, "9-15-26")
            self.assertEqual(5, summary["copied"])

    def test_a_shoot_day_syncs_to_drive_mirroring_the_ssd(self):
        """Stage three. Drive mirrors the SSD folder for folder, checksum verified."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            config = Config(google_destination="gdrive:Team Youtube drive")
            cloud = FakeCloud()
            guardian = Guardian(config, Manifest(root / "m.db"), logging.getLogger("test"), cloud=cloud)

            summary = guardian.sync_day(ssd, "9-15-26")

            self.assertEqual([], summary["failures"])
            self.assertEqual(5, summary["uploaded"])
            remotes = sorted(remote for _, remote in cloud.uploads)
            self.assertEqual([
                "gdrive:Team Youtube drive/9-15-26/360/DCIM/Camera01/VID_001.insv",
                "gdrive:Team Youtube drive/9-15-26/DJI Osmo/DCIM/DJI_001/DJI_0001.MP4",
                "gdrive:Team Youtube drive/9-15-26/Drone/DCIM/DJI_001/DJI_0001.MP4",
                "gdrive:Team Youtube drive/9-15-26/Main Cam/Disk 1/DCIM/100_PANA/P1000001.MOV",
                "gdrive:Team Youtube drive/9-15-26/Main Cam/Disk 2/DCIM/100_PANA/P1000002.MOV",
            ], remotes)
            self.assertTrue(all(demand for demand in cloud.checksum_demands),
                            "every cloud check must require a real checksum")

    def test_sync_refuses_when_drive_cannot_be_reached(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            cloud = FakeCloud()
            cloud.reachable = lambda remote: "Google Drive sign-in has expired."
            guardian = Guardian(Config(google_destination="gdrive:Archive"),
                                Manifest(root / "m.db"), logging.getLogger("test"), cloud=cloud)
            with self.assertRaises(RuntimeError) as caught:
                guardian.sync_day(ssd, "9-15-26")
            self.assertIn("sign-in", str(caught.exception))
            self.assertEqual([], cloud.uploads, "nothing may upload when Drive is unreachable")

    def test_syncing_twice_re_uploads_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ssd = self._shoot_day(root)
            cloud = FakeCloud()
            guardian = Guardian(Config(google_destination="gdrive:Archive"),
                                Manifest(root / "m.db"), logging.getLogger("test"), cloud=cloud)
            guardian.sync_day(ssd, "9-15-26")
            cloud.uploads.clear()
            again = guardian.sync_day(ssd, "9-15-26")
            self.assertEqual(0, again["uploaded"])
            self.assertEqual(5, again["already_there"])
            self.assertEqual([], cloud.uploads)

    def test_a_working_drive_is_refused_rather_than_mirrored_into_one_folder(self):
        """Found on a real 2.5 TB drive holding the archive inside 'Team Youtube'
        alongside Audio, Varicam and edit folders. No dated folder sat at the top,
        and one stray .insv made it look like a 360 card, so every file would have
        been mirrored into 12-31-99/360/. Nothing may be copied from such a drive."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            drive = root / "Working Drive"
            for name in ("Audio", "Varicam", "Edits from the editor", "Project files"):
                (drive / name).mkdir(parents=True)
            (drive / "Team Youtube" / "7-22-26" / "Main Cam").mkdir(parents=True)
            (drive / "Team Youtube" / "7-22-26" / "Main Cam" / "P1000001.MOV").write_bytes(b"real footage")
            (drive / "Audio" / "REC001.WAV").write_bytes(b"audio")
            (drive / "Varicam" / "VID_1.insv").write_bytes(b"a stray 360 clip")
            backup = root / "BACKUP"
            backup.mkdir()
            config = Config(sources=[Source("Drive", str(drive))], backup_path=str(backup),
                            google_destination="gdrive:Archive", stable_seconds=0)
            cloud = FakeCloud()
            events: list[tuple[str, str]] = []
            guardian = Guardian(config, Manifest(root / "m.db"), logging.getLogger("test"), cloud=cloud)
            guardian._event = lambda level, message: events.append((level, message))

            guardian.scan_once()

            self.assertEqual([], cloud.uploads, "nothing may reach Drive from an unclear source")
            self.assertEqual([], list(backup.rglob("*")), "nothing may reach the backup drive either")
            refusal = [m for level, m in events if level == "ERROR"]
            self.assertTrue(refusal, f"expected a refusal, got {events}")
            self.assertIn("Team Youtube", refusal[0], "the refusal must name the folder to use instead")

    def test_an_unconfirmed_dji_card_is_not_filed_at_all(self):
        """Refuse rather than risk. Nothing is copied until the camera is settled,
        and once Kevin confirms, the same scan files it correctly."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card = root / "UNTITLED"
            clip = card / "DCIM" / "DJI_001" / "DJI_0001.MP4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"could be either camera" * 50)
            backup = root / "BACKUP"
            backup.mkdir()
            config = Config(sources=[Source("Card", str(card))], backup_path=str(backup),
                            google_destination="gdrive:Archive", stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            cloud = FakeCloud()
            events: list[tuple[str, str]] = []
            guardian = Guardian(config, manifest, logging.getLogger("test"), cloud=cloud)
            guardian._event = lambda level, message: events.append((level, message))

            guardian.scan_once()
            self.assertEqual([], cloud.uploads)
            self.assertEqual([], list(backup.rglob("*.MP4")))
            self.assertTrue(any(level == "ERROR" and "DJI Osmo" in message for level, message in events),
                            f"expected a clear refusal naming both cameras, got {events}")
            self.assertTrue(clip.exists(), "source footage must be left untouched")

            from footage_guardian.ingest import inspect_card
            manifest.confirm_card_camera(inspect_card(card).fingerprint, "DJI Osmo")
            guardian.scan_once()

            self.assertEqual("SAFE", manifest.rows()[0]["state"])
            filed = [p for p in backup.rglob("DJI_0001.MP4")]
            self.assertEqual(1, len(filed))
            self.assertIn("DJI Osmo", filed[0].relative_to(backup).as_posix())

    def test_removal_refused_without_clearance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root, backup = root / "CARD", root / "BACKUP"
            source_root.mkdir()
            backup.mkdir()
            clip = source_root / "clip.mov"
            clip.write_bytes(b"footage")
            config = Config(sources=[Source("Camera", str(source_root))], backup_path=str(backup), stable_seconds=0)
            manifest = Manifest(root / "manifest.db")
            guardian = Guardian(config, manifest, logging.getLogger("test"), cloud=FakeCloud())
            guardian.scan_once()
            row = manifest.rows()[0]
            with self.assertRaises(RuntimeError):
                guardian.remove_local_backup(row["id"])
            self.assertTrue(Path(row["backup_path"]).exists())


if __name__ == "__main__":
    unittest.main()
