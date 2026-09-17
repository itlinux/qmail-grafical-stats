"""The dashboard serves requests on threads; connections must not be shared."""

import threading

from qmailstats import db


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_each_thread_gets_its_own_connection(monkeypatch):
    """Two requests arriving at once must not talk over one socket -- that is
    what produced 'Protocol error, expecting EOF' and failed both requests."""
    made = []

    def fake_connect(**kwargs):
        conn = FakeConnection()
        made.append(conn)
        return conn

    monkeypatch.setattr(db.pymysql, "connect", fake_connect)
    store = db.Store(FakeConnection(), connect_args={"host": "127.0.0.1"})

    seen = {}

    def grab(name):
        seen[name] = store.conn

    threads = [threading.Thread(target=grab, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen["a"] is not seen["b"]
    assert seen["a"] not in (store._primary,)
    assert seen["b"] not in (store._primary,)


def test_same_thread_reuses_its_connection(monkeypatch):
    monkeypatch.setattr(db.pymysql, "connect", lambda **k: FakeConnection())
    store = db.Store(FakeConnection(), connect_args={"host": "127.0.0.1"})

    def grab():
        return store.conn, store.conn

    first, second = grab()
    assert first is second


def test_without_connect_args_the_given_connection_is_shared(monkeypatch):
    """The fixtures hand in a connection and expect it to be the one used."""
    given = FakeConnection()
    store = db.Store(given)
    seen = []

    thread = threading.Thread(target=lambda: seen.append(store.conn))
    thread.start()
    thread.join()

    assert seen == [given]


def test_close_closes_every_connection_opened(monkeypatch):
    monkeypatch.setattr(db.pymysql, "connect", lambda **k: FakeConnection())
    store = db.Store(FakeConnection(), connect_args={"host": "127.0.0.1"})

    opened = []
    thread = threading.Thread(target=lambda: opened.append(store.conn))
    thread.start()
    thread.join()

    store.close()
    assert store._primary.closed
    assert opened[0].closed
