"""Mail the alerts, but only when the situation changes.

An alert that arrives every minute while a condition holds trains people to
filter it, and a filtered alert is worse than none. So a message goes out when
the set of active alerts changes -- including when it empties, because knowing
a problem stopped matters as much as knowing it started.
"""

import argparse
import configparser
import json
import pathlib
import socket
import subprocess
import sys

from qmailstats import alerts
from qmailstats.db import Store

DEFAULT_STATE = "/var/lib/qmail-grafical-stats/alert-state.json"
SENDMAIL = "/var/qmail/bin/qmail-inject"


def changed_since_last_run(raised, state_path):
    """True when the set of active alerts differs from the last run."""
    path = pathlib.Path(state_path)
    current = sorted(a["id"] for a in raised)
    try:
        previous = sorted(json.loads(path.read_text()).get("active", []))
    except (OSError, ValueError):
        previous = []
    if current == previous:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"active": current}))
    return True


def envelope_sender(settings, host):
    """The address this mail is sent from.

    Never the recipient's: the destination domain publishes which hosts may
    send for it, and this server is not usually one of them. Mail from a mail
    server claiming to be from somebody else's domain fails SPF, and the alert
    about a broken mail server is precisely the mail you cannot afford to have
    rejected.
    """
    configured = (settings.get("mail_from") or "").strip()
    return configured or ("postmaster@" + host)


def compose(raised, host):
    if not raised:
        return ("[mail stats] all clear on %s" % host,
                "No alerts are active any more.\n")
    worst = raised[0]["severity"]
    subject = "[mail stats] %s on %s: %s" % (worst, host, raised[0]["title"])
    body = ["%d alert(s) active on %s:\n" % (len(raised), host)]
    for alert in raised:
        body.append("%s: %s\n  %s\n" % (alert["severity"].upper(),
                                        alert["title"], alert["detail"]))
    return subject, "\n".join(body)


def send(to_address, subject, body, from_address, sendmail=SENDMAIL):
    message = "From: %s\nTo: %s\nSubject: %s\n\n%s" % (
        from_address, to_address, subject, body)
    return subprocess.run([sendmail, "-f", from_address, to_address],
                          input=message, text=True).returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mail alerts when they change.")
    ap.add_argument("--config", default="/etc/qmail-grafical-stats/config.ini")
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--force", action="store_true",
                    help="send even if nothing changed")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent")
    args = ap.parse_args(argv)

    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config)
    settings = dict(config.items("alerts")) if config.has_section("alerts") else {}
    to_address = settings.pop("mail_to", "").strip()
    host = socket.gethostname()
    from_address = envelope_sender(settings, host)
    settings.pop("mail_from", None)

    store = Store.from_dsn(config.get("database", "dsn"))
    try:
        raised = alerts.evaluate(store, settings)
    finally:
        store.close()

    changed = changed_since_last_run(raised, args.state)
    subject, body = compose(raised, host)

    if args.dry_run:
        print("changed:", changed)
        print("from:", from_address)
        print("subject:", subject)
        print(body)
        return 0
    if not to_address:
        return 0          # notification not configured; the UI still shows them
    if not (changed or args.force):
        return 0
    return send(to_address, subject, body, from_address)


if __name__ == "__main__":
    sys.exit(main())
