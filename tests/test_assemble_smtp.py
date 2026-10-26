from qmailstats.assemble import SmtpAssembler


def feed(assembler, lines):
    out = []
    for line in lines:
        out.extend(assembler.feed(line))
    return out


def test_builds_an_inbound_message_from_a_port_25_session():
    a = SmtpAssembler(store_subject=False)
    records = feed(a, [
        "@400000006aa8e01718c83b0c tcpserver: ok 210394 mail.example.com:192.0.2.10:25 :198.51.100.4::56124",
        "@400000006aa8e01a2ad1633c simscan:[210394]:CLEAN (-1.21/9999.00):3.3s:Subject here:198.51.100.224:noreply@bulk.example:export@example.org",
        "@400000006aa8e01a2bdc4f44 mail recv: pid 210394 from <noreply@bulk.example> qp 210429",
    ])
    assert len(records) == 1
    msg = records[0]
    assert msg.qp == 210429
    assert msg.direction == "in"
    assert msg.sender == "noreply@bulk.example"
    assert msg.spam_score == -1.21
    assert msg.auth_user is None


def test_builds_an_outbound_message_and_credits_the_authenticated_account():
    a = SmtpAssembler(store_subject=False)
    records = feed(a, [
        "@400000006aa42ea20000000c tcpserver: ok 358645 mail.example.com:192.0.2.10:587 :192.0.2.4::59874",
        "@400000006aa42ea30450a31c policy_check: local info@example.com -> remote booking@partner.example (AUTHENTICATED SENDER)",
        "@400000006aa42ea40b9ed88c mail recv: pid 358645 from <info@example.com> qp 358661",
    ])
    assert len(records) == 1
    msg = records[0]
    assert msg.direction == "out"
    assert msg.auth_user == "info@example.com"
    assert msg.client_ip == "192.0.2.4"


def test_drops_the_subject_when_the_flag_is_off():
    a = SmtpAssembler(store_subject=False)
    records = feed(a, [
        "@400000006aa8e01718c83b0c tcpserver: ok 1 h:192.0.2.1:25 :192.0.2.8::1",
        "@400000006aa8e01a2ad1633c simscan:[1]:CLEAN (0.0/9999.0):1s:Secret subject:192.0.2.8:a@sender.example:c@recipient.example",
        "@400000006aa8e01a2bdc4f44 mail recv: pid 1 from <a@sender.example> qp 2",
    ])
    assert records[0].subject is None


def test_keeps_the_subject_when_the_flag_is_on():
    a = SmtpAssembler(store_subject=True)
    records = feed(a, [
        "@400000006aa8e01718c83b0c tcpserver: ok 1 h:192.0.2.1:25 :192.0.2.8::1",
        "@400000006aa8e01a2ad1633c simscan:[1]:CLEAN (0.0/9999.0):1s:Secret subject:192.0.2.8:a@sender.example:c@recipient.example",
        "@400000006aa8e01a2bdc4f44 mail recv: pid 1 from <a@sender.example> qp 2",
    ])
    assert records[0].subject == "Secret subject"


def test_a_connection_that_never_sends_mail_produces_no_message():
    a = SmtpAssembler(store_subject=False)
    records = feed(a, [
        "@400000006aa8dd3f1b64ee8c tcpserver: ok 209937 mail.example.com:192.0.2.10:587 :203.0.113.26::59874",
        "@400000006aa8dd4602177044 tcpserver: end 209937 status 0",
    ])
    assert records == []


def test_a_recv_without_a_seen_connect_still_yields_a_message():
    a = SmtpAssembler(store_subject=False, default_direction="in")
    records = feed(a, ["@400000006aa8e01a2bdc4f44 mail recv: pid 7 from <a@sender.example> qp 9"])
    assert len(records) == 1
    assert records[0].direction == "in"
    assert records[0].client_ip is None


def test_pid_state_is_cleared_after_a_message_so_reuse_does_not_leak():
    a = SmtpAssembler(store_subject=False)
    feed(a, [
        "@400000006aa8e01718c83b0c tcpserver: ok 5 h:192.0.2.1:587 :192.0.2.9::1",
        "@400000006aa42ea30450a31c policy_check: local first@account.example -> remote r@dest.example (AUTHENTICATED SENDER)",
        "@400000006aa8e01a2bdc4f44 mail recv: pid 5 from <first@account.example> qp 10",
    ])
    records = feed(a, ["@400000006aa8e01a2bdc5000 mail recv: pid 5 from <second@account.example> qp 11"])
    assert records[0].auth_user is None, "stale auth_user leaked into a reused pid"
