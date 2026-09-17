from qmailstats.logsrc import files_in_order, read_from


def test_orders_rotations_oldest_first_with_current_last(tmp_path):
    (tmp_path / "@400000006a06aba92814d764.s").write_text("old\n")
    (tmp_path / "@400000006b06aba92814d764.s").write_text("newer\n")
    (tmp_path / "current").write_text("now\n")
    assert [p.name for p in files_in_order(tmp_path)] == [
        "@400000006a06aba92814d764.s", "@400000006b06aba92814d764.s", "current",
    ]


def test_ignores_multilog_lock_and_state_files(tmp_path):
    (tmp_path / "current").write_text("now\n")
    (tmp_path / "lock").write_text("")
    (tmp_path / "state").write_text("")
    assert [p.name for p in files_in_order(tmp_path)] == ["current"]


def test_reads_from_an_offset_and_reports_where_it_stopped(tmp_path):
    path = tmp_path / "current"
    path.write_text("one\ntwo\nthree\n")
    lines, offset = read_from(path, 0)
    assert lines == ["one", "two", "three"]
    assert offset == len("one\ntwo\nthree\n")
    path.write_text("one\ntwo\nthree\nfour\n")
    lines, offset = read_from(path, offset)
    assert lines == ["four"]


def test_does_not_return_a_partial_final_line(tmp_path):
    path = tmp_path / "current"
    path.write_bytes(b"complete\npartial-no-newline")
    lines, offset = read_from(path, 0)
    assert lines == ["complete"]
    assert offset == len("complete\n")


def test_starts_over_if_the_file_shrank(tmp_path):
    path = tmp_path / "current"
    path.write_text("a\nb\n")
    lines, offset = read_from(path, 1000)
    assert lines == ["a", "b"]
    assert offset == 4


def test_a_carriage_return_does_not_split_a_line(tmp_path):
    """SMTP transcripts contain CR: "220 Welcome ...\\r".

    str.splitlines() treats CR as a line break, so one logged line became two
    fragments and the half without a timestamp counted as malformed. On a
    server that logs protocol transcripts that was 164,986 phantom lines.
    """
    path = tmp_path / "current"
    path.write_bytes(b"@400000006aa8ff65289d164c server:[1]:220 Welcome ESMTP\r\n"
                     b"@400000006aa8ff6528a8fd2c tcpserver: end 1 status 0\n")
    lines, _ = read_from(path, 0)
    assert len(lines) == 2
    assert lines[0].endswith("ESMTP\r") or lines[0].endswith("ESMTP")
    assert lines[1].startswith("@400000006aa8ff6528a8fd2c")


def test_other_control_characters_do_not_split_lines_either(tmp_path):
    """splitlines() also breaks on form feed, vertical tab and \\x85."""
    path = tmp_path / "current"
    path.write_bytes(b"@400000006aa8ff65289d164c data \x0b \x0c \xc2\x85 more\n"
                     b"@400000006aa8ff6528a8fd2c second line\n")
    lines, _ = read_from(path, 0)
    assert len(lines) == 2, "a control character split a line"


def test_a_huge_file_is_read_in_bounded_pieces(tmp_path):
    """Dovecot logs reach gigabytes when nothing rotates them.

    Reading from the offset to end-of-file means one allocation the size of
    the remainder, so a 2.3 GB log would be read into memory in one go.
    """
    from qmailstats.logsrc import MAX_READ_BYTES, read_from

    path = tmp_path / "big.log"
    line = b"x" * 99 + b"\n"
    with path.open("wb") as handle:
        for _ in range(3000):
            handle.write(line)          # 300 KB

    lines, offset = read_from(path, 0, max_bytes=1000)
    assert offset <= 1000 + 100, "read past the cap"
    assert lines, "nothing came back"
    assert all(len(l) == 99 for l in lines), "a line was cut in half"

    # The rest arrives on subsequent passes rather than being lost.
    total = len(lines)
    while True:
        more, offset = read_from(path, offset, max_bytes=1000)
        if not more:
            break
        total += len(more)
    assert total == 3000, "lines were lost between passes"


def test_the_cap_has_a_sane_default():
    from qmailstats.logsrc import MAX_READ_BYTES
    assert 1_000_000 <= MAX_READ_BYTES <= 256_000_000
