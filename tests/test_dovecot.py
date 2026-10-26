import datetime as dt

import pytest

from qmailstats.dovecot import parse_line

DENVER = "America/Denver"


def test_reads_a_successful_imap_login_in_server_local_time():
    """Syslog writes local time; every other timestamp here is UTC."""
    got = parse_line(
        "Sep 13 00:03:15 imap-login: Info: Login: user=<info@example.com>, "
        "method=PLAIN, rip=203.0.113.207, lip=192.0.2.10, mpid=70157, TLS, "
        "session=<fzDAEldbr82XVI7P>",
        year=2026, tz_name=DENVER,
    )
    assert got["service"] == "imap"
    assert got["result"] == "login"
    assert got["user"] == "info@example.com"
    assert got["client_ip"] == "203.0.113.207"
    assert got["tls"] is True
    # 00:03:15 MDT is 06:03:15 UTC.
    assert got["ts"] == dt.datetime(2026, 9, 13, 6, 3, 15, tzinfo=dt.timezone.utc)


def test_applies_the_right_offset_either_side_of_a_daylight_change():
    """A fixed offset would be an hour out for half the year."""
    summer = parse_line(
        "Jul 01 12:00:00 imap-login: Info: Login: user=<a@sender.example>, method=PLAIN, "
        "rip=192.0.2.1, lip=192.0.2.8, mpid=1, session=<x>",
        year=2026, tz_name=DENVER)
    winter = parse_line(
        "Dec 01 12:00:00 imap-login: Info: Login: user=<a@sender.example>, method=PLAIN, "
        "rip=192.0.2.1, lip=192.0.2.8, mpid=1, session=<x>",
        year=2026, tz_name=DENVER)
    assert summer["ts"].hour == 18   # MDT, UTC-6
    assert winter["ts"].hour == 19   # MST, UTC-7


def test_reads_a_pop3_scan_that_never_tried_to_authenticate():
    got = parse_line(
        "Sep 13 00:12:45 pop3-login: Info: Disconnected: Connection closed "
        "(no auth attempts in 20 secs): user=<>, rip=203.0.113.149, "
        "lip=192.0.2.10, session=<7xW7NFdb8IIodxuV>",
        year=2026, tz_name=DENVER)
    assert got["service"] == "pop3"
    assert got["result"] == "no_auth"
    assert got["user"] is None
    assert got["client_ip"] == "203.0.113.149"
    assert got["tls"] is False


def test_reads_a_client_that_was_cut_off_for_bad_commands():
    got = parse_line(
        "Sep 13 00:32:28 imap-login: Info: Disconnected: Too many invalid commands "
        "(no auth attempts in 0 secs): user=<>, rip=203.0.113.214, "
        "lip=192.0.2.10, session=<p+dGe1dbGrOl9eTW>",
        year=2026, tz_name=DENVER)
    assert got["result"] == "no_auth"
    assert got["client_ip"] == "203.0.113.214"


def test_reads_a_failed_authentication_as_its_own_outcome():
    got = parse_line(
        "Sep 13 01:15:02 imap-login: Info: Disconnected: Aborted login "
        "(auth failed, 1 attempts in 4 secs): user=<admin@example.com>, "
        "method=PLAIN, rip=203.0.113.26, lip=192.0.2.10, TLS, session=<q>",
        year=2026, tz_name=DENVER)
    assert got["result"] == "auth_failed"
    assert got["user"] == "admin@example.com"
    assert got["client_ip"] == "203.0.113.26"


def test_addresses_are_folded_so_one_mailbox_is_one_row():
    got = parse_line(
        "Sep 13 00:03:15 imap-login: Info: Login: user=<Info@Example.COM>, "
        "method=PLAIN, rip=192.0.2.1, lip=192.0.2.8, mpid=1, session=<x>",
        year=2026, tz_name=DENVER)
    assert got["user"] == "info@example.com"


def test_ignores_lines_that_are_not_login_events():
    assert parse_line(
        "Sep 13 00:03:15 imap(info@account.example): Info: Logged out in=123 out=456",
        year=2026, tz_name=DENVER) is None
    assert parse_line("not a syslog line at all", year=2026, tz_name=DENVER) is None


def test_an_unknown_timezone_falls_back_to_utc_rather_than_failing():
    got = parse_line(
        "Sep 13 00:03:15 imap-login: Info: Login: user=<a@sender.example>, method=PLAIN, "
        "rip=192.0.2.1, lip=192.0.2.8, mpid=1, session=<x>",
        year=2026, tz_name="Not/AZone")
    assert got["ts"].hour == 0


def test_a_leap_day_from_an_earlier_year_does_not_crash():
    """Syslog omits the year, and an unrotated log spans several.

    "Feb 29" from a leap year, dated with a later non-leap year, is not a valid
    date -- and it brought the whole ingest down rather than skipping a line.
    """
    line = ("Feb 29 12:00:00 imap-login: Info: Login: user=<a@b.example>, "
            "method=PLAIN, rip=192.0.2.1, lip=192.0.2.2, mpid=1, session=<x>")
    got = parse_line(line, year=2026, tz_name="UTC")     # 2026 is not a leap year
    assert got is not None, "the line was dropped entirely"
    assert got["ts"].year == 2024 or got["ts"].year == 2028, got["ts"]
    assert got["ts"].month == 2 and got["ts"].day == 29


def test_a_line_dated_in_the_future_is_read_as_last_year():
    """A file spanning New Year: December lines carry the following year."""
    now = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc)
    line = ("Dec 28 12:00:00 imap-login: Info: Login: user=<a@b.example>, "
            "method=PLAIN, rip=192.0.2.1, lip=192.0.2.2, mpid=1, session=<x>")
    got = parse_line(line, year=2026, tz_name="UTC", now=now)
    assert got["ts"].year == 2025, "December was dated a year into the future"


def test_an_unreadable_date_is_refused_rather_than_guessed():
    line = ("Feb 31 12:00:00 imap-login: Info: Login: user=<a@b.example>, "
            "method=PLAIN, rip=192.0.2.1, lip=192.0.2.2, mpid=1, session=<x>")
    assert parse_line(line, year=2026, tz_name="UTC") is None
