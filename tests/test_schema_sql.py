import pathlib

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "schema.sql"


def test_defines_every_table_the_code_uses():
    sql = SCHEMA.read_text().lower()
    for table in ("message", "delivery", "checkpoint", "parser_state",
                  "daily_stats", "queue_minute", "service_minute",
                  "auth_event"):
        assert "create table if not exists `%s`" % table in sql


def test_each_log_identifies_a_message_its_own_way():
    """The two logs use different process ids, so each side has its own key."""
    sql = SCHEMA.read_text().lower()
    assert "unique key `uq_message_smtp` (`qp`, `ts`)" in sql
    assert "unique key `uq_message_queue` (`queue_qp`, `queue_ts`)" in sql


def test_a_delivery_is_tied_to_one_instance_of_a_reused_inode():
    sql = SCHEMA.read_text().lower()
    assert "`msg_id`, `msg_ts`, `ts`, `recipient`, `result`" in sql


def test_delivery_is_unique_on_its_natural_key():
    assert "unique key `uq_delivery`" in SCHEMA.read_text().lower()


def test_indexes_exist_for_the_dashboard_queries():
    sql = SCHEMA.read_text().lower()
    for index in (
        "key `ix_message_ts`", "key `ix_message_sender_domain_ts`",
        "key `ix_message_auth_user_ts`", "key `ix_delivery_ts`",
        "key `ix_delivery_recipient_domain_ts`", "key `ix_delivery_recipient_ts`",
        "key `ix_delivery_msg_id`",
    ):
        assert index in sql, index


def test_uses_utf8mb4_so_subjects_and_addresses_survive():
    assert SCHEMA.read_text().lower().count("utf8mb4") >= 5


def test_statements_survive_a_semicolon_inside_a_comment():
    """apply_schema splits on ';', so a comment containing one cut a CREATE
    TABLE in half and the half-statement was sent to MySQL."""
    from qmailstats.db import split_statements

    sql = (
        "CREATE TABLE a (\n"
        "  -- an inode is reused; the timestamp says which instance\n"
        "  id INT\n"
        ");\n"
        "CREATE TABLE b (id INT);\n"
    )
    statements = split_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
    assert "CREATE TABLE b" in statements[1]


def test_no_statement_comes_back_empty():
    from qmailstats.db import split_statements
    assert all(s.strip() for s in split_statements(SCHEMA.read_text()))
