"""The open-session table must not grow without bound."""

from qmailstats import assemble
from qmailstats.assemble import SmtpAssembler

TS = "@4000000068c8a1c0000000"


def line(n, text):
    # A valid TAI64N label; the exact value does not matter here.
    return "@4000000068c8a1c0%08x %s" % (n, text)


def test_a_connection_that_sends_nothing_is_forgotten_at_its_end():
    a = SmtpAssembler(service="smtps")
    a.feed(line(1, "tcpserver: pid 700 from 198.51.100.9"))
    a.feed(line(2, "tcpserver: ok 700 host:192.0.2.2:465 :198.51.100.9::5000"))
    assert 700 in a.sessions
    a.feed(line(3, "tcpserver: end 700 status 256"))
    assert a.sessions == {}


def test_an_open_line_alone_does_not_create_a_session():
    """Refused connections log 'pid ... from' and nothing else."""
    a = SmtpAssembler(service="smtp")
    for pid in range(100, 200):
        a.feed(line(pid, "tcpserver: pid %d from 198.51.100.%d" % (pid, pid % 250)))
    assert a.sessions == {}


def test_a_session_that_sends_mail_still_carries_its_facts():
    a = SmtpAssembler(service="submission")
    a.feed(line(1, "tcpserver: ok 900 host:192.0.2.2:587 :203.0.113.4::5000"))
    got = a.feed(line(2, "mail recv: pid 900 from <user@example.com> qp 12345"))
    assert got and got[0].client_ip == "203.0.113.4"
    assert got[0].direction == "out"
    a.feed(line(3, "tcpserver: end 900 status 0"))
    assert a.sessions == {}


def test_the_table_is_capped_even_if_end_lines_are_lost(monkeypatch):
    monkeypatch.setattr(assemble, "MAX_OPEN_SESSIONS", 50)
    a = SmtpAssembler(service="smtps")
    for pid in range(1, 201):
        a.feed(line(pid, "tcpserver: ok %d host:192.0.2.2:465 :198.51.100.1::%d" % (pid, pid)))
    assert len(a.sessions) == 50
    # The newest are the ones kept: those are the connections still in flight.
    assert min(a.sessions) == 151 and max(a.sessions) == 200


def test_restoring_a_bloated_state_trims_it(monkeypatch):
    monkeypatch.setattr(assemble, "MAX_OPEN_SESSIONS", 10)
    bloated = {"sessions": {str(pid): {"client_ip": "198.51.100.1", "direction": "in"}
                            for pid in range(1000)},
               "minutes": {}}
    a = SmtpAssembler.restore(bloated, service="smtps")
    assert len(a.sessions) == 10
    assert sorted(a.sessions) == list(range(990, 1000))
