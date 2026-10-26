"""Walk a log directory in order and read from a byte offset.

Two shapes are handled: multilog directories, where rotations are named after
their timestamp and the live file is `current`; and syslog files, where
rotations sit beside the live file and are usually gzipped.
"""

import gzip

IGNORED = {"lock", "state"}

# Most of a read at once, per pass. Multilog files are capped around a
# megabyte, but a syslog file nothing rotates reaches gigabytes -- and reading
# from the checkpoint to end-of-file is a single allocation that size. The
# remainder is picked up on the next pass, so progress is bounded rather than
# lost.
MAX_READ_BYTES = 64 * 1024 * 1024


def _lines(text):
    """Split on newlines only.

    Not str.splitlines(): it also breaks on carriage return, form feed,
    vertical tab and \x85, all of which appear in SMTP protocol transcripts.
    A logged line containing one became two fragments, and the fragment without
    a timestamp was counted as malformed -- 164,986 of them on a server that
    logs transcripts alongside its connections.
    """
    return [line for line in text.split("\n") if line]


def files_in_order(directory):
    """Rotations oldest first (their names sort chronologically), then current."""
    rotated = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.name.startswith("@") and p.suffix == ".s"
    )
    current = directory / "current"
    if current.exists():
        rotated.append(current)
    return [p for p in rotated if p.name not in IGNORED]


def syslog_files_in_order(live_path):
    """Rotations of one syslog file, oldest first, with the live file last.

    logrotate names them after the date they were closed, so a plain sort puts
    them in order.
    """
    directory = live_path.parent
    prefix = live_path.name + "-"
    rotations = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.name.startswith(prefix)
    )
    if live_path.exists():
        rotations.append(live_path)
    return rotations


def read_from(path, offset, max_bytes=MAX_READ_BYTES):
    """Return (complete lines after offset, new offset).

    A trailing line with no newline is left for the next run -- multilog may
    still be writing it. If the file is shorter than the offset it was
    replaced, so start again from the beginning.
    """
    if path.suffix == ".gz":
        # A gzip member cannot be resumed from a byte offset into its
        # decompressed stream, and a rotation never grows, so it is read once
        # in full and then skipped by a non-zero checkpoint.
        if offset:
            return [], offset
        with gzip.open(path, "rb") as handle:
            data = handle.read()
        text = data.decode("utf-8", errors="replace")
        return _lines(text), max(1, len(data))

    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(max_bytes)
    if not data:
        return [], offset
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return [], offset
    usable = data[: last_newline + 1]
    text = usable.decode("utf-8", errors="replace")
    return _lines(text), offset + len(usable)
