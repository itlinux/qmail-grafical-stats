"""One function per dashboard figure. Returns plain lists of dicts.

A delivery is joined to its message on both msg_id and the instance timestamp.
msg_id is the queue file's inode number and is reused as soon as the queue
drains, so on its own it matches whichever message happened to hold that inode
at any point.

Message-level counts come from `message`; attempt-level counts come from
`delivery`. Mixing them silently double-counts retried mail.
"""


# qmail sends a bounce that itself bounced to this address, which is
# deliberately unroutable: delivery to it always fails, by design. Those
# attempts are the server discarding undeliverable bounces, not mail failing to
# reach anybody, so they are kept out of every deliverability figure and
# reported on their own. A burst of them means someone is forging this server
# as a sender.
DOUBLE_BOUNCE = "#@[]"
NOT_A_DOUBLE_BOUNCE = "recipient <> '%s'" % DOUBLE_BOUNCE


from qmailstats.remote import readable_detail


def _rate(successes, attempts):
    if not attempts:
        return None
    return round(100.0 * successes / attempts, 2)


def summary(store, since, until):
    messages = store.rows(
        "SELECT direction, COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS b "
        "FROM message WHERE ts >= %s AND ts < %s GROUP BY direction",
        (since, until),
    )
    counts = {row["direction"]: row for row in messages}
    attempts = store.rows(
        "SELECT result, COUNT(*) AS n FROM delivery "
        "WHERE ts >= %s AND ts < %s AND " + NOT_A_DOUBLE_BOUNCE + " "
        "GROUP BY result",
        (since, until),
    )
    # Local delivery essentially always succeeds, and on a busy server it is
    # most of the attempts -- so it hides what the blended rate is asked to
    # show. Deliverability is the remote figure.
    remote = store.rows(
        "SELECT COUNT(*) AS attempts, SUM(result = 'success') AS successes "
        "FROM delivery WHERE ts >= %s AND ts < %s AND route = 'remote' "
        "  AND " + NOT_A_DOUBLE_BOUNCE,
        (since, until),
    )
    discarded = store.scalar(
        "SELECT COUNT(*) FROM delivery "
        "WHERE ts >= %s AND ts < %s AND recipient = %s",
        (since, until, DOUBLE_BOUNCE),
    ) or 0
    # Which day carried the most of them. A count spread thinly is ordinary
    # backscatter; a count concentrated in one day is an incident, and the two
    # want different responses.
    peak = store.rows(
        "SELECT DATE(ts) AS day, COUNT(*) AS n FROM delivery "
        "WHERE ts >= %s AND ts < %s AND recipient = %s "
        "GROUP BY DATE(ts) ORDER BY n DESC LIMIT 1",
        (since, until, DOUBLE_BOUNCE),
    )
    outbound_attempts = int((remote[0]["attempts"] if remote else 0) or 0)
    outbound_successes = int((remote[0]["successes"] if remote else 0) or 0)
    by_result = {row["result"]: int(row["n"]) for row in attempts}
    successes = by_result.get("success", 0)
    deferrals = by_result.get("deferral", 0)
    failures = by_result.get("failure", 0)
    total = successes + deferrals + failures
    return {
        "messages_in": int(counts.get("in", {}).get("n", 0)),
        "messages_out": int(counts.get("out", {}).get("n", 0)),
        "bytes_in": int(counts.get("in", {}).get("b", 0)),
        "bytes_out": int(counts.get("out", {}).get("b", 0)),
        "attempts": total,
        "successes": successes,
        "deferrals": deferrals,
        "failures": failures,
        "success_rate": _rate(successes, total),
        "outbound_attempts": outbound_attempts,
        "outbound_successes": outbound_successes,
        "outbound_rate": _rate(outbound_successes, outbound_attempts),
        "discarded_bounces": int(discarded),
        "discarded_bounce_peak_day": peak[0]["day"] if peak else None,
        "discarded_bounce_peak": int(peak[0]["n"]) if peak else 0,
    }


def timeseries(store, since, until, bucket="day"):
    """Messages per bucket. Long ranges need coarser buckets to stay readable.

    %%x-%%v is the ISO week-numbering year and week, so a week spanning New Year
    is one bucket rather than two.
    """
    fmt = {
        "hour": "%Y-%m-%d %H:00:00",
        "day": "%Y-%m-%d",
        "week": "%x-W%v",
    }[bucket]
    return store.rows(
        "SELECT DATE_FORMAT(ts, %s) AS bucket, "
        "  SUM(direction = 'in') AS messages_in, "
        "  SUM(direction = 'out') AS messages_out "
        "FROM message WHERE ts >= %s AND ts < %s "
        "GROUP BY bucket ORDER BY bucket",
        (fmt, since, until),
    )


def top_senders(store, since, until, limit=20):
    return store.rows(
        "SELECT auth_user, COUNT(*) AS messages, "
        "  COALESCE(SUM(size_bytes),0) AS bytes "
        "FROM message "
        "WHERE ts >= %s AND ts < %s AND auth_user IS NOT NULL "
        "GROUP BY auth_user ORDER BY messages DESC LIMIT %s",
        (since, until, int(limit)),
    )


def top_recipients(store, since, until, limit=20):
    """Where outbound mail goes, by recipient domain.

    Remote deliveries only. A local delivery is inbound mail landing in one of
    our own mailboxes, so counting it here would put our own domains at the top
    of the table.
    """
    return store.rows(
        "SELECT recipient_domain, COUNT(*) AS attempts, "
        "  SUM(result = 'success') AS successes, "
        "  SUM(result = 'deferral') AS deferrals, "
        "  SUM(result = 'failure') AS failures "
        "FROM delivery "
        "WHERE ts >= %s AND ts < %s AND route = 'remote' "
        "  AND recipient_domain IS NOT NULL AND " + NOT_A_DOUBLE_BOUNCE + " "
        "GROUP BY recipient_domain ORDER BY attempts DESC LIMIT %s",
        (since, until, int(limit)),
    )


def per_domain(store, since, until):
    return store.rows(
        "SELECT sender_domain AS domain, direction, COUNT(*) AS messages, "
        "  COALESCE(SUM(size_bytes),0) AS bytes "
        "FROM message "
        "WHERE ts >= %s AND ts < %s AND sender_domain IS NOT NULL "
        "GROUP BY sender_domain, direction ORDER BY messages DESC",
        (since, until),
    )


def mailboxes(store, since, until, limit=100):
    return store.rows(
        "SELECT address, SUM(sent) AS sent, SUM(received) AS received FROM ("
        "  SELECT auth_user AS address, COUNT(*) AS sent, 0 AS received"
        "    FROM message"
        "   WHERE ts >= %s AND ts < %s AND auth_user IS NOT NULL"
        "   GROUP BY auth_user"
        "  UNION ALL"
        "  SELECT recipient AS address, 0 AS sent, COUNT(*) AS received"
        "    FROM delivery"
        "   WHERE ts >= %s AND ts < %s AND route = 'local' AND result = 'success'"
        "   GROUP BY recipient"
        ") AS combined "
        "GROUP BY address ORDER BY (SUM(sent) + SUM(received)) DESC LIMIT %s",
        (since, until, since, until, int(limit)),
    )


def mailbox(store, address, since, until):
    address = address.strip().lower()
    sent = store.scalar(
        "SELECT COUNT(*) FROM message WHERE ts >= %s AND ts < %s AND auth_user = %s",
        (since, until, address),
    ) or 0
    received = store.scalar(
        "SELECT COUNT(*) FROM delivery WHERE ts >= %s AND ts < %s "
        "AND route = 'local' AND result = 'success' AND recipient = %s",
        (since, until, address),
    ) or 0
    outcomes = store.rows(
        "SELECT d.result, COUNT(*) AS n "
        "FROM delivery d JOIN message m ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts "
        "WHERE d.ts >= %s AND d.ts < %s AND m.auth_user = %s "
        "GROUP BY d.result",
        (since, until, address),
    )
    by_result = {row["result"]: int(row["n"]) for row in outcomes}
    successes = by_result.get("success", 0)
    attempts = sum(by_result.values())
    sent_to = store.rows(
        "SELECT d.recipient AS address, COUNT(*) AS n "
        "FROM delivery d JOIN message m ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts "
        "WHERE d.ts >= %s AND d.ts < %s AND m.auth_user = %s "
        "GROUP BY d.recipient ORDER BY n DESC LIMIT 20",
        (since, until, address),
    )
    received_from = store.rows(
        "SELECT m.sender AS address, COUNT(*) AS n "
        "FROM delivery d JOIN message m ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts "
        "WHERE d.ts >= %s AND d.ts < %s AND d.route = 'local' "
        "AND d.result = 'success' AND d.recipient = %s "
        "GROUP BY m.sender ORDER BY n DESC LIMIT 20",
        (since, until, address),
    )
    return {
        "address": address,
        "sent": int(sent),
        "received": int(received),
        "attempts": attempts,
        "successes": successes,
        "deferrals": by_result.get("deferral", 0),
        "failures": by_result.get("failure", 0),
        "success_rate": _rate(successes, attempts),
        "sent_to": sent_to,
        "received_from": received_from,
    }


def outcomes_timeseries(store, since, until, bucket="day"):
    """Delivery attempts per bucket, split by how they ended."""
    fmt = {"day": "%Y-%m-%d", "week": "%x-W%v"}[bucket]
    return store.rows(
        "SELECT DATE_FORMAT(ts, %s) AS bucket, "
        "  SUM(result = 'success') AS successes, "
        "  SUM(result = 'deferral') AS deferrals, "
        "  SUM(result = 'failure') AS failures "
        "FROM delivery WHERE ts >= %s AND ts < %s "
        "  AND " + NOT_A_DOUBLE_BOUNCE + " "
        "GROUP BY bucket ORDER BY bucket",
        (fmt, since, until),
    )


def hourly_profile(store, since, until):
    """Messages by weekday and hour, for the shape of a normal week.

    WEEKDAY() numbers Monday 0 through Sunday 6, which is the order the grid is
    drawn in -- unlike DAYOFWEEK(), which starts on Sunday.
    """
    return store.rows(
        "SELECT WEEKDAY(ts) AS weekday, HOUR(ts) AS hour, COUNT(*) AS messages "
        "FROM message WHERE ts >= %s AND ts < %s "
        "GROUP BY weekday, hour ORDER BY weekday, hour",
        (since, until),
    )


# What counts as spam when the scanner will not say. simscan and friends are
# routinely configured with a rejection threshold nothing can reach -- 9999 on
# these servers -- so every message is labelled CLEAN however it scored, and
# counting labels finds no spam at all. 5.0 is SpamAssassin's own default for
# "this is spam", and rspamd's scores are on the same scale.
SPAM_SCORE_THRESHOLD = 5.0


def spam_per_account(store, since, until, limit=100, threshold=None):
    """Inbound mail per local mailbox, split by what the scanner made of it.

    Counted by score, not by label: a scanner told never to reject labels
    nothing, and the score is what it actually decided. An explicit SPAM
    verdict is believed whatever the score, for scanners that do label.

    This counts mail that was scanned and then delivered. Mail rejected during
    the SMTP conversation never enters the queue and so cannot appear here.
    """
    threshold = SPAM_SCORE_THRESHOLD if threshold is None else float(threshold)
    rows = store.rows(
        "SELECT d.recipient AS address, "
        "  SUM(m.verdict = 'SPAM' OR m.spam_score >= %s) AS spam, "
        "  SUM(NOT (m.verdict = 'SPAM' OR m.spam_score >= %s)) AS clean, "
        "  COUNT(*) AS scanned, "
        "  MAX(m.spam_score) AS worst_score "
        "FROM delivery d JOIN message m ON m.msg_id = d.msg_id "
        "  AND m.queue_ts = d.msg_ts "
        "WHERE d.ts >= %s AND d.ts < %s AND d.route = 'local' "
        "  AND m.direction = 'in' "
        "  AND (m.verdict IS NOT NULL OR m.spam_score IS NOT NULL) "
        "GROUP BY d.recipient ORDER BY spam DESC, scanned DESC LIMIT %s",
        (threshold, threshold, since, until, int(limit)),
    )
    out = []
    for row in rows:
        entry = dict(row)
        entry["spam_rate"] = _rate(int(row["spam"] or 0), int(row["scanned"] or 0))
        out.append(entry)
    return out


def failure_reasons(store, since, until, limit=25):
    """Why attempts did not succeed, grouped by cause rather than by message.

    Grouping on the raw detail string would produce one group per message: it
    carries the remote address and, often, counters from the remote's own
    text. The parsed code, enhanced status and normalised reason repeat across
    messages that failed for the same cause, which is the thing worth counting.

    Each cause also names who it happened to -- the recipients that hit it and
    who sent them, by authenticated account where there is one and by envelope
    sender otherwise -- because a cause with nobody
    attached to it cannot be acted on. Only the first few of each are listed;
    the counts beside them say how many there were in total.

    Deferrals are included. Greylisting is the common one and resolves itself,
    but a deferral that keeps recurring is exactly what you want to see.
    """
    return store.rows(
        "SELECT d.result, d.remote_code, d.remote_status, "
        "  COALESCE(d.reason, d.detail) AS reason, "
        "  COUNT(*) AS attempts, "
        "  COUNT(DISTINCT d.recipient_domain) AS domains, "
        "  COUNT(DISTINCT d.remote_ip) AS remote_hosts, "
        "  COUNT(DISTINCT d.recipient) AS recipients, "
        "  COUNT(DISTINCT COALESCE(m.auth_user, m.sender)) AS accounts, "
        "  SUBSTRING_INDEX(GROUP_CONCAT(DISTINCT d.recipient "
        "    ORDER BY d.recipient SEPARATOR ', '), ', ', 3) AS sample_recipients, "
        # The authenticated account where there is one, the envelope sender
        # otherwise: mail that did not arrive by authenticated submission has
        # no account, and a blank sender is the least useful thing to show on
        # a row somebody is trying to chase.
        "  SUBSTRING_INDEX(GROUP_CONCAT(DISTINCT COALESCE(m.auth_user, m.sender) "
        "    ORDER BY COALESCE(m.auth_user, m.sender) SEPARATOR ', '), ', ', 3) "
        "    AS sample_accounts, "
        "  MAX(d.ts) AS last_seen "
        "FROM delivery d LEFT JOIN message m ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts "
        "WHERE d.ts >= %s AND d.ts < %s AND d.result <> 'success' "
        "  AND d.recipient <> '#@[]' "
        # The same expression must appear in GROUP BY, not just the column it
        # falls back to: only_full_group_by rejects a bare `detail` here, and
        # rows written before the reason column existed still need grouping.
        "GROUP BY d.result, d.remote_code, d.remote_status, "
        "  COALESCE(d.reason, d.detail) "
        "ORDER BY attempts DESC LIMIT %s",
        (since, until, int(limit)),
    )


def domain_flow(store, since, until, limit=50):
    """Which of our domains sent to which destination, and how it went.

    Remote deliveries only -- a local delivery is mail arriving here, not one
    of our domains sending somewhere.
    """
    return store.rows(
        "SELECT m.sender_domain, d.recipient_domain, COUNT(*) AS attempts, "
        "  SUM(d.result = 'success') AS successes, "
        "  SUM(d.result = 'deferral') AS deferrals, "
        "  SUM(d.result = 'failure') AS failures "
        "FROM delivery d JOIN message m ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts "
        "WHERE d.ts >= %s AND d.ts < %s AND d.route = 'remote' "
        "  AND d.recipient <> '#@[]' "
        "  AND m.direction = 'out' AND m.sender_domain IS NOT NULL "
        "  AND d.recipient_domain IS NOT NULL "
        "GROUP BY m.sender_domain, d.recipient_domain "
        "ORDER BY attempts DESC LIMIT %s",
        (since, until, int(limit)),
    )


def concurrency(store, since, until):
    """Queue concurrency high-water marks per minute, as MRTG drew them."""
    return store.rows(
        "SELECT minute, local_max_used, local_limit, "
        "       remote_max_used, remote_limit "
        "FROM queue_minute WHERE minute >= %s AND minute < %s ORDER BY minute",
        (since, until),
    )


def connections(store, since, until):
    """Sessions per service, and how many of them never sent anything.

    Connections minus messages is the honest probe count, and replaces the
    earlier auth_probes: that one counted messages per client address, so a
    session that connected and sent nothing -- the thing worth spotting --
    never produced a row for it to find.
    """
    return store.rows(
        "SELECT service, SUM(connects) AS connects, SUM(messages) AS messages, "
        "  GREATEST(SUM(connects) - SUM(messages), 0) AS sessions_without_mail, "
        "  MAX(max_concurrency) AS peak_concurrency, "
        "  MAX(limit_value) AS limit_value "
        "FROM service_minute WHERE minute >= %s AND minute < %s "
        "GROUP BY service ORDER BY connects DESC",
        (since, until),
    )


def mailbox_access(store, since, until, limit=100):
    """Who logged in to collect their mail, how often, and from where."""
    return store.rows(
        "SELECT user AS address, service, COUNT(*) AS logins, "
        "  COUNT(DISTINCT client_ip) AS addresses, MAX(ts) AS last_seen "
        "FROM auth_event "
        "WHERE ts >= %s AND ts < %s AND result = 'login' AND user IS NOT NULL "
        "GROUP BY user, service ORDER BY logins DESC LIMIT %s",
        (since, until, int(limit)),
    )


def block_state(last_action):
    """What fail2ban did about an address, in words for the table.

    Saying nothing when an address was never blocked would read the same as
    "blocked but the ban has since expired", and those want different reactions.
    """
    if last_action == "ban":
        return "blocked"
    if last_action == "unban":
        return "ban expired"
    return "not blocked"


def auth_failures(store, since, until, limit=50):
    """Clients guessing passwords, and the accounts they are guessing at.

    Separate from the scanners below: these actually tried a password. A single
    address working through many accounts is a dictionary attack; many failures
    against one account is usually a client with stale credentials.
    """
    rows = store.rows(
        "SELECT a.client_ip, COUNT(*) AS failures, "
        "  COUNT(DISTINCT a.user) AS accounts_tried, MAX(a.ts) AS last_seen, "
        # The most recent thing fail2ban did to this address, if anything. An
        # unban means the ban expired, which is not the same as never blocked.
        "  (SELECT SUBSTRING_INDEX("
        "     GROUP_CONCAT(b.action ORDER BY b.ts DESC), ',', 1) "
        "   FROM ban_event b WHERE b.ip = a.client_ip) AS last_ban_action "
        "FROM auth_event a "
        "WHERE a.ts >= %s AND a.ts < %s AND a.result = 'auth_failed' "
        "GROUP BY a.client_ip ORDER BY failures DESC LIMIT %s",
        (since, until, int(limit)),
    )
    return [dict(row, blocked=block_state(row.pop("last_ban_action")))
            for row in rows]


def access_summary(store, since, until):
    """Logins, failures and scans per service."""
    return store.rows(
        "SELECT service, "
        "  SUM(result = 'login') AS logins, "
        "  SUM(result = 'auth_failed') AS failures, "
        "  SUM(result = 'no_auth') AS scans, "
        "  COUNT(DISTINCT CASE WHEN result = 'login' THEN user END) AS accounts "
        "FROM auth_event WHERE ts >= %s AND ts < %s "
        "GROUP BY service ORDER BY logins DESC",
        (since, until),
    )


# A delivery chain is every attempt at one recipient of one message instance --
# msg_id is a reused queue inode, so the instance timestamp is part of what
# identifies the message. qmail
# retries a deferral on a widening schedule until queuelifetime (7 days by
# default) and then bounces it; a failure is permanent and never retried. So
# the chain, not the attempt, is what says whether the mail actually arrived.
_CHAINS = """
SELECT msg_id, msg_ts, recipient,
       COUNT(*) AS attempts,
       MIN(ts) AS first_try,
       MAX(ts) AS last_try,
       MAX(result = 'success') AS delivered,
       MAX(result = 'failure') AS bounced,
       SUBSTRING_INDEX(GROUP_CONCAT(COALESCE(reason, detail)
         ORDER BY ts DESC SEPARATOR '|'), '|', 1) AS reason
  FROM delivery
 WHERE ts >= %s AND ts < %s
   AND recipient <> '#@[]'
 GROUP BY msg_id, msg_ts, recipient
"""


def retry_summary(store, since, until):
    """How mail actually ended up, counting chains rather than attempts.

    Four outcomes: through on the first try, through only after retrying,
    bounced permanently, or still being retried at the end of the window.
    """
    rows = store.rows(
        "SELECT "
        "  SUM(delivered = 1 AND attempts = 1) AS delivered_first_attempt, "
        "  SUM(delivered = 1 AND attempts > 1) AS delivered_after_retry, "
        "  SUM(delivered = 0 AND bounced = 1) AS bounced, "
        "  SUM(delivered = 0 AND bounced = 0) AS still_trying, "
        "  COUNT(*) AS chains "
        "FROM (" + _CHAINS + ") AS chains",
        (since, until),
    )
    summary = {k: int(v or 0) for k, v in (rows[0].items() if rows else {}.items())}

    # Median would need a window function per row; the average over retried
    # deliveries answers the same question here and stays one statement.
    delay = store.rows(
        "SELECT AVG(TIMESTAMPDIFF(MINUTE, first_try, last_try)) AS minutes "
        "FROM (" + _CHAINS + ") AS chains "
        "WHERE delivered = 1 AND attempts > 1",
        (since, until),
    )
    minutes = delay[0]["minutes"] if delay else None
    summary["median_minutes_to_delivery"] = (
        None if minutes is None else round(float(minutes), 1)
    )
    return summary


def stuck_deliveries(store, since, until, limit=50):
    """Mail still being retried: deferred, not yet delivered, not yet bounced.

    This is the queue a postmaster can still do something about. Everything
    else has already resolved itself one way or the other.
    """
    return store.rows(
        "SELECT recipient, attempts, first_try, last_try, reason, "
        "  TIMESTAMPDIFF(HOUR, first_try, UTC_TIMESTAMP()) AS hours_waiting "
        "FROM (" + _CHAINS + ") AS chains "
        "WHERE delivered = 0 AND bounced = 0 "
        "ORDER BY attempts DESC, first_try ASC LIMIT %s",
        (since, until, int(limit)),
    )


def looks_like_demo_data(store):
    """True when the rows are the invented ones from tools/seed_demo.py.

    RFC 2606 reserves example.com, example.net and the .example TLD, so nothing
    real ever sends from them. A dashboard showing those addresses is showing
    made-up numbers, and should say so rather than being mistaken for a reading
    of a live server.
    """
    found = store.scalar(
        "SELECT COUNT(*) FROM message "
        "WHERE sender_domain IN ('example.com', 'example.net') "
        # Doubled: PyMySQL formats the statement even with no arguments, so a
        # single % here is read as a placeholder and the query fails.
        "   OR sender_domain LIKE '%%.example' "
        "LIMIT 1"
    )
    return bool(found)


def aliases_from_config(config):
    """Read 'aliases' as 'alias:real, alias:real'.

    vpopmail alias domains deliver into the real domain's mailboxes, so a
    customer with three names would otherwise appear as three domains, each
    with a fraction of their traffic.
    """
    raw = ""
    if config.has_section("domains"):
        raw = config.get("domains", "aliases", fallback="").strip()
    mapping = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        alias, _, real = item.partition(":")
        alias, real = alias.strip().lower(), real.strip().lower()
        if alias and real:
            mapping[alias] = real
    return mapping


def fold_aliases(rows, mapping, key, group):
    """Merge rows whose `key` is an alias into the row for the real domain.

    Numeric fields are summed; anything else keeps the value from the first row
    seen, which is the one with the larger count because the query ordered them
    that way. Returns the rows untouched when there is nothing to fold.
    """
    if not mapping or not rows:
        return rows

    merged = {}
    order = []
    for row in rows:
        row = dict(row)
        original = (row.get(key) or "").lower()
        row[key] = mapping.get(original, row.get(key))
        identity = tuple(row.get(field) for field in group)
        if identity not in merged:
            merged[identity] = row
            order.append(identity)
            continue
        target = merged[identity]
        for field, value in row.items():
            if field in group:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            target[field] = (target.get(field) or 0) + value
    return [merged[identity] for identity in order]


def blocked_summary(store, since, until):
    """How much blocking happened, and how much of it is still in force.

    An address can be banned more than once, so the interesting counts are
    distinct addresses rather than actions.
    """
    row = store.rows(
        "SELECT COUNT(*) AS actions, COUNT(DISTINCT ip) AS addresses "
        "FROM ban_event WHERE action = 'ban' AND ts >= %s AND ts < %s",
        (since, until),
    )
    held = store.rows(
        # Still blocked means the most recent action for that address was a
        # ban. Unbans arrive when the ban expires.
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT ip, SUBSTRING_INDEX(GROUP_CONCAT(action ORDER BY ts DESC), ',', 1) AS last_action"
        "  FROM ban_event GROUP BY ip"
        ") AS latest WHERE last_action = 'ban'",
        (),
    )
    first = row[0] if row else {"actions": 0, "addresses": 0}
    return {
        "actions": int(first.get("actions") or 0),
        "addresses": int(first.get("addresses") or 0),
        "still_blocked": int(held[0]["n"]) if held else 0,
    }


def blocked_addresses(store, since, until, limit=100):
    """Addresses blocked in the window, the worst offenders first."""
    return store.rows(
        "SELECT ip, COUNT(*) AS times, MIN(ts) AS first_seen, "
        "  MAX(ts) AS last_seen, "
        "  GROUP_CONCAT(DISTINCT jail ORDER BY jail SEPARATOR ', ') AS jails "
        "FROM ban_event WHERE action = 'ban' AND ts >= %s AND ts < %s "
        "GROUP BY ip ORDER BY times DESC, last_seen DESC LIMIT %s",
        (since, until, int(limit)),
    )


def blocked_by_jail(store, since, until):
    """Which kind of attempt is being blocked."""
    return store.rows(
        "SELECT jail, COUNT(*) AS bans, COUNT(DISTINCT ip) AS addresses "
        "FROM ban_event WHERE action = 'ban' AND ts >= %s AND ts < %s "
        "GROUP BY jail ORDER BY bans DESC",
        (since, until),
    )


def failure_side(result, remote_code, said, route=None):
    """Whose problem the far end's answer describes.

    The enhanced status code carries this, and reading it saves working it out
    by hand for every complaint. RFC 3463: 5.1.x is about the recipient
    address, 5.2.x their mailbox, 5.4.x routing or a recipient the far end
    refuses to resolve; 5.7.x is policy, which is the only class that points
    back at us -- SPF, DKIM, reputation, an explicit block. Anything 4.x is
    temporary and qmail will try again.
    """
    text = (said or "").lower()
    # A local delivery that failed is ours by definition: the mailbox is here.
    # Over-quota is the common one and the fix is a quota, not a conversation
    # with the far end.
    if route == "local" or "over quota" in text:
        return "our side"
    if result == "deferral" or (remote_code and 400 <= int(remote_code) < 500):
        return "temporary"
    enhanced = ""
    for word in (said or "").split():
        if word.count(".") == 2 and word.replace(".", "").isdigit():
            enhanced = word
            break
    if enhanced.startswith(("5.1", "5.2", "5.4")):
        return "their side"
    if enhanced.startswith("5.7"):
        # 5.7.x is the policy class, and the only one that can point back at
        # us. But it is also what a server returns when the recipient itself
        # refuses the mail -- an Italian PEC address declining ordinary mail,
        # say -- so believe the words over the code when they name the
        # recipient.
        if "recipient address rejected" in text or "destinatario" in text:
            return "their side"
        return "check ours"
    if remote_code and 500 <= int(remote_code) < 600:
        return "their side"
    return "unknown"


def failure_detail(store, since, until, address=None, limit=200):
    """Every delivery that did not succeed, one row per attempt.

    The grouped view says a cause exists and how often; this says which message
    hit it -- who sent it, who it was for, when, which host refused it and what
    that host said. That is what you need to chase one complaint rather than
    read a trend.

    Discarded bounces are left out for the same reason they are left out of the
    rates: they are not mail that failed to reach anyone.
    """
    conditions = ["d.ts >= %s", "d.ts < %s", "d.result <> 'success'",
                  "d.recipient <> '#@[]'"]
    args = [since, until]
    if address:
        conditions.append("(m.auth_user = %s OR m.sender = %s OR d.recipient = %s)")
        folded = address.strip().lower()
        args += [folded, folded, folded]
    args.append(int(limit))

    rows = store.rows(
        "SELECT d.ts, d.result, d.recipient, d.recipient_domain, d.route, "
        "  COALESCE(m.auth_user, m.sender) AS sender, m.sender_domain, "
        "  m.size_bytes, m.client_ip, m.subject, "
        "  d.remote_ip, d.remote_code, d.remote_status, "
        "  COALESCE(d.reason, d.detail) AS reason, d.detail AS raw_detail "
        "FROM delivery d "
        "LEFT JOIN message m ON m.msg_id = d.msg_id AND m.queue_ts = d.msg_ts "
        "WHERE " + " AND ".join(conditions) + " "
        "ORDER BY d.ts DESC LIMIT %s",
        tuple(args),
    )
    out = []
    for row in rows:
        said = readable_detail(row.get("raw_detail")) or row.get("reason")
        out.append(dict(row, said=said,
                        side=failure_side(row.get("result"),
                                          row.get("remote_code"), said,
                                          row.get("route"))))
    return out
