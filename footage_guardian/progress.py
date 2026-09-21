"""Turning byte counts into something a bar can move on.

Progress used to be reported once per file. A shoot day is a handful of
very large clips, so on an 18GB file the bar sat still for minutes and
read as a hang — the operator's instinct is to force-quit, mid-copy, on
footage that cannot be reshot. That is the bug this module exists to
fix.

The unit is the *data* byte, not the read. Every file is handled in
several passes — its source is hashed, copied, and the copy re-read to
verify it — so a byte of footage is touched three times on an offload
and more when several drives are involved. Reporting raw reads would
have the bar claim 61GB of work on 20GB of footage. Each pass is
weighted instead, so a finished file has contributed exactly its own
size and the total is the size of the footage.
"""

from __future__ import annotations

import time
from typing import Callable

Report = Callable[[int, int, str], None]


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000:
            return f"{n:,.1f} {unit}"
        n /= 1000
    return f"{n:,.1f} PB"


def human_duration(seconds: float) -> str:
    """Rounded, and deliberately vague past an hour.

    A countdown precise to the second invites watching it. This is read
    once, to decide whether there is time to go and do something else.
    """
    if seconds < 0 or seconds != seconds:  # negative or NaN
        return ""
    if seconds < 90:
        return f"{int(seconds)} sec"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(round(minutes))} min"
    return f"{minutes / 60:.1f} hr"


class ByteProgress:
    """Accumulates byte deltas and reports data-equivalent progress.

    `passes` is how many times each byte of footage will be read or
    written before its file is done: 3 for an offload (hash, copy,
    verify), 1 + 2 per drive for a backup. Deltas are divided by it, so
    the reported total is the footage size rather than the I/O volume.

    Reports are throttled. At 240 MB/s an 8MB chunk arrives 30 times a
    second, and every report crosses onto the Tk main thread; flooding
    that queue is its own way to make a window look hung.
    """

    def __init__(self, total_bytes: int, passes: int, report: Report | None,
                 min_interval: float = 0.1,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.total = max(0, int(total_bytes))
        self.passes = max(1, int(passes))
        self._report = report
        self._min_interval = min_interval
        self._clock = clock
        self._done = 0.0
        self._label = ""
        self._started = clock()
        self._last_report = 0.0
        self._reported_once = False

    @property
    def done_bytes(self) -> int:
        return int(self._done)

    def label(self, text: str) -> None:
        """Name the file being worked on. Always reported, never throttled."""
        self._label = text
        self._emit(force=True)

    def add(self, delta: int, phase: str = "") -> None:
        """One chunk was read or written. `phase` is for the caller's benefit."""
        if delta <= 0:
            return
        self._done = min(float(self.total), self._done + delta / self.passes)
        self._emit()

    def finished(self, label: str = "") -> None:
        """Round up to the total, so the bar never stops at 99%."""
        self._done = float(self.total)
        if label:
            self._label = label
        self._emit(force=True)

    # -------------------------------------------------------------- internals

    def _emit(self, force: bool = False) -> None:
        if self._report is None:
            return
        now = self._clock()
        if not force and self._reported_once and now - self._last_report < self._min_interval:
            return
        self._last_report = now
        self._reported_once = True
        self._report(int(self._done), self.total, self.text())

    def text(self) -> str:
        parts = []
        if self._label:
            parts.append(self._label)
        parts.append(f"{human_bytes(self._done)} of {human_bytes(self.total)}")
        elapsed = self._clock() - self._started
        # Under a second of samples the rate is mostly noise, and a wild
        # "4 hr remaining" in the first moment is worse than saying nothing.
        if elapsed >= 1.0 and self._done > 0:
            rate = self._done / elapsed
            parts.append(f"{human_bytes(rate)}/s")
            remaining = self.total - self._done
            if remaining > 0:
                eta = human_duration(remaining / rate)
                if eta:
                    parts.append(f"about {eta} left")
        return "  ·  ".join(parts)
