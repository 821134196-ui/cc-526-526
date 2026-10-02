"""资格快照：名册在议题发起时冻结，此后过户不影响本次表决资格。"""
from helpers import COMMITTEE, OWNER, T_START, BaseCase


class RosterSnapshotTest(BaseCase):

    def test_roster_frozen_at_creation(self):
        issue = self.make_issue()
        roster = self.sys.roster(issue)
        self.assertEqual(len(roster), 5)
        self.assertEqual([r["owner_id"] for r in roster],
                         [OWNER[1], OWNER[2], OWNER[3], OWNER[4], OWNER[5]])
        self.assertAlmostEqual(sum(r["area"] for r in roster), 450.0)

    def test_transfer_after_start_does_not_change_roster(self):
        issue = self.make_issue()
        # 投票期内 3 号户过户给新业主
        new_owner = self.sys.transfer_unit(COMMITTEE, 3, "新业主丙", now=T_START)
        roster = self.sys.roster(issue)
        entry3 = [r for r in roster if r["unit_id"] == 3][0]
        self.assertEqual(entry3["owner_id"], OWNER[3])   # 名册仍是原业主
        self.assertEqual(entry3["owner_name"], "业主3")

        # 新业主提交 -> 无效票（不在名册）；原业主提交 -> 有效
        _, bad = self.sys.cast_vote(new_owner, issue, 3, "approve", now=T_START)
        self.assertEqual(bad, "not_in_roster")
        _, ok = self.sys.cast_vote(OWNER[3], issue, 3, "approve", now=T_START)
        self.assertIsNone(ok)

    def test_transfer_applies_to_future_issues(self):
        self.make_issue()
        new_owner = self.sys.transfer_unit(COMMITTEE, 3, "新业主丙", now=T_START)
        issue2 = self.make_issue(title="第二个议题")
        entry3 = [r for r in self.sys.roster(issue2) if r["unit_id"] == 3][0]
        self.assertEqual(entry3["owner_id"], new_owner)
        # 新议题中角色互换：新业主有效，原业主不再具备资格
        _, ok = self.sys.cast_vote(new_owner, issue2, 3, "reject", now=T_START)
        self.assertIsNone(ok)
        _, bad = self.sys.cast_vote(OWNER[3], issue2, 3, "reject", now=T_START)
        self.assertEqual(bad, "not_in_roster")

    def test_transfer_is_auditable(self):
        self.sys.transfer_unit(COMMITTEE, 2, "新业主乙", now=T_START)
        conn = self.sys._conn()
        rows = conn.execute("SELECT * FROM transfers").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unit_id"], 2)
        # 旧权属已关闭，新权属已开启
        cur = conn.execute(
            "SELECT * FROM ownerships WHERE unit_id=2 AND until IS NULL"
        ).fetchone()
        self.assertEqual(cur["owner_id"], rows[0]["to_owner_id"])
        conn.close()


if __name__ == "__main__":
    import unittest
    unittest.main()
