#!/usr/bin/env python3
"""Formatting shared by every agent adapter.

The panel shows the first report field static, and 32 columns of the 5x7 font
fit exactly five characters, so `fmt_duration` must never exceed that. The
scrolled fields are free, but they are kept short anyway.
"""


def fmt_duration(seconds):
    """At most five characters, trading seconds away as the scale grows."""
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


def fmt_tokens(count):
    """Thresholds are picked so rounding can never add a sixth character:
    99950 would render as "100.0k", so it belongs one tier up."""
    if count < 1000:
        return "%d" % count
    if count < 99950:
        return "%.1fk" % (count / 1000.0)
    if count < 999500:
        return "%.0fk" % (count / 1000.0)
    if count < 99950000:
        return "%.1fM" % (count / 1000000.0)
    return "%.0fM" % (count / 1000000.0)


def pct(used, total):
    if not total:
        return None
    return int(round(100.0 * used / total))
