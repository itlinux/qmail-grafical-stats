"""State machines that turn classified lines into records.

Each assembler is fed lines in file order and yields zero or more records per
line. Unfinished state can be exported with state() and restored with
restore(), which is what lets a message span a log rotation or a parser run.
"""

from qmailstats import lines as lineparse
from qmailstats.models import Delivery, Message
from qmailstats.tai64 import split


def _minutes_to_state(minutes):
    """Minutes are datetime keys; JSON needs strings."""
    return {m.isoformat(): sample for m, sample in minutes.items()}


def _minutes_from_state(payload):
    """Rebuild datetime keys, dropping anything unparseable rather than failing.

    Without this the newest minute -- deliberately held back by drain_minutes
    because it may still be accumulating -- would be discarded at the end of
    every run instead of carried into the next one.
    """
    import datetime as _dt

    restored = {}
    for key, sample in (payload or {}).items():
        try:
            restored[_dt.datetime.fromisoformat(key)] = sample
        except (TypeError, ValueError):
            continue
    return restored


def _int_keys(mapping):
    """Restore integer keys that a round trip through JSON turned into strings.

    Carried state is stored as JSON, and JSON object keys are always strings.
    Delivery ids and pids are looked up as integers, so without this every
    record whose opening line fell in an earlier run would be silently dropped.
    """
    if not mapping:
        return {}
    restored = {}
    for key, value in mapping.items():
        try:
            restored[int(key)] = value
        except (TypeError, ValueError):
            restored[key] = value
    return restored


class MinuteFold:
    """Per-minute accumulation shared by both assemblers.

    The concurrency lines appear on nearly every delivery and connection, so
    storing them raw would add more rows than everything else combined. They
    are folded to one row per minute -- which is also the grain MRTG drew.
    """

    def minutes(self):
        return self._minutes

    def drain_minutes(self, keep_last=True):
        """Return completed minutes, holding back the newest by default.

        The newest minute may still be accumulating in a file we have not
        finished reading, so it is carried to the next run rather than written
        half-counted.
        """
        if not self._minutes:
            return {}
        if not keep_last:
            drained, self._minutes = self._minutes, {}
            return drained
        newest = max(self._minutes)
        drained = {k: v for k, v in self._minutes.items() if k != newest}
        self._minutes = {newest: self._minutes[newest]}
        return drained


class SendAssembler(MinuteFold):
    """Assembles the qmail-send log."""

    def __init__(self):
        self.open_messages = {}    # msg_id -> dict(sender, bytes, qp, ts)
        self.open_deliveries = {}  # delivery_id -> dict(msg_id, recipient, route)
        self.unmatched = 0
        self.malformed = 0
        self._minutes = {}

    def feed(self, line):
        try:
            ts, text = split(line)
        except ValueError:
            self.malformed += 1
            return []
        parsed = lineparse.parse_send(text)
        if parsed is None:
            return []

        kind = parsed["kind"]
        if kind == "info":
            record = dict(parsed)
            record["ts"] = ts
            self.open_messages[parsed["msg_id"]] = record
            return [
                Message(
                    # The queue log never saw the SMTP session, so it claims no
                    # smtp qp; its own qp is a different process's id.
                    qp=None,
                    ts=ts,
                    direction="out",
                    sender=parsed["sender"],
                    msg_id=parsed["msg_id"],
                    size_bytes=parsed["bytes"],
                    queue_qp=parsed["qp"],
                    queue_ts=ts,
                )
            ]
        if kind == "starting":
            opened_message = self.open_messages.get(parsed["msg_id"])
            self.open_deliveries[parsed["delivery_id"]] = {
                "msg_id": parsed["msg_id"],
                "recipient": parsed["recipient"],
                "route": parsed["route"],
                # Which instance of that reused inode this belongs to.
                "msg_ts": opened_message["ts"] if opened_message else None,
            }
            return []
        if kind == "outcome":
            opened = self.open_deliveries.pop(parsed["delivery_id"], None)
            if opened is None:
                self.unmatched += 1
                return []
            return [
                Delivery(
                    msg_id=opened["msg_id"],
                    ts=ts,
                    recipient=opened["recipient"],
                    route=opened["route"],
                    result=parsed["result"],
                    # Delivery itself pulls the cause out of the detail.
                    detail=parsed["detail"][:1024],
                    msg_ts=opened.get("msg_ts"),
                )
            ]
        if kind == "end":
            self.open_messages.pop(parsed["msg_id"], None)
            return []
        if kind == "concurrency":
            minute = ts.replace(second=0, microsecond=0)
            sample = self._minutes.setdefault(minute, {
                "local_max_used": 0, "remote_max_used": 0,
                "local_limit": parsed["local_max"],
                "remote_limit": parsed["remote_max"],
            })
            sample["local_max_used"] = max(
                sample["local_max_used"], parsed["local_used"])
            sample["remote_max_used"] = max(
                sample["remote_max_used"], parsed["remote_used"])
            return []
        return []

    def state(self):
        return {
            "open_messages": self.open_messages,
            "open_deliveries": self.open_deliveries,
            "minutes": _minutes_to_state(self._minutes),
        }

    @classmethod
    def restore(cls, state):
        obj = cls()
        obj.open_messages = _int_keys(state.get("open_messages"))
        obj.open_deliveries = _int_keys(state.get("open_deliveries"))
        obj._minutes = _minutes_from_state(state.get("minutes"))
        return obj


OUTBOUND_PORTS = (587, 465)


# Open SMTP sessions kept between runs. See SmtpAssembler._trim.
MAX_OPEN_SESSIONS = 5000


class SmtpAssembler(MinuteFold):
    """Assembles the smtp, submission and smtps logs.

    Facts arrive spread across several lines of one connection, identified by
    pid. They are held until 'mail recv' supplies the qp, which is the key the
    queue log will use for the same message.
    """

    def __init__(self, store_subject=False, default_direction="in", service="smtp",
                 fallback_auth=False):
        self.store_subject = store_subject
        self.default_direction = default_direction
        self.service = service
        # policy_check comes from a patched qmail-smtpd. Where it is absent,
        # outbound mail can be credited to its envelope sender instead -- but
        # only on the authenticated ports.
        self.fallback_auth = fallback_auth
        self.sessions = {}  # pid -> accumulated facts, oldest first
        self.pending_policy = None
        self.malformed = 0
        self._minutes = {}

    def _session(self, pid):
        session = self.sessions.get(pid)
        if session is None:
            session = self.sessions[pid] = {}
            self._trim()
        return session

    def _trim(self):
        """Keep the open-session table bounded.

        Sessions normally leave on "tcpserver: end". An end line can still be
        lost -- it falls in a rotated file that was never read, or the service
        was killed -- and pids are reused, so drop the oldest instead of
        carrying them forever. Far more concurrent connections than any of
        these servers accept.
        """
        excess = len(self.sessions) - MAX_OPEN_SESSIONS
        if excess > 0:
            for pid in list(self.sessions)[:excess]:
                del self.sessions[pid]

    def _sample(self, minute):
        return self._minutes.setdefault(minute, {
            "connects": 0, "messages": 0, "max_concurrency": 0, "limit": 0,
        })

    def _bump(self, ts, field):
        self._sample(ts.replace(second=0, microsecond=0))[field] += 1

    def feed(self, line):
        try:
            ts, text = split(line)
        except ValueError:
            self.malformed += 1
            return []
        parsed = lineparse.parse_smtp(text)
        if parsed is None:
            return []

        kind = parsed["kind"]
        if kind == "close":
            self.sessions.pop(parsed["pid"], None)
            return []
        # Only lines that carry something about the session create one. An
        # "open" line arrives for every connection, including ones refused
        # before they start, and creating an empty session for each of those
        # was a second way for this dictionary to grow without bound.
        session = (
            self._session(parsed["pid"]) if kind in ("connect", "scan") else None
        )

        if kind == "connect":
            session["client_ip"] = parsed["client_ip"]
            session["direction"] = (
                "out" if parsed["local_port"] in OUTBOUND_PORTS else "in"
            )
            return []
        if kind == "scan":
            session["spam_score"] = parsed["spam_score"]
            session["verdict"] = parsed["verdict"]
            session["client_ip"] = session.get("client_ip") or parsed["client_ip"]
            if self.store_subject:
                session["subject"] = parsed["subject"][:512]
            return []
        if kind == "policy":
            # policy_check carries no pid, so it attaches to the session that
            # receives the next mail recv. Held separately for that reason.
            if parsed["authenticated"]:
                self.pending_policy = parsed["auth_user"]
            return []
        if kind == "open":
            self._bump(ts, "connects")
            return []
        if kind == "concurrency":
            sample = self._sample(ts.replace(second=0, microsecond=0))
            sample["max_concurrency"] = max(
                sample["max_concurrency"], parsed["used"])
            sample["limit"] = parsed["max"]
            return []
        if kind == "recv":
            self._bump(ts, "messages")
            facts = self.sessions.pop(parsed["pid"], {})
            auth_user = self.pending_policy
            # Cleared on every recv: an unclaimed policy line must not attach
            # itself to some later message, and a recycled pid must not inherit
            # the previous session's account.
            self.pending_policy = None
            direction = facts.get("direction", self.default_direction)
            auth_source = None
            if auth_user is not None and direction == "out":
                auth_source = "policy_check"
            else:
                auth_user = None
                # Never on port 25: there the envelope sender is whatever the
                # client claimed, so crediting it would let anyone write
                # themselves into another account's statistics.
                if self.fallback_auth and direction == "out":
                    auth_user = parsed["sender"] or None
                    auth_source = "envelope" if auth_user else None
            return [
                Message(
                    qp=parsed["qp"],
                    ts=ts,
                    direction=direction,
                    sender=parsed["sender"],
                    auth_user=auth_user,
                    auth_source=auth_source,
                    client_ip=facts.get("client_ip"),
                    spam_score=facts.get("spam_score"),
                    verdict=facts.get("verdict"),
                    subject=facts.get("subject"),
                )
            ]
        return []

    def state(self):
        return {
            "sessions": self.sessions,
            "minutes": _minutes_to_state(self._minutes),
        }

    @classmethod
    def restore(cls, state, store_subject=False, default_direction="in",
                service="smtp", fallback_auth=False):
        obj = cls(store_subject=store_subject,
                  default_direction=default_direction, service=service,
                  fallback_auth=fallback_auth)
        obj.sessions = _int_keys(state.get("sessions"))
        # State saved before sessions were closed can hold hundreds of
        # thousands of them; bring it back to size on the first run.
        obj._trim()
        obj._minutes = _minutes_from_state(state.get("minutes"))
        return obj
