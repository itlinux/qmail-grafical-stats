"""The nightly fold must survive losing a deadlock to the ingest."""

import pymysql
import pytest

from qmailstats.rollup import with_retry


def deadlock():
    return pymysql.err.OperationalError(1213, "Deadlock found when trying to get lock")


def test_retries_a_deadlock_and_succeeds(monkeypatch):
    monkeypatch.setattr("qmailstats.rollup.time.sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise deadlock()
        return "folded"

    assert with_retry(flaky) == "folded"
    assert len(calls) == 3


def test_gives_up_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr("qmailstats.rollup.time.sleep", lambda s: None)

    def always():
        raise deadlock()

    with pytest.raises(pymysql.err.OperationalError):
        with_retry(always, attempts=3)


def test_a_lock_wait_timeout_is_also_retried(monkeypatch):
    monkeypatch.setattr("qmailstats.rollup.time.sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise pymysql.err.OperationalError(1205, "Lock wait timeout exceeded")
        return "ok"

    assert with_retry(flaky) == "ok"


def test_other_errors_are_not_swallowed(monkeypatch):
    monkeypatch.setattr("qmailstats.rollup.time.sleep", lambda s: None)

    def broken():
        raise pymysql.err.OperationalError(1146, "Table doesn't exist")

    with pytest.raises(pymysql.err.OperationalError) as caught:
        with_retry(broken)
    assert caught.value.args[0] == 1146


class PruneStore:
    """Deletes from a pretend table of *rows* expired rows."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, sql, args=None):
        self.statements.append(sql)
        limit = int(sql.rsplit("LIMIT", 1)[1])
        taken = min(limit, self.rows)
        self.rows -= taken
        return taken


def test_a_large_backlog_is_pruned_only_up_to_the_budget():
    """A new install's backfill must drain over several nights, not one."""
    from qmailstats.rollup import prune_table

    store = PruneStore(rows=350_000)
    removed = prune_table(store, "delivery", 90, budget=100_000, chunk=5_000, pause=0)
    assert removed == 100_000
    assert store.rows == 250_000


def test_pruning_stops_when_nothing_is_left():
    from qmailstats.rollup import prune_table

    store = PruneStore(rows=12_345)
    assert prune_table(store, "message", 90, budget=100_000, chunk=5_000, pause=0) == 12_345
    assert store.rows == 0


def test_the_oldest_rows_go_first():
    """So a run cut short by the budget leaves whole recent days, not holes."""
    from qmailstats.rollup import prune_table

    store = PruneStore(rows=10)
    prune_table(store, "message", 90, budget=100, chunk=5, pause=0)
    assert all("ORDER BY ts" in sql for sql in store.statements)


def test_folds_never_shrink_a_stored_total():
    """A partly pruned day refolded later must not overwrite the full count."""
    from qmailstats.rollup import FOLD_DELIVERIES, FOLD_MESSAGES

    for sql in (FOLD_MESSAGES, FOLD_DELIVERIES):
        update = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        assignments = [a for a in update.split(",") if "=" in a]
        assert assignments
        assert all("GREATEST(" in a for a in assignments), update
