"""Local time, in one place.

Everything ctempo reads in local time (hour-of-week bins and local hours,
reset clocks on the line, active days in the transcripts, the error log's
time stamps) goes through `localtime`, which is the time module's, so the
zone is the process's own. Tests pin a zone by replacing it, on every OS:
time.tzset does not exist on Windows, and its C runtime knows no IANA zone
names (tests/helpers.py: utc() and zone()).
"""

import time

localtime = time.localtime  # (ts) -> struct_time in local time


def strftime(fmt, ts):
    """ts formatted in local time."""
    return time.strftime(fmt, localtime(ts))
