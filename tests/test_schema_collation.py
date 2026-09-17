"""Every table must share one collation.

A table created without an explicit COLLATE picks up the server's default,
which on MySQL 8 is utf8mb4_0900_ai_ci while this schema uses
utf8mb4_unicode_ci. Joining a column from each then fails outright with
"Illegal mix of collations", and it fails at query time on a real server
rather than anywhere a test would normally look.
"""

import pathlib
import re

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"


def test_every_table_declares_the_same_collation():
    sql = SCHEMA.read_text()
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS\s+`?(\w+)`?", sql)
    endings = re.findall(r"\)\s*ENGINE=InnoDB[^;]*;", sql)
    assert len(tables) == len(endings), (
        "every CREATE TABLE should end in an ENGINE clause; "
        "found %d tables and %d endings" % (len(tables), len(endings))
    )
    wrong = [
        name for name, ending in zip(tables, endings)
        if "COLLATE=utf8mb4_unicode_ci" not in ending
    ]
    assert not wrong, "tables without an explicit collation: %s" % ", ".join(wrong)
