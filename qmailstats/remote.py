"""Pull the useful parts out of a qmail delivery detail string.

A remote delivery detail looks like this, all one field, underscores instead
of spaces, segments separated by slashes:

    198.51.100.175_does_not_like_recipient./Remote_host_said:_451_4.7.1_
    IP_Reputation_Greylisted.../Giving_up_on_198.51.100.175./

Grouping those strings as they stand is useless, because the remote address
and any counters inside the remote's own text differ for every message -- each
failure would form a group of one. So the address, the SMTP code and the
enhanced status come out as fields of their own, and what is left is
normalised into a reason that repeats across messages with the same cause.
"""

import re

# Not \b at the edges: these addresses are followed by an underscore, which is
# a word character, so a word boundary never matches there.
_IP = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
# Enhanced status parts run up to three digits each (RFC 3463): DMARC
# rejections are 5.7.26, and matching only single digits would cut that to
# 5.7.2 and leave a stray 6 in the reason.
_RESPONSE = re.compile(
    r"Remote_host_said:_(?P<code>\d{3})(?:_(?P<status>\d\.\d{1,3}\.\d{1,3}))?"
)
# qmail's own status code on a local failure, e.g. "(#5.1.1)".
_QMAIL_STATUS = re.compile(r"\(#(?P<status>\d\.\d{1,3}\.\d{1,3})\)")
_GIVING_UP = re.compile(r"Giving_up_on_[^/]*/?")
_DOES_NOT_LIKE = re.compile(r"^[^/]*_does_not_like_recipient\./")
_NUMBERS = re.compile(r"\b\d+\b")


_GIVING_UP_ANY = re.compile(r"Giving_up_on_[^/]*/?")


def readable_detail(detail):
    """The remote's own words, made readable but not normalised.

    qmail writes the whole thing as one underscore-separated field, using "/"
    to end each segment. Only those trailing separators are dropped: nothing
    else is collapsed, so URLs, message ids and timestamps survive intact --
    which is what somebody pastes to the other side's administrator.
    """
    if not detail:
        return ""
    text = _GIVING_UP_ANY.sub("", detail)
    text = _DOES_NOT_LIKE.sub("", text)
    text = text.replace("Remote_host_said:_", "")
    # Segment separators are a slash at the very end or one followed by a
    # capitalised word; a slash inside a URL is neither.
    text = re.sub(r"/\s*$", "", text)
    text = re.sub(r"/(?=[A-Z][a-z]+_)", " ", text)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip(" -")


def parse_detail(detail):
    """Return remote_ip, code, status and a groupable reason."""
    if not detail:
        return {"remote_ip": None, "code": None, "status": None, "reason": ""}

    ip_match = _IP.search(detail)
    response = _RESPONSE.search(detail)
    qmail_status = _QMAIL_STATUS.search(detail)

    text = _QMAIL_STATUS.sub("", detail)
    text = _GIVING_UP.sub("", text)
    text = _DOES_NOT_LIKE.sub("", text)
    text = text.replace("Remote_host_said:_", "")
    if response:
        # The code and status are kept as fields; repeating them in the reason
        # only makes the table wider.
        text = text.replace(response.group(0).replace("Remote_host_said:_", ""), "", 1)
    text = _IP.sub("", text)
    text = text.replace("_", " ").replace("/", " ")
    text = _NUMBERS.sub("#", text)
    text = re.sub(r"\s+", " ", text).strip(" -")

    return {
        "remote_ip": ip_match.group(0) if ip_match else None,
        "code": int(response.group("code")) if response else None,
        "status": (
            response.group("status") if response and response.group("status")
            else qmail_status.group("status") if qmail_status
            else None
        ),
        "reason": text,
    }
