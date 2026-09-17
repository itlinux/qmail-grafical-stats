"""Parse qmail logs into MySQL. Backfill and incremental are the same path."""

import argparse
import configparser
import fcntl
import pathlib
import sys
import time

from qmailstats import bans, dovecot, logsrc
from qmailstats.assemble import SendAssembler, SmtpAssembler
from qmailstats.db import Store

# role -> (assembler class, default direction when no connect line was seen)
ROLES = {
    "smtp": (SmtpAssembler, "in"),
    "submission": (SmtpAssembler, "out"),
    "smtps": (SmtpAssembler, "out"),
    "send": (SendAssembler, None),
}
LOGDIRS = ROLES  # kept for callers that predate the rename

DEFAULT_LOGDIRS = [
    ("smtp", "smtp"),
    ("submission", "submission"),
    ("smtps", "smtps"),
    ("send", "send"),
]

# SMTP logs first: they insert the message rows the queue log then annotates.
ORDER = ["smtp", "submission", "smtps", "send"]


def logdirs_from_config(config):
    """Read 'logdirs' as 'directory:role, directory:role'.

    Directory names vary between installs -- qmail-send and qmail-smtpd are
    just as common as send and smtp -- while the roles do not. The queue log is
    always sorted last, whatever order it was given in, because the SMTP logs
    must insert their message rows before it annotates them by qp.
    """
    raw = config.get("parser", "logdirs", fallback="").strip()
    if not raw:
        return list(DEFAULT_LOGDIRS)
    pairs = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        directory, _, role = item.partition(":")
        role = (role or directory).strip()
        if role not in ROLES:
            raise ValueError(
                "unknown log role %r; expected one of %s"
                % (role, ", ".join(sorted(ROLES)))
            )
        pairs.append((directory.strip(), role))
    pairs.sort(key=lambda pair: pair[1] == "send")
    return pairs


def load_config(path):
    # interpolation=None: a URL-encoded password contains %XX escapes,
    # and ConfigParser would otherwise read them as interpolation and
    # refuse the file. Any password with @ / # % or : would be unusable.
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    return parser


def build(role, config, saved_state, service=None):
    cls, default_direction = ROLES[role]
    if cls is SendAssembler:
        return SendAssembler.restore(saved_state)
    return SmtpAssembler.restore(
        saved_state,
        store_subject=config.getboolean("parser", "store_subject", fallback=False),
        default_direction=default_direction,
        service=service or role,
        fallback_auth=config.getboolean("parser", "fallback_auth", fallback=False),
    )


def ingest_dir(store, root, logdir, config, batch_size, pause, role=None):
    """Read one log directory. `logdir` names the folder, `role` its shape."""
    role = role or logdir
    directory = pathlib.Path(root) / logdir
    if not directory.is_dir():
        return {"skipped": True}

    assembler = build(role, config, store.get_parser_state(logdir),
                      service=logdir)
    messages, deliveries = [], []
    counts = {"messages": 0, "deliveries": 0, "files": 0}

    def flush():
        if messages:
            store.write_messages(messages)
            counts["messages"] += len(messages)
            del messages[:]
        if deliveries:
            store.write_deliveries(deliveries)
            counts["deliveries"] += len(deliveries)
            del deliveries[:]
        if pause:
            time.sleep(pause)

    paths = logsrc.files_in_order(directory)
    for index, path in enumerate(paths):
        offset, completed = store.get_checkpoint(logdir, path.name)
        is_current = path.name == "current"
        if completed and not is_current:
            continue

        lines, new_offset = logsrc.read_from(path, offset)
        for line in lines:
            for record in assembler.feed(line):
                if record.__class__.__name__ == "Message":
                    messages.append(record)
                else:
                    deliveries.append(record)
            if len(messages) + len(deliveries) >= batch_size:
                flush()

        flush()
        # A rotated file is finished only once a newer file exists after it.
        store.set_checkpoint(
            logdir, path.name, new_offset,
            completed=not is_current and index < len(paths) - 1,
        )
        counts["files"] += 1

    if role == "send":
        store.write_queue_minutes(assembler.drain_minutes())
    else:
        store.write_service_minutes(logdir, assembler.drain_minutes())

    if role == "send":
        # Now that this run's deliveries are written, the queue-only messages
        # can be labelled by where their mail went.
        store.infer_direction_from_routes()

    store.set_parser_state(logdir, assembler.state())
    counts["unmatched"] = getattr(assembler, "unmatched", 0)
    counts["malformed"] = getattr(assembler, "malformed", 0)
    return counts


def ingest_dovecot(store, live_path, tz_name=None, year=None, batch_size=500,
                   pause=0.0):
    """Read Dovecot's syslog file and its rotations into auth_event.

    Checkpointed the same way as the qmail logs, under the log file's own name
    so the two sources cannot collide.
    """
    live_path = pathlib.Path(live_path)
    if not live_path.exists() and not list(live_path.parent.glob(live_path.name + "-*")):
        return {"skipped": True}

    logkey = "dovecot:" + live_path.name
    # "ignored" is a line this parser understands and has no use for -- a
    # session ending, say. "malformed" is one it could not read at all, and
    # only that number means something is wrong.
    counts = {"events": 0, "files": 0, "ignored": 0, "malformed": 0}
    events = []
    minutes = {}

    def flush():
        if events:
            store.write_auth_events(events)
            counts["events"] += len(events)
            del events[:]
        if pause:
            time.sleep(pause)

    paths = logsrc.syslog_files_in_order(live_path)
    for index, path in enumerate(paths):
        offset, completed = store.get_checkpoint(logkey, path.name)
        is_live = path == live_path
        if completed and not is_live:
            continue

        lines, new_offset = logsrc.read_from(path, offset)
        file_year = year or dovecot.year_for(path)
        for line in lines:
            event = dovecot.parse_line(line, year=file_year, tz_name=tz_name)
            if event is None:
                if dovecot.readable(line):
                    counts["ignored"] += 1
                else:
                    counts["malformed"] += 1
                continue
            events.append(event)
            minute = event["ts"].replace(second=0, microsecond=0)
            sample = minutes.setdefault(
                (event["service"], minute),
                {"connects": 0, "messages": 0, "max_concurrency": 0, "limit": 0},
            )
            sample["connects"] += 1
            if event["result"] == "login":
                # A successful login is this service's equivalent of a session
                # that did something, which is what "messages" counts for SMTP.
                sample["messages"] += 1
            if len(events) >= batch_size:
                flush()

        flush()
        store.set_checkpoint(
            logkey, path.name, new_offset,
            completed=not is_live and index < len(paths) - 1,
        )
        counts["files"] += 1

    by_service = {}
    for (service, minute), sample in minutes.items():
        by_service.setdefault(service, {})[minute] = sample
    for service, samples in by_service.items():
        store.write_service_minutes(service, samples)

    return counts


_UNWRITABLE = (
    "cannot create the ingest lock at %s: its directory is not writable by "
    "this user. /var/lock is root-owned tmpfs, so the unit needs "
    "RuntimeDirectory=qmail-grafical-stats and a lockfile under "
    "/run/qmail-grafical-stats/."
)


def ingest_bans(store, path, batch_size=500):
    """Read fail2ban's log into ban_event.

    The file is small and rotation truncates it, so it is read whole every run
    rather than checkpointed; the unique key turns the repeats into no-ops.
    """
    counts = {"read": 0, "stored": 0}
    try:
        text = pathlib.Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return counts
    except PermissionError:
        # Worth saying out loud: the log is 0600 root by default, and a silent
        # zero here looks exactly like "nothing was banned".
        print("cannot read %s; ban history will be empty" % path,
              file=sys.stderr)
        return counts

    batch = []
    for event in bans.events(text.split("\n")):
        counts["read"] += 1
        batch.append((event.ts, event.jail, event.ip, event.action))
        if len(batch) >= batch_size:
            counts["stored"] += store.insert_bans(batch)
            batch = []
    if batch:
        counts["stored"] += store.insert_bans(batch)
    return counts


def take_lock(path):
    """Hold the ingest lock on *path*, or return None if a run already has it.

    Nothing is ever written to the file; only the flock matters. A backfill run
    by hand as root leaves the file behind owned by root, and the timer runs as
    qmaill, so insisting on write access would stop ingestion until someone
    noticed. Linux grants LOCK_EX on a read-only descriptor, so fall back to
    opening it for reading.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise PermissionError(_UNWRITABLE % path) from None
    try:
        handle = path.open("a")
    except PermissionError:
        # A run by hand as root leaves the file owned by root; the timer runs
        # as qmaill and only needs to flock it, which a read-only descriptor
        # allows. If it is not there at all we cannot create one either, and
        # the lock directory is the thing that needs fixing.
        try:
            handle = path.open("r")
        except FileNotFoundError:
            raise PermissionError(_UNWRITABLE % path) from None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ingest qmail logs into MySQL.")
    ap.add_argument("--config", default="/etc/qmail-grafical-stats/config.ini")
    ap.add_argument("--logroot", default=None, help="override log root for testing")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument(
        "--pause", type=float, default=0.0,
        help="seconds to sleep between batches; use 0.2 for the first backfill",
    )
    args = ap.parse_args(argv)

    config = load_config(args.config)
    lockfile = pathlib.Path(
        config.get("parser", "lockfile",
                   fallback="/run/qmail-grafical-stats/ingest.lock")
    )
    handle = take_lock(lockfile)
    if handle is None:
        print("another ingest run holds the lock; exiting", file=sys.stderr)
        return 0

    root = pathlib.Path(args.logroot or config.get("parser", "logroot"))
    store = Store.from_dsn(config.get("database", "dsn"))
    try:
        for logdir, role in logdirs_from_config(config):
            counts = ingest_dir(store, root, logdir, config, args.batch_size,
                                args.pause, role=role)
            # flush: a backfill takes an hour and its output is usually
            # redirected to a file, where Python's buffering would otherwise
            # show nothing at all until the run ended.
            print("%s: %s" % (logdir, counts), flush=True)

        dovecot_log = config.get("dovecot", "logfile", fallback="").strip()
        if dovecot_log:
            counts = ingest_dovecot(
                store, dovecot_log,
                tz_name=config.get("dovecot", "timezone", fallback=None),
                batch_size=args.batch_size, pause=args.pause,
            )
            print("dovecot: %s" % (counts,), flush=True)

        ban_log = config.get("fail2ban", "logfile", fallback="").strip()
        if ban_log:
            counts = ingest_bans(store, ban_log, batch_size=args.batch_size)
            print("fail2ban: %s" % (counts,), flush=True)

        # Only after everything above succeeded: a run that died part way must
        # not look like a healthy one.
        store.beat()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
