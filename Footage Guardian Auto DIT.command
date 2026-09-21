#!/bin/zsh
cd "${0:A:h}"

# Which python3 runs this matters far more than it looks.
#
# Tkinter's Tk framework has to match the macOS it is running on. A
# Homebrew python3 is built against the newest SDK, so on a Mac a
# version or two behind it aborts with
#
#     macOS 15 (1507) or later required, have instead 15 (1504)
#
# and macOS shows a "Python quit unexpectedly" crash report rather than
# this app. Installing Homebrew is enough to put such a python3 at the
# front of $PATH, which is exactly what the setup instructions ask the
# operator to do first — so `python3` on its own is the one thing this
# launcher must not rely on.
#
# /usr/bin/python3 is Apple's. It arrives with the Command Line Tools
# that Homebrew installs anyway, and its Tk is always the one that
# matches the OS. It is 3.9, which is older than pyproject's stated
# floor, but this app is standard-library only and the whole suite
# passes on it. Deterministic beats new here: every Mac then runs the
# same interpreter.
for python in /usr/bin/python3 "${commands[python3]}"; do
  [[ -n "$python" && -x "$python" ]] || continue
  "$python" -c 'import tkinter' >/dev/null 2>&1 || continue
  exec "$python" -m footage_guardian.cli
done

# Never a stack trace: whoever sees this is a videographer, possibly on
# a shoot, and needs to know what to do next.
print -r -- ""
print -r -- "Footage Guardian could not start."
print -r -- ""
print -r -- "It needs a copy of Python that can draw windows, and could not"
print -r -- "find one. Nothing is wrong with your footage or your drives."
print -r -- ""
print -r -- "Send Stuart a photo of this window. To fix it he will ask you"
print -r -- "to run:  xcode-select --install"
print -r -- ""
print -r -- "(checked: /usr/bin/python3 and ${commands[python3]:-no python3 on PATH})"
print -r -- ""
print -r -- "Press any key to close."
read -k 1 -s
