# Deploying qmail-grafical-stats on a mail server

Written against a RHEL 9 box running qmail, vpopmail, Dovecot and MySQL 8.
Adjust the names, not the order.

The worked example assumes a server whose clock is not the readers' clock --
logs in one timezone, people reading them in another -- because that is the
case most likely to be got wrong silently.

Everything here runs on the mail server. Nothing in this project needs outbound
network access at runtime.

## 0. What this will touch

- a new database and database user, with no rights over the vpopmail schema
- read access to `/var/log/qmail/*` and `/var/log/dovecot.log`
- one cron entry, one systemd service, one nginx location
- no change to qmail, Dovecot, or any mail path

If any step below fails, mail delivery is unaffected: this only ever reads logs.

## 1. Dependencies

```bash
dnf install -y python3-PyMySQL
python3 -c "import pymysql; print(pymysql.__version__)"
```

If the package is unavailable, enable EPEL (`dnf install -y epel-release`) and
retry. Failing that, a virtualenv that touches nothing system-wide:

```bash
python3 -m venv /opt/qmail-grafical-stats/.venv
/opt/qmail-grafical-stats/.venv/bin/pip install pymysql
```

`zoneinfo` is in the standard library from Python 3.9. On a minimal image it
needs the system tz database:

```bash
rpm -q tzdata || dnf install -y tzdata
python3 -c "from zoneinfo import ZoneInfo; print(ZoneInfo('America/Denver'))"
```

Without it, Dovecot timestamps fall back to UTC — the rows still load, shifted
by a known amount, rather than being lost.

## 2. Database and user

The grant is deliberately narrow. This account must be unable to read or write
the vpopmail schema.

```bash
mysql -u root -p <<'SQL'
CREATE DATABASE IF NOT EXISTS qmailstats
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'qmailstats'@'127.0.0.1'
  IDENTIFIED BY 'PUT_A_REAL_PASSWORD_HERE';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER ON qmailstats.*
  TO 'qmailstats'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
```

`ALTER` is needed because the schema applies its own additive migrations: new
columns are added to tables that already exist, which `CREATE TABLE IF NOT
EXISTS` cannot do.

**Verify the isolation actually holds before ingesting anything:**

```bash
mysql -u qmailstats -p -e "SHOW DATABASES;"
```

Expected: `qmailstats` and `information_schema` only. If the vpopmail database
appears, the grant is wrong. Stop and fix it.

## 3. Install

```bash
mkdir -p /opt/qmail-grafical-stats /etc/qmail-grafical-stats
# copy the repository to /opt/qmail-grafical-stats, then:
cp /opt/qmail-grafical-stats/config.ini.example /etc/qmail-grafical-stats/config.ini
chmod 600 /etc/qmail-grafical-stats/config.ini
```

Edit `/etc/qmail-grafical-stats/config.ini`:

- `[database] dsn` — the password chosen above
- `[parser] logroot` — `/var/log/qmail`
- `[parser] logdirs` — leave unset where the directories are named after their
  roles (`send`, `smtp`, `submission`, `smtps`); set it where they are named
  `qmail-send`, `qmail-smtpd` and so on
- `[parser] fallback_auth` — leave `false` where qmail-smtpd logs `policy_check`
  lines; set `true` on a server whose does not
- `[parser] store_subject` — `false` unless subjects are wanted in the database
- `[dovecot] logfile` — `/var/log/dovecot.log`, or empty to skip that source
- `[dovecot] timezone` — the server's own zone, whatever syslog writes in
- `[display] timezone` — the readers' zone, which is not necessarily the
  server's. A box keeping `America/Denver` while carrying Italian mail should
  render `Europe/Rome`, or the hourly figures will not read as a working day.
  Storage stays UTC, so this can be changed at any time without re-ingesting.

The file holds a database password. Keep it at mode 600, and do not paste its
contents into a shared terminal.

Apply the schema:

```bash
mysql -u qmailstats -p qmailstats < /opt/qmail-grafical-stats/schema.sql
mysql -u qmailstats -p qmailstats -e "SHOW TABLES;"
```

Expected: `auth_event`, `checkpoint`, `daily_stats`, `delivery`, `message`,
`parser_state`, `queue_minute`, `service_minute`.

## 4. Log access

The qmail log directories are mode 750, owned `qmaill:qmail`; Dovecot's log is
usually root-owned. Decide which user the parser runs as and confirm both
before the first run:

```bash
sudo -u qmaill test -r /var/log/qmail/send/current && echo qmail logs readable
sudo -u qmaill test -r /var/log/dovecot.log && echo dovecot log readable
```

If Dovecot's log is unreadable, add the user to its group rather than loosening
the file. The dovecot source can also simply be left off — the qmail statistics
do not depend on it.

## 5. First backfill, throttled

Roughly four months of logs. This is the only moment the tool puts real load on
the database that authenticates every mail login, so run it once, slowly, and
outside busy hours.

```bash
cd /opt/qmail-grafical-stats
PYTHONPATH=/opt/qmail-grafical-stats python3 -m qmailstats.ingest \
  --config /etc/qmail-grafical-stats/config.ini --pause 0.2
```

Expected: one line per source with counts. Then check the result:

```bash
mysql -u qmailstats -p qmailstats -e "
  SELECT COUNT(*) AS messages FROM message;
  SELECT direction, COUNT(*) FROM message GROUP BY direction;
  SELECT result, COUNT(*) FROM delivery GROUP BY result;
  SELECT COUNT(*) AS orphan_deliveries FROM delivery d
    LEFT JOIN message m ON m.msg_id = d.msg_id WHERE m.id IS NULL;
  SELECT result, COUNT(*) FROM auth_event GROUP BY result;"
```

A large `orphan_deliveries` count means the qp/msg_id linking is not working. A
small one is expected at the edges of the retained log window.

Sanity-check the timezone conversion, because it is the easiest thing to get
silently wrong:

```bash
mysql -u qmailstats -p qmailstats -e "
  SELECT MIN(ts), MAX(ts) FROM auth_event;
  SELECT MIN(ts), MAX(ts) FROM message;"
```

Both ranges should end near the current UTC time — not six or seven hours off.

## 6. Schedule

Two ways. Timers are preferable: cron cannot run more often than once a
minute, and the dashboard is worth keeping closer to the logs than that.

```bash
cp /opt/qmail-grafical-stats/contrib/*.service /opt/qmail-grafical-stats/contrib/*.timer \
   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now qmail-grafical-stats-ingest.timer
systemctl enable --now qmail-grafical-stats-rollup.timer
systemctl list-timers "qmail-grafical-stats*"
```

A run that overruns its interval cannot overlap the next: the parser takes a
lock and exits if another holds it.

### Or with cron

```bash
cat > /etc/cron.d/qmail-grafical-stats <<'CRON'
PYTHONPATH=/opt/qmail-grafical-stats
*/5 * * * * qmaill /usr/bin/python3 -m qmailstats.ingest --config /etc/qmail-grafical-stats/config.ini >> /var/log/qmail-grafical-stats.log 2>&1
17 3 * * *   qmaill /usr/bin/python3 -m qmailstats.rollup --config /etc/qmail-grafical-stats/config.ini --retain-days 90 --prune >> /var/log/qmail-grafical-stats.log 2>&1
CRON
```

Watch one cycle land:

```bash
sleep 360; tail -20 /var/log/qmail-grafical-stats.log
```

Concurrent runs are prevented by a lock file, so a slow run cannot overlap the
next one.

## 7. Web app

```bash
cat > /etc/systemd/system/qmail-grafical-stats.service <<'UNIT'
[Unit]
Description=qmail-grafical-stats dashboard
After=network.target mysqld.service

[Service]
Type=simple
User=qmaill
WorkingDirectory=/opt/qmail-grafical-stats
Environment=PYTHONPATH=/opt/qmail-grafical-stats
ExecStart=/usr/bin/python3 -m qmailstats.serve --config /etc/qmail-grafical-stats/config.ini
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now qmail-grafical-stats
systemctl status qmail-grafical-stats
curl -s localhost:8765/api/summary | head -c 400
```

If SELinux blocks the socket or the log reads, check `ausearch -m avc -ts
recent` and add a policy rather than disabling enforcement.

## 8. Publishing it — read this before sharing the URL

Put the web server's password file somewhere the web server can read: its own
configuration directory, not this tool's.

```bash
htpasswd -B -c /etc/httpd/qmail-grafical-stats.htpasswd yourname
chown root:apache /etc/httpd/qmail-grafical-stats.htpasswd
chmod 640 /etc/httpd/qmail-grafical-stats.htpasswd
```

`/etc/qmail-grafical-stats` is deliberately closed to everyone but the parser
user, so a password file inside it is unreadable by the web server even when
the file's own owner and mode look right — the directory cannot be traversed.
Apache reports that as a 500, and nothing in its error log says "permission".

The database holds sender and recipient addresses, client IP addresses, and —
if `store_subject` is on — subject lines. This is mail metadata, not a status
page.

The server binds `127.0.0.1` and **has no authentication of its own**. That is
deliberate, and it means it must never be exposed without something in front.
Add an nginx location proxying to `127.0.0.1:8765`, and put the hostname behind
Cloudflare Access with a policy limited to the intended people.

Verify before telling anyone the URL:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<hostname>/api/summary
```

Expected: a redirect or 403 from Access while unauthenticated. **A 200 means
every address and client IP on the box is readable from the internet.** Stop and
fix the Access policy before going further.

## 9. Operational notes

**Reading mailbox facts.** vpopmail stores passwords in the clear, and
`vuserinfo` prints them. To check a quota or usage, read the maildir instead:

```bash
f=/home/vpopmail/domains/DOMAIN/USER/Maildir/maildirsize
echo "quota $(head -1 "$f")  used $(awk 'NR>1{s+=$1}END{print int(s/1048576)}' "$f") MB"
```

**Re-parsing.** The counters in `queue_minute` and `service_minute` accumulate
rather than replace. Clearing checkpoints to re-read logs therefore requires
clearing those two tables for the same period, or connection counts will
double. The `message` and `delivery` tables are safe to re-parse: their writes
are idempotent.

**Retention.** Raw rows are kept 90 days and folded into `daily_stats` nightly.
`/var/lib/mysql` sits on `/`; a full filesystem breaks mail authentication, not
just the dashboard, so leave the rollup job scheduled.

**Subjects.** Turning `store_subject` on collects them from that point forward.
Earlier subjects can be recovered by clearing the relevant checkpoints and
re-reading the rotations still on disk — but see the counter caveat above.

**Adding a second server.** Each server runs its own copy against its own
database. Set `logdirs` if its directories are named differently, and
`fallback_auth = true` if its qmail-smtpd does not write `policy_check` lines.
