from qmailstats.remote import parse_detail


def test_reads_a_real_greylisting_deferral():
    # Taken from a production qmail log, with the addresses replaced.
    got = parse_detail(
        "198.51.100.175_does_not_like_recipient./Remote_host_said:_451_4.7.1_"
        "IP_Reputation_GreylistedGreylisted_-_Riprova_tra_5_minuti/"
        "Giving_up_on_198.51.100.175./"
    )
    assert got["remote_ip"] == "198.51.100.175"
    assert got["code"] == 451
    assert got["status"] == "4.7.1"
    assert "Greylisted" in got["reason"]


def test_strips_the_remote_address_out_of_the_reason():
    """The address varies per message; leaving it in makes every cause unique."""
    first = parse_detail(
        "192.0.2.1_does_not_like_recipient./Remote_host_said:_550_5.1.1_User_unknown/"
        "Giving_up_on_192.0.2.1./"
    )
    second = parse_detail(
        "198.51.100.6_does_not_like_recipient./Remote_host_said:_550_5.1.1_User_unknown/"
        "Giving_up_on_198.51.100.6./"
    )
    assert first["reason"] == second["reason"]
    assert first["remote_ip"] != second["remote_ip"]


def test_reads_a_reason_with_no_remote_response_at_all():
    got = parse_detail("Sorry,_I_wasn't_able_to_establish_an_SMTP_connection./")
    assert got["code"] is None
    assert got["status"] is None
    assert "establish an SMTP connection" in got["reason"]


def test_reads_a_local_delivery_failure():
    got = parse_detail("Sorry,_no_mailbox_here_by_that_name./")
    assert got["code"] is None
    assert got["reason"] == "Sorry, no mailbox here by that name."


def test_recognises_an_spf_rejection_by_its_enhanced_status():
    got = parse_detail(
        "192.0.2.1_does_not_like_recipient./Remote_host_said:_550_5.7.1_"
        "Message_rejected_due_to_SPF/Giving_up_on_192.0.2.1./"
    )
    assert got["code"] == 550
    assert got["status"] == "5.7.1"
    assert "SPF" in got["reason"]


def test_reads_a_success_detail_without_inventing_a_reason():
    got = parse_detail("did_0+0+1/")
    assert got["code"] is None
    assert got["remote_ip"] is None


def test_underscores_become_spaces_so_the_table_is_readable():
    got = parse_detail("Remote_host_said:_550_5.1.1_User_unknown/")
    assert "_" not in got["reason"]


def test_collapses_numbers_that_vary_between_otherwise_identical_reasons():
    """Retry counters and queue ids differ per message; the cause does not."""
    first = parse_detail("Remote_host_said:_452_4.2.2_Mailbox_full_try_again_in_37_minutes/")
    second = parse_detail("Remote_host_said:_452_4.2.2_Mailbox_full_try_again_in_92_minutes/")
    assert first["reason"] == second["reason"]


def test_survives_an_empty_or_missing_detail():
    assert parse_detail("")["reason"] == ""
    assert parse_detail(None)["reason"] == ""


def test_reads_a_multi_digit_enhanced_status():
    """RFC 3463 allows up to three digits per part; DMARC rejections use 5.7.26."""
    got = parse_detail(
        "192.0.2.1_does_not_like_recipient./Remote_host_said:_550_5.7.26_"
        "Unauthenticated_email_from_domain_is_not_accepted_due_to_DMARC_policy/"
        "Giving_up_on_192.0.2.1./"
    )
    assert got["status"] == "5.7.26"
    assert not got["reason"].startswith("#"), "a digit was left behind in the reason"
    assert "DMARC" in got["reason"]


def test_reads_qmails_own_status_code_from_a_local_failure():
    got = parse_detail("Sorry,_no_mailbox_here_by_that_name._(#5.1.1)/")
    assert got["status"] == "5.1.1"
    assert got["reason"] == "Sorry, no mailbox here by that name."


def test_does_not_mangle_a_status_code_into_hashes():
    got = parse_detail("Sorry,_no_mailbox_here_by_that_name._(#5.1.1)/")
    assert "#" not in got["reason"]
