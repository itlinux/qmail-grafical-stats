from qmailstats.assemble import SendAssembler, SmtpAssembler


def feed(assembler, lines):
    out = []
    for line in lines:
        out.extend(assembler.feed(line))
    return out


def test_keeps_the_high_water_mark_for_each_minute():
    a = SendAssembler()
    feed(a, [
        "@400000006aa8e01a2c3ca000 status: local 1/10 remote 2/60",
        "@400000006aa8e01a2c3ca001 status: local 5/10 remote 1/60",
        "@400000006aa8e01a2c3ca002 status: local 2/10 remote 9/60",
    ])
    minutes = a.minutes()
    assert len(minutes) == 1
    (sample,) = minutes.values()
    assert sample["local_max_used"] == 5
    assert sample["remote_max_used"] == 9
    assert sample["local_limit"] == 10


def test_separates_samples_that_fall_in_different_minutes():
    a = SendAssembler()
    feed(a, [
        "@400000006aa8e01a2c3ca000 status: local 1/10 remote 0/60",
        "@400000006aa8e11a2c3ca000 status: local 7/10 remote 0/60",
    ])
    assert len(a.minutes()) == 2


def test_counts_connections_and_messages_per_minute_per_service():
    a = SmtpAssembler(store_subject=False, service="smtp")
    feed(a, [
        "@400000006aa8e01718c73554 tcpserver: pid 1 from 192.0.2.33",
        "@400000006aa8e01718c83b0c tcpserver: ok 1 h:192.0.2.1:25 :192.0.2.33::5",
        "@400000006aa8e01718c83b0d tcpserver: status: 4/200",
        "@400000006aa8e01a2bdc4f44 mail recv: pid 1 from <a@sender.example> qp 9",
        "@400000006aa8e01a2bdc4f45 tcpserver: pid 2 from 192.0.2.44",
    ])
    samples = list(a.minutes().values())
    assert sum(s["connects"] for s in samples) == 2
    assert sum(s["messages"] for s in samples) == 1
    assert max(s["max_concurrency"] for s in samples) == 4


def test_a_minute_with_connections_and_no_mail_is_visible_as_such():
    a = SmtpAssembler(store_subject=False, service="submission")
    feed(a, [
        "@400000006aa8dd3f1b62b054 tcpserver: pid 9 from 203.0.113.26",
        "@400000006aa8dd3f1b64ee8c tcpserver: ok 9 h:192.0.2.1:587 :203.0.113.26::1",
    ])
    (sample,) = a.minutes().values()
    assert sample["connects"] == 1
    assert sample["messages"] == 0


def test_draining_holds_back_the_newest_minute_which_may_be_incomplete():
    a = SendAssembler()
    feed(a, [
        "@400000006aa8e01a2c3ca000 status: local 1/10 remote 0/60",
        "@400000006aa8e11a2c3ca000 status: local 7/10 remote 0/60",
    ])
    drained = a.drain_minutes()
    assert len(drained) == 1, "the newest minute should be kept for the next run"
    assert len(a.minutes()) == 1


def test_draining_everything_is_possible_when_the_log_is_finished():
    a = SendAssembler()
    feed(a, ["@400000006aa8e01a2c3ca000 status: local 1/10 remote 0/60"])
    assert len(a.drain_minutes(keep_last=False)) == 1
    assert a.minutes() == {}


def test_the_held_back_minute_survives_into_the_next_run():
    """Otherwise every run silently drops its newest minute of counters."""
    import json

    a = SendAssembler()
    feed(a, ["@400000006aa8e01a2c3ca000 status: local 3/10 remote 0/60"])
    a.drain_minutes()                       # holds this minute back
    carried = json.loads(json.dumps(a.state(), default=str))

    resumed = SendAssembler.restore(carried)
    feed(resumed, ["@400000006aa8e11a2c3ca000 status: local 1/10 remote 0/60"])
    drained = resumed.drain_minutes()
    assert len(drained) == 1, "the carried minute was lost between runs"
    assert list(drained.values())[0]["local_max_used"] == 3


def test_a_carried_minute_keeps_accumulating_rather_than_restarting():
    import json

    a = SmtpAssembler(store_subject=False, service="smtp")
    feed(a, ["@400000006aa8e01718c73554 tcpserver: pid 1 from 192.0.2.33"])
    a.drain_minutes()
    carried = json.loads(json.dumps(a.state(), default=str))

    resumed = SmtpAssembler.restore(carried, service="smtp")
    feed(resumed, ["@400000006aa8e01718c73555 tcpserver: pid 2 from 192.0.2.44"])
    (sample,) = resumed.minutes().values()
    assert sample["connects"] == 2, "the carried count restarted instead of continuing"
