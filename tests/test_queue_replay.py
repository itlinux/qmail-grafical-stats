"""Re-reading a queue record that is already stored must not stop ingestion.

On a live server the check for "already applied" compared a microsecond
timestamp with a DATETIME(3) column, never matched, and the correlation step
then collided on the queue key. The batch aborted, the checkpoint never moved,
and every later run died on the same record.
"""

import datetime as dt
from types import SimpleNamespace

import pymysql
import pytest

from qmailstats import db


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=()):
        self.conn.calls.append((sql, args))
        if "SELECT id FROM message" in sql:
            self._result = self.conn.already_applied(args)
        elif sql.lstrip().startswith("UPDATE message"):
            if self.conn.correlate_error:
                raise self.conn.correlate_error
            self.rowcount = 1
        else:
            self.rowcount = 1

    def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self, stored=None, correlate_error=None):
        self.calls = []
        self.stored = stored or []
        self.correlate_error = correlate_error
        self.rollbacks = 0
        self._sock = object()

    def already_applied(self, args):
        qp, low, high = args
        for s_qp, s_ts in self.stored:
            if s_qp == qp and low <= s_ts <= high:
                return {"id": 1}
        return None

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def ping(self, reconnect=False):
        pass


def queue_message(ts):
    # The write path only reads attributes, and Message derives some of its
    # fields as read-only properties, so a plain stand-in is simpler.
    return SimpleNamespace(
        qp=None, ts=ts, queue_ts=ts, queue_qp=192830, msg_id=201879319,
        size_bytes=1234, sender="someone@example.com",
        sender_domain="example.com", direction="out", auth_user=None,
        auth_source=None, client_ip=None, spam_score=None, verdict=None,
        subject=None,
    )


STORED = dt.datetime(2026, 9, 16, 4, 3, 42, 312000)
PARSED = dt.datetime(2026, 9, 16, 4, 3, 42, 312456, tzinfo=dt.timezone.utc)


def test_a_record_stored_at_millisecond_precision_is_recognised():
    conn = FakeConnection(stored=[(192830, STORED)])
    db.Store(conn).write_messages([queue_message(PARSED)])
    # Recognised as already applied: no correlation, no insert.
    assert not any(sql.lstrip().startswith("UPDATE") for sql, _ in conn.calls)
    assert not any(sql.lstrip().startswith("INSERT") for sql, _ in conn.calls)


def test_a_duplicate_during_correlation_is_treated_as_applied():
    dup = pymysql.err.IntegrityError(
        1062, "Duplicate entry '192830-...' for key 'message.uq_message_queue'")
    conn = FakeConnection(stored=[], correlate_error=dup)
    db.Store(conn).write_messages([queue_message(PARSED)])  # must not raise
    assert conn.rollbacks == 1
    assert not any(sql.lstrip().startswith("INSERT") for sql, _ in conn.calls)


def test_other_integrity_errors_still_surface():
    other = pymysql.err.IntegrityError(1048, "Column 'ts' cannot be null")
    conn = FakeConnection(stored=[], correlate_error=other)
    with pytest.raises(pymysql.err.IntegrityError):
        db.Store(conn).write_messages([queue_message(PARSED)])


def test_a_genuinely_different_time_is_not_mistaken_for_it():
    conn = FakeConnection(stored=[(192830, STORED - dt.timedelta(seconds=5))])
    db.Store(conn).write_messages([queue_message(PARSED)])
    assert any(sql.lstrip().startswith("UPDATE") for sql, _ in conn.calls)
