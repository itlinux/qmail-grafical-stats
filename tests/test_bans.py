"""Reading fail2ban's log."""

import datetime as dt

from qmailstats.bans import BanEvent, events, parse


def test_a_ban():
    line = ("2026-09-16 09:16:39,984 fail2ban.actions        [46659]: "
            "NOTICE  [qmail-smtps-passfail] Ban 192.0.2.77")
    assert parse(line) == BanEvent(
        dt.datetime(2026, 9, 16, 9, 16, 39),
        "qmail-smtps-passfail", "192.0.2.77", "ban")


def test_an_unban():
    line = ("2026-09-16 10:20:01,001 fail2ban.actions        [979]: "
            "NOTICE  [qt-sub-passfail] Unban 203.0.113.9")
    event = parse(line)
    assert event.action == "unban"
    assert event.ip == "203.0.113.9"
    assert event.jail == "qt-sub-passfail"


def test_a_restored_ban_is_still_a_ban():
    """After a restart fail2ban re-applies what it was already holding."""
    line = ("2026-09-16 07:00:00,000 fail2ban.actions        [979]: "
            "NOTICE  [qmail-smtps-usernotfound] Restore Ban 198.51.100.4")
    assert parse(line).action == "ban"


def test_ipv6_survives():
    line = ("2026-09-16 09:16:39,984 fail2ban.actions        [46659]: "
            "NOTICE  [qmail-smtps-passfail] Ban 2001:db8::dead:beef")
    assert parse(line).ip == "2001:db8::dead:beef"


def test_the_noise_is_ignored():
    """fail2ban logs every near-miss at INFO; none of it is an action."""
    for line in [
        ("2026-09-16 07:41:56,010 fail2ban.observer        [979]: INFO    "
         "[qmail-smtps-usernotfound] Found 198.51.100.23, bad - 2026-09-16"),
        ("2026-09-16 07:41:56,567 fail2ban.observer        [979]: NOTICE  "
         "[qmail-smtps-usernotfound] Increase Ban 198.51.100.23 (5 # 2w 2d)"),
        ("2026-09-16 09:15:43,360 fail2ban                [34942]: ERROR   "
         "Failed during configuration"),
        "",
        "not a log line at all",
    ]:
        assert parse(line) is None


def test_events_skips_what_it_cannot_read():
    lines = [
        "rubbish",
        ("2026-09-16 09:16:39,984 fail2ban.actions        [46659]: "
         "NOTICE  [jail-a] Ban 192.0.2.1"),
        "more rubbish",
        ("2026-09-16 09:17:00,000 fail2ban.actions        [46659]: "
         "NOTICE  [jail-a] Unban 192.0.2.1"),
    ]
    got = list(events(lines))
    assert [e.action for e in got] == ["ban", "unban"]
    assert all(e.ip == "192.0.2.1" for e in got)
