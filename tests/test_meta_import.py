from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from footage_guardian.manifest import Manifest
from footage_guardian.meta_import import MetaImporter, find_recent_meta
from footage_guardian.storage import md5_file


class MetaImportTests(unittest.TestCase):
    def test_finds_recent_media_and_leaves_download_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            downloads, ssd = root / "Downloads", root / "SSD"
            downloads.mkdir(); ssd.mkdir()
            video = downloads / "IMG_1234.MP4"
            video.write_bytes(b"meta video")
            (downloads / "notes.txt").write_text("ignore")
            found = find_recent_meta(downloads)
            self.assertEqual([video], [item.path for item in found])
            destination = MetaImporter(Manifest(root / "manifest.db")).import_files(found, ssd, "8-10-26")
            copied = destination / video.name
            self.assertEqual(ssd.resolve() / "8-10-26" / "meta glasses", destination)
            self.assertEqual(md5_file(video), md5_file(copied))
            self.assertTrue(video.exists())

    def test_old_download_is_not_suggested(self):
        with tempfile.TemporaryDirectory() as temp:
            downloads = Path(temp)
            old = downloads / "old.mov"
            old.write_bytes(b"old")
            old_time = time.time() - 10 * 86400
            old.touch()
            import os
            os.utime(old, (old_time, old_time))
            self.assertEqual([], find_recent_meta(downloads, days=7))


if __name__ == "__main__":
    unittest.main()
