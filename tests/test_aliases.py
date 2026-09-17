import configparser

from qmailstats.queries import aliases_from_config, fold_aliases


def test_reads_an_alias_map_from_configuration():
    config = configparser.ConfigParser()
    config.read_dict({"domains": {
        "aliases": "school-alias.example:school.example, training-alias.example:training.example",
    }})
    assert aliases_from_config(config) == {
        "school-alias.example": "school.example",
        "training-alias.example": "training.example",
    }


def test_no_configuration_means_no_folding():
    config = configparser.ConfigParser()
    config.read_dict({"domains": {}})
    assert aliases_from_config(config) == {}


def test_merges_rows_that_belong_to_the_same_customer():
    rows = [
        {"domain": "school.example", "direction": "out", "messages": 10, "bytes": 100},
        {"domain": "school-alias.example", "direction": "out", "messages": 4, "bytes": 40},
        {"domain": "velvet.example", "direction": "out", "messages": 7, "bytes": 70},
    ]
    folded = fold_aliases(rows, {"school-alias.example": "school.example"},
                          key="domain", group=("domain", "direction"))
    by_domain = {r["domain"]: r for r in folded}
    assert by_domain["school.example"]["messages"] == 14
    assert by_domain["school.example"]["bytes"] == 140
    assert "school-alias.example" not in by_domain
    assert by_domain["velvet.example"]["messages"] == 7


def test_keeps_directions_apart_when_merging():
    rows = [
        {"domain": "school.example", "direction": "in", "messages": 3},
        {"domain": "school-alias.example", "direction": "out", "messages": 5},
    ]
    folded = fold_aliases(rows, {"school-alias.example": "school.example"},
                          key="domain", group=("domain", "direction"))
    assert len(folded) == 2


def test_leaves_non_numeric_fields_from_the_first_row():
    rows = [
        {"domain": "school.example", "last_seen": "2026-09-15", "messages": 1},
        {"domain": "school-alias.example", "last_seen": "2026-09-10", "messages": 2},
    ]
    folded = fold_aliases(rows, {"school-alias.example": "school.example"},
                          key="domain", group=("domain",))
    assert folded[0]["last_seen"] == "2026-09-15"
    assert folded[0]["messages"] == 3


def test_an_empty_map_returns_the_rows_untouched():
    rows = [{"domain": "a.example", "messages": 1}]
    assert fold_aliases(rows, {}, key="domain", group=("domain",)) == rows


def test_folds_the_recipient_side_too():
    rows = [
        {"sender_domain": "owner.example", "recipient_domain": "school.example", "attempts": 2},
        {"sender_domain": "owner.example", "recipient_domain": "school-alias.example", "attempts": 3},
    ]
    folded = fold_aliases(rows, {"school-alias.example": "school.example"},
                          key="recipient_domain",
                          group=("sender_domain", "recipient_domain"))
    assert len(folded) == 1
    assert folded[0]["attempts"] == 5
