#!/bin/bash
# Set up qmail-grafical-stats on one mail server.
#
# Creates the database and its own user, writes the configuration, applies the
# schema and installs the timers. Idempotent: run it again and it leaves what
# already exists alone.
#
# The database password is generated here and written straight into a mode-600
# configuration file. It is never printed.
#
#   ./setup-server.sh [--alert-mail you@example.com] [--timezone Europe/Rome]
#
# Needs: a MySQL root login (prompts unless root is passwordless), and the
# code already unpacked in /opt/qmail-grafical-stats.
set -euo pipefail

PREFIX=/opt/qmail-grafical-stats
CONFDIR=/etc/qmail-grafical-stats
ALERT_MAIL=""
DISPLAY_TZ="UTC"
RUN_AS=qmaill

while [ $# -gt 0 ]; do
  case "$1" in
    --alert-mail) ALERT_MAIL="$2"; shift 2 ;;
    --timezone)   DISPLAY_TZ="$2"; shift 2 ;;
    --user)       RUN_AS="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ -d "$PREFIX" ] || { echo "$PREFIX not found - unpack the code first" >&2; exit 1; }
python3 -c "import pymysql" 2>/dev/null || { echo "python3-PyMySQL missing" >&2; exit 1; }
id "$RUN_AS" >/dev/null 2>&1 || { echo "user $RUN_AS does not exist" >&2; exit 1; }

umask 077
mkdir -p "$CONFDIR"
# The directory must be traversable by the service user, or ConfigParser reads
# nothing and reports a missing section rather than a permissions problem.
chown root:"$(id -gn "$RUN_AS")" "$CONFDIR"
chmod 750 "$CONFDIR"

if [ -f "$CONFDIR/config.ini" ]; then
  echo "config already exists - leaving it alone"
else
  # The server's own timezone, for reading Dovecot's syslog timestamps.
  SERVER_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)
  # Log directories named after their roles, minus any this tool has no use
  # for (smtptx is an SMTP protocol transcript, not a statistics source).
  LOGDIRS=""
  for d in smtp submission smtps send; do
    [ -d "/var/log/qmail/$d" ] && LOGDIRS="${LOGDIRS:+$LOGDIRS, }$d:$d"
  done
  # Dovecot only if this user can actually read the log.
  # Test group membership rather than running a command as the user: a service
  # account usually has a nologin shell, so `sudo -u` fails for reasons that
  # have nothing to do with file permissions.
  DOVECOT_LOG=""
  if [ -f /var/log/dovecot.log ]; then
    LOG_GROUP=$(stat -c %G /var/log/dovecot.log)
    if id -nG "$RUN_AS" | tr " " "\n" | grep -qx "$LOG_GROUP"; then
      DOVECOT_LOG=/var/log/dovecot.log
    else
      echo "note: $RUN_AS is not in group $LOG_GROUP, so Dovecot statistics are off."
      echo "      enable them with: usermod -aG $LOG_GROUP $RUN_AS"
    fi
  fi

  PW=$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | cut -c1-28)
  # Where root's credentials are already available -- a /root/.my.cnf, or
  # socket authentication -- adding -p prompts for a password the caller may
  # not have, and an empty answer is refused. Use whichever already works.
  if mysql -u root -e "SELECT 1" >/dev/null 2>&1; then
    MYSQL_ROOT="mysql -u root"
    echo "Creating the database using the credentials MySQL already has."
  else
    MYSQL_ROOT="mysql -u root -p"
    echo "Creating the database. Enter the MySQL root password when asked."
  fi
  $MYSQL_ROOT <<SQL
CREATE DATABASE IF NOT EXISTS qmailstats
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'qmailstats'@'127.0.0.1' IDENTIFIED BY '$PW';
ALTER USER 'qmailstats'@'127.0.0.1' IDENTIFIED BY '$PW';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER ON qmailstats.*
  TO 'qmailstats'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

  ENCODED=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$PW")
  cat > "$CONFDIR/config.ini" <<CONF
[database]
dsn = mysql://qmailstats:$ENCODED@127.0.0.1:3306/qmailstats

[parser]
logroot = /var/log/qmail
lockfile = /run/qmail-grafical-stats/ingest.lock

[fail2ban]
logfile = /var/log/fail2ban.log
logdirs = $LOGDIRS
store_subject = false
fallback_auth = false

[dovecot]
logfile = $DOVECOT_LOG
timezone = $SERVER_TZ

[display]
timezone = $DISPLAY_TZ

[alerts]
mail_to = $ALERT_MAIL

[server]
listen_host = 127.0.0.1
listen_port = 8765
CONF
  unset PW ENCODED
  chown "$RUN_AS":root "$CONFDIR/config.ini"
  chmod 600 "$CONFDIR/config.ini"
  echo "wrote $CONFDIR/config.ini"
fi

# Prove the account cannot see anything but its own database before loading data.
VISIBLE=$(cd "$PREFIX" && PYTHONPATH="$PREFIX" python3 -c "
import configparser
from qmailstats.db import Store
c = configparser.ConfigParser(interpolation=None); c.read('$CONFDIR/config.ini')
s = Store.from_dsn(c.get('database','dsn'))
print(','.join(sorted(list(r.values())[0] for r in s.rows('SHOW DATABASES'))))
s.close()")
echo "databases visible to the parser: $VISIBLE"
case "$VISIBLE" in
  *vpopmail*) echo "REFUSING: the parser can see the vpopmail schema" >&2; exit 1 ;;
esac

cd "$PREFIX" && PYTHONPATH="$PREFIX" python3 -c "
import configparser
from qmailstats.db import Store
c = configparser.ConfigParser(interpolation=None); c.read('$CONFDIR/config.ini')
s = Store.from_dsn(c.get('database','dsn')); s.apply_schema()
print('tables:', len(s.rows('SHOW TABLES')))
s.close()"

install -d -o "$RUN_AS" -g "$(id -gn "$RUN_AS")" /var/lib/qmail-grafical-stats
touch /var/log/qmail-grafical-stats.log
chown "$RUN_AS":"$(id -gn "$RUN_AS")" /var/log/qmail-grafical-stats.log
chmod 640 /var/log/qmail-grafical-stats.log

cp "$PREFIX"/contrib/*.service "$PREFIX"/contrib/*.timer /etc/systemd/system/
cat > /etc/systemd/system/qmail-grafical-stats.service <<UNIT
[Unit]
Description=qmail-grafical-stats dashboard
After=network.target mysqld.service

[Service]
Type=simple
User=$RUN_AS
WorkingDirectory=$PREFIX
Environment=PYTHONPATH=$PREFIX
ExecStart=/usr/bin/python3 -m qmailstats.serve --config $CONFDIR/config.ini
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now qmail-grafical-stats >/dev/null 2>&1
echo "dashboard: $(systemctl is-active qmail-grafical-stats) on 127.0.0.1:8765"
echo
echo "Two steps remain. Run them one at a time -- the first takes a while."
echo
echo "  1. The first backfill (minutes to an hour, depending on how much log there is):"
echo
echo "       cd $PREFIX && PYTHONPATH=$PREFIX python3 -m qmailstats.ingest --config $CONFDIR/config.ini --pause 0.1"
echo
echo "  2. Once that finishes, keep it current:"
echo
echo "       systemctl enable --now qmail-grafical-stats-ingest.timer qmail-grafical-stats-alerts.timer qmail-grafical-stats-rollup.timer"
echo
