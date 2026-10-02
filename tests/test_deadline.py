"""截止边界：有效期内可投可改，逾期提交记为无效票且留痕，截止后禁改禁撤。"""
from datetime import datetime, timedelta

from helpers import OWNER, T_END, T_START, BaseCase
from voting.services import DomainError


def shift(ts, **kw):
    dt = datetime.fromisoformat(ts) + timedelta(**kw)
    return dt.replace(microsecond=0).isoformat(sep=" ")


class DeadlineTest(BaseCase):

    def test_vote_one_second_before_deadline_is_valid(self):
        issue = self.make_issue()
        _, invalid = self.sys.cast_vote(OWNER[1], issue, 1, "approve",
                                        now=shift(T_END, seconds=-1))
        self.assertIsNone(invalid)

    def test_deadline_moment_is_still_valid(self):
        """有效期含端点：截止时刻（含）之前都可投，之后一律无效。"""
        issue = self.make_issue()
        _, invalid = self.sys.cast_vote(OWNER[3], issue, 3, "approve", now=T_END)
        self.assertIsNone(invalid)

    def test_vote_after_deadline_is_invalid_but_recorded(self):
        issue = self.make_issue()
        for t in (shift(T_END, seconds=1), shift(T_END, hours=2)):
            _, invalid = self.sys.cast_vote(OWNER[2], issue, 2, "reject", now=t)
            self.assertEqual(invalid, "late")
        # 两条逾期提交全部留痕可查
        history = self.sys.ballot_history(issue, 2)
        self.assertEqual(len(history), 2)
        self.assertTrue(all(h["invalid_reason"] == "late" for h in history))

    def test_late_change_does_not_override_valid_vote(self):
        """“只保留该户最新的有效版本”：逾期版本无效，原有效票仍然作数。"""
        issue = self.make_issue()
        self.sys.cast_vote(OWNER[1], issue, 1, "approve",
                           now=shift(T_END, hours=-1))
        _, invalid = self.sys.cast_vote(OWNER[1], issue, 1, "reject",
                                        now=shift(T_END, seconds=30))
        self.assertEqual(invalid, "late")
        cur = self.sys.current_choice(issue, 1)
        self.assertEqual(cur["choice"], "approve")   # 仍是截止前的选择

    def test_cannot_vote_before_start(self):
        issue = self.make_issue()
        _, invalid = self.sys.cast_vote(OWNER[1], issue, 1, "approve",
                                        now=shift(T_START, seconds=-1))
        self.assertEqual(invalid, "not_started")

    def test_cannot_withdraw_after_deadline(self):
        issue = self.make_issue()
        self.sys.cast_vote(OWNER[1], issue, 1, "approve",
                           now=shift(T_END, hours=-1))
        with self.assertRaises(DomainError) as ctx:
            self.sys.withdraw_vote(OWNER[1], issue, 1,
                                   now=shift(T_END, seconds=1))
        self.assertEqual(ctx.exception.code, "closed")

    def test_cannot_tally_before_deadline(self):
        issue = self.make_issue()
        with self.assertRaises(DomainError) as ctx:
            self.sys.tally_issue(2, issue, now=T_END)   # 恰在截止时刻也不行
        self.assertEqual(ctx.exception.code, "voting_not_closed")
        # 截止后即可计票
        tally_id = self.sys.tally_issue(2, issue, now=shift(T_END, seconds=1))
        self.assertIsInstance(tally_id, int)


if __name__ == "__main__":
    import unittest
    unittest.main()
