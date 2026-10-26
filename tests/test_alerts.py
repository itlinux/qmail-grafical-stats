import datetime as dt
import os

import pytest

from qmailstats import alerts, db
from qmailstats.models import Delivery, Message

DSN = os.environ.get("QMAILSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="QMAILSTATS_TEST_DSN not set")

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


@pytest.fixture
def store():
    s = db.Store.from_dsn(DSN)
    s.apply_schema()
    for t in ("delivery", "message", "auth_event", "queue_minute",
              "service_minute", "checkpoint", "parser_state"):
        s.execute("DELETE FROM %s" % t)
    yield s
    s.close()


def recent(minutes):
    return NOW - dt.timedelta(minutes=minutes)


def test_quiet_server_raises_nothing(store):
    store.write_messages([Message(qp=1, ts=recent(5), direction="in",
                                  sender="a@sender.example", client_ip="192.0.2.1")])
    assert alerts.evaluate(store, {}) == []


def test_a_burst_of_discarded_bounces_is_raised(store):
    store.write_deliveries([
        Delivery(msg_id=i, ts=recent(10), recipient="#@[]", route="remote",
                 result="failure", msg_ts=recent(10),
                 detail="Sorry,_I_couldn't_find_any_host_named_[]./")
        for i in range(300)
    ])
    raised = alerts.evaluate(store, {})
    assert any(a["id"] == "bounce_storm" for a in raised)
    assert raised[0]["severity"] == "critical"
    assert "300" in raised[0]["detail"]


def test_outbound_failing_badly_is_raised(store):
    store.write_deliveries(
        [Delivery(msg_id=i, ts=recent(10), recipient="x@dest.example",
                  route="remote", result="failure", msg_ts=recent(10),
                  detail="Remote_host_said:_550_5.7.1_rejected/")
         for i in range(40)]
        + [Delivery(msg_id=100 + i, ts=recent(10), recipient="y@dest.example",
                    route="remote", result="success", msg_ts=recent(10))
           for i in range(10)])
    raised = alerts.evaluate(store, {})
    assert any(a["id"] == "delivery_failing" for a in raised)


def test_a_few_failures_out_of_a_few_attempts_is_not_an_alert(store):
    """Two failures out of three is noise, not a trend worth waking anyone."""
    store.write_deliveries([
        Delivery(msg_id=1, ts=recent(5), recipient="x@dest.example", route="remote",
                 result="failure", msg_ts=recent(5)),
        Delivery(msg_id=2, ts=recent(5), recipient="y@dest.example", route="remote",
                 result="failure", msg_ts=recent(5)),
        Delivery(msg_id=3, ts=recent(5), recipient="z@dest.example", route="remote",
                 result="success", msg_ts=recent(5)),
    ])
    assert not any(a["id"] == "delivery_failing" for a in alerts.evaluate(store, {}))


def test_a_parser_that_stopped_is_raised(store):
    """The dashboard showing yesterday's figures looks like a quiet server."""
    store.write_messages([Message(qp=1, ts=recent(180), direction="in",
                                  sender="a@sender.example", client_ip="192.0.2.1")])
    raised = alerts.evaluate(store, {})
    assert any(a["id"] == "parser_stalled" for a in raised)


def test_password_guessing_is_raised(store):
    # Distinct seconds: attempts identical down to the second collapse into
    # one row, because that is what makes re-reading a log idempotent.
    store.write_auth_events([
        {"ts": recent(10) + dt.timedelta(seconds=i), "service": "imap",
         "result": "auth_failed", "user": "someone@example.com",
         "client_ip": "203.0.113.5", "method": "PLAIN", "tls": True}
        for i in range(200)
    ])
    raised = alerts.evaluate(store, {})
    assert any(a["id"] == "auth_guessing" for a in raised)


def test_thresholds_can_be_configured(store):
    store.write_deliveries([
        Delivery(msg_id=i, ts=recent(10), recipient="#@[]", route="remote",
                 result="failure", msg_ts=recent(10))
        for i in range(10)
    ])
    assert not any(a["id"] == "bounce_storm" for a in alerts.evaluate(store, {}))
    raised = alerts.evaluate(store, {"discarded_bounces_per_hour": 5})
    assert any(a["id"] == "bounce_storm" for a in raised)


# --- notification ------------------------------------------------------------

def test_the_same_alert_is_not_sent_twice(tmp_path):
    from qmailstats.notify import changed_since_last_run

    state = tmp_path / "state.json"
    raised = [{"id": "bounce_storm", "severity": "critical",
               "title": "t", "detail": "d"}]
    assert changed_since_last_run(raised, state) is True
    assert changed_since_last_run(raised, state) is False, "would mail every run"


def test_a_new_alert_alongside_an_old_one_is_sent(tmp_path):
    from qmailstats.notify import changed_since_last_run

    state = tmp_path / "state.json"
    changed_since_last_run([{"id": "bounce_storm"}], state)
    assert changed_since_last_run(
        [{"id": "bounce_storm"}, {"id": "auth_guessing"}], state) is True


def test_recovery_is_worth_sending(tmp_path):
    """Knowing it stopped matters as much as knowing it started."""
    from qmailstats.notify import changed_since_last_run

    state = tmp_path / "state.json"
    changed_since_last_run([{"id": "bounce_storm"}], state)
    assert changed_since_last_run([], state) is True


def test_quiet_stays_quiet(tmp_path):
    from qmailstats.notify import changed_since_last_run

    state = tmp_path / "state.json"
    assert changed_since_last_run([], state) is False
    assert changed_since_last_run([], state) is False


def test_recent_windows_are_measured_in_utc(store):
    """Rows are stored in UTC; MySQL's NOW() is the server's local time.

    On a server six hours behind UTC that made every row look six hours into
    the future: the stalled-parser alert could never fire, and the "last hour"
    windows actually looked at six-to-seven hours ago.
    """
    ahead = store.scalar(
        "SELECT TIMESTAMPDIFF(MINUTE, UTC_TIMESTAMP(), NOW())")
    store.write_messages([Message(qp=1, ts=recent(1), direction="in",
                                  sender="a@sender.example", client_ip="192.0.2.1")])
    age = store.scalar(
        "SELECT TIMESTAMPDIFF(MINUTE, MAX(ts), UTC_TIMESTAMP()) FROM message")
    assert age >= 0, (
        "a message written one minute ago looks %d minutes in the future; "
        "the server's clock is %d minutes from UTC" % (-age, ahead))
    assert not any(a["id"] == "parser_stalled" for a in alerts.evaluate(store, {}))


def test_the_alert_sender_is_local_to_the_sending_server(tmp_path):
    """Using the recipient's address as the envelope sender fails SPF.

    The destination domain publishes which hosts may send for it; this server
    is not one of them, so mail claiming to be from it is rejected -- and the
    alert about a broken mail server is exactly the mail you cannot afford to
    lose.
    """
    from qmailstats.notify import envelope_sender

    assert envelope_sender({}, "mail.example.com") == "postmaster@mail.example.com"
    assert envelope_sender({"mail_from": "alerts@example.net"}, "h") == \
        "alerts@example.net"


def test_the_sender_never_defaults_to_the_recipients_domain(tmp_path):
    from qmailstats.notify import envelope_sender
    sender = envelope_sender({"mail_to": "someone@icloud-hosted.example"}, "mail.example.com")
    assert "icloud-hosted.example" not in sender
