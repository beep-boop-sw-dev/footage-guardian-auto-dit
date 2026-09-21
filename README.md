# Footage Guardian Auto DIT

A local-first Mac app that safeguards original camera footage for a travelling
videographer. It offloads camera cards onto an SSD, keeps a verified second and
third local copy, and pushes a verified copy to Google Drive. It **never deletes
or edits source footage**.

Kevin's own instructions are in `KEVIN-START-HERE.txt`. This file is for whoever
is working on the code.

## The three stages

One window, one tab per stage, in the order a shoot day happens. Each is a
button pressed when the operator is ready — there is no background mode, and
there deliberately has not been one since 2026-09-15. Two ways of doing the same
job is how people come to trust the wrong one.

| Stage | Tab | Engine method |
|---|---|---|
| 1. Copy every camera onto the SSD main drive | `1 · Copy footage` | `CardIngester.offload` |
| 2. Copy that day onto both backup HDDs | `2 · Back up to HDDs` | `Guardian.backup_day` |
| 3. Upload that day to Google Drive | `3 · Sync to Drive` | `Guardian.sync_day` |

Above the tabs sits a standing summary of those three stages for the selected
day. It answers the only question that really matters — *is this card safe to
format?* — without anyone having to remember what they already did. Three ticks
means four copies exist and every one has been checksum verified.

Drives are named in the operator's own language: **SSD main drive**,
**Back up HDD 1**, **Back up HDD 2**.

## The safety model

Every rule here exists because breaking it can destroy work that cannot be
reshot. See `CLAUDE.md` for the full statement of these.

- **Source footage is read-only. Always.** No flags, no exceptions.
- **Nothing counts as copied until it is verified.** Every copy goes through a
  `.footage-guardian-part` temp file, is flushed and fsynced, checked for
  matching size *and* MD5, and only then atomically renamed into place.
- **Never overwrite differing content.** A destination file whose contents
  differ from the source raises ERROR naming both paths. Camera cards reuse
  filenames; silently replacing one is data loss.
- **Deletion only ever touches a local backup, never a source.** Space recovery
  can remove only a manifest-recorded file inside the configured backup root,
  after re-verifying the cloud copy twice, with explicit confirmation.
- **Refuse rather than risk.** When state is ambiguous, stop and say so. A
  confused app that halts is fine; a confident app that guesses is not.

## The folder convention

One tree, three places. The offload assistant writes the production's existing
convention onto the SSD, and the backup drives and Google Drive then mirror it
folder-for-folder — so whatever is on the SSD is exactly what is in Drive, which
is what makes spot-checking both before formatting a card possible.

```
M-D-YY / Main Cam / card 1 or card 2 / original card folders / filename
M-D-YY / 360   / original card folders / filename
M-D-YY / Drone / original card folders / filename
M-D-YY / meta glasses / filename
```

Raw camera cards carry no date or camera name of their own, so the wrapper is
built automatically (`engine._archive_prefix`):

- **Camera** comes from the plugged-in hardware's USB product string where macOS
  reports one, falling back to the card's folder structure. A DJI drone and a
  DJI Osmo write identical cards, so only the hardware can separate them — an
  unrecognised DJI device is refused rather than guessed at.
- **Date** comes from the earliest clip's timestamp, not today's, so a card read
  after midnight still files under the day it was shot.
- **Slot** comes from the card's fingerprint (a hash of every file's path and
  size), so a card already filed keeps its slot and a genuinely new one takes
  the next free slot that day.

New folders are written `M-D-YY`. The colon spelling (`7:22:26`) is also
accepted on read, because that is what macOS stores when a date is typed into
Finder as `7/22/26`, and the existing archive is full of them.

## Stack

Python 3.10+, standard library only — no runtime dependencies. Tkinter for the
UI, SQLite in WAL mode for the persistent manifest, and `rclone` shelled out for
all Google Drive work.

**Why rclone and not the Drive API:** it already handles OAuth, chunked
resumable uploads, retries, rate limiting, and remote hashes, and is heavily
exercised on very large files. The cost is one prerequisite install and a
one-time `rclone config`.

**Why not a web app:** a browser cannot read `/Volumes`, MD5 an 18 GB file,
drive rclone, or eject a card. An earlier Next.js attempt was abandoned for
exactly this reason.

## Layout

- `footage_guardian/engine.py` — the guardian loop, state machine, space recovery
- `footage_guardian/ingest.py` — camera-card detection and guided offload
- `footage_guardian/meta_import.py` — Meta glasses import from Downloads
- `footage_guardian/storage.py` — verified copy primitives and the rclone wrapper
- `footage_guardian/manifest.py` — SQLite schema and queries
- `footage_guardian/config.py` — settings, `~/Library/Application Support/…`
- `footage_guardian/ui.py` — the Tkinter window
- `Footage Guardian Auto DIT.command` — what the operator double-clicks

Application data lives in `~/Library/Application Support/Footage Guardian Auto DIT/`:
`config.json`, `manifest.sqlite3`, and `guardian.log`. WAL mode means progress
survives an ordinary app or computer restart.

## Setup

1. Python 3.10 or newer.
2. `brew install rclone`, or the installer at https://rclone.org/install/
3. `rclone config` — a new remote named `gdrive`, type Google Drive, browser
   sign-in. `KEVIN-START-HERE.txt` walks through the prompts one by one.

## Commands

```sh
python3 -m footage_guardian.cli          # run the app
python3 -m footage_guardian.cli --once   # one headless scan
python3 -m unittest discover -v          # the test suite
python3 tools/dry_run.py                 # end-to-end against real Google Drive
python3 tools/identify_devices.py        # read-only USB device report
```

## Testing reality check

The suite uses temp folders and a **fake rclone**. It proves the logic, not the
integration — do not let passing tests be mistaken for "this works on the
operator's machine".

`tools/dry_run.py` is the one that proves integration: it builds synthetic
camera cards, protects them through real rclone to a disposable Drive folder,
then checks that every file arrived with a matching MD5, that the Drive tree
mirrors the backup tree folder-for-folder, that sequential Main Cam cards got
separate slots, and that the sources came back byte-for-byte untouched. The
destination must contain `DRY-RUN` or it refuses to run. Add `--keep-remote` to
inspect the result, or `--card /Volumes/NAME` to run against a real card.

Two cautions learned the hard way:

- **A test double that only ever agrees cannot catch a wrong assumption.**
  `FakeCloud` used to answer every `verify()` with success, which let a sync
  conclude it had uploaded files it never sent. It now keeps a remote dict and
  answers from it. Anything parsing rclone's JSON needs a test driving the real
  parser against a real-shaped payload — see `tests/test_cloud_verify.py`.
- **Passing engine tests do not mean the window works.** Every engine call was
  tested and correct while the app still froze on launch, because the timer
  walked every mount under `/Volumes` — the startup disk included — on the
  thread that draws the window. `tests/test_ui_wiring.py` drives the real
  window for exactly this reason.

## Known limitations

- **Volume identity is not pinned.** A different disk mounted at the same path
  is trusted as the same drive. Should pin macOS volume UUIDs.
- **Meta glasses and phone have no route in.** Neither mounts as a drive. The
  Mac-side half exists — `meta_import.py` reads recent media from `~/Downloads`
  and verifies it like anything else — but how footage gets from the glasses to
  Downloads is unsettled.
- **rclone's shared Google client_id is being retired during 2026.** A Cloud
  project exists but publishing is blocked: restricted Drive scopes need a
  privacy policy on a verified domain, and Testing-mode tokens expire weekly.
- **After a full process restart** rclone may restart an interrupted file rather
  than resume it, depending on what Google retained. The manifest never treats
  such a file as complete.
- This is a safeguarding assistant, not an archival policy. Keep cards until a
  human has spot-checked both destinations.
