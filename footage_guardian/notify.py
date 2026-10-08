"""A ping to Kevin's phone when a long job ends, so he can walk away from
the Mac and come back to swap cameras.

Through ntfy.sh: a free app on his phone subscribes to a topic, and this
posts to the same topic. No account, no dependency — one stdlib HTTP POST.

The rules, agreed with Stuart (2026-09-21, loosened 2026-10-08):

- A topic is a shared secret: anyone who guesses it reads the messages.
  So the topic is long and random, and the messages say only which job
  ended and whether it worked. Never a date, count, file, folder, client
  or path. Every message is a fixed string from NOTICES below — nothing
  about the work is ever formatted into one.
- Off unless a topic is set.
- It can never fail or delay a transfer. Sending happens on its own
  thread with a short timeout; a failure is logged and forgotten.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

SERVER = "https://ntfy.sh"
TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Notice:
    title: str
    message: str
    problem: bool = False


NOTICES = {
    "offload_done": Notice("Card copy finished",
                           "Every file was copied and checked. Safe to swap cameras."),
    "offload_failed": Notice("Card copy stopped",
                             "The card copy did not finish. Check the Mac.", problem=True),
    "backup_done": Notice("Backup finished", "Both backup HDDs are up to date."),
    "backup_problem": Notice("Backup had a problem",
                             "Some files were not backed up. Check the Mac.", problem=True),
    "sync_done": Notice("Upload finished", "Google Drive is up to date."),
    "sync_full": Notice("Upload finished - Drive is full",
                        "Some footage did not fit in Google Drive. Check the Mac.", problem=True),
    "sync_problem": Notice("Upload had a problem",
                           "Some files did not reach Google Drive. Check the Mac.", problem=True),
    "test": Notice("Footage Guardian", "Test notification. If you can read this, it works."),
}


def new_topic() -> str:
    """96 random bits: not guessable, still short enough to type on a phone."""
    return "footage-guardian-" + secrets.token_hex(12)


# --------------------------------------------- which notice, if any, to send
# Plain functions so the choice is testable without a window.

def notice_for_offload(succeeded: bool) -> Notice:
    return NOTICES["offload_done" if succeeded else "offload_failed"]


def notice_for_backup(summary: dict) -> Notice | None:
    """None when Kevin pressed Stop — he is standing at the Mac."""
    if summary.get("stopped"):
        return None
    if "error" in summary or summary.get("failures"):
        return NOTICES["backup_problem"]
    return NOTICES["backup_done"]


def notice_for_sync(summary: dict) -> Notice | None:
    if summary.get("stopped"):
        return None
    if "error" in summary or summary.get("failures"):
        return NOTICES["sync_problem"]
    if summary.get("not_fitting"):
        return NOTICES["sync_full"]
    return NOTICES["sync_done"]


# ----------------------------------------------------------------- sending

def send(topic: str, notice: Notice, server: str = SERVER,
         opener: Callable = urllib.request.urlopen) -> str:
    """Post one notice. Returns "" on success, otherwise why not. Never raises.

    JSON to the server root rather than headers to /topic: HTTP headers
    must be Latin-1, and a title is safer not depending on that.
    """
    if not topic:
        return "Phone notifications are off."
    payload = {
        "topic": topic,
        "title": notice.title,
        "message": notice.message,
        "priority": 4 if notice.problem else 3,
        "tags": ["warning"] if notice.problem else ["white_check_mark"],
    }
    request = urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
        return "" if 200 <= status < 300 else f"The notification service answered {status}."
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return f"Could not reach the notification service ({reason})."
    except Exception as exc:  # noqa: BLE001 - a ping must never take anything down
        return f"Notification failed ({exc.__class__.__name__})."


def send_in_background(topic: str, notice: Notice | None, log: logging.Logger,
                       on_done: Callable[[str], None] | None = None,
                       opener: Callable = urllib.request.urlopen) -> threading.Thread | None:
    """Fire and forget. Returns the thread (for tests), or None if nothing to send."""
    if not topic or notice is None:
        return None

    def worker() -> None:
        problem = send(topic, notice, opener=opener)
        if problem:
            log.warning("Phone notification not sent: %s", problem)
        if on_done is not None:
            try:
                on_done(problem)
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread
