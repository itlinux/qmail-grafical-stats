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
    # queue_minute and service_minute included deliberately: their counters
    # accumulate rather than replace, so leftovers leak between tests.
    for table in ("delivery", "message", "checkpoint", "parser_state",
                  "daily_stats", "queue_minute", "service_minute"):
        s.execute("DELETE FROM %s" % table)
    yield s
    s.close()


@pytest.fixture
def logroot(tmp_path):
    """A fake /var/log/qmail built from the captured fixtures."""
    for name in ("send", "smtp", "submission", "smtps"):
        (tmp_path / name).mkdir()
    shutil.copy(FIXTURES / "send.log", tmp_path / "send" / "current")
    shutil.copy(FIXTURES / "smtp.log", tmp_path / "smtp" / "current")
    shutil.copy(FIXTURES / "submission.log", tmp_path / "submission" / "current")
    (tmp_path / "smtps" / "current").write_text("")
    return tmp_path


def _config(**parser):
    import configparser
    c = configparser.ConfigParser()
    c.read_dict({"parser": dict(parser), "database": {"dsn": DSN}})
    return c


def _run(store, logroot, config=None):
    config = config or _config()
    out = {}
    for logdir in ingest.ORDER:
        out[logdir] = ingest.ingest_dir(store, logroot, logdir, config, 500, 0.0)
    return out


def test_ingests_the_fixtures_into_rows(store, logroot):
    _run(store, logroot)
    assert store.scalar("SELECT COUNT(*) FROM message") == 3
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 1


def test_inbound_and_outbound_are_told_apart(store, logroot):
    _run(store, logroot)
    rows = store.rows("SELECT direction, COUNT(*) AS n FROM message GROUP BY direction")
    counts = {r["direction"]: int(r["n"]) for r in rows}
    assert counts == {"in": 1, "out": 2}


def test_authenticated_accounts_are_recorded(store, logroot):
    _run(store, logroot)
    rows = store.rows("SELECT auth_user FROM message WHERE auth_user IS NOT NULL")
    assert {r["auth_user"] for r in rows} == {
        "info@example.com", "pc@example.net",
    }


def test_running_twice_does_not_duplicate_rows(store, logroot):
    _run(store, logroot)
    _run(store, logroot)
    assert store.scalar("SELECT COUNT(*) FROM message") == 3
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 1


def test_a_second_run_picks_up_only_the_new_lines(store, logroot):
    _run(store, logroot)
    with (logroot / "send" / "current").open("a") as handle:
        handle.write(
            "@400000006aa8e01a2c3cb000 info msg 70000001: bytes 99 from <new@account.example> qp 5555 uid 89\n"
            "@400000006aa8e01a2c3cb001 starting delivery 800: msg 70000001 to remote far@distant.example\n"
            "@400000006aa8e01a2c3cb002 delivery 800: failure: No_such_address./\n"
        )
    _run(store, logroot)
    assert store.scalar("SELECT COUNT(*) FROM message") == 4
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 2
    assert store.scalar("SELECT result FROM delivery WHERE recipient='far@distant.example'") == "failure"


def test_state_carries_a_delivery_across_two_runs(store, logroot):
    """The 'starting' line lands in one run and its outcome in the next."""
    (logroot / "send" / "current").write_text(
        "@400000006aa8e01a2c3cc000 info msg 81: bytes 10 from <a@sender.example> qp 900 uid 89\n"
        "@400000006aa8e01a2c3cc001 starting delivery 900: msg 81 to remote split@example.com\n"
    )
    _run(store, logroot)
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 0

    with (logroot / "send" / "current").open("a") as handle:
        handle.write("@400000006aa8e01a2c3cc002 delivery 900: success: did_1+0+0/\n")
    _run(store, logroot)
    assert store.scalar("SELECT COUNT(*) FROM delivery") == 1
    assert store.scalar("SELECT recipient FROM delivery") == "split@example.com"


def test_the_qp_join_links_the_smtp_session_to_its_queue_row(store, logroot):
    """The inbound message's qp appears in smtp; msg_id only in send."""
    (logroot / "send" / "current").write_text(
        "@400000006aa8e01a2bdd0000 info msg 68566592: bytes 4523 "
        "from <sm.26791275955.kbntqtrpftk8qpyz02-noreply=newsletter.example@bulk.example> "
        "qp 210429 uid 89\n"
    )
    _run(store, logroot)
    row = store.rows("SELECT qp, msg_id, direction, client_ip FROM message WHERE qp=210429")
    assert len(row) == 1, "the two logs produced two rows instead of one"
    assert row[0]["msg_id"] == 68566592
    assert row[0]["direction"] == "in"
    # The capture's 'tcpserver: ok' line belongs to pid 210485, a different
    # session; this message is pid 210394, whose connect line rotated away.
    # So the client address comes from the simscan line, and the direction
    # falls back to the default for the smtp log rather than a seen port.
    assert row[0]["client_ip"] == "198.51.100.224"


def test_connection_and_concurrency_counters_are_written(store, logroot):
    """A run holds its newest minute back, so the counters land on the next one.

    The fixtures span barely a minute, so the first run writes nothing and
    carries everything forward -- which is the behaviour worth pinning.
    """
    _run(store, logroot)
    # Appending a later line moves the newest minute on, releasing the earlier
    # one exactly as the next cron run would.
    with (logroot / "smtp" / "current").open("a") as handle:
        handle.write("@400000006ab8e01718c73554 tcpserver: pid 99 from 192.0.2.5\n")
    with (logroot / "send" / "current").open("a") as handle:
        handle.write("@400000006ab8e01a2c3caf24 status: local 2/10 remote 1/60\n")
    _run(store, logroot)

    row = store.rows(
        "SELECT SUM(connects) AS connects, SUM(messages) AS messages, "
        "MAX(max_concurrency) AS peak FROM service_minute WHERE service='smtp'"
    )[0]
    assert int(row["connects"]) == 1
    assert int(row["messages"]) == 1
    assert int(row["peak"]) == 1

    queue = store.rows("SELECT local_max_used, local_limit FROM queue_minute")
    assert queue, "the send log's status lines should have been folded"
    assert int(queue[0]["local_limit"]) == 10


def test_connection_counters_do_not_double_when_a_run_repeats(store, logroot):
    _run(store, logroot)
    before = store.scalar("SELECT SUM(connects) FROM service_minute")
    _run(store, logroot)
    after = store.scalar("SELECT SUM(connects) FROM service_minute")
    assert after == before, "the checkpoint should stop a file being counted twice"
