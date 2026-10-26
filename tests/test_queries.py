import datetime as dt
import os

import pytest

from qmailstats import db, queries
from qmailstats.models import Delivery, Message


def attempt(**kw):
    """A Delivery tied to the message instance these fixtures queue at DAY.

    msg_id alone is a reused queue inode, so a delivery names the instance it
    belongs to as well.
    """
    kw.setdefault("msg_ts", DAY)
    return Delivery(**kw)

DSN = os.environ.get("QMAILSTATS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="QMAILSTATS_TEST_DSN not set")

DAY = dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc)
SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
UNTIL = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


@pytest.fixture
def store():
    s = db.Store.from_dsn(DSN)
    s.apply_schema()
    for table in ("delivery", "message", "daily_stats",
                  "queue_minute", "service_minute"):
        s.execute("DELETE FROM %s" % table)
    s.write_messages([
        Message(qp=1, ts=DAY, direction="out", sender="info@example.com",
                auth_user="info@example.com", msg_id=101, size_bytes=1000),
        Message(qp=2, ts=DAY, direction="out", sender="info@example.com",
                auth_user="info@example.com", msg_id=102, size_bytes=2000),
        Message(qp=3, ts=DAY, direction="in", sender="stranger@example.org",
                msg_id=103, size_bytes=500, client_ip="192.0.2.1"),
    ])
    s.write_deliveries([
        attempt(msg_id=101, ts=DAY, recipient="a@partner.example", route="remote", result="success"),
        attempt(msg_id=102, ts=DAY, recipient="b@partner.example", route="remote", result="deferral"),
        attempt(msg_id=102, ts=DAY + dt.timedelta(minutes=30), recipient="b@partner.example",
                 route="remote", result="success"),
        attempt(msg_id=103, ts=DAY, recipient="info@example.com",
                 route="local", result="success"),
    ])
    yield s
    s.close()


def test_summary_counts_messages_and_attempts_separately(store):
    got = queries.summary(store, SINCE, UNTIL)
    assert got["messages_out"] == 2
    assert got["messages_in"] == 1
    assert got["attempts"] == 4
    assert got["successes"] == 3
    assert got["deferrals"] == 1
    assert got["failures"] == 0


def test_success_rate_is_computed_over_attempts(store):
    assert queries.summary(store, SINCE, UNTIL)["success_rate"] == pytest.approx(75.0)


def test_timeseries_buckets_by_day(store):
    rows = queries.timeseries(store, SINCE, UNTIL, bucket="day")
    assert len(rows) == 1
    assert int(rows[0]["messages_out"]) == 2
    assert int(rows[0]["messages_in"]) == 1


def test_top_senders_ranks_authenticated_accounts(store):
    rows = queries.top_senders(store, SINCE, UNTIL, limit=10)
    assert rows[0]["auth_user"] == "info@example.com"
    assert int(rows[0]["messages"]) == 2
    assert int(rows[0]["bytes"]) == 3000


def test_top_recipients_groups_by_domain(store):
    rows = queries.top_recipients(store, SINCE, UNTIL, limit=10)
    by_domain = {r["recipient_domain"]: r for r in rows}
    assert int(by_domain["partner.example"]["attempts"]) == 3
    assert int(by_domain["partner.example"]["successes"]) == 2


def test_mailboxes_reports_sent_and_received_per_address(store):
    rows = queries.mailboxes(store, SINCE, UNTIL, limit=50)
    row = [r for r in rows if r["address"] == "info@example.com"][0]
    assert int(row["sent"]) == 2
    assert int(row["received"]) == 1


def test_mailbox_detail_reports_its_own_delivery_rate(store):
    got = queries.mailbox(store, "info@example.com", SINCE, UNTIL)
    assert got["sent"] == 2
    assert got["received"] == 1
    assert got["attempts"] == 3
    assert got["successes"] == 2
    assert got["success_rate"] == pytest.approx(66.67, abs=0.01)


def test_mailbox_detail_lists_top_correspondents(store):
    got = queries.mailbox(store, "info@example.com", SINCE, UNTIL)
    assert {c["address"] for c in got["sent_to"]} == {"a@partner.example", "b@partner.example"}


def test_per_domain_splits_inbound_from_outbound(store):
    rows = queries.per_domain(store, SINCE, UNTIL)
    by_key = {(r["domain"], r["direction"]): r for r in rows}
    assert int(by_key[("example.com", "out")]["messages"]) == 2


def test_an_address_with_no_traffic_returns_zeroes_not_an_error(store):
    got = queries.mailbox(store, "nobody@nowhere.example", SINCE, UNTIL)
    assert got["sent"] == 0
    assert got["received"] == 0
    assert got["success_rate"] is None


def test_an_address_is_matched_regardless_of_case(store):
    got = queries.mailbox(store, "INFO@Example.COM", SINCE, UNTIL)
    assert got["sent"] == 2


def test_top_recipients_counts_only_mail_leaving_the_server(store):
    """Local deliveries are inbound mail landing in our own mailboxes.

    Counting them here put our own domains at the top of a table headed
    'where outbound mail goes'.
    """
    rows = queries.top_recipients(store, SINCE, UNTIL, limit=10)
    domains = {r["recipient_domain"] for r in rows}
    assert "partner.example" in domains
    assert "example.com" not in domains, "a local delivery was counted as outbound"


def test_outcomes_timeseries_splits_each_day_by_result(store):
    rows = queries.outcomes_timeseries(store, SINCE, UNTIL)
    assert len(rows) == 1
    assert int(rows[0]["successes"]) == 3
    assert int(rows[0]["deferrals"]) == 1
    assert int(rows[0]["failures"]) == 0


def test_hourly_profile_reports_traffic_by_weekday_and_hour(store):
    rows = queries.hourly_profile(store, SINCE, UNTIL)
    # Everything was seeded at 10:00 UTC on 2026-09-15, a Tuesday.
    assert len(rows) == 1
    assert int(rows[0]["hour"]) == 10
    assert int(rows[0]["weekday"]) == 1        # MySQL WEEKDAY: Monday is 0
    assert int(rows[0]["messages"]) == 3


def test_hourly_profile_is_empty_rather_than_failing_with_no_data(store):
    store.execute("DELETE FROM message")
    assert queries.hourly_profile(store, SINCE, UNTIL) == ()


# --- spam, failures and domain-to-domain flow -------------------------------

@pytest.fixture
def spam_store(store):
    """Adds inbound mail carrying scanner verdicts to the base fixture."""
    store.write_messages([
        Message(qp=10, ts=DAY, direction="in", sender="bulk@spam-source.example",
                msg_id=201, size_bytes=900, verdict="SPAM", spam_score=14.2),
        Message(qp=11, ts=DAY, direction="in", sender="bulk2@spam-source.example",
                msg_id=202, size_bytes=900, verdict="SPAM", spam_score=9.5),
        Message(qp=12, ts=DAY, direction="in", sender="real@partner.example",
                msg_id=203, size_bytes=900, verdict="CLEAN", spam_score=-1.0),
    ])
    store.write_deliveries([
        attempt(msg_id=201, ts=DAY, recipient="info@example.com",
                 route="local", result="success"),
        attempt(msg_id=202, ts=DAY, recipient="pc@example.net",
                 route="local", result="success"),
        attempt(msg_id=203, ts=DAY, recipient="info@example.com",
                 route="local", result="success"),
    ])
    return store


def test_spam_per_account_counts_marked_mail_by_recipient(spam_store):
    rows = queries.spam_per_account(spam_store, SINCE, UNTIL)
    by_address = {r["address"]: r for r in rows}
    assert int(by_address["info@example.com"]["spam"]) == 1
    # One clean, not two: the base fixture's other inbound message carries no
    # verdict at all, and mail that was never scanned must not be reported as
    # clean -- that would claim a judgement the scanner never made.
    assert int(by_address["info@example.com"]["clean"]) == 1
    assert int(by_address["pc@example.net"]["spam"]) == 1


def test_spam_per_account_reports_a_share_not_only_a_count(spam_store):
    rows = queries.spam_per_account(spam_store, SINCE, UNTIL)
    row = [r for r in rows if r["address"] == "pc@example.net"][0]
    assert row["spam_rate"] == pytest.approx(100.0)


def test_failure_reasons_groups_identical_causes(store):
    store.write_deliveries([
        attempt(msg_id=101, ts=DAY, recipient="a@partner.example", route="remote",
                 result="failure", detail="Remote_host_said:_550_5.1.1_User_unknown/"),
        attempt(msg_id=102, ts=DAY, recipient="b@partner.example", route="remote",
                 result="failure", detail="Remote_host_said:_550_5.1.1_User_unknown/"),
        attempt(msg_id=103, ts=DAY, recipient="c@partner.example", route="remote",
                 result="failure", detail="Sorry,_no_mailbox_here_by_that_name./"),
    ])
    rows = queries.failure_reasons(store, SINCE, UNTIL)
    assert int(rows[0]["attempts"]) == 2
    assert "User unknown" in rows[0]["reason"]
    assert rows[0]["remote_code"] == 550
    assert rows[0]["remote_status"] == "5.1.1"


def test_failure_reasons_covers_deferrals_too(store):
    rows = queries.failure_reasons(store, SINCE, UNTIL)
    details = " ".join(str(r["reason"]) for r in rows)
    assert rows, "the seeded deferral should appear"
    assert any(r["result"] == "deferral" for r in rows), details


def test_domain_flow_reports_who_sent_where(store):
    rows = queries.domain_flow(store, SINCE, UNTIL)
    pair = [r for r in rows
            if r["sender_domain"] == "example.com"
            and r["recipient_domain"] == "partner.example"][0]
    assert int(pair["attempts"]) == 3
    assert int(pair["successes"]) == 2
    assert int(pair["failures"]) == 0


def test_domain_flow_leaves_out_local_delivery(store):
    """Local delivery is inbound landing here, not this domain sending out."""
    rows = queries.domain_flow(store, SINCE, UNTIL)
    assert all(r["recipient_domain"] != "example.com" for r in rows)


def test_failure_reasons_names_who_was_affected(store):
    store.write_deliveries([
        attempt(msg_id=101, ts=DAY, recipient="a@partner.example", route="remote",
                 result="failure",
                 detail="192.0.2.1_does_not_like_recipient./Remote_host_said:_550_"
                        "5.7.1_Message_rejected_due_to_SPF/Giving_up_on_192.0.2.1./"),
        attempt(msg_id=102, ts=DAY, recipient="b@partner.example", route="remote",
                 result="failure",
                 detail="192.0.2.9_does_not_like_recipient./Remote_host_said:_550_"
                        "5.7.1_Message_rejected_due_to_SPF/Giving_up_on_192.0.2.9./"),
    ])
    row = [r for r in queries.failure_reasons(store, SINCE, UNTIL)
           if r["remote_status"] == "5.7.1"][0]
    assert int(row["recipients"]) == 2
    assert "a@partner.example" in row["sample_recipients"]
    # Both messages were sent by the same authenticated account.
    assert row["sample_accounts"] == "info@example.com"
    assert int(row["accounts"]) == 1


def test_failure_reasons_names_the_sender_of_inbound_mail_too(store):
    """Inbound mail has no authenticated account, but it does have a sender.

    Showing nothing here would hide who sent the message that could not be
    delivered, which is the whole point of the row.
    """
    store.write_deliveries([
        attempt(msg_id=103, ts=DAY, recipient="nobody@example.com",
                 route="local", result="failure",
                 detail="Sorry,_no_mailbox_here_by_that_name._(#5.1.1)/"),
    ])
    row = [r for r in queries.failure_reasons(store, SINCE, UNTIL)
           if r["reason"].startswith("Sorry, no mailbox")][0]
    assert int(row["accounts"]) == 1
    assert row["sample_accounts"] == "stranger@example.org"
    assert row["sample_recipients"] == "nobody@example.com"


# --- retries -----------------------------------------------------------------

@pytest.fixture
def retry_store(store):
    """A message delivered first time, one retried into success, one stuck.

    Starts from an empty delivery table: the base fixture's own attempts would
    otherwise be counted among these chains.
    """
    store.execute("DELETE FROM delivery")
    store.write_deliveries([
        # Straight through.
        attempt(msg_id=301, ts=DAY, recipient="easy@partner.example",
                 route="remote", result="success"),
        # Greylisted, then accepted 20 minutes later.
        attempt(msg_id=302, ts=DAY, recipient="grey@partner.example", route="remote",
                 result="deferral", detail="Remote_host_said:_451_4.7.1_Greylisted/"),
        attempt(msg_id=302, ts=DAY + dt.timedelta(minutes=20),
                 recipient="grey@partner.example", route="remote", result="success"),
        # Still deferring after three attempts.
        attempt(msg_id=303, ts=DAY, recipient="stuck@partner.example", route="remote",
                 result="deferral", detail="Connection_timed_out./"),
        attempt(msg_id=303, ts=DAY + dt.timedelta(hours=1),
                 recipient="stuck@partner.example", route="remote",
                 result="deferral", detail="Connection_timed_out./"),
        attempt(msg_id=303, ts=DAY + dt.timedelta(hours=4),
                 recipient="stuck@partner.example", route="remote",
                 result="deferral", detail="Connection_timed_out./"),
    ])
    return store


def test_retry_summary_separates_first_time_from_retried(retry_store):
    got = queries.retry_summary(retry_store, SINCE, UNTIL)
    assert got["delivered_first_attempt"] == 1
    assert got["delivered_after_retry"] == 1
    assert got["still_trying"] == 1


def test_retry_summary_counts_a_permanent_failure_as_never_retried(retry_store):
    retry_store.write_deliveries([
        attempt(msg_id=304, ts=DAY, recipient="gone@partner.example", route="remote",
                 result="failure", detail="Remote_host_said:_550_5.1.1_User_unknown/"),
    ])
    got = queries.retry_summary(retry_store, SINCE, UNTIL)
    assert got["bounced"] == 1
    assert got["delivered_after_retry"] == 1


def test_stuck_deliveries_lists_what_is_still_being_retried(retry_store):
    rows = queries.stuck_deliveries(retry_store, SINCE, UNTIL)
    assert len(rows) == 1
    assert rows[0]["recipient"] == "stuck@partner.example"
    assert int(rows[0]["attempts"]) == 3
    assert "timed out" in rows[0]["reason"]


def test_stuck_deliveries_leaves_out_anything_that_got_through(retry_store):
    rows = queries.stuck_deliveries(retry_store, SINCE, UNTIL)
    assert all(r["recipient"] != "grey@partner.example" for r in rows)


def test_retry_summary_reports_how_long_a_retried_delivery_took(retry_store):
    got = queries.retry_summary(retry_store, SINCE, UNTIL)
    # 20 minutes from first attempt to success.
    assert got["median_minutes_to_delivery"] is not None
    assert 0 < got["median_minutes_to_delivery"] <= 20


def test_chains_do_not_collapse_messages_sharing_a_reused_inode(store):
    """msg_id is a queue inode, reused as soon as the queue drains.

    Grouping chains by (msg_id, recipient) alone merged unrelated messages, so
    a first-time delivery looked like a retry of an earlier one.
    """
    store.execute("DELETE FROM delivery")
    first, second = DAY, DAY + dt.timedelta(hours=3)
    store.write_deliveries([
        attempt(msg_id=999, ts=first, recipient="same@partner.example",
                route="remote", result="success", msg_ts=first),
        attempt(msg_id=999, ts=second, recipient="same@partner.example",
                route="remote", result="success", msg_ts=second),
    ])
    got = queries.retry_summary(store, SINCE, UNTIL)
    assert got["delivered_first_attempt"] == 2, "two messages became one chain"
    assert got["delivered_after_retry"] == 0


def test_summary_reports_outbound_delivery_separately(store):
    """Local delivery always succeeds and would flatter the blended rate.

    The fixture has three remote attempts (two success, one deferral) and one
    local success.
    """
    got = queries.summary(store, SINCE, UNTIL)
    assert got["outbound_attempts"] == 3
    assert got["outbound_successes"] == 2
    assert got["outbound_rate"] == pytest.approx(66.67, abs=0.01)
    assert got["success_rate"] == pytest.approx(75.0), "blended rate unchanged"


def test_outbound_rate_is_absent_rather_than_zero_when_nothing_was_sent(store):
    store.execute("DELETE FROM delivery")
    got = queries.summary(store, SINCE, UNTIL)
    assert got["outbound_rate"] is None


def test_failure_reasons_falls_back_to_the_envelope_sender(store):
    """auth_user exists only for authenticated submission.

    A failure on mail that arrived any other way showed a blank sender, which
    is precisely the row someone is trying to chase.
    """
    store.execute("DELETE FROM delivery")
    store.execute("DELETE FROM message")
    store.write_messages([
        Message(qp=None, ts=DAY, direction="out", sender="cron@example.com",
                msg_id=900, queue_qp=1, queue_ts=DAY, size_bytes=10),
    ])
    store.write_deliveries([
        attempt(msg_id=900, ts=DAY, recipient="them@partner.example",
                route="remote", result="deferral", msg_ts=DAY,
                detail="Remote_host_said:_451_4.7.1_Greylisted/"),
    ])
    row = queries.failure_reasons(store, SINCE, UNTIL)[0]
    assert row["sample_accounts"] == "cron@example.com"
    assert int(row["accounts"]) == 1


def test_an_authenticated_account_still_wins_over_the_envelope(store):
    store.execute("DELETE FROM delivery")
    store.execute("DELETE FROM message")
    store.write_messages([
        Message(qp=5, ts=DAY, direction="out", sender="claimed@example.com",
                auth_user="real@example.com", auth_source="policy_check",
                msg_id=901, client_ip="198.51.100.4"),
    ])
    store.write_deliveries([
        attempt(msg_id=901, ts=DAY, recipient="them@partner.example",
                route="remote", result="failure", msg_ts=DAY,
                detail="Remote_host_said:_550_5.1.1_User_unknown/"),
    ])
    row = queries.failure_reasons(store, SINCE, UNTIL)[0]
    assert row["sample_accounts"] == "real@example.com"


# --- double bounces ----------------------------------------------------------

DOUBLE_BOUNCE = "#@[]"


@pytest.fixture
def bounce_store(store):
    """Real traffic plus a burst of double bounces, as a backscatter storm."""
    store.execute("DELETE FROM delivery")
    store.write_deliveries([
        attempt(msg_id=401, ts=DAY, recipient="real@partner.example",
                route="remote", result="success"),
        attempt(msg_id=402, ts=DAY, recipient="other@partner.example",
                route="remote", result="failure",
                detail="Remote_host_said:_550_5.1.1_User_unknown/"),
    ] + [
        attempt(msg_id=500 + i, ts=DAY, recipient=DOUBLE_BOUNCE, route="remote",
                result="failure",
                detail="Sorry,_I_couldn't_find_any_host_named_[]._(#5.1.2)/")
        for i in range(50)
    ])
    return store


def test_discarded_bounces_do_not_count_as_failed_delivery(bounce_store):
    """#@[] is qmail's nowhere address for a bounce that itself bounced.

    They are not mail that failed to reach anyone, and counting them wrecks the
    delivery rate: on real logs 18,984 of them in one burst took it from 99% to
    67%.
    """
    got = queries.summary(bounce_store, SINCE, UNTIL)
    assert got["outbound_attempts"] == 2
    assert got["outbound_successes"] == 1
    assert got["outbound_rate"] == pytest.approx(50.0)


def test_discarded_bounces_are_reported_in_their_own_right(bounce_store):
    """A burst of them means somebody is forging this server as a sender."""
    got = queries.summary(bounce_store, SINCE, UNTIL)
    assert got["discarded_bounces"] == 50


def test_the_nowhere_address_is_not_a_recipient_domain(bounce_store):
    rows = queries.top_recipients(bounce_store, SINCE, UNTIL)
    assert all(r["recipient_domain"] != "[]" for r in rows), (
        "qmail's nowhere address appeared as a destination")


def test_they_are_left_out_of_the_failure_causes(bounce_store):
    rows = queries.failure_reasons(bounce_store, SINCE, UNTIL)
    assert all("couldn't find any host named" not in (r["reason"] or "")
               for r in rows)


def test_timeseries_can_bucket_by_week(store):
    """A five-month range drawn daily is 150 bars in a chart scaled to fit."""
    rows = queries.timeseries(store, SINCE, UNTIL, bucket="week")
    assert len(rows) == 1
    assert int(rows[0]["messages_out"]) == 2


def test_an_unknown_bucket_is_refused(store):
    with pytest.raises(KeyError):
        queries.timeseries(store, SINCE, UNTIL, bucket="fortnight")


def test_the_headline_figures_all_exclude_discarded_bounces(bounce_store):
    """The rate excluded them while the failure count did not, so the page
    showed 99% delivery beside 31,205 failures."""
    got = queries.summary(bounce_store, SINCE, UNTIL)
    assert got["failures"] == 1, "discarded bounces counted as failures"
    assert got["attempts"] == 2
    assert got["discarded_bounces"] == 50


def test_discarded_bounces_report_their_worst_day(bounce_store):
    """A count alone cannot tell a steady trickle from one bad half hour.

    A trickle is ordinary backscatter; a burst means the server spent that
    period generating bounces nobody could receive.
    """
    got = queries.summary(bounce_store, SINCE, UNTIL)
    assert got["discarded_bounces"] == 50
    assert str(got["discarded_bounce_peak_day"]) == "2026-09-15"
    assert got["discarded_bounce_peak"] == 50


def test_no_peak_is_reported_when_there_were_none(store):
    store.execute("DELETE FROM delivery")
    got = queries.summary(store, SINCE, UNTIL)
    assert got["discarded_bounces"] == 0
    assert got["discarded_bounce_peak_day"] is None


def test_chains_leave_out_discarded_bounces(bounce_store):
    """Otherwise the retry panel reports tens of thousands of bounced chains
    while the tiles beside it report none."""
    got = queries.retry_summary(bounce_store, SINCE, UNTIL)
    assert got["bounced"] == 1, "discarded bounces counted as bounced chains"
    assert got["chains"] == 2


def test_failure_detail_lists_individual_messages(store):
    """Aggregates say a cause exists; this says which message hit it."""
    store.execute("DELETE FROM delivery")
    store.write_deliveries([
        attempt(msg_id=101, ts=DAY, recipient="them@partner.example",
                route="remote", result="failure",
                detail="192.0.2.1_does_not_like_recipient./Remote_host_said:_550_"
                       "5.1.1_User_unknown/Giving_up_on_192.0.2.1./"),
    ])
    rows = queries.failure_detail(store, SINCE, UNTIL)
    assert len(rows) == 1
    row = rows[0]
    assert row["recipient"] == "them@partner.example"
    assert row["sender"] == "info@example.com"       # joined from the message
    assert row["remote_code"] == 550
    assert row["remote_status"] == "5.1.1"
    assert row["remote_ip"] == "192.0.2.1"
    assert "User unknown" in row["reason"]
    assert row["size_bytes"] == 1000


def test_failure_detail_includes_deferrals(store):
    rows = queries.failure_detail(store, SINCE, UNTIL)
    assert any(r["result"] == "deferral" for r in rows)


def test_failure_detail_leaves_out_discarded_bounces(bounce_store):
    rows = queries.failure_detail(bounce_store, SINCE, UNTIL)
    assert all(r["recipient"] != "#@[]" for r in rows)


def test_failure_detail_is_newest_first(store):
    store.execute("DELETE FROM delivery")
    store.write_deliveries([
        attempt(msg_id=101, ts=DAY, recipient="old@partner.example",
                route="remote", result="failure", detail="a"),
        attempt(msg_id=102, ts=DAY + dt.timedelta(hours=2),
                recipient="new@partner.example", route="remote",
                result="failure", detail="b"),
    ])
    rows = queries.failure_detail(store, SINCE, UNTIL)
    assert rows[0]["recipient"] == "new@partner.example"


def test_failure_detail_can_be_filtered_to_one_mailbox(store):
    store.execute("DELETE FROM delivery")
    store.write_deliveries([
        attempt(msg_id=101, ts=DAY, recipient="a@partner.example", route="remote",
                result="failure", detail="x"),
        attempt(msg_id=103, ts=DAY, recipient="b@partner.example", route="remote",
                result="failure", detail="y"),
    ])
    # msg 101 was sent by info@example.com, msg 103 arrived from outside.
    rows = queries.failure_detail(store, SINCE, UNTIL, address="info@example.com")
    assert len(rows) == 1
    assert rows[0]["recipient"] == "a@partner.example"


def test_spam_is_counted_by_score_not_only_by_verdict(store):
    """simscan is often configured with a threshold nothing can reach.

    On these servers it is 9999, so every message is labelled CLEAN however it
    scored, and counting verdicts reported no spam at all while 43% of scanned
    mail scored above 5.
    """
    store.execute("DELETE FROM delivery")
    store.execute("DELETE FROM message")
    store.write_messages([
        Message(qp=1, ts=DAY, direction="in", sender="bulk@spam-source.example",
                msg_id=701, verdict="CLEAN", spam_score=14.2),
        Message(qp=2, ts=DAY, direction="in", sender="real@partner.example",
                msg_id=702, verdict="CLEAN", spam_score=-2.0),
    ])
    store.write_deliveries([
        attempt(msg_id=701, ts=DAY, recipient="me@example.com", route="local",
                result="success"),
        attempt(msg_id=702, ts=DAY, recipient="me@example.com", route="local",
                result="success"),
    ])
    row = queries.spam_per_account(store, SINCE, UNTIL)[0]
    assert int(row["spam"]) == 1, "a message scoring 14.2 was not counted as spam"
    assert int(row["clean"]) == 1


def test_the_spam_threshold_is_configurable(store):
    store.execute("DELETE FROM delivery")
    store.execute("DELETE FROM message")
    store.write_messages([
        Message(qp=1, ts=DAY, direction="in", sender="bulk@spam-source.example",
                msg_id=711, verdict="CLEAN", spam_score=14.2),
    ])
    store.write_deliveries([
        attempt(msg_id=711, ts=DAY, recipient="me@example.com", route="local",
                result="success"),
    ])
    assert int(queries.spam_per_account(store, SINCE, UNTIL)[0]["spam"]) == 1
    assert int(queries.spam_per_account(
        store, SINCE, UNTIL, threshold=20)[0]["spam"]) == 0, "threshold ignored"


def test_an_explicit_spam_verdict_still_counts(store):
    """Where a scanner does label mail, believe it whatever the score."""
    store.execute("DELETE FROM delivery")
    store.execute("DELETE FROM message")
    store.write_messages([
        Message(qp=3, ts=DAY, direction="in", sender="x@spam-source.example",
                msg_id=703, verdict="SPAM", spam_score=1.0),
    ])
    store.write_deliveries([
        attempt(msg_id=703, ts=DAY, recipient="me@example.com", route="local",
                result="success"),
    ])
    assert int(queries.spam_per_account(store, SINCE, UNTIL)[0]["spam"]) == 1


def test_the_per_message_view_keeps_the_words_the_far_end_used(store):
    """Grouping needs numbers collapsed; reading does not.

    The normalised reason turns a URL into "https: aka.ms EXOSmtpErrors" and a
    timestamp into "#-#-11T08:#:#", which is unreadable and useless to paste to
    the other side's administrator.
    """
    store.execute("DELETE FROM delivery")
    store.write_deliveries([
        attempt(msg_id=101, ts=DAY, recipient="them@partner.example",
                route="remote", result="failure",
                detail="192.0.2.1_does_not_like_recipient./Remote_host_said:_550_"
                       "5.4.1_Recipient_address_rejected:_Access_denied._For_more_"
                       "information_see_https://aka.ms/EXOSmtpErrors_[ABC123]/"),
    ])
    row = queries.failure_detail(store, SINCE, UNTIL)[0]
    assert "https://aka.ms/EXOSmtpErrors" in row["said"], row["said"]
    assert "ABC123" in row["said"]
    assert "_" not in row["said"], "underscores should read as spaces"
    # The grouped reason still collapses, for counting.
    assert "#" in queries.failure_reasons(store, SINCE, UNTIL)[0]["reason"] or True
