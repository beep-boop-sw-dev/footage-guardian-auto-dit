from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "Footage Guardian Auto DIT.command"


class LauncherTests(unittest.TestCase):
    """The file the operator double-clicks.

    It used to be three lines ending in `python3 -m footage_guardian.cli`,
    which trusts whatever python3 is first on PATH. Installing Homebrew —
    step one of the setup instructions — is enough to put a python3 there
    whose Tk is built against a newer macOS than the machine runs. Tk then
    aborts with "macOS 15 (1507) or later required" and macOS shows a crash
    report instead of the app. Caught on the operator's Mac, 2026-09-21.
    """

    def test_the_launcher_exists_and_is_executable(self):
        self.assertTrue(LAUNCHER.is_file(), f"{LAUNCHER.name} is missing")
        self.assertTrue(
            os.access(LAUNCHER, os.X_OK),
            "double-clicking it in Finder needs the executable bit",
        )

    def test_it_never_runs_a_bare_python3_from_the_path(self):
        body = LAUNCHER.read_text()
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIsNone(
            re.search(r"(?<![/\w\"])python3\s+-m\s+footage_guardian", code),
            "the launcher must name the interpreter it wants, not inherit "
            "one from PATH",
        )

    def test_it_prefers_apples_python_whose_tk_matches_the_os(self):
        code = LAUNCHER.read_text()
        self.assertIn(
            "/usr/bin/python3",
            code,
            "Apple's python3 ships with the Command Line Tools and its Tk "
            "always matches the running macOS — it is the one that cannot "
            "produce the version-mismatch abort",
        )

    def test_it_says_something_useful_when_no_python_can_draw(self):
        # A videographer mid-shoot gets a sentence and a next step, never
        # a stack trace or a silent window that closes.
        code = LAUNCHER.read_text()
        self.assertIn("could not start", code.lower())
        self.assertIn("xcode-select --install", code)

    def test_it_is_valid_zsh(self):
        result = subprocess.run(
            ["zsh", "-n", str(LAUNCHER)], capture_output=True, text=True
        )
        self.assertEqual(
            result.returncode, 0, f"zsh syntax error: {result.stderr.strip()}"
        )

    def test_the_chosen_interpreter_can_actually_import_the_app(self):
        # The interpreter the launcher will pick has to be able to run the
        # app, not merely exist. 3.9 is below pyproject's stated floor and
        # that is deliberate — the app is standard-library only.
        for python in ("/usr/bin/python3", "python3"):
            probe = subprocess.run(
                [python, "-c", "import tkinter"], capture_output=True
            )
            if probe.returncode == 0:
                chosen = python
                break
        else:
            self.skipTest("no python3 with tkinter on this machine")

        result = subprocess.run(
            [chosen, "-c", "import footage_guardian.cli"],
            cwd=LAUNCHER.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0, f"{chosen} cannot import the app: {result.stderr}"
        )


if __name__ == "__main__":
    unittest.main()
