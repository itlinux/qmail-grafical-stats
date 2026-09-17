"""Read Dovecot's login lines out of a syslog-style file.

Two things make this different from the qmail logs. Dovecot writes syslog
timestamps -- local time, and with no year at all -- while everything else here
is UTC and self-dating. So a line is only meaningful alongside the year it was
written in and the timezone the server keeps, and it is converted to UTC before
it goes anywhere near the database.
"""

import datetime as dt
import re

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - only on older interpreters
    ZoneInfo = None

_LINE = re.compile(
    r"^(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+"
    r"(?P<service>imap|pop3|submission)-login: \w+: (?P<rest>.*)$"
)
# Any Dovecot syslog line: a timestamp and a service tag. Used only to tell
# "understood but not a login event" -- an imap(user) session line, say --
# from "could not read this at all", which is the only count worth alarm.
_ANY_LINE = re.compile(
    r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+?[:(]"
)
_USER = re.compile(r"user=<(?P<user>[^>]*)>")
_RIP = re.compile(r"\brip=(?P<ip>[0-9a-fA-F.:]+)")
_METHOD = re.compile(r"\bmethod=(?P<method>[A-Za-z0-9-]+)")

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _zone(tz_name):
    """The named zone, or UTC if it cannot be loaded.

    A missing tzdata package is a deployment problem, not a reason to stop
    ingesting: falling back to UTC keeps the rows flowing and shifts them by a
    known amount rather than losing them.
    """
    if not tz_name or ZoneInfo is None:
        return dt.timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return dt.timezone.utc


def classify(rest):
    """What the line says happened, or None if it is not a login event."""
    if rest.startswith("Login:"):
        return "login"
    if "auth failed" in rest:
        return "auth_failed"
    if "no auth attempts" in rest:
        return "no_auth"
    if rest.startswith("Disconnected"):
        # A disconnect that is neither a failure nor a scan: the client
        # authenticated and left, which the Login line already recorded.
        return None
    return None


def _dated(year, month, day, hour, minute, second, zone):
    """Build the timestamp, or None if that date does not exist in that year.

    Syslog omits the year and a log nothing rotates spans several, so the year
    handed in is an estimate. Two corrections: a date that does not exist in
    the estimated year (29 February in a non-leap year) is tried against the
    nearest leap year, and a date that would fall in the future belongs to the
    year before -- which is what December lines look like in January.
    """
    for candidate in (year, year - 1, year + 1, year - 2):
        try:
            return dt.datetime(candidate, month, day, hour, minute, second,
                               tzinfo=zone)
        except ValueError:
            continue
    return None


def parse_line(line, year, tz_name=None, now=None):
    """Return one event dict with a UTC timestamp, or None.

    None means one of two different things, which callers need to tell apart:
    `readable(line)` says whether the line was understood at all. A session's
    "Logged out" line is understood and simply not a login event; a line with
    no syslog timestamp is a parsing failure worth counting.
    """
    match = _LINE.match(line)
    if match is None:
        return None
    result = classify(match.group("rest"))
    if result is None:
        return None

    local = _dated(
        year, MONTHS[match.group("month")], int(match.group("day")),
        int(match.group("hour")), int(match.group("minute")),
        int(match.group("second")), _zone(tz_name),
    )
    if local is None:
        return None
    # A line dated ahead of now belongs to the previous year: that is what
    # December looks like when the file is read in January.
    now = now or dt.datetime.now(dt.timezone.utc)
    if local > now + dt.timedelta(days=1):
        earlier = _dated(local.year - 1, local.month, local.day, local.hour,
                         local.minute, local.second, local.tzinfo)
        if earlier is not None:
            local = earlier

    rest = match.group("rest")
    user_match = _USER.search(rest)
    user = user_match.group("user").strip().lower() if user_match else ""
    ip_match = _RIP.search(rest)
    method_match = _METHOD.search(rest)

    return {
        "ts": local.astimezone(dt.timezone.utc),
        "service": match.group("service"),
        "result": result,
        "user": user or None,
        "client_ip": ip_match.group("ip") if ip_match else None,
        "method": method_match.group("method") if method_match else None,
        # Dovecot writes a bare "TLS" or "TLS handshaking" among the fields.
        "tls": ", TLS" in rest or rest.endswith("TLS"),
    }


def readable(line):
    """True when the line has the shape of a Dovecot log line.

    Broader than the login pattern on purpose: most of the file is session
    activity this tool has no use for, and those lines are not defects.
    """
    return _ANY_LINE.match(line) is not None


def year_for(path, now=None):
    """Guess the year a syslog file's lines belong to.

    Syslog omits it. The file's own last-modified time is the best evidence
    available, and it is right except for a file still being written across a
    New Year -- where the January lines belong to the newer year. Callers that
    can do better should pass the year themselves.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return now.year
    return modified.year
