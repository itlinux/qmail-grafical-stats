import datetime as dt

from qmailstats.models import Delivery, Message


def test_a_message_derives_its_sender_domain():
    msg = Message(qp=1, ts=dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc),
                  direction="out", sender="Info@Example.com")
    assert msg.sender_domain == "example.com"


def test_a_bounce_has_no_sender_domain():
    msg = Message(qp=1, ts=dt.datetime.now(dt.timezone.utc), direction="in", sender="")
    assert msg.sender_domain is None


def test_a_delivery_derives_its_recipient_domain():
    d = Delivery(msg_id=5, ts=dt.datetime.now(dt.timezone.utc),
                 recipient="Export@Example.ORG", route="local", result="success")
    assert d.recipient_domain == "example.org"


def test_addresses_are_stored_lowercased():
    msg = Message(qp=1, ts=dt.datetime.now(dt.timezone.utc), direction="out",
                  sender="Info@Example.com", auth_user="Info@Example.com")
    assert msg.sender == "info@example.com"
    assert msg.auth_user == "info@example.com"


def test_a_failed_delivery_pulls_the_cause_out_of_its_detail():
    d = Delivery(
        msg_id=1, ts=dt.datetime.now(dt.timezone.utc), recipient="x@dest.example",
        route="remote", result="deferral",
        detail=("198.51.100.175_does_not_like_recipient./Remote_host_said:_451_"
                "4.7.1_IP_Reputation_Greylisted/Giving_up_on_198.51.100.175./"),
    )
    assert d.remote_ip == "198.51.100.175"
    assert d.remote_code == 451
    assert d.remote_status == "4.7.1"
    assert "Greylisted" in d.reason


def test_a_successful_delivery_invents_no_cause():
    d = Delivery(msg_id=1, ts=dt.datetime.now(dt.timezone.utc), recipient="x@dest.example",
                 route="remote", result="success", detail="did_1+0+0/")
    assert d.reason is None
    assert d.remote_code is None


def test_a_vpopmail_local_recipient_is_reduced_to_the_real_mailbox():
    """qmail-send writes local deliveries as 'domain-user@domain'.

    Left alone, one mailbox appears twice: under the prefixed form when it
    receives, and under its real address when it sends.
    """
    d = Delivery(msg_id=1, ts=dt.datetime.now(dt.timezone.utc),
                 recipient="example.org-export@example.org",
                 route="local", result="success")
    assert d.recipient == "export@example.org"


def test_the_prefix_is_only_stripped_when_it_matches_the_domain():
    """A local part that merely contains a hyphen must survive intact."""
    d = Delivery(msg_id=1, ts=dt.datetime.now(dt.timezone.utc),
                 recipient="anna-maria@example.net",
                 route="local", result="success")
    assert d.recipient == "anna-maria@example.net"


def test_a_prefix_from_a_different_domain_is_left_alone():
    d = Delivery(msg_id=1, ts=dt.datetime.now(dt.timezone.utc),
                 recipient="othersite.example-export@example.org",
                 route="local", result="success")
    assert d.recipient == "othersite.example-export@example.org"


def test_a_remote_recipient_is_never_rewritten():
    d = Delivery(msg_id=1, ts=dt.datetime.now(dt.timezone.utc),
                 recipient="partner.example-sales@partner.example",
                 route="remote", result="success")
    assert d.recipient == "partner.example-sales@partner.example"
