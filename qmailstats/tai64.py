"""Decode the TAI64N labels multilog puts at the front of every line.

A label is '@' followed by 24 hex digits: 16 for TAI seconds, 8 for
nanoseconds. TAI runs 10 seconds ahead of Unix time, which is why the
timestamps look 10 seconds into the future if you skip the correction.
"""

import datetime as dt
import re

LABEL = re.compile(r"^@([0-9a-f]{24})\b")

_TAI_EPOCH = 0x4000000000000000
_TAI_MINUS_UNIX = 10


def decode(label):
    """Return a timezone-aware UTC datetime for a TAI64N label."""
    match = LABEL.match(label)
    if match is None:
        raise ValueError("not a TAI64N label: %r" % (label[:40],))
    raw = match.group(1)
    seconds = int(raw[:16], 16) - _TAI_EPOCH - _TAI_MINUS_UNIX
    nanoseconds = int(raw[16:], 16)
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).replace(
        microsecond=nanoseconds // 1000
    )


def split(line):
    """Split 'label rest-of-line' into (datetime, rest). Raises on no label."""
    match = LABEL.match(line)
    if match is None:
        raise ValueError("no TAI64N label: %r" % (line[:40],))
    return decode(line), line[match.end():].lstrip()
