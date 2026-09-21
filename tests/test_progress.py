from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from footage_guardian.progress import ByteProgress, human_bytes, human_duration
from footage_guardian.storage import CHUNK, copy_verified, md5_file


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ByteProgressTests(unittest.TestCase):
    """The bar used to move once per file.

    A shoot day is a handful of very large clips. On an 18GB file the bar
    sat still for minutes, which reads as a hang — and the instinct then
    is to force-quit, mid-copy, on footage that cannot be reshot.
    """

    def setUp(self):
        self.seen: list[tuple[int, int, str]] = []
        self.clock = FakeClock()

    def bar(self, total=3000, passes=3, interval=0.0):
        return ByteProgress(total, passes=passes,
                            report=lambda *args: self.seen.append(args),
                            min_interval=interval, clock=self.clock)

    def test_it_reports_footage_bytes_not_disk_reads(self):
        # Three passes over a 3000-byte file is 9000 bytes of I/O, but
        # only 3000 bytes of footage. A bar claiming 9000 would be lying.
        bar = self.bar(total=3000, passes=3)
        for _ in range(9):
            bar.add(1000)
        done, total, _ = self.seen[-1]
        self.assertEqual(total, 3000)
        self.assertEqual(done, 3000)

    def test_a_single_file_moves_the_bar_as_it_goes(self):
        bar = self.bar(total=3000, passes=3)
        bar.add(1000)
        bar.add(1000)
        values = [done for done, _, _ in self.seen]
        self.assertEqual(values, [333, 666])
        self.assertTrue(all(v < 3000 for v in values), "must not jump to complete")

    def test_it_never_exceeds_the_total(self):
        # A file that grew, or a miscounted pass, must not send the bar
        # past the end.
        bar = self.bar(total=1000, passes=1)
        bar.add(5000)
        done, total, _ = self.seen[-1]
        self.assertEqual(done, total)

    def test_finished_closes_the_gap(self):
        # Rounding across many files leaves a few bytes short; stopping
        # at 99% looks like something failed quietly.
        bar = self.bar(total=1000, passes=3)
        bar.add(999)
        self.assertLess(self.seen[-1][0], 1000)
        bar.finished("done")
        self.assertEqual(self.seen[-1][0], 1000)

    def test_reports_are_throttled(self):
        # At 240 MB/s an 8MB chunk lands 30 times a second, and every
        # report crosses onto the Tk main thread.
        bar = self.bar(total=10_000, passes=1, interval=0.1)
        for _ in range(50):
            bar.add(100)
        self.assertLessEqual(len(self.seen), 2, "should have collapsed the burst")

    def test_a_label_is_never_throttled_away(self):
        # The filename changing is how the operator sees it is still
        # working when the percentage barely moves.
        bar = self.bar(total=10_000, passes=1, interval=999)
        bar.label("first.mov")
        bar.label("second.mov")
        self.assertIn("second.mov", self.seen[-1][2])

    def test_no_rate_is_claimed_before_there_is_one(self):
        # A wild "about 4 hr left" in the first moment is worse than
        # saying nothing.
        bar = self.bar(total=10_000, passes=1)
        bar.add(100)
        self.assertNotIn("/s", self.seen[-1][2])

    def test_rate_and_estimate_appear_once_measurable(self):
        bar = self.bar(total=10_000, passes=1, interval=0.0)
        bar.add(1_000)
        self.clock.advance(2.0)
        bar.add(1_000)
        text = self.seen[-1][2]
        self.assertIn("/s", text)
        self.assertIn("left", text)

    def test_it_does_nothing_without_a_report(self):
        bar = ByteProgress(100, passes=1, report=None)
        bar.add(50)
        bar.finished()
        self.assertEqual(bar.done_bytes, 100)


class HumanTests(unittest.TestCase):
    def test_bytes_read_as_a_person_would_say_them(self):
        self.assertEqual(human_bytes(0), "0.0 B")
        self.assertEqual(human_bytes(18_000_000_000), "18.0 GB")

    def test_durations_get_vaguer_as_they_get_longer(self):
        # Read once, to decide whether there is time to go and do
        # something else — not watched.
        self.assertEqual(human_duration(45), "45 sec")
        self.assertEqual(human_duration(600), "10 min")
        self.assertEqual(human_duration(7200), "2.0 hr")

    def test_a_nonsense_duration_says_nothing(self):
        self.assertEqual(human_duration(-1), "")


class CopyReportsBytesTests(unittest.TestCase):
    """copy_verified has to report both passes.

    It writes the file and then re-reads it to prove what landed. The
    re-read is the whole point of the function, and it is half the wait
    on a large clip, so a bar that ignored it would still freeze.
    """

    def test_both_the_copy_and_the_verify_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mov"
            payload = b"x" * (CHUNK * 2 + 17)
            source.write_bytes(payload)

            seen: list[tuple[int, str]] = []
            copy_verified(source, root / "out" / "clip.mov", md5_file(source),
                          lambda delta, phase: seen.append((delta, phase)))

            phases = {phase for _, phase in seen}
            self.assertEqual(phases, {"copy", "verify"})
            for phase in ("copy", "verify"):
                moved = sum(d for d, p in seen if p == phase)
                self.assertEqual(moved, len(payload), f"{phase} under-reported")

    def test_it_reports_in_chunks_rather_than_one_lump(self):
        # The point of the exercise: a big file must produce many
        # reports, not one at the end.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mov"
            source.write_bytes(b"y" * (CHUNK * 3))
            seen: list[int] = []
            copy_verified(source, root / "out.mov", md5_file(source),
                          lambda delta, phase: seen.append(delta))
            self.assertGreaterEqual(len(seen), 6, "3 chunks copied + 3 verified")

    def test_progress_cannot_change_the_verdict(self):
        # A reporting callback that throws must not be able to make a
        # bad copy look good, or a good one fail.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "clip.mov"
            source.write_bytes(b"z" * 1000)
            with self.assertRaises(IOError):
                copy_verified(source, root / "out.mov", "not-the-right-hash")
            self.assertFalse((root / "out.mov").exists())
            self.assertEqual(list(root.glob("*.footage-guardian-part")), [])


if __name__ == "__main__":
    unittest.main()
