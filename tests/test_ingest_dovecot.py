import datetime as dt
import gzip
import os
import pathlib
import shutil

import pytest

from qmailstats import db, ingest

DSN = os.environ.get("QMAILSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="QMAILSTATS_TEST_DSN not set")

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def store():
    s = db.Store.from_dsn(DSN)
    s.apply_schema()
    for table in ("auth_event", "checkpoint", "service_minute"):
        s.execute("DELETE FROM %s" % table)
    yield s
    s.close()


@pytest.fixture
def logfile(tmp_path):
    path = tmp_path / "dovecot.log"
    shutil.copy(FIXTURES / "dovecot.log", path)
    return path


def _run(store, path, tz="America/Denver"):
    return ingest.ingest_dovecot(store, path, tz_name=tz, year=2026, batch_size=500)


def test_ingests_logins_scans_and_failures(store, logfile):
    _run(store, logfile)
    rows = store.rows("SELECT result, COUNT(*) AS n FROM auth_event GROUP BY result")
    counts = {r["result"]: int(r["n"]) for r in rows}
    assert counts == {"login": 3, "no_auth": 7, "auth_failed": 2}


def test_leaves_out_the_lines_that_are_not_login_events(store, logfile):
    _run(store, logfile)
    # The "Logged out" line is session bookkeeping, not an access event.
    assert store.scalar("SELECT COUNT(*) FROM auth_event") == 12


def test_stores_timestamps_converted_to_utc(store, logfile):
    _run(store, logfile)
    first = store.scalar(
        "SELECT MIN(ts) FROM auth_event WHERE result = 'login'")
    # 00:03:15 MDT on 13 September is 06:03:15 UTC.
    assert first == dt.datetime(2026, 9, 13, 6, 3, 15)


def test_running_twice_does_not_duplicate_events(store, logfile):
    _run(store, logfile)
    _run(store, logfile)
    assert store.scalar("SELECT COUNT(*) FROM auth_event") == 12


def test_a_second_run_picks_up_only_new_lines(store, logfile):
    _run(store, logfile)
    with logfile.open("a") as handle:
        handle.write(
            "Sep 13 09:00:00 imap-login: Info: Login: user=<new@account.example>, "
            "method=PLAIN, rip=192.0.2.1, lip=192.0.2.8, mpid=1, TLS, session=<n>\n")
    _run(store, logfile)
    assert store.scalar("SELECT COUNT(*) FROM auth_event") == 13


def test_reads_gzipped_rotations_alongside_the_live_file(store, logfile):
    rotation = logfile.parent / "dovecot.log-20260906.gz"
    rotation.write_bytes(gzip.compress(
        b"Sep  6 10:00:00 imap-login: Info: Login: user=<old@account.example>, "
        b"method=PLAIN, rip=192.0.2.9, lip=192.0.2.8, mpid=1, TLS, session=<o>\n"))
    _run(store, logfile)
    assert store.scalar(
        "SELECT COUNT(*) FROM auth_event WHERE user = 'old@account.example'") == 1


def test_folds_access_into_the_same_per_minute_counters_as_smtp(store, logfile):
    _run(store, logfile)
    rows = store.rows(
        "SELECT service, SUM(connects) AS connects FROM service_minute "
        "GROUP BY service")
    services = {r["service"]: int(r["connects"]) for r in rows}
    assert services.get("imap", 0) >= 1
    assert services.get("pop3", 0) >= 1


def test_lines_that_are_not_login_events_are_not_called_malformed(store, logfile):
    """A session's "Logged out" line is not a login event and not a defect.

    Counting them as malformed made the figure meaningless: 103,128 of them on
    a real server, which hides an actual parsing failure in the noise.
    """
    counts = _run(store, logfile)
    assert counts["malformed"] == 0, "ordinary non-login lines counted as broken"
    assert counts.get("ignored", 0) >= 1, "they should be counted as ignored"


def test_a_genuinely_unreadable_line_is_still_counted(store, logfile):
    with logfile.open("a") as handle:
        handle.write("this line has no syslog timestamp at all\n")
    counts = _run(store, logfile)
    assert counts["malformed"] == 1
