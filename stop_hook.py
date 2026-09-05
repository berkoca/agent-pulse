#!/usr/bin/env python3
"""Stop hook: flash DONE on the panel, then report the finished job.

Works for both Claude Code and Codex. The two agents share the hook payload
shape (session_id, transcript_path, cwd, hook_event_name), so all this has to
do is pick the adapter that understands the transcript and hand its screens to
the board.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pulse.agents import claude, codex          # noqa: E402

SENDER = os.path.join(HERE, "send.py")
MARKER_DIR = "/tmp/agent-pulse-turn"


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

    # Retire this turn's marker first, so the Claude cancellation watcher sees
    # the turn ended normally and stays quiet.
    session = payload.get("session_id")
    if session:
        try:
            os.unlink(os.path.join(MARKER_DIR, str(session)))
        except OSError:
            pass

    transcript = payload.get("transcript_path")
    screens = []
    if transcript:
        adapter = codex if codex.owns(payload, transcript) else claude
        try:
            screens = adapter.report_screens(transcript, payload)
        except Exception:
            screens = []   # a broken read should still flash DONE

    try:
        subprocess.Popen(
            [sys.executable, SENDER, "--", "#N" + ",".join(screens)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # no board plugged in is not a reason to fail the hook
    return 0


if __name__ == "__main__":
    sys.exit(main())
