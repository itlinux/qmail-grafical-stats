"""The ingest lock must survive a lock file the run cannot write to."""

import os
import pathlib

import pytest

from qmailstats.ingest import take_lock


def test_creates_the_lock_and_holds_it(tmp_path):
    path = tmp_path / "sub" / "ingest.lock"
    handle = take_lock(path)
    assert handle is not None
    assert path.exists()
    handle.close()


def test_second_run_is_turned_away(tmp_path):
    path = tmp_path / "ingest.lock"
    first = take_lock(path)
    assert first is not None
    # A second attempt in this process sees its own flock, exactly as a second
    # timer firing while a backfill is still running would.
    assert take_lock(path) is None
    first.close()


def test_lock_left_behind_by_root_still_works(tmp_path):
    """The bug that stopped ingestion: a root-run backfill leaves the file
    owned by root, and the unprivileged timer then cannot open it for writing."""
    path = tmp_path / "ingest.lock"
    path.touch()
    path.chmod(0o444)
    if os.access(path, os.W_OK):  # running as root, which ignores the mode
        pytest.skip("test only means something as an unprivileged user")
    with pytest.raises(PermissionError):
        path.open("a")
    handle = take_lock(path)
    assert handle is not None
    handle.close()


def test_read_only_lock_still_excludes_a_second_run(tmp_path):
    path = tmp_path / "ingest.lock"
    path.touch()
    path.chmod(0o444)
    if os.access(path, os.W_OK):
        pytest.skip("test only means something as an unprivileged user")
    first = take_lock(path)
    assert first is not None
    assert take_lock(path) is None
    first.close()


def test_missing_lock_in_an_unwritable_directory_says_what_to_fix(tmp_path):
    """The overnight failure: the root-owned lock file in /var/lock was cleaned
    up, and the service user could neither create a new one nor read the old."""
    lockdir = tmp_path / "lock"
    lockdir.mkdir()
    lockdir.chmod(0o555)
    path = lockdir / "ingest.lock"
    if os.access(lockdir, os.W_OK):  # root ignores the mode
        pytest.skip("test only means something as an unprivileged user")

    with pytest.raises(PermissionError) as caught:
        take_lock(path)
    assert "RuntimeDirectory" in str(caught.value)


def test_lock_is_created_under_a_writable_runtime_directory(tmp_path):
    """What systemd's RuntimeDirectory= gives us: a directory we own."""
    path = tmp_path / "qmail-grafical-stats" / "ingest.lock"
    handle = take_lock(path)
    assert handle is not None
    assert path.exists()
    handle.close()


def test_default_lock_path_is_the_runtime_directory():
    import inspect

    from qmailstats import ingest

    source = inspect.getsource(ingest.main)
    assert "/run/qmail-grafical-stats/ingest.lock" in source
    assert 'fallback="/var/lock' not in source
