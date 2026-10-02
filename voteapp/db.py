"""SQLite 数据层：schema、连接与事务助手。

所有写操作通过 DB.tx() 串行化（threading.RLock），
并用唯一部分索引在数据库层保证：
  - 同一议题同一户仅有一条 active 投票（并发提交只保留最新有效版本）
  - 同一议题同一户仅有一条 active 代理授权
"""
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('manager','owner','scrutineer','counter','reviewer'))
);

CREATE TABLE IF NOT EXISTS units (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,          -- 户号，如 1-1-101
  area REAL NOT NULL                  -- 专有部分面积（㎡），计票面积权重以此为准
);

CREATE TABLE IF NOT EXISTS owners (
  id INTEGER PRIMARY KEY,
  user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
  name TEXT NOT NULL
);

-- 产权归属历史：until 为 NULL 表示当前业主；过户即关闭旧行、开启新行
CREATE TABLE IF NOT EXISTS unit_ownership (
  id INTEGER PRIMARY KEY,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  owner_id INTEGER NOT NULL REFERENCES owners(id),
  since TEXT NOT NULL,
  until TEXT
);

CREATE TABLE IF NOT EXISTS transfers (
  id INTEGER PRIMARY KEY,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  from_owner_id INTEGER NOT NULL,
  to_owner_id INTEGER NOT NULL,
  effective_at TEXT NOT NULL,
  registered_by INTEGER NOT NULL,
  registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,          -- 议题编号 YT-2026-001
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','announced','published')),
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  start_at TEXT,                      -- 投票开始（同时是名册冻结时点）
  end_at TEXT,                        -- 投票截止（不含此刻）
  rule_text TEXT,                     -- 计票规则说明
  quorum REAL,                        -- 参与门槛（户数与面积均须达到的比例）
  pass_ratio REAL,                    -- 通过门槛（同意占参与的比例）
  roster_id INTEGER,
  reinitiated_from INTEGER,           -- 由哪个议题重新发起
  published_at TEXT
);

-- 议题内容版本：公示后不可再改，内容变更必须重新发起新议题
CREATE TABLE IF NOT EXISTS topic_versions (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  version_no INTEGER NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  options TEXT NOT NULL,              -- JSON 数组，如 ["同意","反对","弃权"]
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(topic_id, version_no)
);

-- 投票资格名册快照：投票开始时冻结，之后过户不影响本次资格
CREATE TABLE IF NOT EXISTS rosters (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER UNIQUE NOT NULL REFERENCES topics(id),
  frozen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roster_entries (
  id INTEGER PRIMARY KEY,
  roster_id INTEGER NOT NULL REFERENCES rosters(id),
  unit_id INTEGER NOT NULL,
  unit_code TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  owner_name TEXT NOT NULL,
  area REAL NOT NULL,
  eligible INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  UNIQUE(roster_id, unit_id)
);

-- 代理授权：授权书文件存本地 data/auth_files/，此处记录路径
CREATE TABLE IF NOT EXISTS proxies (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  unit_id INTEGER NOT NULL,
  grantor_owner_id INTEGER NOT NULL,  -- 委托人（名册业主）
  proxy_owner_id INTEGER NOT NULL,    -- 受托人
  auth_file TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by INTEGER NOT NULL,
  revoked_at TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked','superseded'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_proxy_active
  ON proxies(topic_id, unit_id) WHERE status='active';

-- 投票版本链：完整保留每次提交；仅 status='active' 的一条参与计票
CREATE TABLE IF NOT EXISTS votes (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  unit_id INTEGER NOT NULL,
  voter_owner_id INTEGER NOT NULL,
  via_proxy_id INTEGER,
  choice TEXT NOT NULL,
  seq INTEGER NOT NULL,               -- 该户第几次提交（版本号）
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active','superseded','withdrawn','invalid')),
  invalid_reason TEXT,
  submitted_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_vote_active
  ON votes(topic_id, unit_id) WHERE status='active';

CREATE TABLE IF NOT EXISTS counting_runs (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  counter_id INTEGER NOT NULL,        -- 计票员
  created_at TEXT NOT NULL,
  result TEXT NOT NULL,               -- JSON 汇总结果
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected'))
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES counting_runs(id),
  reviewer_id INTEGER NOT NULL,       -- 复核员（不得与计票员同人，服务层强制）
  decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
  note TEXT,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER UNIQUE NOT NULL REFERENCES topics(id),
  run_id INTEGER NOT NULL REFERENCES counting_runs(id),
  published_by INTEGER NOT NULL,      -- 监票人
  published_at TEXT NOT NULL,
  payload TEXT NOT NULL               -- JSON：户数比例、面积比例、无效票原因、判定
);

-- 审计日志：所有关键操作（含被拒绝的尝试）可追溯
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor_id INTEGER,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id INTEGER,
  result TEXT NOT NULL DEFAULT 'ok',  -- ok / rejected
  detail TEXT                         -- 注意：不记录具体选项，避免泄露未截止投票
);

-- 模拟通知适配器的出站记录
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  user_id INTEGER,
  event TEXT NOT NULL,
  payload TEXT
);
"""


class DB:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    @contextmanager
    def tx(self):
        """写事务：串行化 + 失败回滚。"""
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def q(self, sql, args=()):
        """只读查询，返回 Row 列表。"""
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None
