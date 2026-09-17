"""Fold raw rows older than the retention window into daily_stats."""

import argparse
import configparser
import sys
import time

import pymysql

from qmailstats.db import Store

# Totals are kept with GREATEST, not replaced. A day older than the retention
# window only ever loses raw rows -- they are being pruned -- so if a prune is
# cut short, by the per-run cap or by anything else, the next fold sees part of
# that day and would otherwise overwrite the complete total with a smaller one.
FOLD_MESSAGES = """
INSERT INTO daily_stats (day, domain, direction, messages, bytes)
SELECT DATE(ts), sender_domain, direction, COUNT(*), COALESCE(SUM(size_bytes),0)
  FROM message
 WHERE ts < UTC_TIMESTAMP() - INTERVAL %s DAY AND sender_domain IS NOT NULL
 GROUP BY DATE(ts), sender_domain, direction
ON DUPLICATE KEY UPDATE
  messages = GREATEST(messages, VALUES(messages)),
  bytes    = GREATEST(bytes, VALUES(bytes))
"""

FOLD_DELIVERIES = """
INSERT INTO daily_stats (day, domain, direction, successes, deferrals, failures)
SELECT DATE(d.ts), d.recipient_domain, 'out',
       SUM(d.result='success'), SUM(d.result='deferral'), SUM(d.result='failure')
  FROM delivery d
 WHERE d.ts < UTC_TIMESTAMP() - INTERVAL %s DAY AND d.recipient_domain IS NOT NULL
 GROUP BY DATE(d.ts), d.recipient_domain
ON DUPLICATE KEY UPDATE
  successes = GREATEST(successes, VALUES(successes)),
  deferrals = GREATEST(deferrals, VALUES(deferrals)),
  failures  = GREATEST(failures, VALUES(failures))
"""


# MySQL: 1213 is a deadlock, 1205 a lock-wait timeout.
_CONTENTION = (1205, 1213)


def with_retry(action, attempts=5, pause=2.0):
    """Run *action*, trying again if InnoDB made it the loser of a deadlock.

    The rollup deletes in bounded chunks while the ingest is still inserting
    into the same tables, so contention is expected rather than exceptional --
    InnoDB rolls one of them back and the loser simply needs to try again.
    Failing the nightly run over it means the fold never happens.
    """
    for attempt in range(attempts):
        try:
            return action()
        except pymysql.err.OperationalError as exc:
            if exc.args[0] not in _CONTENTION or attempt == attempts - 1:
                raise
            time.sleep(pause * (attempt + 1))


# Rows removed from each table in one nightly run. A new install backfills
# months of logs, and deleting all of it in one night grew one server's undo
# tablespaces to 36 GB before purge could catch up. At this rate a backfill
# drains over a few nights instead.
MAX_PRUNE_ROWS = 100_000
PRUNE_CHUNK = 5_000
PRUNE_PAUSE = 0.5


def prune_table(store, table, retain_days, budget=None, chunk=None, pause=None):
    """Delete a table's oldest expired rows, up to *budget*; return how many.

    Oldest first, so whole days leave together and a run stopped by the budget
    leaves the newest expired days intact. The pause between chunks lets
    InnoDB purge keep up instead of piling undo up behind the deletes.
    """
    budget = MAX_PRUNE_ROWS if budget is None else budget
    chunk = PRUNE_CHUNK if chunk is None else chunk
    pause = PRUNE_PAUSE if pause is None else pause
    removed = 0
    while removed < budget:
        size = min(chunk, budget - removed)
        deleted = with_retry(lambda: store.execute(
            "DELETE FROM %s WHERE ts < UTC_TIMESTAMP() - INTERVAL %%s DAY "
            "ORDER BY ts LIMIT %d" % (table, size),
            (retain_days,),
        )) or 0
        removed += deleted
        if deleted < size:
            break
        if pause:
            time.sleep(pause)
    return removed


def run(store, retain_days, prune, budget=None):
    with_retry(lambda: store.execute(FOLD_MESSAGES, (retain_days,)))
    with_retry(lambda: store.execute(FOLD_DELIVERIES, (retain_days,)))
    if not prune:
        return {}
    return {table: prune_table(store, table, retain_days, budget)
            for table in ("delivery", "message")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Roll up and prune old rows.")
    ap.add_argument("--config", default="/etc/qmail-grafical-stats/config.ini")
    ap.add_argument("--retain-days", type=int, default=90)
    ap.add_argument("--prune", action="store_true", help="delete rows after folding")
    ap.add_argument("--max-prune-rows", type=int, default=MAX_PRUNE_ROWS,
                    help="rows to delete from each table in one run")
    args = ap.parse_args(argv)

    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config)
    store = Store.from_dsn(config.get("database", "dsn"))
    try:
        removed = run(store, args.retain_days, args.prune,
                      budget=args.max_prune_rows)
        print("rollup complete; pruned %s" % (removed or "nothing"))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
