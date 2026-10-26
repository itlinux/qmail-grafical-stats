"""Push the alerts to a phone, as well as mailing them.

Mail is the wrong carrier for the two alerts that matter most. If MySQL stops,
vpopmail cannot validate recipients and this server accepts nothing -- so the
mail saying so cannot be delivered either. The same is true of any fault in
qmail itself. A push notification leaves by a different road: an HTTPS POST
that needs neither MySQL nor the local mail system.

The transport is ntfy. The topic name is the only secret, so it must be long
and random; anybody who knows it can read the alerts and send them.
"""

import json
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 10

# ntfy's JSON publish API takes priority as an integer 1-5, not the words used
# by its X-Priority header: 5 max, 4 high, 3 default. Sending the strings is
# rejected with HTTP 400. This decides whether a phone makes a sound at night.
PRIORITY = {"critical": 5, "serious": 4, "warning": 3}


def compose(raised, host):
    """The title and body of one push, or None when there is nothing to say."""
    if not raised:
        return None
    worst = raised[0]
    title = ("%s: %s" % (host, worst["title"])) if host else worst["title"]
    lines = [worst["detail"]]
    if len(raised) > 1:
        lines.append("")
        lines.extend("also: %s" % a["title"] for a in raised[1:])
    return {
        "title": title,
        "message": "\n".join(lines),
        "priority": PRIORITY.get(worst["severity"], 3),
    }


def send(url, raised, host, token=None, opener=None):
    """POST one notification. Returns True when the server accepted it.

    Never raises. This is the path that reports a broken server, so a failure
    to push must not become a second failure that hides the first -- the caller
    still has mail to fall back on.
    """
    payload = compose(raised, host)
    if not (url and payload):
        return False
    body = json.dumps({
        "topic": url.rstrip("/").rsplit("/", 1)[-1],
        "title": payload["title"],
        "message": payload["message"],
        "priority": payload["priority"],
    }).encode("utf-8")
    base = url.rstrip("/").rsplit("/", 1)[0]
    request = urllib.request.Request(
        base, data=body, headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", "Bearer %s" % token)
    try:
        with (opener or urllib.request.urlopen)(
                request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def send_plain(url, host, title, message, priority="urgent", token=None,
               opener=None):
    """Push a message that did not come from the alert evaluator.

    Used when the database could not be reached at all, which is the one case
    the evaluator cannot report on because it needs the database to run.
    """
    severity = {"urgent": "critical", "high": "serious"}.get(priority, "warning")
    return send(url, [{"title": title, "detail": message,
                       "severity": severity, "id": "direct"}],
                host=host, token=token, opener=opener)
