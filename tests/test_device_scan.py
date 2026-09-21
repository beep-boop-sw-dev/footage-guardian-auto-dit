from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from footage_guardian.ingest import (
    CardIngester,
    describe_mounted_devices,
    is_system_volume,
)
from footage_guardian.manifest import Manifest
from footage_guardian.ui import status_snapshot


class SystemVolumeTests(unittest.TestCase):
    """The window froze because every mount under /Volumes was walked.

    /Volumes/Macintosh HD is the startup disk. A recursive scan of it does not
    finish, and it ran on the thread that draws the window, every four seconds.
    Nothing macOS mounted for itself may reach that scan.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.volumes = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_apple_helper_volumes_are_excluded_by_name(self) -> None:
        for name in ("Data", "Preboot", "Recovery", "VM", "Update", "xarts"):
            self.assertTrue(is_system_volume(self.volumes / name, None),
                            f"{name} should be treated as a system volume")

    def test_apple_namespaced_and_hidden_mounts_are_excluded(self) -> None:
        self.assertTrue(is_system_volume(self.volumes / "com.apple.TimeMachine.localsnapshots", None))
        self.assertTrue(is_system_volume(self.volumes / ".hidden", None))

    def test_the_startup_disk_is_excluded_under_any_name(self) -> None:
        """A name list cannot promise to know what the boot volume is called."""
        drive = self.volumes / "Macintosh HD"
        drive.mkdir()
        self.assertTrue(is_system_volume(drive, drive.stat().st_dev))

    def test_a_plugged_in_drive_survives(self) -> None:
        drive = self.volumes / "Extral SSD 4Tb"
        drive.mkdir()
        self.assertFalse(is_system_volume(drive, drive.stat().st_dev + 1))

    def test_mounted_cards_returns_only_real_drives(self) -> None:
        for name in ("Data", "Preboot", "com.apple.TimeMachine.localsnapshots",
                     "CAMERA_SD", "Extral SSD 4Tb"):
            (self.volumes / name).mkdir()
        found = [path.name for path in CardIngester.mounted_cards(self.volumes)]
        self.assertEqual(found, ["CAMERA_SD", "Extral SSD 4Tb"])


class DescribeMountedDevicesTests(unittest.TestCase):
    """Describing a drive must be cheap before it is thorough.

    Only something that already looks like a camera card may be walked file by
    file; anything else is described from its top two levels alone.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.volumes = self.root / "Volumes"
        self.volumes.mkdir()
        self.manifest = Manifest(self.root / "manifest.sqlite3")
        self.addCleanup(self.temp.cleanup)

    def _card(self, name: str) -> Path:
        drive = self.volumes / name
        clips = drive / "DCIM" / "100GOPRO"
        clips.mkdir(parents=True)
        (clips / "GX010001.MP4").write_bytes(b"footage")
        return drive

    def test_a_camera_card_is_identified(self) -> None:
        self._card("CAMERA_SD")
        rows = describe_mounted_devices(manifest=self.manifest, volumes_root=self.volumes)
        self.assertEqual(len(rows), 1)
        volume, camera, note = rows[0]
        self.assertEqual(volume, "CAMERA_SD")
        self.assertEqual(camera, "GoPro")
        self.assertIn("1 files", note)

    def test_a_working_drive_is_described_without_being_walked(self) -> None:
        """The 2.5 TB drive that would have been mirrored into one folder."""
        drive = self.volumes / "Tri Valley Roofing Disk 2"
        buried = drive / "Team Youtube" / "7:25:26"
        buried.mkdir(parents=True)
        (buried / "clip.mp4").write_bytes(b"x")

        rows = describe_mounted_devices(manifest=self.manifest, volumes_root=self.volumes)
        volume, camera, note = rows[0]
        self.assertEqual(volume, "Tri Valley Roofing Disk 2")
        self.assertEqual(camera, "—")
        self.assertIn("Team Youtube", note)

    def test_an_already_organised_drive_is_not_offered_as_a_card(self) -> None:
        drive = self.volumes / "Archive"
        (drive / "9-16-26" / "Main Cam").mkdir(parents=True)
        rows = describe_mounted_devices(manifest=self.manifest, volumes_root=self.volumes)
        self.assertEqual(rows[0][1], "—")
        self.assertIn("already organised", rows[0][2])

    def test_the_configured_ssd_is_left_out(self) -> None:
        """The SSD is the destination, never something to offload from."""
        ssd = self._card("SSD main drive")
        self._card("CAMERA_SD")
        rows = describe_mounted_devices(ssd=ssd, manifest=self.manifest,
                                        volumes_root=self.volumes)
        self.assertEqual([row[0] for row in rows], ["CAMERA_SD"])

    def test_an_unreadable_drive_does_not_stop_the_others(self) -> None:
        blocked = self.volumes / "Locked"
        blocked.mkdir()
        os.chmod(blocked, 0o000)
        self.addCleanup(os.chmod, blocked, 0o755)
        self._card("CAMERA_SD")

        rows = describe_mounted_devices(manifest=self.manifest, volumes_root=self.volumes)
        self.assertIn("CAMERA_SD", [row[0] for row in rows])


class StatusSnapshotTests(unittest.TestCase):
    """Counting a day walks three drives, so it must be a plain function.

    Being separable from the window is the point: it runs in a worker thread,
    and the numbers alone come back to be displayed.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = Manifest(self.root / "manifest.sqlite3")
        self.addCleanup(self.temp.cleanup)

    def test_no_ssd_asks_for_one(self) -> None:
        snapshot = status_snapshot(None, "", [], self.manifest)
        self.assertIn("Set the SSD main drive", snapshot["message"])

    def test_an_unplugged_hdd_is_named_and_nothing_is_claimed(self) -> None:
        ssd = self.root / "ssd"
        (ssd / "9-16-26" / "Main Cam").mkdir(parents=True)
        (ssd / "9-16-26" / "Main Cam" / "clip.mp4").write_bytes(b"footage")

        snapshot = status_snapshot(ssd, "9-16-26", [self.root / "Back up HDD 2"],
                                   self.manifest)
        self.assertEqual(snapshot["files"], 1)
        self.assertEqual(snapshot["missing"], ["Back up HDD 2"])
        self.assertIn("Plug in Back up HDD 2", snapshot["backup_state"])

    def test_hidden_files_are_not_counted(self) -> None:
        """44 .DS_Store files in the archive must not read as footage."""
        ssd = self.root / "ssd"
        day = ssd / "9-16-26"
        day.mkdir(parents=True)
        (day / "clip.mp4").write_bytes(b"footage")
        (day / ".DS_Store").write_bytes(b"junk")

        snapshot = status_snapshot(ssd, "9-16-26", [], self.manifest)
        self.assertEqual(snapshot["files"], 1)


if __name__ == "__main__":
    unittest.main()
