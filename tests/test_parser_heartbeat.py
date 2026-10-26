"""A stopped parser must be told apart from a quiet server."""

import datetime as dt

from qmailstats.alerts import parser_staleness

NOW = dt.datetime(2026, 9, 17, 4, 30, tzinfo=dt.timezone.utc)


class FakeStore:
    def __init__(self, beat=None, newest_age=None):
        self._beat = beat
        self._newest_age = newest_age

    def last_beat(self):
        return self._beat

    def scalar(self, sql, args=None):
        if "MAX(ts)" in sql and "TIMESTAMPDIFF" not in sql:
            return None if self._newest_age is None else "something"
        return self._newest_age


def test_a_quiet_server_with_a_live_parser_is_not_stale():
    """The false alarm this replaces: no mail for an hour, parser fine."""
    store = FakeStore(beat=NOW - dt.timedelta(seconds=40), newest_age=63)
    assert parser_staleness(store, 10, now=NOW) is None


def test_a_parser_that_stopped_is_reported():
    store = FakeStore(beat=NOW - dt.timedelta(minutes=25), newest_age=2)
    alert = parser_staleness(store, 10, now=NOW)
    assert alert is not None
    assert alert["id"] == "parser_stalled"
    assert "25 minutes" in alert["detail"]


def test_just_under_the_limit_is_fine():
    store = FakeStore(beat=NOW - dt.timedelta(minutes=9, seconds=59))
    assert parser_staleness(store, 10, now=NOW) is None


def test_without_a_heartbeat_falls_back_to_message_age():
    """Installs that have not written one yet keep the old behaviour."""
    assert parser_staleness(FakeStore(newest_age=30), 10, now=NOW) is None
    alert = parser_staleness(FakeStore(newest_age=90), 10, now=NOW)
    assert alert is not None
    assert "cannot be told apart" in alert["detail"]


def test_an_empty_database_raises_nothing():
    assert parser_staleness(FakeStore(), 10, now=NOW) is None
