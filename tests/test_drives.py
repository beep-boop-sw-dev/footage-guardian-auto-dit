from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from footage_guardian.drives import DrivePlan, mounted_volumes, propose_drives


class DriveProposalTests(unittest.TestCase):
    """Filling in the Drives tab instead of asking him to type paths.

    Everything here proposes. A wrong guess has to be one click to fix,
    and a saved setting must never be quietly overwritten — this decides
    where footage gets written.
    """

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.volumes = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        self.state: dict[str, bool | None] = {}

    def volume(self, name: str, *, dated: bool = False, ssd: bool | None = None) -> Path:
        path = self.volumes / name
        path.mkdir()
        if dated:
            (path / "9-21-26").mkdir()
        self.state[name] = ssd
        return path

    def plan(self, ssd: str = "", backups: tuple[str, ...] = ()) -> DrivePlan:
        return propose_drives(ssd, backups, self.volumes,
                              solid_state=lambda p: self.state.get(p.name))

    # ------------------------------------------------------------ the names

    def test_it_reads_the_names_the_operator_gave_the_drives(self):
        self.volume("SSD main drive", dated=True)
        self.volume("Back up HDD 1", dated=True)
        self.volume("Back up HDD 2", dated=True)

        plan = self.plan()
        self.assertEqual(plan.ssd.path.name, "SSD main drive")
        self.assertEqual([b.path.name for b in plan.backups],
                         ["Back up HDD 1", "Back up HDD 2"])
        self.assertTrue(plan.ssd.confident)

    def test_backups_keep_their_numbering(self):
        # HDD 2 mounting first must not make it HDD 1; the operator
        # labels the physical drives and expects them to match.
        self.volume("Back up HDD 2")
        self.volume("Back up HDD 1")
        plan = self.plan()
        self.assertEqual([b.path.name for b in plan.backups],
                         ["Back up HDD 1", "Back up HDD 2"])

    def test_backup_is_matched_however_it_is_spelled(self):
        self.volume("Backup-HDD 1")
        plan = self.plan()
        self.assertEqual(plan.backups[0].path.name, "Backup-HDD 1")

    # ------------------------------------------------- what is already set

    def test_a_saved_drive_that_is_plugged_in_is_kept(self):
        saved = self.volume("Scratch")
        self.volume("SSD main drive")
        plan = self.plan(ssd=str(saved))
        self.assertEqual(plan.ssd.path, saved, "a confirmed setting is not second-guessed")
        self.assertIn("already set", plan.ssd.reason)

    def test_a_saved_drive_that_is_unplugged_is_reported_missing(self):
        # Blanking it would lose the setting; saying nothing would let
        # him start a backup onto a drive that is not there.
        plan = self.plan(backups=("/Volumes/Back up HDD 1",))
        self.assertTrue(any("Back up HDD 1" in m for m in plan.missing))
        self.assertIsNone(plan.backups[0].path)

    def test_one_drive_is_never_proposed_for_two_jobs(self):
        only = self.volume("SSD main drive")
        plan = self.plan()
        self.assertEqual(plan.ssd.path, only)
        self.assertTrue(all(b.path != only for b in plan.backups))

    # --------------------------------------------------- shape and hardware

    def test_an_unnamed_solid_state_drive_with_dated_folders_is_offered(self):
        self.volume("Untitled", dated=True, ssd=True)
        self.volume("Spinner", dated=True, ssd=False)
        plan = self.plan()
        self.assertEqual(plan.ssd.path.name, "Untitled")
        self.assertFalse(plan.ssd.confident, "a guess from shape is not a confident one")
        self.assertEqual(plan.backups[0].path.name, "Spinner")

    def test_a_drive_macos_will_not_describe_is_left_alone(self):
        # None means unknown. Guessing "rotational, so it is a backup"
        # would propose writing footage somewhere on no evidence.
        self.volume("Mystery", dated=True, ssd=None)
        plan = self.plan()
        self.assertIsNone(plan.ssd.path)
        self.assertIsNone(plan.backups[0].path)
        self.assertIn(self.volumes / "Mystery", plan.volumes)

    def test_every_mounted_volume_stays_available_to_choose(self):
        self.volume("SSD main drive")
        self.volume("Someone elses drive")
        plan = self.plan()
        names = sorted(p.name for p in plan.volumes)
        self.assertEqual(names, ["SSD main drive", "Someone elses drive"])

    def test_nothing_plugged_in_proposes_nothing_and_does_not_throw(self):
        plan = self.plan()
        self.assertIsNone(plan.ssd.path)
        self.assertEqual([b.path for b in plan.backups], [None, None])
        self.assertEqual(plan.volumes, [])

    def test_a_camera_card_is_never_proposed_as_a_drive(self):
        # A real Mac had a camera card and an unrelated client's work
        # drive mounted while this ran. A card holds footage waiting to
        # be rescued; it is never somewhere to write it.
        card = self.volumes / "LUMIX"
        (card / "DCIM" / "100_PANA").mkdir(parents=True)
        (card / "DCIM" / "100_PANA" / "P1000001.MOV").write_bytes(b"clip")
        self.state["LUMIX"] = False  # even if macOS calls it rotational

        plan = self.plan()
        self.assertIsNone(plan.ssd.path)
        self.assertTrue(all(b.path is None for b in plan.backups))
        self.assertIn(card, plan.volumes, "still offered, just never proposed")

    def test_an_unorganised_drive_is_offered_but_not_proposed(self):
        # Someone else's work drive, or a freshly formatted one. Failing
        # to detect it costs one click; proposing it could write footage
        # onto the wrong disk.
        self.volume("Tri Valley Roofing Disk 2", ssd=False)
        plan = self.plan()
        self.assertTrue(all(b.path is None for b in plan.backups))
        self.assertEqual([p.name for p in plan.volumes], ["Tri Valley Roofing Disk 2"])

    def test_every_guess_says_why(self):
        # The operator has to be able to judge the proposal, not just
        # accept it.
        self.volume("SSD main drive", dated=True)
        self.volume("Back up HDD 1", dated=True)
        plan = self.plan()
        self.assertTrue(plan.ssd.reason)
        self.assertTrue(plan.backups[0].reason)


class MountedVolumeTests(unittest.TestCase):
    def test_hidden_and_apple_volumes_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".hidden").mkdir()
            (root / "com.apple.thing").mkdir()
            (root / "Recovery").mkdir()
            (root / "Real Drive").mkdir()
            self.assertEqual([p.name for p in mounted_volumes(root)], ["Real Drive"])

    def test_a_missing_volumes_folder_is_not_an_error(self):
        self.assertEqual(mounted_volumes(Path("/nope/not/here")), [])


if __name__ == "__main__":
    unittest.main()
