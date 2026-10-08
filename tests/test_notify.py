"""Phone pings when a job ends. None of these touch the network."""
from __future__ import annotations

import json
import logging
import re
import socket
import unittest
import urllib.error

from footage_guardian.config import Config
from footage_guardian.notify import (
    NOTICES,
    new_topic,
    notice_for_backup,
    notice_for_offload,
    notice_for_sync,
    send,
    send_in_background,
)


class Recorder:
    """Stands in for urlopen and remembers every request."""

    def __init__(self, status: int = 200, error: Exception | None = None):
        self.status, self.error = status, error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        recorder = self

        class Response:
            status = recorder.status
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Response()


class NotifyTests(unittest.TestCase):
    def test_off_unless_a_topic_is_set(self):
        self.assertEqual(Config().notify_topic, "")
        recorder = Recorder()
        self.assertIsNone(send_in_background("", NOTICES["backup_done"], logging.getLogger("t"),
                                             opener=recorder))
        self.assertEqual(recorder.requests, [])

    def test_a_finished_job_sends_exactly_one_ping(self):
        recorder = Recorder()
        thread = send_in_background("footage-guardian-abc", NOTICES["backup_done"],
                                    logging.getLogger("t"), opener=recorder)
        thread.join(5)
        self.assertEqual(len(recorder.requests), 1)
        request, timeout = recorder.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["topic"], "footage-guardian-abc")
        self.assertEqual(payload["title"], "Backup finished")
        self.assertEqual(request.full_url, "https://ntfy.sh")
        self.assertLessEqual(timeout, 10)

    def test_a_broken_network_never_raises(self):
        for error in (urllib.error.URLError("no route to host"), socket.timeout("timed out"),
                      ConnectionResetError(), RuntimeError("anything at all")):
            problem = send("footage-guardian-abc", NOTICES["sync_done"], opener=Recorder(error=error))
            self.assertTrue(problem, error)

    def test_a_failed_ping_is_logged_and_reported_not_raised(self):
        logs = []
        log = logging.getLogger("notify-test")
        handler = logging.Handler()
        handler.emit = lambda record: logs.append(record.getMessage())
        log.addHandler(handler)
        results = []
        thread = send_in_background("footage-guardian-abc", NOTICES["offload_done"], log,
                                    on_done=results.append,
                                    opener=Recorder(error=urllib.error.URLError("offline")))
        thread.join(5)
        self.assertIn("not sent", logs[0])
        self.assertTrue(results[0])

    def test_a_server_error_is_reported(self):
        self.assertIn("429", send("t", NOTICES["test"], opener=Recorder(status=429)))

    def test_messages_carry_nothing_about_the_work(self):
        # The topic is a guessable secret. Fixed strings only: no digits
        # (dates, counts), no paths, no folder or camera names.
        for key, notice in NOTICES.items():
            for text in (notice.title, notice.message):
                self.assertIsNone(re.search(r"\d|/|:", text), f"{key}: {text!r}")
                for word in ("Main Cam", "360", "Drone", "Osmo", "Volumes", "gdrive"):
                    self.assertNotIn(word, text)

    def test_the_payload_has_only_the_agreed_fields(self):
        recorder = Recorder()
        send("footage-guardian-abc", NOTICES["sync_problem"], opener=recorder)
        payload = json.loads(recorder.requests[0][0].data)
        self.assertEqual(set(payload), {"topic", "title", "message", "priority", "tags"})
        self.assertEqual(payload["priority"], 4, "a problem is louder")

    def test_topics_are_long_random_and_typeable(self):
        topics = {new_topic() for _ in range(50)}
        self.assertEqual(len(topics), 50)
        for topic in topics:
            self.assertRegex(topic, r"^footage-guardian-[0-9a-f]{24}$")


class WhichNoticeTests(unittest.TestCase):
    def test_card_copy(self):
        self.assertIn("Safe to swap cameras", notice_for_offload(True).message)
        self.assertTrue(notice_for_offload(False).problem)

    def test_backup(self):
        self.assertEqual(notice_for_backup({"failures": [], "stopped": False}), NOTICES["backup_done"])
        self.assertEqual(notice_for_backup({"failures": ["x"], "stopped": False}), NOTICES["backup_problem"])
        self.assertEqual(notice_for_backup({"error": "HDD 2 not plugged in"}), NOTICES["backup_problem"])

    def test_sync(self):
        self.assertEqual(notice_for_sync({"failures": [], "stopped": False}), NOTICES["sync_done"])
        self.assertEqual(notice_for_sync({"failures": [], "not_fitting": 3}), NOTICES["sync_full"])
        self.assertEqual(notice_for_sync({"failures": ["x"]}), NOTICES["sync_problem"])
        self.assertEqual(notice_for_sync({"error": "offline"}), NOTICES["sync_problem"])

    def test_pressing_stop_sends_nothing(self):
        # He pressed it, so he is at the Mac.
        self.assertIsNone(notice_for_backup({"failures": [], "stopped": True}))
        self.assertIsNone(notice_for_sync({"failures": [], "stopped": True}))


if __name__ == "__main__":
    unittest.main()
