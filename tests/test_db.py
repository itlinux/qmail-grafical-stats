import datetime as dt
import os

import pytest

from qmailstats import db
from qmailstats.models import Delivery, Message

DSN = os.environ.get("QMAILSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="QMAILSTATS_TEST_DSN not set")


@pytest.fixture
def store():
    s = db.Store.from_dsn(DSN)
    s.apply_schema()
    # queue_minute and service_minute included deliberately: their counters
    # accumulate rather than replace, so leftovers leak between tests.
    for table in ("delivery", "message", "checkpoint", "parser_state",
                  "daily_stats", "queue_minute", "service_minute"):
        s.execute("DELETE FROM %s" % table)
    yield s
    s.close()


def _message(**kw):
    base = dict(qp=1, ts=dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc),
                direction="in", sender="a@sender.example")
    base.update(kw)
    return Message(**base)


def test_writing_the_same_message_twice_leaves_one_row(store):
    store.write_messages([_message(), _message()])
    assert store.scalar("SELECT COUNT(*) FROM message") == 1


def test_stores_the_derived_domain_not_just_the_address(store):
    store.write_messages([_message(sender="Someone@Example.COM")])
    assert store.scalar("SELECT sender_domain FROM message") == "example.com"


def test_the_queue_side_fills_in_msg_id_without_creating_a_second_row(store):
    store.write_messages([_message(qp=42)])
    store.write_messages([_message(qp=42, msg_id=999, size_bytes=1234, direction="out")])
    assert store.scalar("SELECT COUNT(*) FROM message") == 1
    assert store.scalar("SELECT msg_id FROM message") == 999
    assert store.scalar("SELECT size_bytes FROM message") == 1234


def test_the_smtp_side_wins_on_direction_because_it_saw_the_port(store):
    store.write_messages([_message(qp=42, msg_id=999, direction="out")])
    store.write_messages([_message(qp=42, direction="in", client_ip="192.0.2.1")])
    assert store.scalar("SELECT direction FROM message") == "in"


def test_writing_the_same_delivery_twice_leaves_one_row(store):
    d = Delivery(msg_id=1, ts=dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc),
                 recipient="x@dest.example", route="remote", result="success")
    store.write_deliveries([d, d])
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 1


def test_a_retry_is_kept_as_a_separate_row(store):
    when = dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc)
    store.write_deliveries([
        Delivery(msg_id=1, ts=when, recipient="x@dest.example", route="remote", result="deferral"),
        Delivery(msg_id=1, ts=when + dt.timedelta(minutes=20), recipient="x@dest.example",
                 route="remote", result="success"),
    ])
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 2


def test_checkpoints_round_trip(store):
    assert store.get_checkpoint("send", "current") == (0, False)
    store.set_checkpoint("send", "current", 4096, completed=False)
    assert store.get_checkpoint("send", "current") == (4096, False)
    store.set_checkpoint("send", "@40000.s", 999, completed=True)
    assert store.get_checkpoint("send", "@40000.s") == (999, True)


def test_parser_state_round_trips_through_json(store):
    store.set_parser_state("send", {"open_deliveries": {"1": {"recipient": "x@dest.example"}}})
    got = store.get_parser_state("send")
    assert got["open_deliveries"]["1"]["recipient"] == "x@dest.example"


def test_a_long_lived_reader_sees_rows_written_after_it_started(store):
    """A serving process must not be pinned to the snapshot of its first read.

    With autocommit off, the first SELECT opens a transaction that is never
    committed, and REPEATABLE READ then freezes the dashboard on the data as it
    was when the service started.
    """
    assert store.scalar("SELECT COUNT(*) FROM message") == 0

    writer = db.Store.from_dsn(DSN)
    try:
        writer.write_messages([_message(qp=7777)])
    finally:
        writer.close()

    assert store.scalar("SELECT COUNT(*) FROM message") == 1, (
        "the reader is stuck on a stale snapshot"
    )


def test_a_column_added_later_reaches_a_database_that_already_exists(store):
    """schema.sql only creates tables; it cannot add a column to an existing one.

    Servers that installed an earlier version would otherwise keep a message
    table with no verdict column, and every query naming it would fail.
    """
    store.execute("ALTER TABLE message DROP COLUMN verdict")
    assert "verdict" not in store.columns_of("message")

    store.apply_schema()

    assert "verdict" in store.columns_of("message")
    store.write_messages([_message(qp=4242, verdict="SPAM")])
    assert store.scalar("SELECT verdict FROM message WHERE qp=4242") == "SPAM"


def test_a_query_with_a_literal_percent_and_no_arguments_runs(store):
    """PyMySQL formats the statement even when no arguments are passed.

    An unescaped % in a LIKE pattern is then read as a placeholder and the
    query dies with "not enough arguments for format string".
    """
    store.write_messages([_message(qp=555, sender="someone@demo.example")])
    found = store.scalar(
        "SELECT COUNT(*) FROM message WHERE sender_domain LIKE '%%.example'"
    )
    assert int(found) == 1


def test_a_timestamp_revived_from_json_is_accepted():
    """Carried parser state is JSON, so nested datetimes come back as strings.

    A delivery whose opening line fell in an earlier run carries its instance
    timestamp through that round trip.
    """
    import datetime as dt
    from qmailstats.db import _naive_utc

    revived = _naive_utc("2026-09-15 06:05:04.742178+00:00")
    assert isinstance(revived, dt.datetime)
    assert revived.tzinfo is None
    assert revived.year == 2026 and revived.hour == 6


def test_an_unparseable_timestamp_is_refused_rather_than_stored_wrong():
    import pytest as _pytest
    from qmailstats.db import _naive_utc
    with _pytest.raises(ValueError):
        _naive_utc("not a timestamp")


def test_a_dropped_connection_is_reopened(store):
    """MySQL restarts, and idle connections are closed after wait_timeout.

    A process holding one connection for its whole life then fails every query
    until somebody restarts it -- which is what happened to both the dashboard
    and a running backfill when MySQL was restarted.
    """
    store.write_messages([_message(qp=9001)])
    assert store.scalar("SELECT COUNT(*) FROM message") == 1

    store.conn.close()                      # exactly what a restart looks like

    assert store.scalar("SELECT COUNT(*) FROM message") == 1, (
        "the connection was not reopened")


def test_writes_survive_a_dropped_connection_too(store):
    store.conn.close()
    store.write_messages([_message(qp=9002)])
    assert store.scalar("SELECT COUNT(*) FROM message") == 1
