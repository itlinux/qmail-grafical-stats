"""Read-only HTTP over queries.py. Binds loopback; auth belongs to the proxy."""

import argparse
import configparser
import datetime as dt
import json
import pathlib
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from qmailstats import alerts, queries
from qmailstats.db import Store

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ROUTES = {
    # Not range-dependent: an alert is about now, whatever window is on screen.
    "/api/blocked": lambda s, a: queries.blocked_addresses(s, a["since"], a["until"]),
    "/api/blocked-summary": lambda s, a: queries.blocked_summary(s, a["since"], a["until"]),
    "/api/blocked-by-jail": lambda s, a: queries.blocked_by_jail(s, a["since"], a["until"]),
    "/api/alerts": lambda s, a: alerts.evaluate(s, a.get("alert_config") or {}),
    "/api/summary": lambda s, a: queries.summary(s, a["since"], a["until"]),
    "/api/timeseries": lambda s, a: queries.timeseries(
        s, a["since"], a["until"], a.get("bucket", "day")
    ),
    "/api/top-senders": lambda s, a: queries.top_senders(s, a["since"], a["until"]),
    "/api/top-recipients": lambda s, a: queries.fold_aliases(
        queries.top_recipients(s, a["since"], a["until"]), a["aliases"],
        key="recipient_domain", group=("recipient_domain",)),
    "/api/per-domain": lambda s, a: queries.fold_aliases(
        queries.per_domain(s, a["since"], a["until"]), a["aliases"],
        key="domain", group=("domain", "direction")),
    "/api/mailboxes": lambda s, a: queries.mailboxes(s, a["since"], a["until"]),
    "/api/mailbox": lambda s, a: queries.mailbox(s, a["address"], a["since"], a["until"]),
    "/api/outcomes": lambda s, a: queries.outcomes_timeseries(
        s, a["since"], a["until"], a.get("bucket", "day")),
    "/api/hourly": lambda s, a: queries.hourly_profile(s, a["since"], a["until"]),
    "/api/spam": lambda s, a: queries.spam_per_account(s, a["since"], a["until"]),
    "/api/failures": lambda s, a: queries.failure_reasons(s, a["since"], a["until"]),
    "/api/failure-detail": lambda s, a: queries.failure_detail(
        s, a["since"], a["until"], a.get("address")),
    "/api/domain-flow": lambda s, a: queries.fold_aliases(
        queries.fold_aliases(
            queries.domain_flow(s, a["since"], a["until"]), a["aliases"],
            key="sender_domain", group=("sender_domain", "recipient_domain")),
        a["aliases"], key="recipient_domain",
        group=("sender_domain", "recipient_domain")),
    "/api/concurrency": lambda s, a: queries.concurrency(s, a["since"], a["until"]),
    "/api/connections": lambda s, a: queries.connections(s, a["since"], a["until"]),
    "/api/retries": lambda s, a: queries.retry_summary(s, a["since"], a["until"]),
    "/api/stuck": lambda s, a: queries.stuck_deliveries(s, a["since"], a["until"]),
    "/api/access": lambda s, a: queries.access_summary(s, a["since"], a["until"]),
    "/api/mailbox-access": lambda s, a: queries.mailbox_access(s, a["since"], a["until"]),
    "/api/auth-failures": lambda s, a: queries.auth_failures(s, a["since"], a["until"]),
}


def route(path):
    return ROUTES.get(path)


def parse_range(params):
    """Turn query parameters into a UTC datetime pair, or raise ValueError."""

    def one(name):
        values = params.get(name)
        if not values:
            return None
        value = values[0]
        if not DATE.match(value):
            raise ValueError("bad %s: expected YYYY-MM-DD" % name)
        return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)

    until = one("until") or dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + dt.timedelta(days=1)
    since = one("since") or until - dt.timedelta(days=7)
    if since >= until:
        raise ValueError("since must be before until")
    return since, until


def make_handler(store, display_timezone="UTC", domain_aliases=None,
                 alert_config=None):
    domain_aliases = domain_aliases or {}
    alert_config = alert_config or {}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # nginx already logs; keep the journal quiet

        def _send(self, code, body, content_type):
            payload = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/config":
                # The page needs to know which zone to render in; storage is
                # always UTC.
                return self._send(
                    200,
                    json.dumps({
                        "display_timezone": display_timezone,
                        "demo_data": queries.looks_like_demo_data(store),
                    }),
                    "application/json")
            if parsed.path in ("/", "/index.html"):
                return self._send(
                    200, (STATIC / "index.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            handler = route(parsed.path)
            if handler is None:
                return self._send(404, '{"error":"not found"}', "application/json")
            params = parse_qs(parsed.query)
            try:
                since, until = parse_range(params)
                args = {"since": since, "until": until,
                        "aliases": domain_aliases,
                        "alert_config": alert_config}
                if "bucket" in params and params["bucket"][0] in ("day", "hour", "week"):
                    args["bucket"] = params["bucket"][0]
                if parsed.path == "/api/mailbox":
                    if not params.get("address"):
                        raise ValueError("address is required")
                    args["address"] = params["address"][0][:320]
                if parsed.path == "/api/failure-detail" and params.get("address"):
                    args["address"] = params["address"][0][:320]
                result = handler(store, args)
            except ValueError as exc:
                return self._send(400, json.dumps({"error": str(exc)}), "application/json")
            return self._send(200, json.dumps(result, default=str), "application/json")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serve the qmail-grafical-stats dashboard.")
    ap.add_argument("--config", default="/etc/qmail-grafical-stats/config.ini")
    args = ap.parse_args(argv)
    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config)
    store = Store.from_dsn(config.get("database", "dsn"))
    host = config.get("server", "listen_host", fallback="127.0.0.1")
    port = config.getint("server", "listen_port", fallback=8765)
    display_timezone = config.get("display", "timezone", fallback="UTC")
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(store, display_timezone,
                     queries.aliases_from_config(config),
                     dict(config.items("alerts"))
                     if config.has_section("alerts") else {}))
    print("listening on %s:%d" % (host, port))
    server.serve_forever()


if __name__ == "__main__":
    main()
