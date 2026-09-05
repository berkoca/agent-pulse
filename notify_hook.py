#!/usr/bin/env python3
"""Fire-and-forget hook: hand one command to the LED matrix and return.

Usage: notify_hook.py '#L'

Prints nothing on stdout, because a UserPromptSubmit hook's stdout is injected
into the session context. Spawns send.py detached so Claude Code is never held
up by the ~1.5s the board's reset costs.
"""
import os
import subprocess
import sys

SENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send.py")

try:
    sys.stdin.read()  # drain the payload; nothing here needs it
except Exception:
    pass

if len(sys.argv) > 1:
    try:
        subprocess.Popen(
            [sys.executable, SENDER, "--", sys.argv[1]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # no board plugged in is not a reason to fail the hook

sys.exit(0)
