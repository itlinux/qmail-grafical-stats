from qmailstats.lines import parse_smtp


def test_reads_the_tcpserver_accept_line_for_port_and_client():
    got = parse_smtp("tcpserver: ok 210485 mail.example.com:192.0.2.10:25 :198.51.100.4::56124")
    assert got == {
        "kind": "connect", "pid": 210485, "local_port": 25, "client_ip": "198.51.100.4",
    }


def test_reads_the_submission_port_from_the_same_shape():
    got = parse_smtp("tcpserver: ok 209937 mail.example.com:192.0.2.10:587 :203.0.113.26::59874")
    assert got["local_port"] == 587
    assert got["client_ip"] == "203.0.113.26"


def test_reads_the_mail_recv_line_that_carries_qp():
    got = parse_smtp("mail recv: pid 210394 from <someone@bulk.example> qp 210429")
    assert got == {
        "kind": "recv", "pid": 210394, "sender": "someone@bulk.example", "qp": 210429,
    }


def test_reads_an_authenticated_policy_check():
    got = parse_smtp(
        "policy_check: local info@example.com -> remote "
        "booking@partner.example (AUTHENTICATED SENDER)"
    )
    assert got == {
        "kind": "policy", "auth_user": "info@example.com",
        "recipient": "booking@partner.example", "authenticated": True,
    }


def test_marks_an_unauthenticated_policy_check_as_such():
    got = parse_smtp("policy_check: remote a@sender.example -> local c@recipient.example")
    assert got["authenticated"] is False
    assert got["auth_user"] == "a@sender.example"


def test_reads_the_simscan_verdict_with_score_and_addresses():
    line = (
        "simscan:[210394]:CLEAN (-1.21/9999.00):3.3608s:"
        "=?UTF-8?Q?=F0=9F=98=8D_SUNNYDEALS?=:198.51.100.224:"
        "noreply@bulk.example:export@example.org"
    )
    got = parse_smtp(line)
    assert got["kind"] == "scan"
    assert got["pid"] == 210394
    assert got["verdict"] == "CLEAN"
    assert got["spam_score"] == -1.21
    assert got["client_ip"] == "198.51.100.224"
    assert got["sender"] == "noreply@bulk.example"
    assert got["recipient"] == "export@example.org"
    assert got["subject"] == "=?UTF-8?Q?=F0=9F=98=8D_SUNNYDEALS?="


def test_reads_a_subject_that_contains_colons():
    line = "simscan:[99]:CLEAN (0.00/9999.00):1.0s:Re: your order: shipped:192.0.2.2:a@sender.example:c@recipient.example"
    got = parse_smtp(line)
    assert got["subject"] == "Re: your order: shipped"
    assert got["sender"] == "a@sender.example"
    assert got["recipient"] == "c@recipient.example"


def test_ignores_simscan_housekeeping():
    assert parse_smtp("simscan:[210394]: skipping check_spam, disabled for this domain") is None


def test_reads_the_end_of_a_connection():
    # Ignoring this line was how the open-session table reached 244,000
    # entries: a connection that never sent mail was never let go.
    assert parse_smtp("tcpserver: end 210485 status 0") == {
        "kind": "close", "pid": 210485}
    assert parse_smtp("tcpserver: end 94244 status 256")["pid"] == 94244


def test_keeps_an_ipv6_client_address_whole():
    # The address contains '::' itself; only the last one separates the port.
    got = parse_smtp("tcpserver: ok 5 host:192.0.2.2:465 :2001:db8::1::40000")
    assert got["client_ip"] == "2001:db8::1"
    assert got["local_port"] == 465


def test_keeps_a_full_length_ipv6_client_address():
    got = parse_smtp(
        "tcpserver: ok 5 host:192.0.2.2:25 :2001:0db8:85a3:0000:0000:8a2e:0370:7334::25"
    )
    assert got["client_ip"] == "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
