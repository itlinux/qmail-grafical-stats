import datetime as dt
import os

import pytest

from qmailstats import db
from qmailstats.models import Delivery, Message

DSN = os.environ.get("QMAILSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="QMAILSTATS_TEST_DSN not set")

WHEN = dt.datetime(2026, 9, 16, 2, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def store():
    s = db.Store.from_dsn(DSN)
    s.apply_schema()
    for t in ("delivery", "message", "checkpoint", "parser_state",
              "queue_minute", "service_minute", "daily_stats"):
        s.execute("DELETE FROM %s" % t)
    yield s
    s.close()


def smtp_side(sender, ts, qp, **kw):
    return Message(qp=qp, ts=ts, direction="in", sender=sender,
                   client_ip="198.51.100.4", **kw)


def queue_side(sender, ts, queue_qp, msg_id, **kw):
    return Message(qp=None, ts=ts, direction="out", sender=sender,
                   msg_id=msg_id, queue_qp=queue_qp, queue_ts=ts,
                   size_bytes=1000, **kw)


def test_the_two_sides_of_one_message_become_one_row(store):
    """Their qp values differ; sender and time are what tie them together."""
    store.write_messages([smtp_side("a@sender.example", WHEN, 272486)])
    store.write_messages([
        queue_side("a@sender.example", WHEN + dt.timedelta(microseconds=600000),
                   272491, 68566592)])
    assert store.scalar("SELECT COUNT(*) FROM message") == 1
    row = store.rows("SELECT qp, queue_qp, msg_id, client_ip, direction FROM message")[0]
    assert row["qp"] == 272486
    assert row["queue_qp"] == 272491
    assert row["msg_id"] == 68566592
    assert row["client_ip"] == "198.51.100.4"
    assert row["direction"] == "in", "the side that saw the port decides"


def test_a_queue_record_with_no_smtp_session_stands_alone(store):
    """Locally generated mail -- cron, bounces -- never had an SMTP session."""
    store.write_messages([queue_side("root@localhost", WHEN, 999, 12345)])
    assert store.scalar("SELECT COUNT(*) FROM message") == 1
    assert store.scalar("SELECT qp FROM message") is None


def test_a_different_sender_at_the_same_moment_is_not_matched(store):
    store.write_messages([smtp_side("a@sender.example", WHEN, 100)])
    store.write_messages([queue_side("b@sender.example", WHEN, 200, 1)])
    assert store.scalar("SELECT COUNT(*) FROM message") == 2


def test_the_same_sender_much_later_is_not_matched(store):
    store.write_messages([smtp_side("a@sender.example", WHEN, 100)])
    store.write_messages([
        queue_side("a@sender.example", WHEN + dt.timedelta(minutes=30), 200, 1)])
    assert store.scalar("SELECT COUNT(*) FROM message") == 2


def test_re_running_the_queue_side_does_not_duplicate_or_steal(store):
    store.write_messages([smtp_side("a@sender.example", WHEN, 100)])
    queue = queue_side("a@sender.example", WHEN + dt.timedelta(seconds=1), 200, 1)
    store.write_messages([queue])
    store.write_messages([queue])
    assert store.scalar("SELECT COUNT(*) FROM message") == 1


def test_two_sessions_from_one_sender_match_their_own_queue_records(store):
    first, second = WHEN, WHEN + dt.timedelta(seconds=20)
    store.write_messages([
        smtp_side("bulk@sender.example", first, 100),
        smtp_side("bulk@sender.example", second, 101),
    ])
    store.write_messages([
        queue_side("bulk@sender.example", second + dt.timedelta(seconds=1), 201, 2),
        queue_side("bulk@sender.example", first + dt.timedelta(seconds=1), 200, 1),
    ])
    assert store.scalar("SELECT COUNT(*) FROM message") == 2
    rows = store.rows("SELECT qp, msg_id FROM message ORDER BY ts")
    assert [r["qp"] for r in rows] == [100, 101]
    assert [r["msg_id"] for r in rows] == [1, 2], "matched to the wrong session"


def test_deliveries_attach_to_the_right_instance_of_a_reused_inode(store):
    first, second = WHEN, WHEN + dt.timedelta(hours=2)
    store.write_messages([
        queue_side("first@sender.example", first, 1, 68566592),
        queue_side("second@sender.example", second, 2, 68566592),
    ])
    store.write_deliveries([
        Delivery(msg_id=68566592, ts=first, recipient="one@dest.example",
                 route="remote", result="success", msg_ts=first),
        Delivery(msg_id=68566592, ts=second, recipient="two@dest.example",
                 route="remote", result="failure", msg_ts=second),
    ])
    rows = store.rows(
        "SELECT m.sender, d.recipient FROM delivery d "
        "JOIN message m ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts "
        "ORDER BY d.ts")
    assert [(r["sender"], r["recipient"]) for r in rows] == [
        ("first@sender.example", "one@dest.example"),
        ("second@sender.example", "two@dest.example"),
    ]


def test_a_delivery_with_no_known_instance_still_writes_only_once(store):
    """A 'starting delivery' whose info line rotated away has no msg_ts.

    NULL is distinct from NULL in a MySQL unique key, so leaving it null would
    let every re-parse insert the row again.
    """
    d = Delivery(msg_id=5, ts=WHEN, recipient="x@dest.example",
                 route="remote", result="success")
    store.write_deliveries([d])
    store.write_deliveries([d])
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 1


def test_an_unknown_instance_does_not_pretend_to_match_a_message(store):
    store.write_messages([queue_side("a@sender.example", WHEN, 1, 5)])
    store.write_deliveries([
        Delivery(msg_id=5, ts=WHEN, recipient="x@dest.example",
                 route="remote", result="success")])
    joined = store.scalar(
        "SELECT COUNT(*) FROM delivery d JOIN message m "
        "ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts")
    assert joined == 0, "an unknown instance was matched to a real message"


def test_a_queue_only_message_is_labelled_by_where_its_mail_went(store):
    """The queue log cannot see a port, so direction must be inferred.

    Delivered only locally means it arrived here; delivered only remotely means
    it left. Defaulting everything to outbound counted inbound mail as sent.
    """
    arrived = WHEN
    left = WHEN + dt.timedelta(hours=1)
    store.write_messages([
        queue_side("stranger@elsewhere.example", arrived, 1, 100),
        queue_side("us@example.com", left, 2, 101),
    ])
    store.write_deliveries([
        Delivery(msg_id=100, ts=arrived, recipient="staff@example.com",
                 route="local", result="success", msg_ts=arrived),
        Delivery(msg_id=101, ts=left, recipient="them@dest.example",
                 route="remote", result="success", msg_ts=left),
    ])
    store.infer_direction_from_routes()
    rows = {r["sender"]: r["direction"] for r in
            store.rows("SELECT sender, direction FROM message")}
    assert rows["stranger@elsewhere.example"] == "in"
    assert rows["us@example.com"] == "out"


def test_inference_never_overrides_a_session_that_saw_the_port(store):
    store.write_messages([smtp_side("a@sender.example", WHEN, 500)])
    store.write_deliveries([
        Delivery(msg_id=None or 700, ts=WHEN, recipient="x@dest.example",
                 route="remote", result="success", msg_ts=WHEN)])
    store.infer_direction_from_routes()
    assert store.scalar("SELECT direction FROM message WHERE qp=500") == "in", (
        "a row whose port was observed must keep its direction")
