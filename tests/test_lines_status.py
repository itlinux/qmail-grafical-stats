from qmailstats.lines import parse_send, parse_smtp


def test_reads_queue_concurrency_against_its_limits():
    got = parse_send("status: local 1/10 remote 3/60")
    assert got == {
        "kind": "concurrency", "local_used": 1, "local_max": 10,
        "remote_used": 3, "remote_max": 60,
    }


def test_reads_session_concurrency_from_tcpserver():
    assert parse_smtp("tcpserver: status: 7/200") == {
        "kind": "concurrency", "used": 7, "max": 200,
    }


def test_reads_the_connect_line_as_a_countable_event():
    assert parse_smtp("tcpserver: pid 210485 from 198.51.100.4") == {
        "kind": "open", "pid": 210485, "client_ip": "198.51.100.4",
    }
