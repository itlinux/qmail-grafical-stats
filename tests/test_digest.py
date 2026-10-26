import datetime as dt
import os

import pytest

from qmailstats import db, digest
from qmailstats.models import Delivery, Message

DSN = os.environ.get("QMAILSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="QMAILSTATS_TEST_DSN not set")

DAY = dt.date(2026, 9, 15)
NOON = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def store():
    s = db.Store.from_dsn(DSN)
    s.apply_schema()
    for t in ("delivery", "message", "auth_event", "queue_minute",
              "service_minute", "daily_stats"):
        s.execute("DELETE FROM %s" % t)
    s.write_messages([
        Message(qp=1, ts=NOON, direction="out", sender="me@example.com",
                auth_user="me@example.com", msg_id=1, size_bytes=1000,
                client_ip="192.0.2.1"),
        Message(qp=2, ts=NOON, direction="in", sender="them@partner.example",
                msg_id=2, size_bytes=2000, client_ip="198.51.100.1",
                verdict="SPAM", spam_score=12.0),
    ])
    s.write_deliveries([
        Delivery(msg_id=1, ts=NOON, recipient="you@partner.example",
                 route="remote", result="failure", msg_ts=NOON,
                 detail="Remote_host_said:_550_5.1.1_User_unknown/"),
        Delivery(msg_id=2, ts=NOON, recipient="me@example.com",
                 route="local", result="success", msg_ts=NOON),
    ])
    s.write_auth_events([
        {"ts": NOON, "service": "imap", "result": "login", "user": "me@example.com",
         "client_ip": "192.0.2.5", "method": "PLAIN", "tls": True},
        {"ts": NOON + dt.timedelta(seconds=1), "service": "imap",
         "result": "auth_failed", "user": "root@example.com",
         "client_ip": "203.0.113.9", "method": "PLAIN", "tls": True},
    ])
    yield s
    s.close()


def test_the_digest_covers_one_day(store):
    text = digest.compose(store, DAY, "mail.example.com")
    assert "2026-09-15" in text
    assert "mail.example.com" in text


def test_it_reports_volume_and_delivery(store):
    text = digest.compose(store, DAY, "mail.example.com")
    assert "Received" in text and "Sent" in text


def test_it_names_what_failed_and_why(store):
    """The thing logwatch cannot tell you: which message, and what was said."""
    text = digest.compose(store, DAY, "mail.example.com")
    assert "you@partner.example" in text
    assert "User unknown" in text


def test_it_reports_mailbox_access_and_guessing(store):
    text = digest.compose(store, DAY, "mail.example.com")
    assert "203.0.113.9" in text, "the address guessing passwords is not named"


def test_a_quiet_day_still_produces_a_report(store):
    text = digest.compose(store, dt.date(2020, 1, 1), "mail.example.com")
    assert "2020-01-01" in text
    assert "no mail" in text.lower() or "0" in text
