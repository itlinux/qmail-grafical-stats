"""Read fail2ban's log into ban and unban events.

The dashboard already says who failed to authenticate; this says what was done
about it. fail2ban writes one NOTICE per action, which is all we need:

    2026-09-16 09:16:39,984 fail2ban.actions [46659]: NOTICE  [jail] Ban 192.0.2.1

Times are local to the server and carry no zone, exactly like Dovecot's log.
"""

import datetime as dt
import re

_ACTION = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})[,.]\d+\s+"
    r"fail2ban\.actions\s*\[\d+\]:\s*\w+\s+"
    r"\[(?P<jail>[^\]]+)\]\s+"
    r"(?P<action>Ban|Unban|Restore Ban)\s+"
    r"(?P<ip>[0-9a-fA-F:.]+)\s*$"
)


class BanEvent:
    __slots__ = ("ts", "jail", "ip", "action")

    def __init__(self, ts, jail, ip, action):
        self.ts = ts
        self.jail = jail
        self.ip = ip
        self.action = action

    def __eq__(self, other):
        return (isinstance(other, BanEvent)
                and (self.ts, self.jail, self.ip, self.action)
                == (other.ts, other.jail, other.ip, other.action))

    def __repr__(self):
        return "BanEvent(%r, %r, %r, %r)" % (self.ts, self.jail, self.ip,
                                             self.action)


def parse(line):
    """One ban or unban, or None for the many lines that are neither.

    fail2ban logs a great deal at INFO -- every "Found" that has not yet
    reached the threshold -- and none of that is an action taken.
    """
    match = _ACTION.match(line.strip())
    if not match:
        return None
    stamp = dt.datetime.strptime(
        "%s %s" % (match.group("date"), match.group("time")),
        "%Y-%m-%d %H:%M:%S",
    )
    # "Restore Ban" is fail2ban re-applying a ban it already held after a
    # restart. It is a ban as far as the firewall is concerned.
    action = "ban" if match.group("action").endswith("Ban") else "unban"
    return BanEvent(stamp, match.group("jail"), match.group("ip"), action)


def events(lines):
    for line in lines:
        event = parse(line)
        if event is not None:
            yield event
