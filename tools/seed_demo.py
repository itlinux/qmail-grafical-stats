"""Fill a database with INVENTED mail traffic, for looking at the dashboard.

Development only -- never point this at a real qmail-grafical-stats database.
Running the test suite clears the tables, so re-run this afterwards.

Every figure this produces is random. The addresses are deliberately obvious
placeholders on example.com and example.net, reserved by RFC 2606 and belonging
to nobody, so that a dashboard full of demo data can never be mistaken for a
reading of a real server.

    .venv/bin/python tools/seed_demo.py 'mysql://root:test@127.0.0.1:13306/qmailstats_test'
"""

import datetime as dt
import random
import sys

from qmailstats.db import Store
from qmailstats.models import Delivery, Message

# RFC 2606 reserves these: they resolve to nothing and belong to nobody.
ACCOUNTS = [
    "demo-sales@example.com",
    "demo-office@example.com",
    "demo-export@example.net",
    "demo-accounts@example.net",
]
PARTNERS = [
    "partner-one.example", "partner-two.example", "partner-three.example",
    "mailbox-provider.example", "webmail.example",
]
SPAM_SOURCES = ["bulk.example", "offers.example", "newsletter.example"]
# Shaped like the real thing, remote address and all, so the demo exercises
# the same parsing the live logs will.
FAILURES = [
    "Sorry,_no_mailbox_here_by_that_name._(#5.1.1)/",
    "{ip}_does_not_like_recipient./Remote_host_said:_550_5.1.1_User_unknown/Giving_up_on_{ip}./",
    "{ip}_does_not_like_recipient./Remote_host_said:_550_5.7.1_Message_rejected_due_to_SPF_check/Giving_up_on_{ip}./",
    "{ip}_does_not_like_recipient./Remote_host_said:_550_5.7.26_Unauthenticated_email_from_domain_is_not_accepted_due_to_DMARC_policy/Giving_up_on_{ip}./",
]
DEFERRALS = [
    "Sorry,_I_wasn't_able_to_establish_an_SMTP_connection./",
    "{ip}_does_not_like_recipient./Remote_host_said:_451_4.7.1_IP_Reputation_Greylisted_-_Riprova_tra_5_minuti/Giving_up_on_{ip}./",
    "{ip}_does_not_like_recipient./Remote_host_said:_452_4.2.2_Mailbox_full/Giving_up_on_{ip}./",
]


def _detail(template):
    return template.format(
        ip="%d.%d.%d.%d" % tuple(random.randint(1, 250) for _ in range(4)))


def seed(dsn, days=14):
    store = Store.from_dsn(dsn)
    store.apply_schema()
    for table in ("delivery", "message", "daily_stats", "checkpoint", "parser_state"):
        store.execute("DELETE FROM %s" % table)

    random.seed(11)
    midnight = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    qp, msg_id = 1000, 500000
    messages, deliveries = [], []

    for day_back in range(days, -1, -1):
        day = midnight - dt.timedelta(days=day_back)
        weekend = day.weekday() >= 5
        volume = 0.35 if weekend else 1.0

        for _ in range(int(random.randint(40, 90) * volume)):   # inbound
            qp += 1
            msg_id += 1
            # Office hours, with a tail either side.
            hour = min(23, max(0, int(random.gauss(11, 3))))
            ts = day + dt.timedelta(hours=hour, minutes=random.randint(0, 59))
            spam = random.random() < 0.22
            source = random.choice(SPAM_SOURCES if spam else PARTNERS)
            recipient = random.choice(ACCOUNTS)
            messages.append(Message(
                qp=qp, ts=ts, direction="in",
                sender="sender%d@%s" % (random.randint(1, 60), source),
                msg_id=msg_id, size_bytes=random.randint(2000, 120000),
                client_ip="188.117.%d.%d" % (random.randint(1, 250), random.randint(1, 250)),
                spam_score=round(random.uniform(6, 18) if spam else random.uniform(-4, 3), 2),
                verdict="SPAM" if spam else "CLEAN",
            ))
            deliveries.append(Delivery(
                msg_id=msg_id, ts=ts, recipient=recipient,
                route="local", result="success",
            ))

        for _ in range(int(random.randint(15, 45) * volume)):   # outbound
            qp += 1
            msg_id += 1
            hour = min(23, max(0, int(random.gauss(13, 2.5))))
            ts = day + dt.timedelta(hours=hour, minutes=random.randint(0, 59))
            account = random.choice(ACCOUNTS)
            messages.append(Message(
                qp=qp, ts=ts, direction="out", sender=account, auth_user=account,
                msg_id=msg_id, size_bytes=random.randint(1500, 300000),
                client_ip="91.80.%d.%d" % (random.randint(1, 250), random.randint(1, 250)),
                verdict="CLEAN",
            ))
            recipient = "client%d@%s" % (random.randint(1, 90), random.choice(PARTNERS))
            roll = random.random()
            if roll < 0.10:
                deliveries.append(Delivery(
                    msg_id=msg_id, ts=ts, recipient=recipient, route="remote",
                    result="deferral", detail=_detail(random.choice(DEFERRALS))))
                deliveries.append(Delivery(
                    msg_id=msg_id, ts=ts + dt.timedelta(minutes=random.randint(15, 60)),
                    recipient=recipient, route="remote", result="success",
                    detail="did_1+0+0/"))
            elif roll < 0.16:
                deliveries.append(Delivery(
                    msg_id=msg_id, ts=ts, recipient=recipient, route="remote",
                    result="failure", detail=_detail(random.choice(FAILURES))))
            else:
                deliveries.append(Delivery(
                    msg_id=msg_id, ts=ts, recipient=recipient, route="remote",
                    result="success", detail="did_1+0+0/"))

    store.write_messages(messages)
    store.write_deliveries(deliveries)
    print("seeded %d INVENTED messages, %d delivery attempts over %d days" % (
        store.scalar("SELECT COUNT(*) FROM message"),
        store.scalar("SELECT COUNT(*) FROM delivery"), days + 1))
    print("These figures are random. They are not a reading of any mail server.")
    store.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    seed(sys.argv[1])
