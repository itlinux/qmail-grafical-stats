from qmailstats.assemble import SendAssembler


def feed(assembler, lines):
    out = []
    for line in lines:
        out.extend(assembler.feed(line))
    return out


def test_emits_a_message_and_its_successful_delivery():
    a = SendAssembler()
    records = feed(a, [
        "@400000006aa8e01a2c3ca000 info msg 68566592: bytes 4523 from <a@sender.example> qp 210429 uid 89",
        "@400000006aa8e01a2c3cab3c starting delivery 759: msg 68566592 to local export@example.org",
        "@400000006aa8e01a2cf3d5dc delivery 759: success: did_0+0+1/",
        "@400000006aa8e01a2cf4f304 end msg 68566592",
    ])
    messages = [r for r in records if r.__class__.__name__ == "Message"]
    deliveries = [r for r in records if r.__class__.__name__ == "Delivery"]
    assert len(messages) == 1
    assert messages[0].msg_id == 68566592
    # The queue log's qp is its own process id, not the SMTP session's.
    assert messages[0].queue_qp == 210429
    assert messages[0].qp is None
    assert messages[0].sender == "a@sender.example"
    assert messages[0].size_bytes == 4523
    assert len(deliveries) == 1
    assert deliveries[0].msg_id == 68566592
    assert deliveries[0].recipient == "export@example.org"
    assert deliveries[0].result == "success"
    assert deliveries[0].route == "local"


def test_counts_a_retry_as_two_attempts():
    a = SendAssembler()
    records = feed(a, [
        "@400000006aa8e01a2c3ca000 info msg 100: bytes 10 from <a@sender.example> qp 1 uid 89",
        "@400000006aa8e01a2c3ca001 starting delivery 1: msg 100 to remote x@dest.example",
        "@400000006aa8e01a2c3ca002 delivery 1: deferral: Connection_timed_out./",
        "@400000006aa8e01a2c3ca003 starting delivery 2: msg 100 to remote x@dest.example",
        "@400000006aa8e01a2c3ca004 delivery 2: success: did_1+0+0/",
    ])
    deliveries = [r for r in records if r.__class__.__name__ == "Delivery"]
    assert [d.result for d in deliveries] == ["deferral", "success"]
    assert all(d.recipient == "x@dest.example" for d in deliveries)


def test_handles_a_message_with_several_recipients():
    a = SendAssembler()
    records = feed(a, [
        "@400000006aa8e01a2c3ca000 info msg 100: bytes 10 from <a@sender.example> qp 1 uid 89",
        "@400000006aa8e01a2c3ca001 starting delivery 1: msg 100 to remote x@dest.example",
        "@400000006aa8e01a2c3ca002 starting delivery 2: msg 100 to remote z@dest.example",
        "@400000006aa8e01a2c3ca003 delivery 2: failure: No_such_address./",
        "@400000006aa8e01a2c3ca004 delivery 1: success: did_1+0+0/",
    ])
    deliveries = [r for r in records if r.__class__.__name__ == "Delivery"]
    assert {(d.recipient, d.result) for d in deliveries} == {
        ("x@dest.example", "success"), ("z@dest.example", "failure"),
    }


def test_an_outcome_for_an_unknown_delivery_is_dropped_not_crashed():
    a = SendAssembler()
    records = feed(a, ["@400000006aa8e01a2c3ca000 delivery 999: success: did_0+0+1/"])
    assert records == []
    assert a.unmatched == 1


def test_state_survives_being_carried_across_a_file_boundary():
    a = SendAssembler()
    feed(a, [
        "@400000006aa8e01a2c3ca000 info msg 100: bytes 10 from <a@sender.example> qp 1 uid 89",
        "@400000006aa8e01a2c3ca001 starting delivery 1: msg 100 to remote x@dest.example",
    ])
    resumed = SendAssembler.restore(a.state())
    records = feed(resumed, ["@400000006aa8e01a2c3ca002 delivery 1: success: did_1+0+0/"])
    deliveries = [r for r in records if r.__class__.__name__ == "Delivery"]
    assert len(deliveries) == 1
    assert deliveries[0].recipient == "x@dest.example"


def test_ignores_lines_with_no_timestamp_rather_than_failing():
    a = SendAssembler()
    assert a.feed("garbage without a label") == []
    assert a.malformed == 1


def test_state_survives_a_round_trip_through_json():
    """Carried state is stored as JSON, whose object keys are always strings."""
    import json

    a = SendAssembler()
    feed(a, [
        "@400000006aa8e01a2c3ca000 info msg 100: bytes 10 from <a@sender.example> qp 1 uid 89",
        "@400000006aa8e01a2c3ca001 starting delivery 1: msg 100 to remote x@dest.example",
    ])
    carried = json.loads(json.dumps(a.state(), default=str))

    resumed = SendAssembler.restore(carried)
    records = feed(resumed, ["@400000006aa8e01a2c3ca002 delivery 1: success: did_1+0+0/"])
    deliveries = [r for r in records if r.__class__.__name__ == "Delivery"]
    assert len(deliveries) == 1, "the delivery was lost crossing a run boundary"
    assert deliveries[0].recipient == "x@dest.example"
