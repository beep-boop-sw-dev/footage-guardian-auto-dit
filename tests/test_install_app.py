from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import install_app  # noqa: E402

LAUNCHER = Path(__file__).resolve().parent.parent / "Footage Guardian Auto DIT.command"


class InstallAppTests(unittest.TestCase):
    """The app Kevin clicks instead of the .command file.

    None of these open a window. The bundle is run directly, with a stand-in
    launcher that leaves a marker, and osascript replaced so no dialog can
    appear and hang the suite.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.folder = self.tmp / "Footage Guardian"
        self.folder.mkdir()
        self.marker = self.tmp / "launched"
        self.launcher = self.folder / "Footage Guardian Auto DIT.command"
        self.launcher.write_text(f"#!/bin/zsh\nprint -r -- ran > {self.marker}\n")
        self.launcher.chmod(0o755)
        self.applications = self.tmp / "Applications"
        # A fake osascript that records what it was asked to show.
        fakes = self.tmp / "bin"
        fakes.mkdir()
        self.alerts = self.tmp / "alerts"
        osascript = fakes / "osascript"
        osascript.write_text(f'#!/bin/zsh\nprint -r -- "${{@[-1]}}" >> {self.alerts}\n')
        osascript.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{fakes}:/usr/bin:/bin:/usr/sbin:/sbin")

    def tearDown(self):
        self._tmp.cleanup()

    def run_app(self, app: Path) -> subprocess.CompletedProcess:
        executable = app / "Contents" / "MacOS" / install_app.APP_NAME
        return subprocess.run([str(executable)], env=self.env, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=30)

    def test_clicking_the_app_runs_the_launcher_in_the_folder(self):
        app = install_app.build_app(self.applications, self.launcher)
        self.assertEqual(self.run_app(app).returncode, 0)
        self.assertEqual(self.marker.read_text().strip(), "ran")

    def test_a_folder_with_spaces_and_quotes_still_launches(self):
        odd = self.tmp / "Kevin's Footage Guardian"
        odd.mkdir()
        launcher = odd / self.launcher.name
        launcher.write_text(self.launcher.read_text())
        app = install_app.build_app(self.applications, launcher)
        self.assertEqual(self.run_app(app).returncode, 0)
        self.assertTrue(self.marker.exists())

    def test_a_moved_folder_is_reported_not_silent(self):
        app = install_app.build_app(self.applications, self.launcher)
        self.launcher.unlink()
        result = self.run_app(app)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())
        said = self.alerts.read_text()
        self.assertIn("cannot find its folder", said)
        self.assertIn(str(self.launcher.resolve()), said)

    def test_the_bundle_is_one_macos_accepts(self):
        app = install_app.build_app(self.applications, self.launcher)
        plist = app / "Contents" / "Info.plist"
        lint = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)
        info = plistlib.loads(plist.read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], install_app.BUNDLE_ID)
        executable = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
        self.assertTrue(os.access(executable, os.X_OK))
        self.assertIn("never changes or deletes", info["NSRemovableVolumesUsageDescription"])

    def test_reinstalling_replaces_its_own_app(self):
        install_app.build_app(self.applications, self.launcher)
        moved = self.tmp / "Moved"
        moved.mkdir()
        new_launcher = moved / self.launcher.name
        new_launcher.write_text(self.launcher.read_text())
        app = install_app.build_app(self.applications, new_launcher)
        script = (app / "Contents" / "MacOS" / install_app.APP_NAME).read_text()
        self.assertIn(str(new_launcher.resolve()), script)
        self.assertEqual([p.name for p in self.applications.iterdir()], [app.name])

    def test_it_never_replaces_someone_elses_app(self):
        other = self.applications / f"{install_app.APP_NAME}.app" / "Contents"
        other.mkdir(parents=True)
        (other / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
        (other / "keep-me").write_text("theirs")
        with self.assertRaises(install_app.InstallRefused):
            install_app.build_app(self.applications, self.launcher)
        self.assertEqual((other / "keep-me").read_text(), "theirs")

    def test_it_refuses_without_a_launcher(self):
        with self.assertRaises(install_app.InstallRefused):
            install_app.build_app(self.applications, self.tmp / "nowhere.command")
        self.assertFalse((self.applications / f"{install_app.APP_NAME}.app").exists())

    def test_the_installer_runs_on_apples_python(self):
        # `python3` in Kevin's Terminal may still be Apple's 3.9.
        apple = Path("/usr/bin/python3")
        if not apple.exists():
            self.skipTest("no /usr/bin/python3 on this machine")
        tools = Path(install_app.__file__).parent
        result = subprocess.run(
            [str(apple), "-c", f"import sys; sys.path.insert(0, {str(tools)!r}); "
             f"import install_app; from pathlib import Path; "
             f"install_app.build_app(Path({str(self.applications)!r}), Path({str(self.launcher)!r}))"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class LauncherFromTheDockTests(unittest.TestCase):
    """What changes when the launcher is opened as an app, not from Terminal."""

    def setUp(self):
        self.code = "\n".join(line for line in LAUNCHER.read_text().splitlines()
                              if not line.lstrip().startswith("#"))

    def test_homebrew_is_on_the_path_so_rclone_is_found(self):
        # Opened from the Dock, PATH is /usr/bin:/bin:/usr/sbin:/sbin and
        # rclone in /opt/homebrew/bin would be missing for every Drive sync.
        assignment = next(line for line in self.code.splitlines() if line.startswith("path=("))
        snippet = f"{assignment}; export PATH; print -r -- $PATH"
        result = subprocess.run(["zsh", "-fc", snippet], capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())})
        entries = result.stdout.strip().split(":")
        self.assertIn("/opt/homebrew/bin", entries)
        self.assertIn("/usr/local/bin", entries)
        self.assertLess(entries.index("/opt/homebrew/bin"), entries.index("/usr/bin"))

    def test_without_a_terminal_problems_go_to_a_dialog(self):
        self.assertIn("osascript", self.code)
        self.assertIn("-t 1", self.code)
        self.assertIn("brew install python-tk", self.code.split("from_app )); then")[-1])


if __name__ == "__main__":
    unittest.main()
