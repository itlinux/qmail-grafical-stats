import datetime as dt

import pytest

from qmailstats.tai64 import decode


def test_decodes_a_real_qmail_timestamp():
    ts = decode("@400000006aa8e01a2c3cab3c")
    assert isinstance(ts, dt.datetime)
    assert ts.tzinfo == dt.timezone.utc
    assert ts.year == 2026
    assert ts.month == 9


def test_subtracts_the_ten_second_tai_offset():
    ts = decode("@400000000000000000000000")
    assert ts == dt.datetime(1969, 12, 31, 23, 59, 50, tzinfo=dt.timezone.utc)


def test_keeps_nanosecond_field_as_microseconds():
    ts = decode("@4000000000000000000f4240")
    assert ts.microsecond == 1000


def test_rejects_a_line_that_is_not_a_timestamp():
    with pytest.raises(ValueError):
        decode("status: local 1/10 remote 0/60")
