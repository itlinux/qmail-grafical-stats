"""The push transport and the cracked-account check.

These need no database. The alert tests that do are in test_alerts.py and skip
without QMAILSTATS_TEST_DSN; the point of keeping these separate is that the
one alert which reports a database outage must be testable without one.
"""

import json

from qmailstats import alerts, push


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for urllib.request.urlopen and records what was sent."""

    def __init__(self, status=200, raises=None):
        self.status = status
        self.raises = raises
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        if self.raises:
            raise self.raises
        return FakeResponse(self.status)


class FakeStore:
    """Returns canned rows and remembers the query it was given."""

    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.args = None

    def rows(self, sql, args=None):
        self.sql = sql
        self.args = args
        return self._rows


def test_nothing_to_say_sends_nothing():
    assert push.compose([], "q7") is None
    opener = FakeOpener()
    assert push.send("https://ntfy.sh/topic", [], "q7", opener=opener) is False
    assert opener.calls == []


def test_the_worst_alert_leads_and_the_rest_are_listed():
    payload = push.compose([
        {"id": "a", "severity": "critical", "title": "Account cracked",
         "detail": "login after 11 failures"},
        {"id": "b", "severity": "warning", "title": "Disk at 91%",
         "detail": "/ is nearly full"},
    ], "qmail7")
    assert payload["title"] == "qmail7: Account cracked"
    assert payload["message"].startswith("login after 11 failures")
    assert "also: Disk at 91%" in payload["message"]
    assert payload["priority"] == 5


def test_severity_decides_whether_a_phone_makes_a_sound():
    def priority(sev):
        return push.compose(
            [{"id": "x", "severity": sev, "title": "t", "detail": "d"}],
            "h")["priority"]

    assert priority("critical") == 5     # ntfy wants an int, not "urgent"
    assert priority("serious") == 4
    assert priority("warning") == 3


def test_the_topic_is_taken_from_the_url_and_posted_to_its_base():
    opener = FakeOpener()
    push.send("https://ntfy.sh/mail-alerts-abc123",
              [{"id": "x", "severity": "critical", "title": "t", "detail": "d"}],
              "q7", opener=opener)
    request = opener.calls[0]
    assert request.full_url == "https://ntfy.sh"
    assert json.loads(request.data)["topic"] == "mail-alerts-abc123"


def test_priority_is_an_integer_because_ntfy_rejects_the_words():
    """Sending "urgent" instead of 5 is answered with HTTP 400."""
    opener = FakeOpener()
    push.send("https://ntfy.sh/t",
              [{"id": "x", "severity": "critical", "title": "t", "detail": "d"}],
              "q7", opener=opener)
    assert json.loads(opener.calls[0].data)["priority"] == 5


def test_a_token_is_sent_as_a_bearer_header():
    opener = FakeOpener()
    push.send("https://ntfy.sh/t",
              [{"id": "x", "severity": "warning", "title": "t", "detail": "d"}],
              "q7", token="secret", opener=opener)
    assert opener.calls[0].get_header("Authorization") == "Bearer secret"


def test_a_failed_push_never_raises():
    """The alert about a broken server must not become a second failure."""
    opener = FakeOpener(raises=OSError("network down"))
    sent = push.send("https://ntfy.sh/t",
                     [{"id": "x", "severity": "critical", "title": "t",
                       "detail": "d"}], "q7", opener=opener)
    assert sent is False


def test_send_plain_needs_no_alert_evaluator():
    """This is the path used when the database cannot be reached at all."""
    opener = FakeOpener()
    assert push.send_plain("https://ntfy.sh/t", "q7", "Database unreachable",
                           "mysqld is down", opener=opener) is True
    body = json.loads(opener.calls[0].data)
    assert body["title"] == "q7: Database unreachable"
    assert body["priority"] == 5


def test_no_url_configured_is_not_an_error():
    assert push.send("", [{"id": "x", "severity": "warning", "title": "t",
                           "detail": "d"}], "q7") is False


def test_a_quiet_server_raises_no_cracked_account_alert():
    assert alerts.cracked_account(FakeStore([]), 5, 60) is None


def test_a_login_after_repeated_failures_is_reported():
    store = FakeStore([
        {"user": "sara@example.com", "client_ip": "203.0.113.9",
         "failures": 11, "seen": "2026-10-25 14:00:00"},
    ])
    alert = alerts.cracked_account(store, 5, 60)
    assert alert["severity"] == "critical"
    assert "sara@example.com" in alert["detail"]
    assert "203.0.113.9" in alert["detail"]
    assert "11" in alert["detail"]


def test_several_cracked_accounts_are_all_listed():
    store = FakeStore([
        {"user": "a@x.com", "client_ip": "198.51.100.1", "failures": 9,
         "seen": "2026-10-25 14:00:00"},
        {"user": "b@x.com", "client_ip": "198.51.100.2", "failures": 6,
         "seen": "2026-10-25 14:01:00"},
    ])
    detail = alerts.cracked_account(store, 5, 60)["detail"]
    assert "a@x.com" in detail and "b@x.com" in detail


def test_trusted_addresses_are_excluded_in_the_query():
    """Shared egress is not a user: the gateway everyone NATs through, and
    localhost, which is how a mail server sees its own webmail."""
    store = FakeStore([])
    alerts.cracked_account(store, 5, 60, ("127.0.0.1", "208.53.44.225"))
    assert "NOT IN" in store.sql
    assert "127.0.0.1" in store.args
    assert "208.53.44.225" in store.args


def test_without_trusted_addresses_no_exclusion_is_added():
    store = FakeStore([])
    alerts.cracked_account(store, 5, 60, ())
    assert "NOT IN" not in store.sql


def test_the_window_and_threshold_are_passed_through():
    store = FakeStore([])
    alerts.cracked_account(store, 7, 90, ())
    assert store.args[0] == 90      # window minutes, bounds the outer scan
    assert store.args[-1] == 7      # failures required


# --- Store.release(): the dashboard's connection leak ---------------------
#
# serve.py runs a ThreadingHTTPServer, so every request gets its own thread
# and its own MySQL connection. Nothing closed them when the thread ended, so
# the server climbed to max_connections; on q7 that starved vpopmail and took
# webmail down. release() hands the thread's connection back.

class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _store(primary, connect_args=None):
    # qmailstats.db imports pymysql, which need not be installed to exercise
    # the connection bookkeeping. A stub keeps these runnable anywhere, like
    # the rest of this file.
    import sys, types
    if "pymysql" not in sys.modules:
        stub = types.ModuleType("pymysql")
        stub.connect = lambda **kw: FakeConn()
        stub.err = types.ModuleType("pymysql.err")
        stub.err.OperationalError = OSError
        stub.err.InterfaceError = OSError
        stub.cursors = types.ModuleType("pymysql.cursors")
        stub.cursors.DictCursor = object
        sys.modules["pymysql"] = stub
        sys.modules["pymysql.err"] = stub.err
        sys.modules["pymysql.cursors"] = stub.cursors
    from qmailstats.db import Store
    return Store(primary, connect_args=connect_args)


def test_release_closes_a_connection_the_thread_opened():
    primary, mine = FakeConn(), FakeConn()
    s = _store(primary, {"host": "x"})
    s.conn = mine                      # as a request thread's connection would be
    s.release()
    assert mine.closed is True
    assert primary.closed is False     # the primary is not ours to close
    assert mine not in s._opened       # and it is no longer tracked


def test_release_leaves_the_primary_connection_alone():
    """Single-threaded callers and the fixtures were handed it and need it."""
    primary = FakeConn()
    s = _store(primary, {"host": "x"})
    s.release()
    assert primary.closed is False


def test_release_is_safe_to_call_twice():
    primary, mine = FakeConn(), FakeConn()
    s = _store(primary, {"host": "x"})
    s.conn = mine
    s.release()
    s.release()                        # no connection left; must not raise
    assert mine.closed is True


def test_release_tolerates_a_connection_that_already_failed():
    class Angry(FakeConn):
        def close(self):
            raise OSError("gone")
    primary, mine = FakeConn(), Angry()
    s = _store(primary, {"host": "x"})
    s.conn = mine
    s.release()                        # must not propagate
    assert mine not in s._opened


def test_close_still_closes_everything():
    """release() must not break the shutdown path."""
    primary, mine = FakeConn(), FakeConn()
    s = _store(primary, {"host": "x"})
    s.conn = mine
    s.close()
    assert primary.closed is True and mine.closed is True
