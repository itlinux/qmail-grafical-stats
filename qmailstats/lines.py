"""Turn a single log line into a dict, or None if it carries nothing we want.

Every function here is pure: one line in, one dict out. Anything that needs
to remember a previous line belongs in assemble.py instead.
"""

import re

_INFO = re.compile(
    r"^info msg (?P<msg_id>\d+): bytes (?P<bytes>\d+) from <(?P<sender>[^>]*)> "
    r"qp (?P<qp>\d+) uid (?P<uid>\d+)"
)
_STARTING = re.compile(
    r"^starting delivery (?P<delivery_id>\d+): msg (?P<msg_id>\d+) "
    r"to (?P<route>local|remote) (?P<recipient>\S+)"
)
_OUTCOME = re.compile(
    r"^delivery (?P<delivery_id>\d+): (?P<result>success|deferral|failure): "
    r"(?P<detail>.*)$"
)
_END = re.compile(r"^end msg (?P<msg_id>\d+)")
_QUEUE_STATUS = re.compile(
    r"^status: local (?P<lu>\d+)/(?P<lm>\d+) remote (?P<ru>\d+)/(?P<rm>\d+)"
)


def parse_send(text):
    """Classify a line from the qmail-send log."""
    match = _INFO.match(text)
    if match:
        return {
            "kind": "info",
            "msg_id": int(match.group("msg_id")),
            "bytes": int(match.group("bytes")),
            "sender": match.group("sender"),
            "qp": int(match.group("qp")),
            "uid": int(match.group("uid")),
        }
    match = _STARTING.match(text)
    if match:
        return {
            "kind": "starting",
            "delivery_id": int(match.group("delivery_id")),
            "msg_id": int(match.group("msg_id")),
            "route": match.group("route"),
            "recipient": match.group("recipient"),
        }
    match = _OUTCOME.match(text)
    if match:
        return {
            "kind": "outcome",
            "delivery_id": int(match.group("delivery_id")),
            "result": match.group("result"),
            "detail": match.group("detail"),
        }
    match = _END.match(text)
    if match:
        return {"kind": "end", "msg_id": int(match.group("msg_id"))}
    match = _QUEUE_STATUS.match(text)
    if match:
        return {
            "kind": "concurrency",
            "local_used": int(match.group("lu")),
            "local_max": int(match.group("lm")),
            "remote_used": int(match.group("ru")),
            "remote_max": int(match.group("rm")),
        }
    return None


_CONNECT = re.compile(
    r"^tcpserver: ok (?P<pid>\d+) \S+?:(?P<local_ip>[0-9.]+):(?P<local_port>\d+) "
    # The client field ends the line as ':address::port'. An IPv6 client
    # address contains '::' of its own, so match the address greedily and
    # anchor the port at the end -- backtracking then finds the LAST '::',
    # which is the real separator. Matching lazily would cut 2001:db8::1
    # down to 2001:db8.
    r":(?P<client_ip>[0-9a-fA-F.:]+)::(?P<client_port>\d+)\s*$"
)
_RECV = re.compile(
    r"^mail recv: pid (?P<pid>\d+) from <(?P<sender>[^>]*)> qp (?P<qp>\d+)"
)
_POLICY = re.compile(
    r"^policy_check: \w+ (?P<auth_user>\S+) -> \w+ (?P<recipient>\S+)"
    r"(?P<auth> \(AUTHENTICATED SENDER\))?"
)
_TCP_STATUS = re.compile(r"^tcpserver: status: (?P<used>\d+)/(?P<max>\d+)")
_TCP_OPEN = re.compile(
    r"^tcpserver: pid (?P<pid>\d+) from (?P<client_ip>[0-9a-fA-F.:]+)"
)
# Every connection ends with this, mail or no mail. Without it a session that
# never sent a message -- a failed login, a scanner, a dropped TLS handshake --
# was never let go, and the carried parser state reached 244,000 entries.
_TCP_END = re.compile(r"^tcpserver: end (?P<pid>\d+) status (?P<status>\d+)")
_SCAN_HEAD = re.compile(
    r"^simscan:\[(?P<pid>\d+)\]:(?P<verdict>[A-Z]+) "
    r"\((?P<score>-?[0-9.]+)/[0-9.]+\):[0-9.]+s:"
)


def parse_smtp(text):
    """Classify a line from the smtp, submission or smtps log."""
    match = _CONNECT.match(text)
    if match:
        return {
            "kind": "connect",
            "pid": int(match.group("pid")),
            "local_port": int(match.group("local_port")),
            "client_ip": match.group("client_ip"),
        }
    match = _RECV.match(text)
    if match:
        return {
            "kind": "recv",
            "pid": int(match.group("pid")),
            "sender": match.group("sender"),
            "qp": int(match.group("qp")),
        }
    match = _POLICY.match(text)
    if match:
        return {
            "kind": "policy",
            "auth_user": match.group("auth_user"),
            "recipient": match.group("recipient"),
            "authenticated": match.group("auth") is not None,
        }
    match = _SCAN_HEAD.match(text)
    if match:
        # Everything after the header is subject:client_ip:sender:recipient.
        # Subjects contain colons, so take the last three fields from the right
        # and give whatever remains back to the subject.
        tail = text[match.end():]
        parts = tail.rsplit(":", 3)
        if len(parts) != 4:
            return None
        subject, client_ip, sender, recipient = parts
        return {
            "kind": "scan",
            "pid": int(match.group("pid")),
            "verdict": match.group("verdict"),
            "spam_score": float(match.group("score")),
            "subject": subject,
            "client_ip": client_ip,
            "sender": sender,
            "recipient": recipient,
        }
    # After _CONNECT, so "tcpserver: ok" is never mistaken for these.
    match = _TCP_STATUS.match(text)
    if match:
        return {
            "kind": "concurrency",
            "used": int(match.group("used")),
            "max": int(match.group("max")),
        }
    match = _TCP_END.match(text)
    if match:
        return {"kind": "close", "pid": int(match.group("pid"))}
    match = _TCP_OPEN.match(text)
    if match:
        return {
            "kind": "open",
            "pid": int(match.group("pid")),
            "client_ip": match.group("client_ip"),
        }
    return None
