import configparser

from qmailstats.assemble import SmtpAssembler
from qmailstats.ingest import logdirs_from_config


def feed(assembler, lines):
    out = []
    for line in lines:
        out.extend(assembler.feed(line))
    return out


def test_falls_back_to_the_envelope_sender_when_there_is_no_policy_line():
    a = SmtpAssembler(store_subject=False, default_direction="out", fallback_auth=True)
    records = feed(a, [
        "@400000006aa42ea20000000c tcpserver: ok 1 h:192.0.2.1:587 :192.0.2.4::1",
        "@400000006aa42ea40b9ed88c mail recv: pid 1 from <info@example.com> qp 2",
    ])
    assert records[0].auth_user == "info@example.com"
    assert records[0].auth_source == "envelope"


def test_marks_attribution_as_verified_when_the_policy_line_is_present():
    a = SmtpAssembler(store_subject=False, default_direction="out", fallback_auth=True)
    records = feed(a, [
        "@400000006aa42ea20000000c tcpserver: ok 1 h:192.0.2.1:587 :192.0.2.4::1",
        "@400000006aa42ea30450a31c policy_check: local real@account.example -> remote r@dest.example (AUTHENTICATED SENDER)",
        "@400000006aa42ea40b9ed88c mail recv: pid 1 from <spoofed@other.example> qp 2",
    ])
    assert records[0].auth_user == "real@account.example"
    assert records[0].auth_source == "policy_check"


def test_never_attributes_inbound_mail_to_its_envelope_sender():
    """Port 25 is unauthenticated: the envelope sender is an unverified claim."""
    a = SmtpAssembler(store_subject=False, default_direction="in", fallback_auth=True)
    records = feed(a, [
        "@400000006aa8e01718c83b0c tcpserver: ok 1 h:192.0.2.1:25 :192.0.2.9::1",
        "@400000006aa8e01a2bdc4f44 mail recv: pid 1 from <anyone@elsewhere.example> qp 2",
    ])
    assert records[0].auth_user is None
    assert records[0].auth_source is None


def test_fallback_can_be_turned_off_entirely():
    a = SmtpAssembler(store_subject=False, default_direction="out", fallback_auth=False)
    records = feed(a, [
        "@400000006aa42ea40b9ed88c mail recv: pid 1 from <info@account.example> qp 2",
    ])
    assert records[0].auth_user is None


def test_log_directory_names_come_from_configuration():
    config = configparser.ConfigParser()
    config.read_dict({"parser": {
        "logdirs": "qmail-send:send, qmail-smtpd:smtp, sub:submission",
    }})
    assert logdirs_from_config(config) == [
        ("qmail-smtpd", "smtp"), ("sub", "submission"), ("qmail-send", "send"),
    ]


def test_default_log_directory_names_match_this_server():
    config = configparser.ConfigParser()
    config.read_dict({"parser": {}})
    assert logdirs_from_config(config) == [
        ("smtp", "smtp"), ("submission", "submission"),
        ("smtps", "smtps"), ("send", "send"),
    ]


def test_the_queue_log_is_always_read_last_whatever_the_order_given():
    """SMTP rows must exist before the queue log annotates them by qp."""
    config = configparser.ConfigParser()
    config.read_dict({"parser": {"logdirs": "send:send, smtp:smtp"}})
    assert logdirs_from_config(config)[-1][1] == "send"


def test_an_unknown_role_is_refused_rather_than_silently_skipped():
    import pytest
    config = configparser.ConfigParser()
    config.read_dict({"parser": {"logdirs": "weird:nonsense"}})
    with pytest.raises(ValueError):
        logdirs_from_config(config)
