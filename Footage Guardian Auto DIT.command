#!/bin/zsh
cd "${0:A:h}"

# Which python3 runs this decides whether a window opens at all.
#
# Tkinter needs a Tk that works on this Mac, and on macOS there are two
# kinds:
#
#   * A Python that ships its own Tk. python.org's installer bundles
#     libtk8.6.dylib inside its framework; Homebrew's python-tk brings
#     its own too. These work.
#
#   * A Python that borrows /System/Library/Frameworks/Tk.framework,
#     which is Tcl/Tk 8.5.9 and frozen years ago. Apple's
#     /usr/bin/python3 is this one. On the operator's Mac (macOS 15.7.3,
#     Apple Silicon) it aborts inside TkpInit with a Tcl_Panic and macOS
#     shows a crash report instead of the app. It happened to survive on
#     the author's newer macOS, which is exactly why it got shipped once
#     — do not trust a Tk 8.5 result observed on one machine.
#
# So: take the first python3 that reports Tk 8.6 or newer. That test is
# safe to run — reading tkinter.TkVersion loads the library but does not
# initialise the GUI, so it cannot trigger the abort we are screening
# for. Apple's 8.5 is ruled out by the version alone.
typeset -a candidates
candidates=(
  /Library/Frameworks/Python.framework/Versions/3.*/bin/python3(NOn)
  /opt/homebrew/bin/python3(N)
  /usr/local/bin/python3(N)
  ${commands[python3]}
)

for python in $candidates; do
  [[ -n "$python" && -x "$python" ]] || continue
  "$python" -c 'import sys, tkinter; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)' \
    >/dev/null 2>&1 || continue
  exec "$python" -m footage_guardian.cli
done

# Never a stack trace: whoever reads this is a videographer, possibly on
# a shoot, and needs one instruction.
print -r -- ""
print -r -- "Footage Guardian cannot start yet."
print -r -- ""
print -r -- "It needs a version of Python that can draw windows. The one"
print -r -- "built into macOS cannot — that is Apple's, not yours, and"
print -r -- "nothing is wrong with your Mac, your footage or your drives."
print -r -- ""
print -r -- "To fix it, paste this and press Enter:"
print -r -- ""
print -r -- "    brew install python-tk"
print -r -- ""
print -r -- "Then double-click Footage Guardian Auto DIT.command again."
print -r -- ""
print -r -- "If that does not do it, install Python from python.org:"
print -r -- "    https://www.python.org/downloads/macos/"
print -r -- "Take the latest 'macOS 64-bit universal2 installer', open it,"
print -r -- "click through, then try again. Send Stuart this window if you"
print -r -- "are stuck."
print -r -- ""
print -r -- "Press any key to close."
read -k 1 -s
