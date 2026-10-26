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
    # A login that follows repeated failures against the SAME account from the
    # SAME address is a password that was guessed successfully. Five, because
    # the alternatives were measured over seventy days of real logins: a
    # threshold of three raised two alerts, both a user with a stale password
    # in a client, and ten raised none at all.
    "cracked_account_failures": 5,
    # Only logins this recent are examined, which keeps the query bounded; the
    # unbounded form took over two minutes against 159,000 rows and this runs
    # every five. An hour is twelve overlapping passes, and repeats are
    # suppressed by the caller's change detection.
    "cracked_account_window_minutes": 60,
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


def cracked_account(store, limit_failures, window_minutes, trusted=()):
    """An alert if somebody guessed a password and got in, else None.

    Correlating on the address alone does not work here. Several of these
    accounts share one office NAT, and a mail server sees its own webmail as
    127.0.0.1, so one person mistyping a password put every colleague's
    successful login next to a pile of failures. Requiring the failures to be
    aimed at the same account that then succeeded removes both cases, and a
    shared mobile carrier -- these users roam across hundreds of Vodafone
    addresses -- stops mattering at all.

    *trusted* exists for addresses that are shared egress rather than a user:
    the old gateway everybody NATs through, and localhost for webmail.
    """
    placeholders = ",".join(["%s"] * len(trusted)) if trusted else None
    exclusion = (" AND l.client_ip NOT IN (%s)" % placeholders) if trusted else ""
    rows = store.rows(
        "SELECT l.user AS user, l.client_ip AS client_ip, "
        "       COUNT(DISTINCT f.id) AS failures, MAX(l.ts) AS seen "
        "FROM auth_event l "
        "JOIN auth_event f ON f.user = l.user AND f.client_ip = l.client_ip "
        "  AND f.result = 'auth_failed' "
        "  AND f.ts < l.ts AND f.ts > l.ts - INTERVAL 7 DAY "
        "WHERE l.result = 'login' "
        "  AND l.ts > UTC_TIMESTAMP() - INTERVAL %%s MINUTE%s "
        "GROUP BY l.user, l.client_ip "
        "HAVING failures >= %%s "
        "ORDER BY failures DESC" % exclusion,
        (int(window_minutes),) + tuple(trusted) + (int(limit_failures),))
    if not rows:
        return None
    worst = rows[0]
    listed = ", ".join("%s from %s (%d failures)" %
                       (r["user"], r["client_ip"], int(r["failures"]))
                       for r in rows[:4])
    return {
        "id": "account_cracked",
        "severity": "critical",
        "title": "A password was guessed successfully",
        "detail": ("%s logged in from %s after %d failed attempts against that "
                   "same account from that same address. Change the password "
                   "now; the account is in somebody else's hands until you do."
                   "%s" % (worst["user"], worst["client_ip"],
                           int(worst["failures"]),
                           ("\nAll: " + listed) if len(rows) > 1 else "")),
    }


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

    trusted = tuple(ip.strip() for ip in str(
        (config or {}).get("trusted_ips", "127.0.0.1,::1")).split(",")
        if ip.strip())
    cracked = cracked_account(store,
                              _setting(config, "cracked_account_failures"),
                              _setting(config, "cracked_account_window_minutes"),
                              trusted)
    if cracked:
        raised.append(cracked)

    paths = [p.strip() for p in str(
        (config or {}).get("disk_paths", "/")).split(",") if p.strip()]
    raised.extend(disk_pressure(_setting(config, "disk_percent"), paths))

    order = {"critical": 0, "serious": 1, "warning": 2}
    raised.sort(key=lambda a: order.get(a["severity"], 9))
    return raised
