#!/usr/bin/env python3
"""Claude Code adapter.

Claude Code exposes far less than Codex does, so most of this is inference.

The duration is meant to cover the whole job, not just the last leg. A turn
where Claude used AskUserQuestion needs nothing special: the answer comes back
as a tool_result inside the same turn, so Stop never fires mid-question. But a
turn that ended because Claude asked something in plain text does fire Stop,
and the user's answer then starts a fresh turn - hence the walk back through
question/answer pairs.

The context window is not recorded either, and the model id drops its [1m]
suffix, so the limit is read from settings and widened if the transcript
proves the window is bigger.
"""
import json
import os
from datetime import datetime, timezone

from .. import report

# Chaining back is a heuristic (it keys off a trailing "?"), so bound it both
# by hop count and by a plausible wall-clock span.
MAX_QUESTION_HOPS = 6
MAX_SPAN_HOURS = 12

SETTINGS = os.path.expanduser("~/.claude/settings.json")
NARROW_CONTEXT = 200000
WIDE_CONTEXT = 1000000

# Claude Code logs an interrupt as a user entry, so it has to be excluded or it
# would count as a fresh prompt and reset the duration.
INTERRUPT_PREFIX = "[Request interrupted"


def owns(payload, transcript):
    return True   # the fallback adapter


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


def report_screens(transcript, payload=None):
    events, usages, context = read_transcript(transcript)
    started = work_started_at(events) if events else None
    if started is None:
        return []

    screens = [report.fmt_duration(
        (datetime.now(timezone.utc) - started).total_seconds())]

    spent = sum(tokens for stamp, tokens in usages if stamp >= started)
    if spent:
        screens.append("token " + report.fmt_tokens(spent))

    share = report.pct(context, context_limit(context)) if context else None
    if share is not None:
        screens.append("context %d%%" % share)

    return screens
