# Footage Guardian Auto DIT — Project Instructions

## What this is
A local-first **Mac desktop app** that safeguards original camera footage for a
travelling videographer. It offloads camera cards onto an SSD, keeps a verified
second local copy, and pushes a verified copy to Google Drive. It **never deletes
or edits source footage**.

This is a standalone project. It has nothing to do with any other repo on this
machine — if you have loaded instructions about a SaaS, content pipelines, or
Next.js, they are the wrong project.

## Who it is for
One named user: **Kevin**, a working videographer, on one Mac. Not a product, no
accounts, no multi-tenancy, no hosted component. Decisions should optimise for
"Kevin can trust this with footage he cannot reshoot", not for generality.

The author is a solo non-engineer. Explain architectural decisions in plain
English, and say plainly when something is a risk rather than burying it.

## The safety model — these are not negotiable
This app handles footage that cannot be recreated. Every rule below exists
because breaking it can destroy someone's work.

- **Source footage is read-only. Always.** The guardian never deletes, moves, or
  edits anything on a source drive or camera card. No exceptions, no flags.
- **Nothing counts as copied until it is verified.** Every copy goes through a
  `.footage-guardian-part` temp file, is flushed and fsynced, then checked for
  matching size *and* MD5, and only then atomically renamed into place.
- **Never overwrite differing content.** If a destination already holds a file
  whose contents differ from the source, refuse and raise ERROR naming both
  paths. Camera cards reuse filenames; silently replacing is data loss.
- **Deletion is only ever a local backup, never a source.** The space-recovery
  tool can remove only a manifest-recorded file inside the configured backup
  root, after re-verifying the cloud copy twice, with explicit confirmation.
  `engine._validated_backup_path` enforces the boundary — keep it that way.
- **Refuse rather than risk.** When state is ambiguous, stop and tell the human.
  A confused app that halts is fine; a confident app that guesses is not.

## The folder convention
One tree, three places. The offload assistant writes the production's existing
convention onto the SSD:

```
M-D-YY / Main Cam / card 1 or card 2 / original card folders / filename
M-D-YY / 360   / original card folders / filename
M-D-YY / Drone / original card folders / filename
M-D-YY / meta glasses / filename
```

The backup drive and Google Drive then **mirror the source folder-for-folder**.
Whatever Kevin sees on the SSD is exactly what he sees in Drive — that is what
makes spot-checking both destinations possible before formatting a card.

Kevin also points the guardian straight at raw camera cards, which carry no date
or camera name of their own. Those get the same wrapper built automatically
(`engine._archive_prefix`):

- **Camera** is detected from the card's folder structure, preferring any name
  Kevin previously confirmed for that structure in the offload dialog. An
  unrecognised card is filed under its volume name and logs a WARNING.
- **Date** comes from the earliest clip's timestamp, not today's date, so a card
  read after midnight still files under the day it was shot.
- **Slot** comes from the card's fingerprint (a hash of every file's path and
  size). Nothing on a second card says it is the second card, so a card already
  filed keeps its slot and a genuinely new one takes the next free slot that day.
  Main Cam always gets a slot; other cameras only from the second card onward.

The decision is made **once per drive and then remembered**, so rescans never
refile footage and a card remounted elsewhere keeps its slot.

**Date folders are written `M-D-YY` (2026-09-11).** Kevin's existing archive was
typed into Finder as `7/22/26`, which macOS stores on disk as `7:22:26` — that
colon form is what the drives and Google Drive actually contain. New folders use
hyphens, but `looks_offloaded` accepts both, because failing to recognise a date
folder buries an already-organised drive inside a second dated wrapper.

Decided 2026-08-10. If you change path logic, change it in one place and keep all
three trees identical.

## Stack (do not deviate without asking)
- Python 3.10+, standard library only — no runtime dependencies
- Tkinter for the UI, on a Python that **ships its own Tk 8.6+**. Apple's
  `/usr/bin/python3` borrows the frozen system Tcl/Tk 8.5.9 and aborts in
  `TkpInit`; "ships with Python, no install for Kevin" turned out to be
  false on his Mac. The launcher screens for this — see the Current state
  section.
- SQLite in WAL mode for the persistent manifest
- `rclone` shells out for all Google Drive work
- **One hosted component, agreed 2026-09-21: ntfy.sh**, for the
  finished-transfer notification and nothing else. A stdlib `urllib` POST
  to a secret topic, no account, no dependency. The ping carries **no
  shoot date, file count, client name or path** — a topic URL is a
  guessable shared secret, so nothing about the work may travel over it.
  "Footage Guardian: transfer finished" and that is all. The app must
  still work fully with notifications switched off or the network down;
  a failed POST is logged and never fails a transfer.

**Why rclone and not the Drive API:** rclone already handles OAuth, chunked
resumable uploads, retries, rate limiting, and remote hashes, and is heavily
exercised on very large files. Cost is one prerequisite install and a one-time
`rclone config`. Do not replace it with a hand-rolled API client.

**Why not a web app:** a browser cannot read `/Volumes`, MD5 an 18GB file, drive
rclone, or eject a card. This has to be a local process. An earlier attempt at a
Next.js version was abandoned for exactly this reason — do not revive it.

## Layout
- `footage_guardian/engine.py` — the guardian loop, state machine, space recovery
- `footage_guardian/ingest.py` — camera-card detection and guided offload
- `footage_guardian/meta_import.py` — Meta glasses import from Downloads
- `footage_guardian/storage.py` — verified copy primitives and the rclone wrapper
- `footage_guardian/manifest.py` — SQLite schema and queries
- `footage_guardian/config.py` — settings, `~/Library/Application Support/…`
- `footage_guardian/ui.py` — Tkinter window
- `Footage Guardian Auto DIT.command` — what Kevin double-clicks

The Python package stays `footage_guardian` even though the app is named
"Footage Guardian Auto DIT"; renaming it churns every import for no user benefit.

## Hard rules for working here
- **Write a test for every behaviour that protects footage.** Run the suite and
  show passing output before calling anything done.
- Small commits with clear messages after each working step.
- Every user-facing string is read by a videographer under time pressure on a
  shoot. Plain English, say what to do next, never a stack trace.
- If a task feels bigger than one session, say so and propose how to split it.

## Testing reality check
The suite uses temp folders and a **fake rclone**. It proves the logic, not the
integration. Do not let passing tests be mistaken for "this works on Kevin's
machine" — use `python3 tools/dry_run.py`, which exercises real rclone against a
disposable Drive folder and checks arrival, MD5 match, tree shape, slot
allocation, and that sources come back untouched.

Local half verified end-to-end 2026-08-10. **Cloud half verified 2026-08-11** —
real uploads to My Drive, remote MD5s compared against the local backup, tree
shape and card slots correct, sources unchanged, then the folder purged.

A caution learned the hard way: the fake rclone only ever returns what the code
already expects, so it cannot catch a wrong assumption about rclone's *output*.
Anything that parses rclone JSON needs a test driving the real parser against a
real-shaped payload — see `tests/test_cloud_verify.py`.

## Commands
- Run the app: `python3 -m footage_guardian.cli`
- One headless scan: `python3 -m footage_guardian.cli --once`
- Tests: `python3 -m unittest discover -v`

## Current state (2026-09-16)
**The camera workflow is built, tested and packaged for Kevin** at
`~/Desktop/FootageGuardian-for-Kevin.zip`. 59 tests pass; `tools/dry_run.py`
is green against real Google Drive. `HANDOVER.md` has the full history, the
account layout, the real archive's shape and hard-won cautions — it is kept
out of this repo because it names the client's account and archive, and lives
alongside it on the maintainer's machine.

The window is now **three tabs matching how Kevin works**: copy every camera
onto the SSD main drive, back that day up to both HDDs, then sync it to Drive.
Each is a button he presses. The old continuous background mode was removed
(2026-09-15) — two ways of doing the same job is how people trust the wrong one.

Known gaps, in priority order:
1. **Meta glasses and phone have no route in.** Neither mounts as a drive.
   Deliberately deferred so the camera workflow could ship. Kevin has never
   offloaded any glasses footage; he prefers the Meta app because it carries the
   date and sometimes location. Before building: find out what that app exports,
   where it lands, and whether it preserves *capture* date rather than export
   date. See `~/Desktop/kevin-chat-2026-09-16.md`.
2. **Never run against a real camera card.** Still true — the dry run only uses
   synthetic cards. Run `python3 tools/dry_run.py --card /Volumes/<card>`
   against each real camera before the shoot.
3. **The UI has never been clicked.** Every engine call is tested, but the
   wiring from button to engine is unproven: macOS denies this terminal both
   Screen Recording and Accessibility, so GUI work must be driven through the
   engine or done by hand. The window itself is now known to open — the
   launcher was run for real on 2026-09-21 and stayed up — but no button in
   it has been pressed by anyone.

   The first real attempt on the operator's Mac (macOS 15.7.3, Apple
   Silicon) crashed, twice, for one reason: **Apple's `/usr/bin/python3`
   cannot draw a window.** It borrows
   `/System/Library/Frameworks/Tk.framework`, which is Tcl/Tk 8.5.9 and
   frozen years ago, and aborts inside `TkpInit` with a `Tcl_Panic` — macOS
   shows a crash report instead of the app. `python3 -m footage_guardian.cli`
   resolved to it (Homebrew installs no Python of its own, so PATH still led
   there), and the first fix then *pinned* it there, on the false premise
   that Apple's Tk always matches the OS.

   The rule that actually holds: **take a Python that ships its own Tk.**
   python.org's installer bundles `libtk8.6.dylib` inside its framework and
   Homebrew's `python-tk` brings its own; Apple's borrows the system one.
   The launcher now takes the first python3 reporting Tk >= 8.6, which rules
   Apple's out by version alone. Reading `tkinter.TkVersion` is safe —
   it loads the library without initialising the GUI, so screening cannot
   trigger the abort it screens for. `tests/test_launcher.py` guards all of
   it and fails against both earlier versions.

   **The lesson worth keeping: Tk 8.5 survived here and aborted there, and
   that one observation was treated as proof.** Anything the operator's
   machine does differently is untested until it is run *there*. This
   machine cannot validate a GUI fix for his.
4. **DJI device strings are inferred, not confirmed.** `DEVICE_NAME_PATTERNS`
   matches on DJI's model naming. Kevin running `tools/identify_devices.py`
   with everything plugged in gives the real strings.
5. **rclone's shared Google client_id is being retired during 2026.** A Cloud
   project exists but publishing is blocked (restricted Drive scopes need a
   verified domain), and Testing-mode tokens expire weekly. Both remotes run on
   the shared client_id today. Details in `HANDOVER.md`.
6. **Volume identity is not pinned.** A different disk mounted at the same path
   is silently trusted as the same drive. Should pin macOS volume UUIDs.

## In progress — agreed scope (2026-09-21)

Five pieces, in build order. Each lands with tests and is usable on its own.
Written down because the first two asks turned out to be already built, and
the difference between "add a transfer button" and "the bar does not move on
an 18GB clip" is the whole job.

**1. Byte-level progress.** `copy_verified` moves whole files, and progress is
reported per file, so a single 18GB clip freezes the bar for minutes and reads
as hung. Copy in chunks, report bytes, and show transferred / total, rate and
rough time remaining. The MD5 is already computed while copying — do not add a
second read of the file to get a percentage.
*Done when:* the bar advances during one large file, and the suite proves
progress is reported in byte increments rather than once per file.

**2. Drive auto-detection.** The Drives tab is three text boxes. Every signal
needed to fill them already exists — `classify_source`, `device_identity`,
`looks_offloaded`. Detect the plugged-in volumes, propose which is the SSD
main drive and which are Back up HDD 1 and 2, and let Kevin confirm. The
dropdown keeps every mounted volume so a wrong guess is one click to fix.
Detection proposes; it never silently rewrites a saved setting.
*Done when:* a Mac with the three drives plugged in shows them pre-filled and
correct, and an unplugged drive is shown as missing rather than blanked.

**3. Confirm-first card flow.** `inspect_card` already suggests the camera;
Kevin has to press "Detect Card" to see it. Detect on open and on card change,
and lead with the answer — "Main Cam, card 2, 412 files, 61 GB" and a Confirm
button — with the dropdown there for when it is wrong.
**Fix the camera list while doing it:** it is hardcoded to `("Main Cam", "360",
"Drone")`, and `ui.py` blanks the field when the detection is not one of the
three. A correctly-detected DJI Osmo currently leaves Kevin with an empty
dropdown and no way to say what it is. The Osmo and the drone write identical
cards, so this is the one the hardware check exists to answer.
*Done when:* every device in "How Kevin actually works" can be chosen, and a
detected Osmo arrives pre-selected rather than blank.

**4. Transfer on the main window.** Copying a camera lives behind
`CardOffload`, a dialog he has to open first. Bring the detected cards, the
confirm step and the transfer onto tab 1 so the common path is visible on
arrival. Keep one job at a time.
*Done when:* a card can be copied without opening a dialog, and the existing
busy-guard still prevents two transfers at once.

**5. Finished-transfer notification.** A ping to his phone when a transfer
ends, so he can walk away. ntfy.sh, per the Stack note above — contentless,
optional, and unable to fail a transfer. Off unless a topic is configured.
*Done when:* finishing a transfer sends one notification, a broken network
leaves the transfer reported as successful, and the suite covers both without
touching the network.

Out of scope for now: notifications for anything but a finished transfer,
and any notification carrying detail about the work.

## How Kevin actually works (2026-09-16)
Three deliberate stages, in order, each a button he presses:

1. **Offload every device onto the SSD main drive**, all in one session, into a
   single folder named for the day (`M-D-YY`) with a folder per camera inside.
2. **Plug in both backup HDDs** and duplicate that whole day onto each.
3. **Sync that day to Google Drive.**

- **Devices:** Main Cam (Lumix), 360 (Insta360), Drone (DJI), **DJI Osmo**,
  Meta glasses, phone. The Osmo is new and matters: it and the drone write
  identical cards, so only the plugged-in hardware can tell them apart.
- **He plugs the cameras in directly**, not their cards into a reader. This is
  what makes drone-versus-Osmo answerable at all — say so in any instructions.
- **Main Cam cards are sequential, not simultaneous** — card 1 fills, he swaps
  to card 2. The two hold *different* footage and must never be merged.
- **Drive naming he uses:** SSD main drive, Back up HDD 1, Back up HDD 2.
- **Google Drive** is a Workspace plan with room to spare; storage limits are not
  a design constraint.
- **Meta glasses and phone** have no route in yet. He has never offloaded any
  glasses footage. They reach the Meta app and his phone, neither of which
  mounts as a drive.
- **Interaction is manual** by choice. No auto-start on plug-in, no background
  mode — that was removed 2026-09-15.
