"""How a queued message is tied to the SMTP session that delivered it.

These use the shape of real qmail logs: the qp values differ by a few between
the two logs because they are different process ids, and msg_id is a queue
inode number that is reused as soon as the queue drains.
"""

import datetime as dt

from qmailstats.assemble import SendAssembler, SmtpAssembler


def feed(assembler, lines):
    out = []
    for line in lines:
        out.extend(assembler.feed(line))
    return out


def test_the_queue_record_carries_its_own_timestamp_for_matching():
    a = SendAssembler()
    records = feed(a, [
        "@400000006aa9f5ea12a8b5e4 info msg 68566592: bytes 73180 from <a@sender.example> qp 272491 uid 89",
    ])
    assert records[0].queue_qp == 272491
    assert records[0].queue_ts is not None
    assert records[0].msg_id == 68566592
    # The queue log never sees the SMTP session, so it claims no smtp qp.
    assert records[0].qp is None


def test_the_smtp_record_keeps_its_own_qp_and_no_queue_identity():
    a = SmtpAssembler(store_subject=False)
    records = feed(a, [
        "@400000006aa9f5ea12071acc tcpserver: ok 272482 h:192.0.2.10:25 :198.51.100.4::1",
        "@400000006aa9f5ea12071acd mail recv: pid 272482 from <a@sender.example> qp 272486",
    ])
    assert records[0].qp == 272486
    assert records[0].queue_qp is None
    assert records[0].msg_id is None


def test_a_delivery_records_which_instance_of_the_message_it_belongs_to():
    """msg_id is a reused inode; the queue timestamp disambiguates instances."""
    a = SendAssembler()
    records = feed(a, [
        "@400000006aa9f5ea12a8b5e4 info msg 68566592: bytes 100 from <a@sender.example> qp 1 uid 89",
        "@400000006aa9f5ea12a8b5e5 starting delivery 1: msg 68566592 to remote x@dest.example",
        "@400000006aa9f5ea12a8b5e6 delivery 1: success: did_1+0+0/",
        "@400000006aa9f5ea12a8b5e7 end msg 68566592",
    ])
    message = [r for r in records if r.__class__.__name__ == "Message"][0]
    delivery = [r for r in records if r.__class__.__name__ == "Delivery"][0]
    assert delivery.msg_ts == message.queue_ts, "delivery not tied to its instance"


def test_two_messages_reusing_one_inode_stay_apart():
    a = SendAssembler()
    records = feed(a, [
        "@400000006aa9f5ea12a8b5e4 info msg 68566592: bytes 100 from <first@sender.example> qp 1 uid 89",
        "@400000006aa9f5ea12a8b5e5 starting delivery 1: msg 68566592 to remote one@dest.example",
        "@400000006aa9f5ea12a8b5e6 delivery 1: success: did_1+0+0/",
        "@400000006aa9f5ea12a8b5e7 end msg 68566592",
        "@400000006aa9f6ea12a8b5e4 info msg 68566592: bytes 200 from <second@sender.example> qp 2 uid 89",
        "@400000006aa9f6ea12a8b5e5 starting delivery 2: msg 68566592 to remote two@dest.example",
        "@400000006aa9f6ea12a8b5e6 delivery 2: failure: No_such_address./",
        "@400000006aa9f6ea12a8b5e7 end msg 68566592",
    ])
    messages = [r for r in records if r.__class__.__name__ == "Message"]
    deliveries = [r for r in records if r.__class__.__name__ == "Delivery"]
    assert len({m.queue_ts for m in messages}) == 2
    pairs = {(d.recipient, d.msg_ts) for d in deliveries}
    by_sender = {m.sender: m.queue_ts for m in messages}
    assert ("one@dest.example", by_sender["first@sender.example"]) in pairs
    assert ("two@dest.example", by_sender["second@sender.example"]) in pairs
