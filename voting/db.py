"""SQLite 存储层：建表与连接工具。"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL,
  role  TEXT NOT NULL          -- owner/committee/teller/reviewer，可逗号分隔兼任
);

CREATE TABLE IF NOT EXISTS units (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,          -- 房号，如 1-101
  area REAL NOT NULL                  -- 专有部分面积（平方米）
);

-- 户与业主的权属关系；until 为 NULL 表示当前持有。过户即关闭旧行、开启新行。
CREATE TABLE IF NOT EXISTS ownerships (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id  INTEGER NOT NULL REFERENCES units(id),
  owner_id INTEGER NOT NULL REFERENCES users(id),
  since    TEXT NOT NULL,
  until    TEXT
);

CREATE TABLE IF NOT EXISTS transfers (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id        INTEGER NOT NULL REFERENCES units(id),
  from_owner_id  INTEGER NOT NULL REFERENCES users(id),
  to_owner_id    INTEGER NOT NULL REFERENCES users(id),
  transferred_at TEXT NOT NULL
);

-- 议题。内容一经发布不可改：需要变更时作废旧议题并以更高 version 重新发起。
CREATE TABLE IF NOT EXISTS issues (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,          -- 议题原文
  version     INTEGER NOT NULL DEFAULT 1,
  status      TEXT NOT NULL DEFAULT 'open'
              CHECK (status IN ('open','tallied','published','voided')),
  rule_json   TEXT NOT NULL,          -- 计票规则（发起时冻结）
  start_at    TEXT NOT NULL,
  end_at      TEXT NOT NULL,
  created_by  INTEGER NOT NULL REFERENCES users(id),
  created_at  TEXT NOT NULL,
  supersedes  INTEGER REFERENCES issues(id),  -- 本议题取代的旧议题
  void_reason TEXT
);

-- 投票资格名册快照：议题发起时冻结，此后过户不影响本次表决资格。
CREATE TABLE IF NOT EXISTS roster_entries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id   INTEGER NOT NULL REFERENCES issues(id),
  unit_id    INTEGER NOT NULL,
  unit_code  TEXT NOT NULL,
  area       REAL NOT NULL,
  owner_id   INTEGER NOT NULL,
  owner_name TEXT NOT NULL,
  UNIQUE (issue_id, unit_id)
);

-- 代理授权；授权书文件存本地 docs_dir，库中只存路径。
CREATE TABLE IF NOT EXISTS proxies (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id   INTEGER NOT NULL REFERENCES issues(id),
  unit_id    INTEGER NOT NULL,
  grantor_id INTEGER NOT NULL REFERENCES users(id),
  holder_id  INTEGER NOT NULL REFERENCES users(id),
  doc_path   TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
-- 同一议题同一户同时只允许一份有效授权
CREATE UNIQUE INDEX IF NOT EXISTS idx_proxies_active
  ON proxies (issue_id, unit_id) WHERE revoked_at IS NULL;

-- 投票版本：同户每次提交追加一行，计票时只取该户“最新的有效版本”。
CREATE TABLE IF NOT EXISTS votes (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id       INTEGER NOT NULL REFERENCES issues(id),
  unit_id        INTEGER NOT NULL,
  voter_id       INTEGER NOT NULL REFERENCES users(id),
  proxy_id       INTEGER REFERENCES proxies(id),
  choice         TEXT NOT NULL CHECK (choice IN ('approve','reject','abstain')),
  version_no     INTEGER NOT NULL,
  submitted_at   TEXT NOT NULL,
  withdrawn      INTEGER NOT NULL DEFAULT 0,
  withdrawn_at   TEXT,
  invalid_reason TEXT               -- NULL 表示有效；否则记录无效原因（可追溯）
);
CREATE INDEX IF NOT EXISTS idx_votes_unit ON votes (issue_id, unit_id, version_no);

-- 计票单：计票员生成，复核员复核后公布；两者不得为同一人。
CREATE TABLE IF NOT EXISTS tallies (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id     INTEGER NOT NULL UNIQUE REFERENCES issues(id),
  teller_id    INTEGER NOT NULL REFERENCES users(id),
  tallied_at   TEXT NOT NULL,
  result_json  TEXT NOT NULL,
  reviewed_by  INTEGER REFERENCES users(id),
  reviewed_at  TEXT,
  published_at TEXT
);

-- 模拟通知适配器的投递记录
CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  template   TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path):
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def is_empty(db_path):
    """数据库尚无业务数据（用于决定是否灌入样例）。"""
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return row["c"] == 0
    finally:
        conn.close()
