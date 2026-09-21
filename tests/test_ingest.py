from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from footage_guardian.ingest import (
    CardIngester,
    camera_from_device_name,
    classify_source,
    inspect_card,
    looks_offloaded,
)
from footage_guardian.manifest import Manifest
from footage_guardian.storage import md5_file


class DatedFolderTests(unittest.TestCase):
    """An already-organised drive must be mirrored verbatim, never re-wrapped.

    Kevin types his date folders in Finder as 7/22/26. macOS stores that on disk
    as 7:22:26, which is what the drive and Google Drive actually contain. Only
    the hyphen spelling was recognised, so a correctly-organised SSD was treated
    as a raw card and buried inside a second dated wrapper. New folders use
    hyphens (decided 2026-09-11); the colon form still has to be understood so
    the existing archive is never misfiled.
    """

    def _drive_containing(self, name: str) -> bool:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / name / "Main Cam").mkdir(parents=True)
            return looks_offloaded(root)

    def test_hyphen_dates_are_recognised(self):
        for name in ("7-22-26", "12-5-26", "9-1-26"):
            self.assertTrue(self._drive_containing(name), name)

    def test_colon_dates_from_finder_are_recognised(self):
        for name in ("7:22:26", "12:5:26"):
            self.assertTrue(self._drive_containing(name), name)

    def test_a_raw_card_is_still_not_mistaken_for_an_offloaded_drive(self):
        for name in ("DCIM", "PRIVATE", "Untitled", "2026-07-22"):
            self.assertFalse(self._drive_containing(name), name)


class SourceClassificationTests(unittest.TestCase):
    """A drive is an offloaded tree, a camera card, or something we must not guess at.

    Found on a real 2.5 TB working drive: it held the archive nested inside a
    'Team Youtube' folder alongside Audio, Varicam and edit folders. Because no
    dated folder sat at the top level it failed the offloaded test, and because
    one .insv file existed somewhere it was called a 360 card - so the whole
    drive would have been mirrored into 12-31-99/360/. Refusing is the only safe
    answer for anything that is not clearly one thing or the other.
    """

    def _classify(self, build) -> str:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build(root)
            return classify_source(root).kind

    def test_a_dated_tree_at_the_top_is_offloaded(self):
        def build(root: Path) -> None:
            (root / "7-22-26" / "Main Cam").mkdir(parents=True)
            (root / "9-1-26" / "Drone").mkdir(parents=True)
        self.assertEqual("offloaded", self._classify(build))

    def test_a_camera_card_is_a_card(self):
        for folders in (("DCIM", "DJI_001"), ("PRIVATE", "AVCHD"), ("DCIM", "Camera01"), ("MISC", "THM")):
            def build(root: Path, folders=folders) -> None:
                (root.joinpath(*folders)).mkdir(parents=True)
                (root.joinpath(*folders) / "CLIP.MP4").write_bytes(b"x")
            self.assertEqual("card", self._classify(build), folders)

    def test_loose_files_with_no_folders_are_a_card(self):
        def build(root: Path) -> None:
            (root / "clip.mov").write_bytes(b"x")
        self.assertEqual("card", self._classify(build))

    def test_a_working_drive_is_refused_not_guessed_at(self):
        """The real drive that exposed this. Project folders, archive nested inside."""
        def build(root: Path) -> None:
            for name in ("Audio", "Team Youtube", "Drone", "Edits from the editor",
                         "Varicam", "Project files", "Lumix internal"):
                (root / name).mkdir()
            (root / "Team Youtube" / "7:22:26" / "Main Cam").mkdir(parents=True)
            (root / "Drone" / "VID_1.insv").write_bytes(b"x")
        self.assertEqual("unclear", self._classify(build))

    def test_a_nested_dated_tree_is_refused_and_names_the_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Team Youtube" / "7-22-26" / "Main Cam").mkdir(parents=True)
            (root / "Audio").mkdir()
            verdict = classify_source(root)
            self.assertEqual("unclear", verdict.kind)
            self.assertIn("Team Youtube", verdict.reason)


class DeviceNameTests(unittest.TestCase):
    """Kevin plugs the cameras in rather than pulling their cards.

    The cards cannot distinguish a DJI drone from a DJI Osmo, but the hardware
    names itself over USB. When it does, there is nothing left to confirm.
    """

    def test_an_osmo_names_itself(self):
        for product in ("Osmo Action 4", "DJI Osmo Pocket 3", "Osmo Action 5 Pro"):
            self.assertEqual("DJI Osmo", camera_from_device_name(product), product)

    def test_a_drone_names_itself(self):
        for product in ("Mavic 3 Pro", "DJI Mini 4 Pro", "Air 3S", "DJI Avata 2"):
            self.assertEqual("Drone", camera_from_device_name(product), product)

    def test_other_cameras_are_recognised_too(self):
        self.assertEqual("360", camera_from_device_name("Insta360 X4"))
        self.assertEqual("GoPro", camera_from_device_name("HERO12 Black"))
        self.assertEqual("Main Cam", camera_from_device_name("LUMIX DC-GH6"))

    def test_an_unknown_dji_device_is_not_guessed_at(self):
        """The whole point. Returning 'Drone' for anything DJI-shaped is the bug."""
        for product in ("DJI Device", "DJI", "", "Generic USB Reader"):
            self.assertEqual("", camera_from_device_name(product), product)

    def test_the_vendor_string_is_considered(self):
        self.assertEqual("360", camera_from_device_name("X4", "Insta360"))


class AmbiguousCameraTests(unittest.TestCase):
    """A DJI drone and a DJI Osmo write the same tree, so the app must not guess.

    Both put DJI_####.MP4 in DCIM/DJI_001 and both may carry MISC/THM thumbnails.
    Guessing would file a day of Osmo footage under Drone and merge two cameras
    with nothing downstream ever revealing it.
    """

    def _dji_card(self, root: Path, name: str, clip: bytes) -> Path:
        card = root / name
        target = card / "DCIM" / "DJI_001" / "DJI_0001.MP4"
        target.parent.mkdir(parents=True)
        target.write_bytes(clip)
        (card / "MISC" / "THM").mkdir(parents=True)
        return card

    def test_a_dji_card_is_reported_as_undecidable(self):
        with tempfile.TemporaryDirectory() as temp:
            card = self._dji_card(Path(temp), "UNTITLED", b"aerial")
            info = inspect_card(card)
            self.assertEqual(("Drone", "DJI Osmo"), info.alternatives)

    def test_a_lumix_card_is_not_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            card = Path(temp) / "CARD"
            clip = card / "PRIVATE" / "AVCHD" / "BDMV" / "STREAM" / "00000.MTS"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"lumix")
            info = inspect_card(card)
            self.assertEqual((), info.alternatives)
            self.assertEqual("Main Cam", info.suggested_camera)

    def test_confirming_one_card_settles_only_that_card(self):
        """The crux: an Osmo and a drone share a folder signature.

        Confirming one must not teach the app the wrong answer for the other, so
        the decision is remembered per physical card, never per signature.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = Manifest(root / "m.db")
            osmo = self._dji_card(root, "OSMO", b"handheld footage")
            drone = self._dji_card(root, "DRONE", b"a totally different aerial clip")

            first, second = inspect_card(osmo, manifest), inspect_card(drone, manifest)
            self.assertEqual(first.signature, second.signature)   # identical trees
            self.assertNotEqual(first.fingerprint, second.fingerprint)

            manifest.confirm_card_camera(first.fingerprint, "DJI Osmo")

            settled = inspect_card(osmo, manifest)
            self.assertEqual("DJI Osmo", settled.suggested_camera)
            self.assertEqual((), settled.alternatives)

            untouched = inspect_card(drone, manifest)
            self.assertEqual(("Drone", "DJI Osmo"), untouched.alternatives)


class CardIngestTests(unittest.TestCase):
    def test_detects_camera_tree_and_builds_stable_card_id(self):
        with tempfile.TemporaryDirectory() as temp:
            card = Path(temp) / "SONY_CARD"
            clip = card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"camera footage")
            first = inspect_card(card)
            second = inspect_card(card)
            self.assertEqual("Sony", first.suggested_camera)
            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(1, len(first.files))

    def test_offload_preserves_tree_verifies_and_blocks_duplicate_card(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card, ssd = root / "CARD", root / "SSD"
            clip = card / "DCIM" / "100GOPRO" / "GX010001.MP4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"video" * 1000)
            ssd.mkdir()
            manifest = Manifest(root / "manifest.db")
            info = inspect_card(card, manifest)
            ingester = CardIngester(manifest)
            destination = ingester.offload(info, ssd, "8-10-26", "360")
            copied = destination / "DCIM" / "100GOPRO" / "GX010001.MP4"
            self.assertEqual(md5_file(clip), md5_file(copied))
            prior = ingester.prior_ingest(info.fingerprint)
            self.assertEqual("VERIFIED", prior["state"])
            with self.assertRaises(RuntimeError):
                ingester.offload(info, ssd, "8-10-26", "360")

    def test_unknown_camera_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card, ssd = root / "CARD", root / "SSD"
            card.mkdir(); ssd.mkdir()
            (card / "clip.bin").write_bytes(b"data")
            ingester = CardIngester(Manifest(root / "manifest.db"))
            with self.assertRaises(RuntimeError):
                ingester.offload(inspect_card(card), ssd, "8-10-26", "Unknown camera")

    def test_main_cam_uses_numbered_card_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card, ssd = root / "LUMIX", root / "SSD"
            card.mkdir(); ssd.mkdir()
            (card / "clip.mov").write_bytes(b"lumix")
            ingester = CardIngester(Manifest(root / "manifest.db"))
            destination = ingester.offload(inspect_card(card), ssd, "8-10-26", "Main Cam", "card 2")
            self.assertEqual(ssd.resolve() / "8-10-26" / "Main Cam" / "card 2", destination)


if __name__ == "__main__":
    unittest.main()
