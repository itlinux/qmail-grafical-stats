"""A plain-text summary of one day, for reading in a mail client.

Deliberately not a copy of the dashboard. A morning report is read in thirty
seconds before the first coffee, so it answers four questions and stops: how
much mail moved, what did not arrive and why, who is being locked out or
guessed at, and is anything still stuck.
"""

import argparse
import configparser
import datetime as dt
import socket
import subprocess
import sys

from qmailstats import queries
from qmailstats.db import Store
from qmailstats.notify import SENDMAIL, envelope_sender


def _window(day):
    start = dt.datetime.combine(day, dt.time.min)
    return start, start + dt.timedelta(days=1)


def _table(rows, columns, indent="  "):
    if not rows:
        return [indent + "(none)"]
    widths = [max(len(str(c[0])), max(len(str(r.get(c[1], "") or "")) for r in rows))
              for c in columns]
    out = [indent + "  ".join(str(c[0]).ljust(w) for c, w in zip(columns, widths))]
    for row in rows:
        out.append(indent + "  ".join(
            str(row.get(c[1], "") or "").ljust(w) for c, w in zip(columns, widths)))
    return out


def compose(store, day, host):
    """Return the report for one day as text."""
    since, until = _window(day)
    lines = ["Mail on %s for %s" % (host, day.isoformat()), ""]

    summary = queries.summary(store, since, until)
    if not (summary["messages_in"] or summary["messages_out"]):
        lines.append("  No mail moved on this day.")
        return "\n".join(lines)

    lines += [
        "  Received %s      Sent %s" % (summary["messages_in"], summary["messages_out"]),
        "  Delivery attempts %s: %s delivered, %s deferred, %s failed" % (
            summary["attempts"], summary["successes"],
            summary["deferrals"], summary["failures"]),
    ]
    if summary["outbound_rate"] is not None:
        lines.append("  Outbound reached its destination %s%% of the time (%s of %s)"
                     % (summary["outbound_rate"], summary["outbound_successes"],
                        summary["outbound_attempts"]))
    if summary["discarded_bounces"]:
        lines.append("  %s undeliverable bounces discarded%s" % (
            summary["discarded_bounces"],
            " -- somebody may be forging this server as a sender"
            if summary["discarded_bounces"] > 100 else ""))

    chains = queries.retry_summary(store, since, until)
    if chains.get("still_trying"):
        lines.append("  %s deliveries are still being retried" % chains["still_trying"])

    failures = queries.failure_detail(store, since, until, limit=12)
    lines += ["", "What did not get through (%d)" % len(failures)]
    lines += _table(
        [{"to": f["recipient"], "from": f["sender"], "why": (f["reason"] or "")[:52],
          "status": f["remote_status"] or ""} for f in failures],
        [("To", "to"), ("From", "from"), ("Status", "status"), ("Reason", "why")])

    spam = [s for s in queries.spam_per_account(store, since, until, limit=5)
            if int(s["spam"] or 0)]
    if spam:
        lines += ["", "Spam marked, by mailbox"]
        lines += _table(
            [{"mailbox": s["address"], "spam": s["spam"], "of": s["scanned"]}
             for s in spam],
            [("Mailbox", "mailbox"), ("Spam", "spam"), ("Of", "of")])

    guessing = queries.auth_failures(store, since, until, limit=5)
    if guessing:
        lines += ["", "Clients guessing passwords"]
        lines += _table(
            [{"ip": g["client_ip"], "tries": g["failures"],
              "accounts": g["accounts_tried"]} for g in guessing],
            [("Address", "ip"), ("Failures", "tries"), ("Accounts", "accounts")])

    access = queries.mailbox_access(store, since, until, limit=5)
    if access:
        lines += ["", "Busiest mailboxes collecting mail"]
        lines += _table(
            [{"mailbox": a["address"], "logins": a["logins"],
              "from": a["addresses"]} for a in access],
            [("Mailbox", "mailbox"), ("Logins", "logins"), ("Addresses", "from")])

    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mail a one-day summary.")
    ap.add_argument("--config", default="/etc/qmail-grafical-stats/config.ini")
    ap.add_argument("--day", help="YYYY-MM-DD, default yesterday")
    ap.add_argument("--print", action="store_true", dest="show",
                    help="write it to the terminal instead of sending it")
    args = ap.parse_args(argv)

    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config)
    settings = dict(config.items("alerts")) if config.has_section("alerts") else {}
    to_address = settings.get("digest_to", settings.get("mail_to", "")).strip()

    day = (dt.date.fromisoformat(args.day) if args.day
           else dt.date.today() - dt.timedelta(days=1))
    host = socket.gethostname()

    store = Store.from_dsn(config.get("database", "dsn"))
    try:
        body = compose(store, day, host)
    finally:
        store.close()

    if args.show or not to_address:
        print(body)
        return 0

    from_address = envelope_sender(settings, host)
    subject = "[mail stats] %s on %s" % (day.isoformat(), host)
    message = "From: %s\nTo: %s\nSubject: %s\n\n%s\n" % (
        from_address, to_address, subject, body)
    return subprocess.run([SENDMAIL, "-f", from_address, to_address],
                          input=message, text=True).returncode


if __name__ == "__main__":
    sys.exit(main())
