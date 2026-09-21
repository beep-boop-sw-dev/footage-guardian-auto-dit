"""Tests for the real Rclone.verify parsing.

The guardian's FakeCloud stubs verify() out entirely, so nothing ever exercised
the code that reads rclone's JSON. That is exactly where the bug lived: real
rclone emits a lowercase "md5" key, the code looked for "MD5", found nothing,
and silently downgraded every cloud check to size-only — including the checks
that stand between Kevin and a deleted local backup.

These drive the real Rclone against a stub executable emitting real rclone shapes.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from footage_guardian.storage import Rclone, remote_hash

DIGEST = "e9f779bf20a5b313886fcc1c51912d74"


def rclone_emitting(root: Path, payload: list[dict]) -> Rclone:
    """A stand-in rclone that answers lsjson with exactly this payload."""
    data = root / "payload.json"
    data.write_text(json.dumps(payload))
    script = root / "fake-rclone"
    script.write_text(f'#!/bin/sh\ncat "{data}"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return Rclone(executable=str(script))


def entry(size: int = 106496, hashes: dict | None = None) -> dict:
    """One lsjson entry shaped the way Google Drive really answers."""
    item = {
        "Path": "A001.MP4", "Name": "A001.MP4", "Size": size,
        "MimeType": "video/mp4", "ModTime": "2026-08-11T17:07:42.587Z",
        "IsDir": False, "ID": "1ZJ1u0zMUQm6RsNp2KxqfkVKjCQtR1D7D",
    }
    if hashes is not None:
        item["Hashes"] = hashes
    return item


class RemoteHashTests(unittest.TestCase):
    def test_reads_the_lowercase_key_real_rclone_emits(self):
        self.assertEqual(DIGEST, remote_hash(entry(hashes={"md5": DIGEST, "sha1": "x"})))

    def test_reads_an_uppercase_key_too(self):
        self.assertEqual(DIGEST, remote_hash(entry(hashes={"MD5": DIGEST})))

    def test_normalises_case_of_the_digest_itself(self):
        self.assertEqual(DIGEST, remote_hash(entry(hashes={"md5": DIGEST.upper()})))

    def test_absent_hashes_give_an_empty_string_not_a_crash(self):
        self.assertEqual("", remote_hash(entry()))
        self.assertEqual("", remote_hash(entry(hashes={})))
        self.assertEqual("", remote_hash(entry(hashes={"sha1": "x"})))


class VerifyTests(unittest.TestCase):
    def test_matching_lowercase_md5_counts_as_a_checksum_check(self):
        """The regression test. This reported 'remote size' before the fix."""
        with tempfile.TemporaryDirectory() as temp:
            cloud = rclone_emitting(Path(temp), [entry(hashes={"md5": DIGEST})])
            self.assertEqual("MD5 checksum and size", cloud.verify("gdrive:A001.MP4", 106496, DIGEST))

    def test_differing_remote_md5_is_refused(self):
        """Proves the comparison is live. It was dead code before the fix."""
        with tempfile.TemporaryDirectory() as temp:
            cloud = rclone_emitting(Path(temp), [entry(hashes={"md5": "0" * 32})])
            with self.assertRaises(RuntimeError) as caught:
                cloud.verify("gdrive:A001.MP4", 106496, DIGEST)
            self.assertIn("checksum", str(caught.exception).lower())

    def test_right_size_but_wrong_content_is_caught(self):
        """The whole point: identical size, different bytes, must not pass."""
        with tempfile.TemporaryDirectory() as temp:
            cloud = rclone_emitting(Path(temp), [entry(size=106496, hashes={"md5": "a" * 32})])
            with self.assertRaises(RuntimeError):
                cloud.verify("gdrive:A001.MP4", 106496, DIGEST)

    def test_wrong_size_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            cloud = rclone_emitting(Path(temp), [entry(size=999, hashes={"md5": DIGEST})])
            with self.assertRaises(RuntimeError) as caught:
                cloud.verify("gdrive:A001.MP4", 106496, DIGEST)
            self.assertIn("size", str(caught.exception).lower())

    def test_missing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            cloud = rclone_emitting(Path(temp), [])
            with self.assertRaises(RuntimeError):
                cloud.verify("gdrive:A001.MP4", 106496, DIGEST)

    def test_without_a_remote_hash_it_reports_size_only_honestly(self):
        with tempfile.TemporaryDirectory() as temp:
            cloud = rclone_emitting(Path(temp), [entry()])
            self.assertEqual("remote size", cloud.verify("gdrive:A001.MP4", 106496, DIGEST))

    def test_deletion_refuses_when_drive_reports_no_checksum(self):
        """Size alone must never be enough to justify deleting a local backup."""
        with tempfile.TemporaryDirectory() as temp:
            cloud = rclone_emitting(Path(temp), [entry()])
            with self.assertRaises(RuntimeError) as caught:
                cloud.verify("gdrive:A001.MP4", 106496, DIGEST, require_checksum=True)
            message = str(caught.exception)
            self.assertIn("Refusing", message)
            self.assertNotIn("Traceback", message)


if __name__ == "__main__":
    unittest.main()
