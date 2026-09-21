from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "Footage Guardian Auto DIT.command"


class LauncherTests(unittest.TestCase):
    """The file the operator double-clicks.

    Two failures on his Mac (macOS 15.7.3, Apple Silicon), one day, same
    root cause:

    1. The launcher was `python3 -m footage_guardian.cli`, which takes
       whatever python3 is first on PATH. That was Apple's 3.9.6 from the
       Command Line Tools.
    2. It was then pinned to /usr/bin/python3 — the same interpreter —
       on the mistaken belief that Apple's Tk always matches the OS.

    Apple's python3 borrows /System/Library/Frameworks/Tk.framework,
    which is Tcl/Tk 8.5.9 and frozen years ago. It aborts inside TkpInit
    with a Tcl_Panic and macOS shows a crash report. It survives on the
    author's newer macOS, which is how it got shipped — so these tests
    assert the rule, not any one machine's observation.
    """

    def setUp(self):
        self.body = LAUNCHER.read_text()
        self.code = "\n".join(
            line for line in self.body.splitlines() if not line.lstrip().startswith("#")
        )

    def test_the_launcher_exists_and_is_executable(self):
        self.assertTrue(LAUNCHER.is_file(), f"{LAUNCHER.name} is missing")
        self.assertTrue(
            os.access(LAUNCHER, os.X_OK),
            "double-clicking it in Finder needs the executable bit",
        )

    def test_it_never_runs_a_bare_python3_from_the_path(self):
        self.assertIsNone(
            re.search(r"(?<![/\w\"$])python3\s+-m\s+footage_guardian", self.code),
            "the launcher must choose its interpreter, not inherit one",
        )

    def test_it_requires_tk_86_or_newer(self):
        # The one rule that separates a Python that can draw a window
        # from one that aborts: 8.5 is Apple's frozen system Tk, 8.6+ is
        # a Tk the Python brought with it.
        self.assertIn(
            "TkVersion >= 8.6",
            self.code,
            "the interpreter has to be screened on its Tk version",
        )

    def test_it_does_not_pin_apples_system_python(self):
        self.assertNotIn(
            "/usr/bin/python3",
            self.code,
            "Apple's python3 is the one that aborts — it must not be a "
            "candidate, and the Tk screen alone should not be relied on "
            "to remember why",
        )

    def test_the_screen_does_not_initialise_a_window(self):
        # Reading tkinter.TkVersion loads the library without starting
        # the GUI. Creating a Tk root to probe would trigger the very
        # abort being screened for, and the operator would get the crash
        # report anyway.
        self.assertNotIn("Tk()", self.code)
        self.assertIn("tkinter.TkVersion", self.code)

    def test_it_says_what_to_do_when_no_python_can_draw(self):
        lowered = self.body.lower()
        self.assertIn("cannot start", lowered)
        self.assertIn("brew install python-tk", self.body)
        self.assertIn("python.org", self.body)

    def test_it_is_valid_zsh(self):
        result = subprocess.run(
            ["zsh", "-n", str(LAUNCHER)], capture_output=True, text=True
        )
        self.assertEqual(
            result.returncode, 0, f"zsh syntax error: {result.stderr.strip()}"
        )

    def test_apples_python_is_correctly_rejected_by_the_screen(self):
        # Runs the real screen against the real interpreter that failed.
        apple = Path("/usr/bin/python3")
        if not apple.exists():
            self.skipTest("no /usr/bin/python3 on this machine")
        result = subprocess.run(
            [
                str(apple),
                "-c",
                "import sys, tkinter; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)",
            ],
            capture_output=True,
        )
        self.assertNotEqual(
            result.returncode, 0, "Apple's Tk 8.5 must not pass the screen"
        )

    def test_the_chosen_interpreter_can_import_the_app(self):
        chosen = subprocess.run(
            ["zsh", "-c", self._selection_snippet()],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not chosen:
            self.skipTest("no python with Tk 8.6+ on this machine")

        result = subprocess.run(
            [chosen, "-c", "import footage_guardian.cli"],
            cwd=LAUNCHER.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0, f"{chosen} cannot import the app: {result.stderr}"
        )

    @staticmethod
    def _selection_snippet() -> str:
        """The launcher's own candidate list, used to report its choice."""
        return (
            "typeset -a c; c=("
            "/Library/Frameworks/Python.framework/Versions/3.*/bin/python3(NOn) "
            "/opt/homebrew/bin/python3(N) /usr/local/bin/python3(N) "
            "${commands[python3]}); "
            "for p in $c; do [[ -n \"$p\" && -x \"$p\" ]] || continue; "
            '"$p" -c "import sys,tkinter; sys.exit(0 if tkinter.TkVersion>=8.6 else 1)" '
            ">/dev/null 2>&1 || continue; print -r -- $p; break; done"
        )


if __name__ == "__main__":
    unittest.main()
