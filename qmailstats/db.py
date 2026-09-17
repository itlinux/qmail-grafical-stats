"""MySQL access. Every write is idempotent so a re-run changes nothing."""

import datetime as dt
import json
import pathlib
import threading
from urllib.parse import unquote, urlparse

import pymysql

SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[1] / "schema.sql"

# Columns added after the first release, applied by apply_schema() when the
# table already exists. Additive only: never drop or retype a column here,
# because an operator may still be running the previous version against it.
# How far apart the two logs may record the same message. They are written
# within the same second in practice; a few seconds of slack covers a loaded
# queue without letting one sender's next message be claimed by mistake.
CORRELATION_WINDOW_SECONDS = 5

MIGRATIONS = [
    ("message", "queue_qp", "INT UNSIGNED NULL AFTER `qp`"),
    ("message", "queue_ts", "DATETIME(3) NULL AFTER `queue_qp`"),
    ("delivery", "msg_ts",
     "DATETIME(3) NOT NULL DEFAULT '1970-01-01 00:00:00' AFTER `detail`"),
    ("message", "verdict", "VARCHAR(16) NULL AFTER `spam_score`"),
    ("message", "auth_source",
     "ENUM('policy_check','envelope') NULL AFTER `verdict`"),
    ("delivery", "remote_ip", "VARCHAR(45) NULL AFTER `detail`"),
    ("delivery", "remote_code", "SMALLINT UNSIGNED NULL AFTER `remote_ip`"),
    ("delivery", "remote_status", "VARCHAR(8) NULL AFTER `remote_code`"),
    ("delivery", "reason", "VARCHAR(255) NULL AFTER `remote_status`"),
]

# Columns whose type changed after first release. Widening only -- never
# narrow one, because rows already stored could not survive the change.
RETYPES = [
    ("delivery", "detail", "VARCHAR(1024) NULL", 1024),
]

# Columns that became nullable. `qp` was mandatory while the two logs were
# thought to share it; a record seen only by the queue log has no SMTP qp at
# all, so the column has to accept NULL.
NULLABLE = [
    ("message", "qp", "INT UNSIGNED NULL"),
]

# Indexes that changed shape. Dropped first, then added, and both steps skip
# themselves when the database is already in the wanted state.
DROP_INDEXES = [
    ("message", "uq_message_qp_ts"),
    ("delivery", "uq_delivery"),
]
ADD_INDEXES = [
    ("message", "uq_message_smtp", "UNIQUE KEY `uq_message_smtp` (`qp`, `ts`)"),
    ("message", "uq_message_queue",
     "UNIQUE KEY `uq_message_queue` (`queue_qp`, `queue_ts`)"),
    ("message", "ix_message_sender_ts", "KEY `ix_message_sender_ts` (`sender`, `ts`)"),
    ("message", "ix_message_msgid_queuets",
     "KEY `ix_message_msgid_queuets` (`msg_id`, `queue_ts`)"),
    ("delivery", "uq_delivery",
     "UNIQUE KEY `uq_delivery` (`msg_id`, `msg_ts`, `ts`, `recipient`, `result`)"),
]

_SMTP_MESSAGE_SQL = """
INSERT INTO message
  (qp, ts, direction, sender, sender_domain, auth_user, auth_source, msg_id,
   size_bytes, client_ip, spam_score, verdict, subject, queue_qp, queue_ts)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  msg_id      = COALESCE(VALUES(msg_id), msg_id),
  size_bytes  = COALESCE(VALUES(size_bytes), size_bytes),
  auth_user   = COALESCE(VALUES(auth_user), auth_user),
  auth_source = COALESCE(VALUES(auth_source), auth_source),
  client_ip   = COALESCE(VALUES(client_ip), client_ip),
  spam_score  = COALESCE(VALUES(spam_score), spam_score),
  verdict     = COALESCE(VALUES(verdict), verdict),
  subject     = COALESCE(VALUES(subject), subject),
  queue_qp    = COALESCE(VALUES(queue_qp), queue_qp),
  queue_ts    = COALESCE(VALUES(queue_ts), queue_ts),
  direction   = IF(VALUES(client_ip) IS NOT NULL, VALUES(direction), direction)
"""

# Attach a queue record to the SMTP session that fed it: same sender, closest
# in time, not already claimed by another queue record.
_CORRELATE_SQL = """
UPDATE message
   SET msg_id = %s, size_bytes = %s, queue_qp = %s, queue_ts = %s
 WHERE sender = %s
   AND queue_qp IS NULL
   AND qp IS NOT NULL
   AND ts BETWEEN %s AND %s
 ORDER BY ABS(TIMESTAMPDIFF(MICROSECOND, ts, %s))
 LIMIT 1
"""

# A millisecond either side, not equality. TAI64N gives the parser
# microseconds, queue_ts is DATETIME(3), and MySQL rounds on the way in -- so an
# equality test never matched a record that was already stored, and re-reading
# one tried to apply it a second time. queue_qp is a process id, which cannot
# repeat inside a millisecond, so the window cannot join two real messages.
_ALREADY_CORRELATED_SQL = """
SELECT id FROM message
 WHERE queue_qp = %s AND queue_ts BETWEEN %s AND %s
 LIMIT 1
"""

_DELIVERY_SQL = """
INSERT IGNORE INTO delivery
  (msg_id, msg_ts, ts, recipient, recipient_domain, route, result, detail,
   remote_ip, remote_code, remote_status, reason)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


_QUEUE_MINUTE_SQL = """
INSERT INTO queue_minute
  (minute, local_max_used, local_limit, remote_max_used, remote_limit)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  local_max_used  = GREATEST(local_max_used, VALUES(local_max_used)),
  remote_max_used = GREATEST(remote_max_used, VALUES(remote_max_used)),
  local_limit     = VALUES(local_limit),
  remote_limit    = VALUES(remote_limit)
"""

_SERVICE_MINUTE_SQL = """
INSERT INTO service_minute
  (minute, service, connects, messages, max_concurrency, limit_value)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  connects        = connects + VALUES(connects),
  messages        = messages + VALUES(messages),
  max_concurrency = GREATEST(max_concurrency, VALUES(max_concurrency)),
  limit_value     = VALUES(limit_value)
"""


_AUTH_EVENT_SQL = """
INSERT IGNORE INTO auth_event
  (ts, service, result, user, client_ip, method, tls)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def split_statements(sql):
    """Split a schema file into statements.

    Comments are stripped first: a ';' inside one would otherwise cut a
    statement in half, and MySQL would be handed the fragment.
    """
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return [s.strip() for s in without_comments.split(";") if s.strip()]


# Stands in for "which instance of this inode is unknown". A real timestamp is
# never this value, and using it rather than NULL keeps the delivery unique
# key working: in MySQL, NULL is distinct from NULL, so a null here would let
# every re-parse insert the same row again.
UNKNOWN_INSTANCE = dt.datetime(1970, 1, 1, 0, 0, 0)


def _naive_utc(ts):
    """MySQL DATETIME has no timezone. Store UTC, drop the tzinfo.

    Accepts an ISO string as well as a datetime: carried parser state is JSON,
    and a timestamp nested inside it comes back as text.
    """
    if isinstance(ts, str):
        ts = dt.datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        return ts
    return ts.replace(tzinfo=None)


_BAN_EVENT_SQL = (
    "INSERT IGNORE INTO ban_event (ts, jail, ip, action) VALUES (%s, %s, %s, %s)"
)


class Store:
    def __init__(self, connection, connect_args=None):
        # One connection per thread. The dashboard is a threading HTTP server
        # and the page asks for several endpoints at once; sharing a single
        # PyMySQL connection let two requests interleave on the same socket,
        # which desynchronises the wire protocol ("Protocol error, expecting
        # EOF") and fails both. Threads that arrive without a connection of
        # their own open one, unless the Store was built around a connection
        # handed in directly, as the tests do.
        self._local = threading.local()
        self._local.conn = connection
        self._primary = connection
        self._opened = [connection]
        self._opened_lock = threading.Lock()
        self._connect_args = connect_args or {}

    @property
    def conn(self):
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        if not self._connect_args:
            # No way to open another one; share the original, which is what
            # single-threaded callers and the fixtures expect.
            return self._primary
        fresh = pymysql.connect(**self._connect_args)
        self._local.conn = fresh
        with self._opened_lock:
            self._opened.append(fresh)
        return fresh

    @conn.setter
    def conn(self, value):
        self._local.conn = value
        with self._opened_lock:
            if value not in self._opened:
                self._opened.append(value)

    @classmethod
    def from_dsn(cls, dsn):
        """Accepts mysql://user:password@host:port/database."""
        parts = urlparse(dsn)
        connect_args = dict(
            host=parts.hostname or "127.0.0.1",
            port=parts.port or 3306,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=(parts.path or "/").lstrip("/"),
            charset="utf8mb4",
            # Autocommit matters for the serving process, not for write speed:
            # with it off, the first SELECT opens a transaction that is never
            # committed, and REPEATABLE READ then pins every later read to that
            # snapshot -- the dashboard would show the data as it was when the
            # service started and never move. Batched writes still group their
            # rows in a single executemany.
            autocommit=True,
        )
        return cls(pymysql.connect(**connect_args), connect_args)

    def close(self):
        # Tolerant: a connection may already be gone -- MySQL restarted, or it
        # was closed to test that path -- and closing twice should not be an
        # error worth raising. Every connection this Store opened is closed,
        # not just the calling thread's.
        with self._opened_lock:
            connections = list(self._opened)
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass

    def _live(self):
        """Reopen the connection if it has gone away.

        A process here can outlive its connection: MySQL gets restarted, or
        closes an idle one after wait_timeout. Holding a single connection for
        the life of the process then means every query fails until somebody
        notices and restarts the service.
        """
        try:
            # ping(reconnect=True) is deprecated in PyMySQL 2 and no longer
            # reliably reconnects, so decide and reconnect explicitly.
            if getattr(self.conn, "_sock", None) is None:
                raise pymysql.err.InterfaceError(0, "connection is closed")
            self.conn.ping(reconnect=False)
        except Exception:
            if not self._connect_args:
                raise
            self.conn = pymysql.connect(**self._connect_args)
        return self.conn

    def apply_schema(self):
        """Create anything missing, then add columns introduced since.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a server that installed an earlier version would keep its
        old columns forever. Every additive change since the first release is
        listed in MIGRATIONS and applied only when absent.
        """
        sql = SCHEMA_PATH.read_text()
        with self._live().cursor() as cur:
            for statement in split_statements(sql):
                cur.execute(statement)
        self.conn.commit()

        for table, column, definition in MIGRATIONS:
            if column in self.columns_of(table):
                continue
            self.execute(
                "ALTER TABLE `%s` ADD COLUMN `%s` %s" % (table, column, definition)
            )

        for table, column, definition in NULLABLE:
            if self.is_nullable(table, column):
                continue
            self.execute("ALTER TABLE `%s` MODIFY COLUMN `%s` %s"
                         % (table, column, definition))

        for table, index in DROP_INDEXES:
            if self.has_index(table, index):
                self.execute("ALTER TABLE `%s` DROP INDEX `%s`" % (table, index))

        for table, index, definition in ADD_INDEXES:
            if self.has_index(table, index):
                continue
            self.execute("ALTER TABLE `%s` ADD %s" % (table, definition))

        for table, column, definition, wanted_length in RETYPES:
            current = self.scalar(
                "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "AND COLUMN_NAME = %s",
                (table, column),
            )
            if current is not None and int(current) < wanted_length:
                self.execute(
                    "ALTER TABLE `%s` MODIFY COLUMN `%s` %s"
                    % (table, column, definition)
                )

    def columns_of(self, table):
        """Column names of one table in the connected database."""
        rows = self.rows(
            "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        )
        return {row["name"] for row in rows}

    def is_nullable(self, table, column):
        value = self.scalar(
            "SELECT IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND COLUMN_NAME = %s",
            (table, column),
        )
        return value is None or value == "YES"

    def has_index(self, table, index):
        return bool(self.scalar(
            "SELECT COUNT(*) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND INDEX_NAME = %s",
            (table, index),
        ))

    def execute(self, sql, args=None):
        """Run a statement, commit it, and return the number of rows it touched."""
        with self._live().cursor() as cur:
            cur.execute(sql, args or ())
            affected = cur.rowcount
        self.conn.commit()
        return affected

    def scalar(self, sql, args=None):
        with self._live().cursor() as cur:
            cur.execute(sql, args or ())
            row = cur.fetchone()
        return None if row is None else row[0]

    def rows(self, sql, args=None):
        with self._live().cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, args or ())
            return cur.fetchall()

    def write_messages(self, messages):
        """Write both kinds of message record.

        A record from an SMTP log identifies itself by its own qp. A record
        from the queue log has no SMTP qp at all -- the two logs use different
        process ids -- so it is matched onto the session that fed it by sender
        and time, and only inserted on its own when no session is found, which
        is what locally generated mail looks like.
        """
        smtp_side = [m for m in messages if m.qp is not None]
        queue_side = [m for m in messages if m.qp is None]

        if smtp_side:
            payload = [
                (m.qp, _naive_utc(m.ts), m.direction, m.sender, m.sender_domain,
                 m.auth_user, m.auth_source, m.msg_id, m.size_bytes, m.client_ip,
                 m.spam_score, m.verdict, m.subject, m.queue_qp,
                 _naive_utc(m.queue_ts) if m.queue_ts else None)
                for m in smtp_side
            ]
            with self._live().cursor() as cur:
                cur.executemany(_SMTP_MESSAGE_SQL, payload)
            self.conn.commit()

        for m in queue_side:
            self._write_queue_message(m)

    def _write_queue_message(self, message):
        queue_ts = _naive_utc(message.queue_ts or message.ts)
        window = dt.timedelta(seconds=CORRELATION_WINDOW_SECONDS)

        with self._live().cursor() as cur:
            slack = dt.timedelta(milliseconds=1)
            cur.execute(_ALREADY_CORRELATED_SQL,
                        (message.queue_qp, queue_ts - slack, queue_ts + slack))
            if cur.fetchone():
                return  # this queue record has already been applied

            try:
                cur.execute(_CORRELATE_SQL, (
                    message.msg_id, message.size_bytes, message.queue_qp, queue_ts,
                    message.sender,
                    queue_ts - window, queue_ts + window,
                    queue_ts,
                ))
            except pymysql.err.IntegrityError as exc:
                # The queue key is already taken, so this record is already in
                # the table under another row. Treat it as applied: raising
                # here aborts the batch, the checkpoint never moves, and every
                # later run replays the same record and dies the same way.
                if exc.args[0] != 1062:
                    raise
                self.conn.rollback()
                return
            matched = cur.rowcount
        self.conn.commit()
        if matched:
            return

        # No SMTP session for it: locally generated mail, or a session whose
        # log has already rotated away.
        with self._live().cursor() as cur:
            cur.execute(_SMTP_MESSAGE_SQL, (
                None, _naive_utc(message.ts), message.direction, message.sender,
                message.sender_domain, message.auth_user, message.auth_source,
                message.msg_id, message.size_bytes, message.client_ip,
                message.spam_score, message.verdict, message.subject,
                message.queue_qp, queue_ts,
            ))
        self.conn.commit()

    def write_deliveries(self, deliveries):
        payload = [
            (d.msg_id, _naive_utc(d.msg_ts) if d.msg_ts else UNKNOWN_INSTANCE,
             _naive_utc(d.ts), d.recipient, d.recipient_domain,
             d.route, d.result, d.detail, d.remote_ip, d.remote_code,
             d.remote_status, d.reason)
            for d in deliveries
        ]
        if not payload:
            return
        with self._live().cursor() as cur:
            cur.executemany(_DELIVERY_SQL, payload)
        self.conn.commit()

    def write_queue_minutes(self, minutes):
        payload = [
            (m.replace(tzinfo=None), s["local_max_used"], s["local_limit"],
             s["remote_max_used"], s["remote_limit"])
            for m, s in minutes.items()
        ]
        if not payload:
            return
        with self._live().cursor() as cur:
            cur.executemany(_QUEUE_MINUTE_SQL, payload)
        self.conn.commit()

    def write_service_minutes(self, service, minutes):
        """Counters accumulate rather than replace, so this is NOT idempotent.

        The checkpoint keeps a file from being read twice in normal operation,
        and drain_minutes() hands each minute over exactly once. If checkpoints
        are ever cleared for a re-parse, these two tables must be cleared for
        the same period or connection counts will double.
        """
        payload = [
            (m.replace(tzinfo=None), service, s["connects"], s["messages"],
             s["max_concurrency"], s["limit"])
            for m, s in minutes.items()
        ]
        if not payload:
            return
        with self._live().cursor() as cur:
            cur.executemany(_SERVICE_MINUTE_SQL, payload)
        self.conn.commit()

    def write_auth_events(self, events):
        payload = [
            (_naive_utc(e["ts"]), e["service"], e["result"], e["user"],
             e["client_ip"], e["method"], 1 if e["tls"] else 0)
            for e in events
        ]
        if not payload:
            return
        with self._live().cursor() as cur:
            cur.executemany(_AUTH_EVENT_SQL, payload)
        self.conn.commit()

    def insert_bans(self, rows):
        """Store ban/unban actions, ignoring ones already held.

        The log is re-read whole on every run, so almost every row on the
        second pass is a duplicate; INSERT IGNORE against the unique key is
        what keeps that cheap.
        """
        if not rows:
            return 0
        with self._live().cursor() as cur:
            cur.executemany(_BAN_EVENT_SQL, rows)
            stored = cur.rowcount
        self.conn.commit()
        return stored or 0

    def infer_direction_from_routes(self):
        """Label queue-only messages by where their mail actually went.

        The queue log never sees which port a message arrived on, so a record
        with no SMTP session has no evidence of direction in itself. Its
        deliveries do: delivered only locally means it arrived here, only
        remotely means it left. Rows whose port was observed are left alone --
        that is direct evidence and beats any inference.
        """
        self.execute("""
            UPDATE message m
              JOIN (SELECT msg_id, msg_ts,
                           SUM(route = 'remote') AS remote_count,
                           SUM(route = 'local') AS local_count
                      FROM delivery GROUP BY msg_id, msg_ts) d
                ON d.msg_id = m.msg_id AND d.msg_ts = m.queue_ts
               SET m.direction = IF(d.remote_count = 0 AND d.local_count > 0,
                                    'in', 'out')
             WHERE m.qp IS NULL
        """)

    def get_checkpoint(self, logdir, filename):
        row = self.rows(
            "SELECT byte_offset, completed FROM checkpoint "
            "WHERE logdir=%s AND filename=%s",
            (logdir, filename),
        )
        if not row:
            return (0, False)
        return (int(row[0]["byte_offset"]), bool(row[0]["completed"]))

    def set_checkpoint(self, logdir, filename, byte_offset, completed):
        self.execute(
            "INSERT INTO checkpoint (logdir, filename, byte_offset, completed) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE byte_offset=VALUES(byte_offset), "
            "completed=VALUES(completed)",
            (logdir, filename, byte_offset, 1 if completed else 0),
        )

    def get_parser_state(self, logdir):
        row = self.rows(
            "SELECT payload FROM parser_state WHERE logdir=%s "
            # NOW() is right here, unlike elsewhere: updated_at is a TIMESTAMP,
            # which MySQL converts to the session timezone on both write and
            # read, so both sides of this comparison are in the same zone.
            "AND updated_at > NOW() - INTERVAL 1 DAY",
            (logdir,),
        )
        if not row:
            return {}
        payload = row[0]["payload"]
        return json.loads(payload) if isinstance(payload, str) else payload

    HEARTBEAT = "__heartbeat__"

    def beat(self, now=None):
        """Record that an ingest run finished.

        Written on every run, mail or no mail, so its age says whether the
        parser is alive -- which the age of the newest message cannot, since a
        quiet server and a stopped parser look identical from the mail alone.
        """
        at = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
        self.set_parser_state(self.HEARTBEAT, {"at": at.isoformat()})

    def last_beat(self):
        """When the parser last finished, as an aware UTC datetime, or None.

        Read without get_parser_state's one-day filter: a heartbeat a week old
        is exactly the case worth reporting, not one to ignore.
        """
        row = self.rows(
            "SELECT payload FROM parser_state WHERE logdir=%s", (self.HEARTBEAT,))
        if not row:
            return None
        payload = row[0]["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        try:
            at = dt.datetime.fromisoformat(payload["at"])
        except (KeyError, TypeError, ValueError):
            return None
        return at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)

    def set_parser_state(self, logdir, payload):
        self.execute(
            "INSERT INTO parser_state (logdir, payload) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE payload=VALUES(payload)",
            (logdir, json.dumps(payload, default=str)),
        )
