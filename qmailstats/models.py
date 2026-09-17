"""The two record types the parser produces."""

from dataclasses import dataclass
from typing import Optional

from qmailstats.remote import parse_detail


def _fold(address):
    if address is None:
        return None
    return address.strip().lower()


def _unprefix_vpopmail(address):
    """Turn vpopmail's 'domain-user@domain' back into 'user@domain'.

    qmail-send writes local deliveries in that form. Only a prefix that matches
    the address's own domain is removed, so a local part that merely contains a
    hyphen -- anna-maria@ -- is left alone.
    """
    if not address or "@" not in address:
        return address
    local, domain = address.rsplit("@", 1)
    prefix = domain + "-"
    if local.startswith(prefix) and len(local) > len(prefix):
        return local[len(prefix):] + "@" + domain
    return address


def _domain_of(address):
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1] or None


@dataclass
class Message:
    """One message, seen from the SMTP side, the queue side, or both.

    The two logs do not share an identifier. Their qp values are process ids of
    different qmail-queue invocations -- close in value, never equal -- so each
    side records its own, and the two are correlated later by sender and time.
    """

    qp: object   # the SMTP session's qp, or None when only the queue saw it
    ts: object
    direction: str  # "in" (port 25) or "out" (authenticated submission)
    sender: str
    auth_user: Optional[str] = None
    msg_id: Optional[int] = None
    size_bytes: Optional[int] = None
    client_ip: Optional[str] = None
    spam_score: Optional[float] = None
    verdict: Optional[str] = None  # the scanner's label, e.g. CLEAN or SPAM
    auth_source: Optional[str] = None  # "policy_check" or "envelope"
    queue_qp: Optional[int] = None     # the queue log's own qp
    queue_ts: object = None            # when the queue logged it
    subject: Optional[str] = None

    def __post_init__(self):
        self.sender = _fold(self.sender)
        self.auth_user = _fold(self.auth_user)
        # A record that came from the queue log is timestamped by that line.
        if self.msg_id is not None and self.queue_ts is None:
            self.queue_ts = self.ts

    @property
    def sender_domain(self):
        return _domain_of(self.sender)


@dataclass
class Delivery:
    """One delivery attempt for one recipient."""

    msg_id: int
    ts: object
    recipient: str
    route: str   # "local" or "remote"
    result: str  # "success", "deferral" or "failure"
    detail: str = ""
    # msg_id is a queue inode number and is reused as soon as the queue drains,
    # so it identifies a message only together with the time that instance was
    # queued.
    msg_ts: object = None
    remote_ip: Optional[str] = None
    remote_code: Optional[int] = None
    remote_status: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self):
        self.recipient = _fold(self.recipient)
        if self.route == "local":
            # Only local deliveries carry vpopmail's prefix; a remote address
            # that happens to look like one belongs to somebody else.
            self.recipient = _unprefix_vpopmail(self.recipient)
        # Derived here rather than by the caller, so a Delivery is consistent
        # however it was built. A success detail is qmail's own bookkeeping and
        # carries no cause to extract.
        if self.result != "success" and self.reason is None and self.detail:
            extracted = parse_detail(self.detail)
            self.remote_ip = self.remote_ip or extracted["remote_ip"]
            self.remote_code = self.remote_code or extracted["code"]
            self.remote_status = self.remote_status or extracted["status"]
            self.reason = extracted["reason"] or None

    @property
    def recipient_domain(self):
        return _domain_of(self.recipient)
