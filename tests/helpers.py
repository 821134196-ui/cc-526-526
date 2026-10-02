"""测试公共脚手架：临时库 + 固定时钟 + 基础户档。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from voting.db import init_db
from voting.services import VotingSystem

# 固定时间轴，便于精确控制截止边界
T0 = "2026-10-01 09:00:00"   # 议题创建/名册冻结
T_START = "2026-10-02 09:00:00"
T_END = "2026-10-10 18:00:00"
T_AFTER = "2026-10-11 09:00:00"

COMMITTEE, TELLER, REVIEWER = 1, 2, 3
DUAL = 9                                # 兼任计票员与复核员（用于验证“不得为同一人”）
OWNER = {1: 4, 2: 5, 3: 6, 4: 7, 5: 8}   # 户 id -> 业主 user id
AREAS = {1: 80.0, 2: 100.0, 3: 60.0, 4: 120.0, 5: 90.0}   # 合计 450


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.docs = os.path.join(self.tmp.name, "docs")
        init_db(self.db)
        self.sys = VotingSystem(self.db, self.docs, clock=lambda: T0)
        conn = self.sys._conn()
        with conn:
            conn.executemany(
                "INSERT INTO users (name, role) VALUES (?,?)",
                [("王主任", "committee"), ("李计票", "teller"),
                 ("赵复核", "reviewer")]
                + [(f"业主{i}", "owner") for i in range(1, 6)]
                + [("钱双", "teller,reviewer")])
            for i in range(1, 6):
                conn.execute("INSERT INTO units (id, code, area) VALUES (?,?,?)",
                             (i, f"1-10{i}", AREAS[i]))
            conn.executemany(
                "INSERT INTO ownerships (unit_id, owner_id, since) "
                "VALUES (?,?,?)",
                [(i, OWNER[i], "2026-01-01 00:00:00") for i in range(1, 6)])
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def make_issue(self, **kw):
        args = dict(title="测试议题", body="议题原文", start_at=T_START,
                    end_at=T_END)
        args.update(kw)
        return self.sys.create_issue(COMMITTEE, now=T0, **args)
