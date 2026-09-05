#!/usr/bin/env python3
"""Codex CLI adapter.

Codex hands almost everything over directly, so this is mostly reading:

    task_started       turn_id, started_at
    task_complete      turn_id, duration_ms, completed_at
    token_usage_record turn_token_usage vs thread_token_usage
    token_count        info.model_context_window, info.last_token_usage,
                       and rate_limits - the real usage quota, which Claude
                       Code does not expose to a hook at all

None of the guesswork the Claude adapter needs is required here: no walking
back through question/answer pairs for the duration, no inferring the context
window from the model id.
"""
import json
import os
import time

from .. import report

# The Stop hook can fire a few milliseconds before the last rollout lines are
# flushed, so give them a moment rather than dropping a screen.
SETTLE_TRIES = 4
SETTLE_SLEEP = 0.15


def owns(payload, transcript):
    if transcript and "/.codex/" in transcript:
        return True
    # turn_id is a Codex-specific extension to the shared payload shape
    return "turn_id" in payload and not (transcript or "").startswith(
        os.path.expanduser("~/.claude/"))


def _scan(transcript, turn_id):
    found = {"started_at": None, "duration_ms": None, "tokens": None,
             "context_used": None, "context_window": None, "quota": None}
    try:
        fh = open(transcript, "r", encoding="utf-8", errors="replace")
    except OSError:
        return found
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            body = entry.get("payload")
            if not isinstance(body, dict):
                continue
            kind = body.get("type") or entry.get("type")
            mine = turn_id is None or body.get("turn_id") == turn_id

            if kind == "task_started" and mine:
                found["started_at"] = body.get("started_at")
            elif kind == "task_complete" and mine:
                found["duration_ms"] = body.get("duration_ms")
                found["started_at"] = body.get("started_at") or found["started_at"]
            elif entry.get("type") == "token_usage_record" and mine:
                usage = body.get("turn_token_usage") or {}
                if usage.get("total_tokens") is not None:
                    found["tokens"] = usage["total_tokens"]
            elif kind == "token_count":
                # token_count carries no turn_id, so the last one wins
                info = body.get("info") or {}
                last = info.get("last_token_usage") or {}
                if last.get("input_tokens") is not None:
                    found["context_used"] = last["input_tokens"]
                if info.get("model_context_window"):
                    found["context_window"] = info["model_context_window"]
                primary = (body.get("rate_limits") or {}).get("primary") or {}
                if primary.get("used_percent") is not None:
                    found["quota"] = primary["used_percent"]
    return found


def report_screens(transcript, payload):
    turn_id = payload.get("turn_id")
    found = _scan(transcript, turn_id)
    for _ in range(SETTLE_TRIES):
        if found["tokens"] is not None and found["duration_ms"] is not None:
            break
        time.sleep(SETTLE_SLEEP)
        found = _scan(transcript, turn_id)

    screens = []

    if found["duration_ms"] is not None:
        seconds = found["duration_ms"] / 1000.0
    elif found["started_at"]:
        seconds = time.time() - found["started_at"]
    else:
        seconds = None
    if seconds is not None:
        screens.append(report.fmt_duration(seconds))

    if found["tokens"]:
        screens.append("token " + report.fmt_tokens(found["tokens"]))

    if found["quota"] is not None:
        screens.append("limit %d%%" % int(round(found["quota"])))

    share = report.pct(found["context_used"] or 0, found["context_window"])
    if share is not None:
        screens.append("context %d%%" % share)

    return screens
