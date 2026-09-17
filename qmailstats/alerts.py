"""Conditions worth telling somebody about, evaluated against recent rows.

Each check answers a question an operator would otherwise have to remember to
ask: is the server generating bounces nobody can receive, is outbound mail
being refused, has the parser stopped, is somebody working through passwords.

Every window is measured against UTC_TIMESTAMP(), not NOW(). Rows are stored
in UTC while NOW() returns the server's local time, so on a server six hours
behind UTC every row looked six hours into the future -- which made the
stalled-parser alert unable to fire at all.

Thresholds are deliberately dull. An alert that fires on ordinary variation
gets muted, and a muted alert is worse than none: it teaches people the
dashboard cries wolf.
"""

import datetime as dt


DEFAULTS = {
    # A burst of bounces to qmail's nowhere address means the server is
    # generating undeliverable notices, usually because it is being forged.
    "discarded_bounces_per_hour": 200,
    # Outbound refusals, as a share of attempts. The floor matters as much as
    # the percentage: three failures out of four is noise on a quiet hour.
    "failure_rate_percent": 25,
    "failure_rate_minimum_attempts": 20,
    # The dashboard showing stale figures looks exactly like a quiet server,
    # which is the most dangerous failure this tool has. Measured from the
    # parser's own heartbeat, and the timer fires every 30 seconds, so ten
    # minutes is twenty missed runs.
    "stale_minutes": 10,
    # Failed logins from anywhere, per hour.
    "auth_failures_per_hour": 150,
    # Queue concurrency sitting at its configured ceiling means mail is
    # queueing behind the limit rather than the network.
    "concurrency_percent": 90,
    # This tool's own carried parser state. It should be a few kilobytes; it
    # reached 15 MB once through a leak, and because it is rewritten every
    # 30 seconds that turned into gigabytes of binary log an hour.
    "parser_state_kb": 1024,
    # A full disk stops MySQL, and vpopmail authenticates against MySQL, so
    # this is a mail outage waiting to happen rather than a housekeeping note.
    "disk_percent": 85,
}


def parser_staleness(store, limit_minutes, now=None):
    """An alert if the parser has stopped, else None.

    The heartbeat is the evidence: ingest writes it at the end of every run
    whether or not any mail arrived. The age of the newest message was used
    before, and it cannot tell a stopped parser from an hour with no mail --
    it fired on a quiet evening while the parser was running every 30 seconds.

    Installs that have not written a heartbeat yet fall back to message age,
    with the old one-hour limit, because that is still better than nothing.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    beat = store.last_beat()
    if beat is not None:
        age = int((now - beat).total_seconds() // 60)
        if age < limit_minutes:
            return None
        return {
            "id": "parser_stalled",
            "severity": "serious",
            "title": "These figures are not current",
            "detail": ("The log parser last finished %d minutes ago and runs "
                       "every 30 seconds, so it has stopped. Check "
                       "systemctl status qmail-grafical-stats-ingest." % age),
        }

    newest = store.scalar("SELECT MAX(ts) FROM message")
    if newest is None:
        return None
    age = store.scalar(
        "SELECT TIMESTAMPDIFF(MINUTE, MAX(ts), UTC_TIMESTAMP()) FROM message") or 0
    if age < 60:
        return None
    return {
        "id": "parser_stalled",
        "severity": "serious",
        "title": "These figures may not be current",
        "detail": ("The newest message read is %d minutes old and this parser "
                   "has not recorded a heartbeat yet, so a quiet server and a "
                   "stopped parser cannot be told apart." % age),
    }


def _setting(config, name):
    try:
        return type(DEFAULTS[name])(config.get(name, DEFAULTS[name]))
    except (TypeError, ValueError):
        return DEFAULTS[name]


def oversized_state(store, limit_kb):
    """An alert if any carried parser state has grown past *limit_kb*."""
    # No ORDER BY: sorting by payload length over a bloated table is exactly
    # what runs out of sort memory, which is the case this has to report.
    rows = store.rows(
        "SELECT logdir, LENGTH(payload) AS size FROM parser_state", ())
    big = [(r["logdir"], int(r["size"] or 0)) for r in rows
           if int(r["size"] or 0) > limit_kb * 1024]
    if not big:
        return None
    big.sort(key=lambda item: -item[1])
    listed = ", ".join("%s %.1f MB" % (name, size / 1048576.0) for name, size in big)
    return {
        "id": "parser_state_large",
        "severity": "serious",
        "title": "The parser is carrying too much state",
        "detail": ("Saved state should be a few kilobytes but is %s. It is "
                   "rewritten every run, so this fills the binary log and "
                   "churns MySQL memory." % listed),
    }


def disk_pressure(limit_percent, paths=("/",), usage=None):
    """An alert for each filesystem above *limit_percent* full."""
    import shutil

    usage = usage or shutil.disk_usage
    raised = []
    seen = set()
    for path in paths:
        try:
            total, used, free = usage(path)
        except OSError:
            continue
        if not total or (total, free) in seen:
            continue  # two paths on the same filesystem report once
        seen.add((total, free))
        percent = 100.0 * used / total
        if percent >= limit_percent:
            raised.append({
                "id": "disk_full:%s" % path,
                "severity": "critical" if percent >= 95 else "serious",
                "title": "Disk %s is %.0f%% full" % (path, percent),
                "detail": ("%.1f GB free. MySQL stops when the disk fills, and "
                           "vpopmail authenticates against it." % (free / 1e9)),
            })
    return raised


def evaluate(store, config=None):
    """Return the alerts that currently hold, worst first."""
    config = config or {}
    raised = []

    bounce_limit = _setting(config, "discarded_bounces_per_hour")
    bounces = store.scalar(
        "SELECT COUNT(*) FROM delivery "
        "WHERE recipient = '#@[]' AND ts > UTC_TIMESTAMP() - INTERVAL 1 HOUR"
    ) or 0
    if bounces >= bounce_limit:
        raised.append({
            "id": "bounce_storm",
            "severity": "critical",
            "title": "Bounces nobody can receive",
            "detail": ("%d undeliverable bounces discarded in the last hour. "
                       "Something is generating mail this server cannot return, "
                       "usually a forged sender." % bounces),
        })

    minimum = _setting(config, "failure_rate_minimum_attempts")
    rate_limit = _setting(config, "failure_rate_percent")
    row = store.rows(
        "SELECT COUNT(*) AS attempts, SUM(result = 'failure') AS failures "
        "FROM delivery WHERE route = 'remote' AND recipient <> '#@[]' "
        "  AND ts > UTC_TIMESTAMP() - INTERVAL 1 HOUR"
    )[0]
    attempts = int(row["attempts"] or 0)
    failures = int(row["failures"] or 0)
    if attempts >= minimum and 100.0 * failures / attempts >= rate_limit:
        raised.append({
            "id": "delivery_failing",
            "severity": "critical",
            "title": "Outbound mail is being refused",
            "detail": ("%d of %d delivery attempts failed in the last hour "
                       "(%.0f%%)." % (failures, attempts,
                                      100.0 * failures / attempts)),
        })

    stale = parser_staleness(store, _setting(config, "stale_minutes"))
    if stale:
        raised.append(stale)

    auth_limit = _setting(config, "auth_failures_per_hour")
    failed_logins = store.scalar(
        "SELECT COUNT(*) FROM auth_event "
        "WHERE result = 'auth_failed' AND ts > UTC_TIMESTAMP() - INTERVAL 1 HOUR"
    ) or 0
    if failed_logins >= auth_limit:
        worst = store.rows(
            "SELECT client_ip, COUNT(*) AS n FROM auth_event "
            "WHERE result = 'auth_failed' AND ts > UTC_TIMESTAMP() - INTERVAL 1 HOUR "
            "GROUP BY client_ip ORDER BY n DESC LIMIT 1")
        where = (" Worst single address: %s with %d." %
                 (worst[0]["client_ip"], int(worst[0]["n"]))) if worst else ""
        raised.append({
            "id": "auth_guessing",
            "severity": "serious",
            "title": "Somebody is guessing passwords",
            "detail": "%d failed logins in the last hour.%s" % (failed_logins, where),
        })

    concurrency_limit = _setting(config, "concurrency_percent")
    queue = store.rows(
        "SELECT MAX(remote_max_used) AS used, MAX(remote_limit) AS ceiling "
        "FROM queue_minute WHERE minute > UTC_TIMESTAMP() - INTERVAL 1 HOUR")
    if queue and queue[0]["ceiling"]:
        used = int(queue[0]["used"] or 0)
        ceiling = int(queue[0]["ceiling"])
        if ceiling and 100.0 * used / ceiling >= concurrency_limit:
            raised.append({
                "id": "queue_saturated",
                "severity": "warning",
                "title": "The queue is running at its limit",
                "detail": ("Remote deliveries reached %d of a limit of %d. "
                           "Mail is waiting on the limit rather than on the "
                           "network." % (used, ceiling)),
            })

    big = oversized_state(store, _setting(config, "parser_state_kb"))
    if big:
        raised.append(big)

    paths = [p.strip() for p in str(
        (config or {}).get("disk_paths", "/")).split(",") if p.strip()]
    raised.extend(disk_pressure(_setting(config, "disk_percent"), paths))

    order = {"critical": 0, "serious": 1, "warning": 2}
    raised.sort(key=lambda a: order.get(a["severity"], 9))
    return raised
