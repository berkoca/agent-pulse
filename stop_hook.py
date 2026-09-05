#!/usr/bin/env python3
"""Claude Code Stop hook: blink DONE on the LED matrix, then report the job.

Sends `#N<duration>,token <n>,context <pct>%` - the firmware holds the first
field still and scrolls the rest, so only the duration is bound by the panel's
5-character static width.

The duration is meant to cover the whole job, not just the last leg. A turn
where Claude used AskUserQuestion needs nothing special: the answer comes back
as a tool_result inside the same turn, so Stop never fires mid-question and the
measurement already spans the wait. But a turn that ended because Claude asked
something in plain text does fire Stop, and the user's answer then starts a
fresh turn. So walk back through those question/answer pairs to find where the
work actually began.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SENDER = os.path.join(HERE, "send.py")

# Chaining back is a heuristic (it keys off a trailing "?"), so bound it both
# by hop count and by a plausible wall-clock span.
MAX_QUESTION_HOPS = 6
MAX_SPAN_HOURS = 12

# Claude Code records neither the context window nor the [1m] suffix on the
# model id, so start from the configured model and widen if the transcript
# proves the window is bigger than that.
SETTINGS = os.path.expanduser("~/.claude/settings.json")
MARKER_DIR = "/tmp/agent-pulse-turn"

# Claude Code logs an interrupt as a user entry, so it has to be excluded or it
# would count as a fresh prompt and reset the duration.
INTERRUPT_PREFIX = "[Request interrupted"
NARROW_CONTEXT = 200000
WIDE_CONTEXT = 1000000


def parse_ts(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_human_turn(entry):
    """True for a real user prompt, as opposed to a tool result echoed back."""
    if entry.get("type") != "user":
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return not content.startswith(INTERRUPT_PREFIX)
    if not isinstance(content, list):
        return False
    blocks = [b for b in content if isinstance(b, dict)]
    if any(b.get("type") == "tool_result" for b in blocks):
        return False
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    if not texts:
        return False
    return not any(t.startswith(INTERRUPT_PREFIX) for t in texts)


def total_tokens(usage):
    """Everything the request was charged for, cache included."""
    if not isinstance(usage, dict):
        return 0
    return (usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("output_tokens", 0))


def context_used(usage):
    """What the last request carried in, i.e. how full the window is."""
    if not isinstance(usage, dict):
        return 0
    return (usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0))


def context_limit(used):
    limit = NARROW_CONTEXT
    try:
        with open(SETTINGS, "r", encoding="utf-8") as fh:
            if "[1m]" in json.load(fh).get("model", ""):
                limit = WIDE_CONTEXT
    except (OSError, ValueError):
        pass
    return WIDE_CONTEXT if used > limit else limit


def read_transcript(transcript_path):
    """Return (events, usages, context).

    events:  ordered ("H", timestamp) prompts and ("A", text) assistant replies.
    usages:  (timestamp, tokens) per assistant request that reported usage.
    context: tokens carried by the most recent request.
    """
    events = []
    usages = []
    context = 0
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("isSidechain"):
                    continue
                if is_human_turn(entry):
                    stamp = parse_ts(entry.get("timestamp"))
                    if stamp:
                        events.append(("H", stamp))
                elif entry.get("type") == "assistant":
                    message = entry.get("message", {})
                    usage = message.get("usage")
                    tokens = total_tokens(usage)
                    if tokens:
                        stamp = parse_ts(entry.get("timestamp"))
                        if stamp:
                            usages.append((stamp, tokens))
                        context = context_used(usage)
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "text"
                                and block.get("text", "").strip()):
                            events.append(("A", block["text"]))
    except OSError:
        return [], [], 0
    return events, usages, context


def looks_like_question(text):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    return bool(lines) and lines[-1].endswith("?")


def work_started_at(events):
    prompts = [i for i, e in enumerate(events) if e[0] == "H"]
    if not prompts:
        return None

    k = len(prompts) - 1
    for _ in range(MAX_QUESTION_HOPS):
        if k == 0:
            break
        previous = None
        for j in range(prompts[k] - 1, -1, -1):
            if events[j][0] == "A":
                previous = events[j][1]
                break
        if previous is None or not looks_like_question(previous):
            break
        k -= 1  # that prompt was an answer, so keep going back

    start = events[prompts[k]][1]
    span = (datetime.now(timezone.utc) - start).total_seconds()
    if span > MAX_SPAN_HOURS * 3600:
        return events[prompts[-1]][1]
    return start


def fmt_tokens(count):
    """Compact enough for the 5-character static display."""
    # Thresholds are picked so rounding can never push the result to 6
    # characters: 99950 would render as "100.0k", so it belongs one tier up.
    if count < 1000:
        return "%d" % count
    if count < 99950:
        return "%.1fk" % (count / 1000.0)
    if count < 999500:
        return "%.0fk" % (count / 1000.0)
    if count < 99950000:
        return "%.1fM" % (count / 1000000.0)
    return "%.0fM" % (count / 1000000.0)


def fmt_duration(seconds):
    """Format to at most 5 characters, which is what 4 modules can show."""
    seconds = int(round(seconds))
    if seconds < 0:
        return ""
    if seconds < 60:
        return "%ds" % seconds
    minutes, secs = divmod(seconds, 60)
    if minutes < 10:
        return "%dm%02ds" % (minutes, secs)
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 10:
        return "%dh%02d" % (hours, minutes)
    return "%dh" % hours


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

    # Retire this turn's marker first, so the cancellation watcher sees the
    # turn ended normally and stays quiet.
    session = payload.get("session_id")
    if session:
        try:
            os.unlink(os.path.join(MARKER_DIR, str(session)))
        except OSError:
            pass

    events, usages, context = [], [], 0
    transcript = payload.get("transcript_path")
    if transcript:
        events, usages, context = read_transcript(transcript)

    started = work_started_at(events) if events else None

    screens = []
    if started is not None:
        screens.append(fmt_duration(
            (datetime.now(timezone.utc) - started).total_seconds()))
        spent = sum(tokens for stamp, tokens in usages if stamp >= started)
        if spent:
            screens.append("token " + fmt_tokens(spent))
        if context:
            screens.append("context %d%%" % round(100.0 * context / context_limit(context)))

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
