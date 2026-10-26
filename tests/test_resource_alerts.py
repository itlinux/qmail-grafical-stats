"""Alerts for the resources this tool can itself exhaust."""

from qmailstats.alerts import disk_pressure, oversized_state


class StateStore:
    def __init__(self, sizes):
        self.sizes = sizes
        self.sql = []

    def rows(self, sql, args=()):
        self.sql.append(sql)
        return [{"logdir": k, "size": v} for k, v in self.sizes.items()]


def test_normal_state_raises_nothing():
    store = StateStore({"smtp": 126, "send": 9000, "__heartbeat__": 50})
    assert oversized_state(store, 1024) is None


def test_the_leak_that_happened_is_reported():
    store = StateStore({"smtp": 126, "smtps": 15_290_000, "submission": 8_300_000})
    alert = oversized_state(store, 1024)
    assert alert["id"] == "parser_state_large"
    # Worst first, both named.
    assert alert["detail"].index("smtps") < alert["detail"].index("submission")
    assert "smtp " not in alert["detail"]


def test_the_state_query_does_not_sort():
    """Sorting a bloated table by payload length runs out of sort memory."""
    store = StateStore({"smtp": 126})
    oversized_state(store, 1024)
    assert all("ORDER BY" not in s.upper() for s in store.sql)


def fake_usage(table):
    def usage(path):
        if path not in table:
            raise OSError("no such path")
        return table[path]
    return usage


GB = 1024 ** 3


def test_a_roomy_disk_is_quiet():
    assert disk_pressure(85, ["/"], usage=fake_usage({"/": (100 * GB, 50 * GB, 50 * GB)})) == []


def test_a_full_disk_is_serious_and_a_nearly_full_one_critical():
    got = disk_pressure(85, ["/"], usage=fake_usage({"/": (100 * GB, 88 * GB, 12 * GB)}))
    assert len(got) == 1 and got[0]["severity"] == "serious"
    got = disk_pressure(85, ["/"], usage=fake_usage({"/": (100 * GB, 97 * GB, 3 * GB)}))
    assert got[0]["severity"] == "critical"


def test_the_same_filesystem_is_reported_once():
    same = (100 * GB, 90 * GB, 10 * GB)
    got = disk_pressure(85, ["/", "/var/lib/mysql"],
                        usage=fake_usage({"/": same, "/var/lib/mysql": same}))
    assert len(got) == 1


def test_an_unreadable_path_is_skipped_not_fatal():
    got = disk_pressure(85, ["/missing", "/"],
                        usage=fake_usage({"/": (100 * GB, 10 * GB, 90 * GB)}))
    assert got == []
