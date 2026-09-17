import gzip

from qmailstats.logsrc import read_from, syslog_files_in_order


def test_orders_rotations_oldest_first_with_the_live_file_last(tmp_path):
    (tmp_path / "dovecot.log").write_text("live\n")
    (tmp_path / "dovecot.log-20260906.gz").write_bytes(gzip.compress(b"older\n"))
    (tmp_path / "dovecot.log-20260913.gz").write_bytes(gzip.compress(b"newer\n"))
    (tmp_path / "unrelated.log").write_text("no\n")
    names = [p.name for p in syslog_files_in_order(tmp_path / "dovecot.log")]
    assert names == [
        "dovecot.log-20260906.gz", "dovecot.log-20260913.gz", "dovecot.log",
    ]


def test_reads_a_compressed_rotation_whole(tmp_path):
    path = tmp_path / "dovecot.log-20260906.gz"
    path.write_bytes(gzip.compress(b"one\ntwo\n"))
    lines, offset = read_from(path, 0)
    assert lines == ["one", "two"]
    # A compressed file is read in full or not at all; there is no meaningful
    # byte offset into the decompressed stream to resume from.
    assert offset > 0


def test_a_compressed_file_already_read_is_not_read_again(tmp_path):
    path = tmp_path / "dovecot.log-20260906.gz"
    path.write_bytes(gzip.compress(b"one\ntwo\n"))
    _, offset = read_from(path, 0)
    lines, _ = read_from(path, offset)
    assert lines == []


def test_a_plain_syslog_file_still_resumes_by_byte_offset(tmp_path):
    path = tmp_path / "dovecot.log"
    path.write_text("one\n")
    _, offset = read_from(path, 0)
    with path.open("a") as handle:
        handle.write("two\n")
    lines, _ = read_from(path, offset)
    assert lines == ["two"]
