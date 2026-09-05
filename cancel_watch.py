#!/usr/bin/env python3
"""Watch one turn's transcript for an interrupt and scroll "cancelled".

Claude Code has no cancellation hook, so nothing fires when you press Ctrl+C
and the working animation would otherwise run forever. It does write an
explicit record though - a user entry whose text is "[Request interrupted by
user]" - so this tails the transcript for exactly that and sends #X, which
flashes "cancel" on the panel.

Started detached by prompt_hook.py at the top of each turn, and lives only as
long as the turn: stop_hook.py deletes the turn marker on a normal finish, and
a new prompt replaces the nonce inside it, either of which ends this watcher.

Usage: cancel_watch.py <marker_path> <nonce> <transcript_path>
"""
import json
import os
import subprocess
import sys
import time

SENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send.py")
MESSAGE = "#X"
POLL_SECONDS = 0.4
MAX_LIFETIME_SECONDS = 6 * 3600
INTERRUPT_PREFIX = "[Request interrupted"


def marker_is_ours(marker_path, nonce):
    try:
        with open(marker_path, "r", encoding="utf-8") as fh:
            return fh.read().strip() == nonce
    except OSError:
        return False


def is_interrupt(line):
    try:
        entry = json.loads(line)
    except ValueError:
        return False
    if entry.get("type") != "user":
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return content.startswith(INTERRUPT_PREFIX)
    if not isinstance(content, list):
        return False
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "text"
                and block.get("text", "").startswith(INTERRUPT_PREFIX)):
            return True
    return False


def send(text):
    try:
        subprocess.Popen(
            [sys.executable, SENDER, "--", text],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def main():
    if len(sys.argv) < 4:
        return 1
    marker_path, nonce, transcript = sys.argv[1], sys.argv[2], sys.argv[3]

    # Only entries written from here on count; the transcript already holds
    # every interrupt from earlier in the session.
    try:
        offset = os.path.getsize(transcript)
    except OSError:
        offset = 0

    deadline = time.monotonic() + MAX_LIFETIME_SECONDS
    while time.monotonic() < deadline:
        if not marker_is_ours(marker_path, nonce):
            return 0  # the turn finished normally, or a new turn took over
        try:
            size = os.path.getsize(transcript)
        except OSError:
            time.sleep(POLL_SECONDS)
            continue
        if size > offset:
            try:
                with open(transcript, "r", encoding="utf-8",
                          errors="replace") as fh:
                    fh.seek(offset)
                    fresh = fh.read()
                    offset = fh.tell()
            except OSError:
                fresh = ""
            for line in fresh.splitlines():
                if line.strip() and is_interrupt(line):
                    send(MESSAGE)
                    try:
                        os.unlink(marker_path)
                    except OSError:
                        pass
                    return 0
        time.sleep(POLL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
