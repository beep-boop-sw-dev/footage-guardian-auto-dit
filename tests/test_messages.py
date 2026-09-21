from __future__ import annotations

import unittest

from footage_guardian.storage import readable_rclone_error


# Captured verbatim from a real failed run on 2026-08-10.
EXPIRED_SIGN_IN = """2026/08/10 17:05:48 NOTICE: gdrive{84KcY}: This remote uses rclone's shared Google Drive client_id, which is being retired and will stop working during 2026. Create your own client_id to avoid interruption: https://rclone.org/drive/#making-your-own-client-id
2026/08/10 17:05:48 CRITICAL: Failed to create file system for destination "gdrive:FG-Auto-DIT-DRY-RUN/8-10-26/Main Cam/card 1/PRIVATE/AVCHD/BDMV/STREAM/": drive: failed when making oauth client: failed to create oauth client: empty token found - please run "rclone config reconnect gdrive{84KcY}:"
"""


class RcloneMessageTests(unittest.TestCase):
    def test_expired_sign_in_becomes_one_actionable_line(self):
        message = readable_rclone_error(EXPIRED_SIGN_IN, "gdrive:Archive/clip.mov")
        self.assertEqual(
            "Google Drive sign-in has expired. In Terminal run: rclone config reconnect gdrive:",
            message,
        )

    def test_deprecation_notice_alone_is_not_treated_as_the_failure(self):
        notice_only = EXPIRED_SIGN_IN.splitlines()[0]
        self.assertNotIn("client_id", readable_rclone_error(notice_only, "gdrive:x"))

    def test_offline_reads_as_temporary(self):
        message = readable_rclone_error(
            '2026/08/10 17:05:48 ERROR: dial tcp: lookup www.googleapis.com: no such host', "gdrive:x")
        self.assertEqual("Cannot reach Google Drive right now. It will keep trying.", message)

    def test_full_drive_says_so(self):
        message = readable_rclone_error(
            "2026/08/10 17:05:48 ERROR: googleapi: Error 403: The user's Drive storage quota has been exceeded",
            "gdrive:x")
        self.assertIn("out of space", message)

    def test_unrecognised_failure_keeps_the_detail_without_the_prefix(self):
        message = readable_rclone_error("2026/08/10 17:05:48 ERROR: something unusual happened", "gdrive:x")
        self.assertEqual("something unusual happened", message)

    def test_silence_still_produces_a_sentence(self):
        self.assertEqual("Google Drive upload failed without explanation.", readable_rclone_error("", "gdrive:x"))


if __name__ == "__main__":
    unittest.main()
