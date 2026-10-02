"""票数与面积汇总：最新有效版本口径、撤票、无效票、代理与同户多人提交。"""
from datetime import datetime, timedelta

from helpers import (AREAS, OWNER, REVIEWER, T_AFTER, T_END, T_START, TELLER,
                     BaseCase)
from voting.services import DomainError


def shift(ts, **kw):
    dt = datetime.fromisoformat(ts) + timedelta(**kw)
    return dt.replace(microsecond=0).isoformat(sep=" ")


def tally_of(sys_, issue):
    sys_.tally_issue(TELLER, issue, now=T_AFTER)
    return sys_.get_tally(issue)["result"]


class TallyTest(BaseCase):

    def test_units_and_area_aggregation(self):
        """混合场景：改票、逾期改票、撤票、逾期无效票，汇总须精确。"""
        issue = self.make_issue()
        s = T_START
        # 1 号户：先赞成后改反对 -> 计反对（最新有效版本）
        self.sys.cast_vote(OWNER[1], issue, 1, "approve", now=s)
        self.sys.cast_vote(OWNER[1], issue, 1, "reject",
                           now=shift(s, hours=1))
        # 2 号户：赞成
        self.sys.cast_vote(OWNER[2], issue, 2, "approve", now=s)
        # 3 号户：赞成后逾期改弃权 -> 逾期版本无效，仍计赞成
        self.sys.cast_vote(OWNER[3], issue, 3, "approve", now=s)
        _, bad = self.sys.cast_vote(OWNER[3], issue, 3, "abstain",
                                    now=shift(T_END, minutes=5))
        self.assertEqual(bad, "late")
        # 4 号户：弃权后撤回 -> 不计入参与
        self.sys.cast_vote(OWNER[4], issue, 4, "abstain", now=s)
        self.sys.withdraw_vote(OWNER[4], issue, 4, now=shift(s, hours=2))
        # 5 号户：仅一条逾期提交 -> 无效票
        self.sys.cast_vote(OWNER[5], issue, 5, "approve",
                           now=shift(T_END, minutes=1))

        r = tally_of(self.sys, issue)
        # 参与：1、2、3 号户；面积 80+100+60=240
        self.assertEqual(r["participated_units"], 3)
        self.assertAlmostEqual(r["participated_area"], 240.0)
        self.assertEqual(r["total_units"], 5)
        self.assertAlmostEqual(r["total_area"], 450.0)
        # 赞成：2、3 号户（100+60）；反对：1 号户（80）；弃权：0
        self.assertEqual(r["choices"]["approve"]["units"], 2)
        self.assertAlmostEqual(r["choices"]["approve"]["area"], 160.0)
        self.assertEqual(r["choices"]["reject"]["units"], 1)
        self.assertAlmostEqual(r["choices"]["reject"]["area"], 80.0)
        self.assertEqual(r["choices"]["abstain"]["units"], 0)
        # 无效票：5 号户逾期
        self.assertEqual(len(r["invalid"]), 1)
        self.assertEqual(r["invalid"][0]["unit_code"], "1-105")
        self.assertEqual(r["invalid"][0]["reason"], "late")
        # 可追溯计数：1 条被取代版本（1 号户 v1）、1 条撤回
        self.assertEqual(r["superseded_versions"], 1)
        self.assertEqual(r["withdrawn_count"], 1)
        # 参与率 3/5=60% < 2/3 -> 未达法定参与比例
        self.assertFalse(r["quorum_met"])
        self.assertFalse(r["passed"])

    def test_pass_verdict_and_ratios(self):
        """全员参与：双三分之二达成，赞成双过半 -> 通过。"""
        issue = self.make_issue()
        s = T_START
        self.sys.cast_vote(OWNER[1], issue, 1, "approve", now=s)    # 80
        self.sys.cast_vote(OWNER[2], issue, 2, "approve", now=s)    # 100
        self.sys.cast_vote(OWNER[3], issue, 3, "approve", now=s)    # 60
        self.sys.cast_vote(OWNER[4], issue, 4, "reject", now=s)     # 120
        self.sys.cast_vote(OWNER[5], issue, 5, "abstain", now=s)    # 90
        r = tally_of(self.sys, issue)
        self.assertTrue(r["quorum_met"])
        self.assertTrue(r["passed"])
        # 弃权计入参与分母：赞成户数比 3/5=60%，赞成面积比 240/450≈53.3%
        self.assertAlmostEqual(
            r["choices"]["approve"]["units"] / r["participated_units"], 0.6)
        self.assertAlmostEqual(
            r["choices"]["approve"]["area"] / r["participated_area"],
            240.0 / 450.0)

    def test_same_unit_multiple_submitters_latest_wins(self):
        """同户多人提交（本人 + 代理人）：计票只取最新有效版本。"""
        issue = self.make_issue()
        s = T_START
        proxy = self.sys.grant_proxy(OWNER[2], issue, 2, OWNER[1], now=s)
        self.sys.cast_vote(OWNER[2], issue, 2, "approve", now=s)          # 本人 v1
        self.sys.cast_vote(OWNER[1], issue, 2, "reject", proxy_id=proxy,  # 代理 v2
                           now=shift(s, hours=1))
        for u in (1, 3, 4, 5):   # 其余户投赞成，凑足参与
            self.sys.cast_vote(OWNER[u], issue, u, "approve", now=s)
        r = tally_of(self.sys, issue)
        self.assertEqual(r["choices"]["reject"]["units"], 1)
        self.assertAlmostEqual(r["choices"]["reject"]["area"], AREAS[2])
        self.assertEqual(r["choices"]["approve"]["units"], 4)
        self.assertEqual(r["superseded_versions"], 1)

    def test_invalid_proxy_vote_is_recorded(self):
        """授权被撤销后代投 -> 无效票（proxy_invalid），且留痕。"""
        issue = self.make_issue()
        s = T_START
        proxy = self.sys.grant_proxy(OWNER[2], issue, 2, OWNER[1], now=s)
        self.sys.revoke_proxy(OWNER[2], proxy, now=shift(s, hours=1))
        _, bad = self.sys.cast_vote(OWNER[1], issue, 2, "approve",
                                    proxy_id=proxy, now=shift(s, hours=2))
        self.assertEqual(bad, "proxy_invalid")
        for u in (1, 3, 4, 5):
            self.sys.cast_vote(OWNER[u], issue, u, "approve", now=s)
        r = tally_of(self.sys, issue)
        self.assertEqual(len(r["invalid"]), 1)
        self.assertEqual(r["invalid"][0]["reason"], "proxy_invalid")
        self.assertEqual(r["participated_units"], 4)   # 2 号户未计入

    def test_withdraw_then_revote_counts_new_choice(self):
        issue = self.make_issue()
        s = T_START
        self.sys.cast_vote(OWNER[4], issue, 4, "abstain", now=s)
        self.sys.withdraw_vote(OWNER[4], issue, 4, now=shift(s, hours=1))
        self.sys.cast_vote(OWNER[4], issue, 4, "approve",
                           now=shift(s, hours=2))
        cur = self.sys.current_choice(issue, 4)
        self.assertEqual(cur["choice"], "approve")
        self.assertEqual(cur["version_no"], 2)   # 撤票只标记不新增版本行

    def test_tally_requires_teller_role_and_open_status(self):
        issue = self.make_issue()
        with self.assertRaises(DomainError):
            self.sys.tally_issue(REVIEWER, issue, now=T_AFTER)  # 复核员不能计票
        self.sys.tally_issue(TELLER, issue, now=T_AFTER)
        with self.assertRaises(DomainError):
            self.sys.tally_issue(TELLER, issue, now=T_AFTER)  # 不可重复计票


if __name__ == "__main__":
    import unittest
    unittest.main()
