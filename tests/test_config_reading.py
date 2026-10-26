import pathlib

from qmailstats.ingest import load_config


def test_a_percent_in_the_dsn_is_not_read_as_interpolation(tmp_path):
    """A URL-encoded password contains %XX escapes.

    ConfigParser interpolates % by default, so a password with an @ in it --
    encoded as %40 -- raises InterpolationSyntaxError before the DSN is ever
    parsed. Any password containing @ / # % or : would be unusable.
    """
    path = tmp_path / "config.ini"
    path.write_text(
        "[database]\n"
        "dsn = mysql://qmailstats:pass%40word%2Fhere@127.0.0.1:3306/qmailstats\n"
    )
    config = load_config(str(path))
    assert config.get("database", "dsn") == (
        "mysql://qmailstats:pass%40word%2Fhere@127.0.0.1:3306/qmailstats"
    )


def test_the_encoded_password_survives_all_the_way_to_the_connection(tmp_path):
    from urllib.parse import unquote, urlparse

    path = tmp_path / "config.ini"
    path.write_text(
        "[database]\ndsn = mysql://u:a%40b%25c%3Ad@127.0.0.1:3306/db\n")
    dsn = load_config(str(path)).get("database", "dsn")
    parts = urlparse(dsn)
    assert unquote(parts.password) == "a@b%c:d"
