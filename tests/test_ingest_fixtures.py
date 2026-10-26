import pathlib

from qmailstats.assemble import SendAssembler, SmtpAssembler

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def _run(assembler, name):
    records = []
    for line in (FIXTURES / name).read_text().splitlines():
        records.extend(assembler.feed(line))
    return records


def test_the_captured_send_log_yields_the_delivery_we_can_see_in_it():
    records = _run(SendAssembler(), "send.log")
    deliveries = [r for r in records if r.__class__.__name__ == "Delivery"]
    assert len(deliveries) == 1
    # The log says "example.org-export@example.org"; that is
    # vpopmail's local delivery form for export@example.org, and it is
    # reduced so one mailbox does not appear twice.
    assert deliveries[0].recipient == "export@example.org"
    assert deliveries[0].result == "success"
    assert deliveries[0].route == "local"


def test_the_captured_smtp_log_yields_one_inbound_message():
    records = _run(SmtpAssembler(store_subject=False), "smtp.log")
    assert len(records) == 1
    assert records[0].direction == "in"
    assert records[0].qp == 210429
    assert records[0].spam_score == -1.21


def test_the_captured_submission_log_credits_both_accounts():
    records = _run(
        SmtpAssembler(store_subject=False, default_direction="out"), "submission.log"
    )
    assert len(records) == 2
    assert {m.auth_user for m in records} == {
        "info@example.com", "pc@example.net",
    }
    assert all(m.direction == "out" for m in records)
