from qmailstats.lines import parse_send


def test_reads_the_info_line_that_carries_sender_and_qp():
    got = parse_send("info msg 68566592: bytes 4523 from <a@sender.example> qp 210429 uid 89")
    assert got == {
        "kind": "info", "msg_id": 68566592, "bytes": 4523,
        "sender": "a@sender.example", "qp": 210429, "uid": 89,
    }


def test_reads_a_bounce_sender_as_the_empty_string():
    assert parse_send("info msg 5: bytes 100 from <> qp 7 uid 0")["sender"] == ""


def test_reads_the_start_of_a_delivery_attempt():
    got = parse_send("starting delivery 759: msg 68566592 to local export@example.org")
    assert got == {
        "kind": "starting", "delivery_id": 759, "msg_id": 68566592,
        "route": "local", "recipient": "export@example.org",
    }


def test_reads_a_remote_delivery_start():
    got = parse_send("starting delivery 12: msg 34 to remote someone@example.org")
    assert got["route"] == "remote"
    assert got["recipient"] == "someone@example.org"


def test_reads_a_successful_outcome():
    got = parse_send("delivery 759: success: did_0+0+1/")
    assert got == {
        "kind": "outcome", "delivery_id": 759,
        "result": "success", "detail": "did_0+0+1/",
    }


def test_reads_a_deferral_with_its_reason():
    got = parse_send("delivery 12: deferral: Sorry,_I_wasn't_able_to_establish_an_SMTP_connection./")
    assert got["result"] == "deferral"
    assert "SMTP_connection" in got["detail"]


def test_reads_a_failure_with_its_reason():
    got = parse_send("delivery 13: failure: No_such_address./")
    assert got["result"] == "failure"
    assert got["detail"] == "No_such_address./"


def test_reads_the_end_of_a_message():
    assert parse_send("end msg 68566592") == {"kind": "end", "msg_id": 68566592}


def test_ignores_queue_chatter_that_carries_nothing():
    assert parse_send("new msg 68566592") is None
    assert parse_send("bounce msg 1 qp 2") is None
