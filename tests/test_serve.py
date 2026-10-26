import datetime as dt

import pytest

from qmailstats.serve import parse_range, route


def test_defaults_to_the_last_seven_days():
    since, until = parse_range({})
    assert (until - since).days == 7


def test_accepts_an_explicit_range():
    since, until = parse_range({"since": ["2026-09-01"], "until": ["2026-09-08"]})
    assert since == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    assert until == dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)


def test_rejects_a_malformed_date_instead_of_passing_it_to_sql():
    with pytest.raises(ValueError):
        parse_range({"since": ["2026-09-01'; DROP TABLE message; --"]})


def test_rejects_a_range_that_runs_backwards():
    with pytest.raises(ValueError):
        parse_range({"since": ["2026-09-08"], "until": ["2026-09-01"]})


def test_an_unknown_path_is_not_routed():
    assert route("/api/nonsense") is None


def test_every_advertised_endpoint_routes():
    for path in (
        "/api/summary", "/api/timeseries", "/api/top-senders",
        "/api/top-recipients", "/api/per-domain", "/api/mailboxes",
        "/api/mailbox", "/api/outcomes", "/api/hourly", "/api/spam", "/api/failures",
        "/api/domain-flow", "/api/concurrency", "/api/connections",
        "/api/access", "/api/mailbox-access", "/api/auth-failures",
        "/api/retries", "/api/stuck", "/api/failure-detail",
    ):
        assert route(path) is not None, path


def test_the_page_asks_for_its_data_by_relative_path():
    """It must work mounted under a prefix such as /stats.

    An absolute /api/... would escape the prefix and hit the web server's root,
    where the proxy does not listen and authentication does not apply.
    """
    import pathlib
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "static" / "index.html").read_text()
    assert 'fetch("/api' not in page
    assert '"/api/' not in page, "an absolute API path would escape the prefix"


def test_the_page_refreshes_itself():
    """A dashboard nobody reloads shows the moment it was opened."""
    import pathlib
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "static" / "index.html").read_text()
    assert "setInterval" in page, "nothing schedules a refresh"
    assert "REFRESH_SECONDS" in page


def test_refreshing_can_be_paused():
    """A reader comparing two rows should not have them move underneath."""
    import pathlib
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "static" / "index.html").read_text()
    assert 'id="autorefresh"' in page


def test_the_page_can_hide_addresses():
    """For screenshots and demos: real figures, no real identities."""
    import pathlib
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "static" / "index.html").read_text()
    assert 'id="redact"' in page
    assert "function pseudonym" in page
    # Consistent: the same address must always map to the same stand-in, or
    # the tables stop agreeing with each other.
    assert "pseudonyms" in page


def test_the_view_can_be_set_from_the_url():
    """So a range is bookmarkable, and a screenshot can be reproduced."""
    import pathlib
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "static" / "index.html").read_text()
    assert "URLSearchParams" in page


def test_alerts_are_served_and_shown():
    import pathlib
    from qmailstats.serve import route
    assert route("/api/alerts") is not None
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "static" / "index.html").read_text()
    assert 'id="alerts"' in page
