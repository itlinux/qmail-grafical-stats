"""Which end of the conversation a failure belongs to."""

import pytest

from qmailstats.queries import failure_side


@pytest.mark.parametrize("code,said,expected", [
    # The Exchange Online rejection that prompted this: the recipient address
    # was refused, nothing to do with the sending server.
    (550, "550 5.4.1 Recipient address rejected: Access denied. "
          "For more information see https://aka.ms/EXOSmtpErrors", "their side"),
    (550, "550 5.1.1 Invalid Recipient <x@libero.it>", "their side"),
    (552, "552 5.2.2 Mailbox full", "their side"),
    # 5.7.x is policy -- SPF, DKIM, reputation, an explicit block -- and is the
    # one class worth looking at our own configuration for.
    (550, "550 5.7.1 Message rejected due to SPF", "check ours"),
    (554, "554 5.7.1 Service unavailable; client blocked", "check ours"),
])
def test_permanent_failures_are_attributed(code, said, expected):
    assert failure_side("failure", code, said) == expected


def test_greylisting_is_temporary_not_a_fault():
    said = "451 4.7.1 IP Reputation Greylisted - Riprova tra 5 minuti"
    assert failure_side("deferral", 451, said) == "temporary"
    # Even read as a failure row, a 4xx code is still temporary.
    assert failure_side("failure", 451, said) == "temporary"


def test_missing_or_unreadable_answers_do_not_guess():
    assert failure_side("failure", None, None) == "unknown"
    assert failure_side("failure", None, "connection timed out") == "unknown"


def test_a_bare_5xx_without_an_enhanced_code_is_still_the_far_end():
    assert failure_side("failure", 550, "550 no such user here") == "their side"


def test_a_pec_address_refusing_ordinary_mail_is_their_side():
    """Italian certified-mail addresses answer 5.7.1, but the cause is that the
    recipient does not accept ordinary mail -- nothing to change here."""
    said = ("554 5.7.1 <suap@pec.comune.example.it>: Recipient address "
            "rejected: Messaggio rifiutato dal sistema. Indirizzo destinatario "
            "sconosciuto o non abilitato alla ricezione di posta non certificata.")
    assert failure_side("failure", 554, said) == "their side"


def test_a_real_policy_block_still_points_at_us():
    assert failure_side("failure", 550,
                        "550 5.7.1 Message rejected due to SPF") == "check ours"
    assert failure_side("failure", 554,
                        "554 5.7.1 Service unavailable; client blocked") == "check ours"


def test_a_full_mailbox_of_ours_is_our_side():
    assert failure_side("failure", None, "user is over quota", "local") == "our side"
    assert failure_side("failure", None, "user is over quota") == "our side"


def test_local_delivery_failures_are_ours():
    assert failure_side("failure", None, "no mailbox here by that name",
                        "local") == "our side"


def test_block_state_reads_the_last_action():
    from qmailstats.queries import block_state

    assert block_state("ban") == "blocked"
    # An expired ban is not the same as never having been blocked.
    assert block_state("unban") == "ban expired"
    assert block_state(None) == "not blocked"
