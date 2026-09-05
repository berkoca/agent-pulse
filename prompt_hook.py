#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: start the working animation and the
cancellation watcher.

Prints nothing on stdout, because a UserPromptSubmit hook's stdout is injected
into the session context.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SENDER = os.path.join(HERE, "send.py")
WATCHER = os.path.join(HERE, "cancel_watch.py")
MARKER_DIR = "/tmp/claude-code-matrix-turn"


def spawn(argv):
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # no board plugged in is not a reason to fail the hook


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

    spawn([sys.executable, SENDER, "--", "#L"])

    session = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if session and transcript:
        # The nonce lets a stale watcher notice a newer turn has taken over.
        nonce = "%d" % time.time_ns()
        marker = os.path.join(MARKER_DIR, str(session))
        try:
            os.makedirs(MARKER_DIR, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(nonce)
        except OSError:
            return 0
        spawn([sys.executable, WATCHER, marker, nonce, transcript])
    return 0


if __name__ == "__main__":
    sys.exit(main())
