-- qmail-grafical-stats schema. MySQL 8.0.
-- Applied by: mysql qmailstats < schema.sql

CREATE TABLE IF NOT EXISTS `message` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `qp`            INT UNSIGNED NULL,
  `queue_qp`      INT UNSIGNED NULL,
  `queue_ts`      DATETIME(3) NULL,
  `ts`            DATETIME(3) NOT NULL,
  `direction`     ENUM('in','out') NOT NULL,
  `sender`        VARCHAR(320) NULL,
  `sender_domain` VARCHAR(255) NULL,
  `auth_user`     VARCHAR(320) NULL,
  `msg_id`        BIGINT UNSIGNED NULL,
  `size_bytes`    BIGINT UNSIGNED NULL,
  `client_ip`     VARCHAR(45) NULL,
  `spam_score`    DECIMAL(7,2) NULL,
  `verdict`       VARCHAR(16) NULL,
  `auth_source`   ENUM('policy_check','envelope') NULL,
  `subject`       VARCHAR(512) NULL,
  PRIMARY KEY (`id`),
  -- Each log identifies a message its own way -- neither key sees the other's
  -- rows, and MySQL allows repeated NULLs in a unique key, so a message known
  -- to only one side does not collide with anything.
  UNIQUE KEY `uq_message_smtp` (`qp`, `ts`),
  UNIQUE KEY `uq_message_queue` (`queue_qp`, `queue_ts`),
  KEY `ix_message_sender_ts` (`sender`, `ts`),
  KEY `ix_message_msgid_queuets` (`msg_id`, `queue_ts`),
  KEY `ix_message_ts` (`ts`),
  KEY `ix_message_sender_domain_ts` (`sender_domain`, `ts`),
  KEY `ix_message_auth_user_ts` (`auth_user`, `ts`),
  KEY `ix_message_msg_id_ts` (`msg_id`, `ts`),
  KEY `ix_message_verdict_ts` (`verdict`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `delivery` (
  `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `msg_id`           BIGINT UNSIGNED NOT NULL,
  `ts`               DATETIME(3) NOT NULL,
  `recipient`        VARCHAR(320) NOT NULL,
  `recipient_domain` VARCHAR(255) NULL,
  `route`            ENUM('local','remote') NOT NULL,
  `result`           ENUM('success','deferral','failure') NOT NULL,
  `detail`           VARCHAR(1024) NULL,
  -- msg_id is a reused queue inode, so msg_ts says which instance of it
  `msg_ts`           DATETIME(3) NOT NULL DEFAULT '1970-01-01 00:00:00',
  `remote_ip`        VARCHAR(45) NULL,
  `remote_code`      SMALLINT UNSIGNED NULL,
  `remote_status`    VARCHAR(8) NULL,
  `reason`           VARCHAR(255) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_delivery` (`msg_id`, `msg_ts`, `ts`, `recipient`, `result`),
  KEY `ix_delivery_ts` (`ts`),
  KEY `ix_delivery_recipient_domain_ts` (`recipient_domain`, `ts`),
  KEY `ix_delivery_recipient_ts` (`recipient`, `ts`),
  KEY `ix_delivery_msg_id` (`msg_id`, `msg_ts`),
  KEY `ix_delivery_reason_ts` (`reason`, `ts`),
  KEY `ix_delivery_remote_code_ts` (`remote_code`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Where the parser stopped in each log file.
CREATE TABLE IF NOT EXISTS `checkpoint` (
  `logdir`      VARCHAR(64) NOT NULL,
  `filename`    VARCHAR(64) NOT NULL,
  `byte_offset` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `completed`   TINYINT(1) NOT NULL DEFAULT 0,
  `updated_at`  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`logdir`, `filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Half-assembled records carried between runs, so a message that straddles a
-- rotation or a run boundary is not lost. Expired after 24h on read.
CREATE TABLE IF NOT EXISTS `parser_state` (
  `logdir`     VARCHAR(64) NOT NULL,
  `payload`    JSON NOT NULL,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`logdir`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rollups kept indefinitely once raw rows age out.
CREATE TABLE IF NOT EXISTS `daily_stats` (
  `day`        DATE NOT NULL,
  `domain`     VARCHAR(255) NOT NULL,
  `direction`  ENUM('in','out') NOT NULL,
  `messages`   BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `bytes`      BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `successes`  BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `deferrals`  BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `failures`   BIGINT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`day`, `domain`, `direction`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Queue concurrency, one row per minute. Folded in the parser because the
-- source lines appear on almost every delivery.
CREATE TABLE IF NOT EXISTS `queue_minute` (
  `minute`          DATETIME NOT NULL,
  `local_max_used`  INT UNSIGNED NOT NULL DEFAULT 0,
  `local_limit`     INT UNSIGNED NOT NULL DEFAULT 0,
  `remote_max_used` INT UNSIGNED NOT NULL DEFAULT 0,
  `remote_limit`    INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`minute`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Sessions per service, one row per minute. Connections minus messages is
-- what credential probing looks like: sessions that never sent anything.
CREATE TABLE IF NOT EXISTS `service_minute` (
  `minute`          DATETIME NOT NULL,
  `service`         VARCHAR(16) NOT NULL,
  `connects`        INT UNSIGNED NOT NULL DEFAULT 0,
  `messages`        INT UNSIGNED NOT NULL DEFAULT 0,
  `max_concurrency` INT UNSIGNED NOT NULL DEFAULT 0,
  `limit_value`     INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`minute`, `service`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Mailbox access events from Dovecot. Timestamps are converted from the
-- server's local syslog time to UTC on the way in, so they line up with the
-- qmail rows beside them.
CREATE TABLE IF NOT EXISTS `auth_event` (
  `id`        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `ts`        DATETIME NOT NULL,
  `service`   VARCHAR(16) NOT NULL,
  `result`    ENUM('login','auth_failed','no_auth') NOT NULL,
  `user`      VARCHAR(320) NULL,
  `client_ip` VARCHAR(45) NULL,
  `method`    VARCHAR(32) NULL,
  `tls`       TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  -- Makes re-reading a log file idempotent. The cost is that attempts
  -- identical down to the second -- same address, same user, same result --
  -- count once, which slightly understates a very fast password guesser.
  UNIQUE KEY `uq_auth_event` (`ts`, `service`, `result`, `user`, `client_ip`),
  KEY `ix_auth_event_ts` (`ts`),
  KEY `ix_auth_event_user_ts` (`user`, `ts`),
  KEY `ix_auth_event_ip_ts` (`client_ip`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- What fail2ban did about the authentication failures above. One row per
-- action; the unique key makes re-reading the log harmless.
CREATE TABLE IF NOT EXISTS ban_event (
  id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  ts       DATETIME(3) NOT NULL,
  jail     VARCHAR(64) NOT NULL,
  ip       VARCHAR(45) NOT NULL,
  action   ENUM('ban','unban') NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_ban_event (ts, jail, ip, action),
  KEY idx_ban_event_ts (ts),
  KEY idx_ban_event_ip (ip)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
