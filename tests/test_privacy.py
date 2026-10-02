"""隐私权限：截止前管理人不得见个人选择；计票员与复核员不得为同一人。"""
from helpers import (COMMITTEE, DUAL, OWNER, REVIEWER, T_AFTER, T_START,
                     TELLER, BaseCase)
from voting.services import DomainError, PermissionError_, PrivacyError


class PrivacyTest(BaseCase):

    def _issue_with_votes(self):
        issue = self.make_issue()
        self.sys.cast_vote(OWNER[1], issue, 1, "approve", now=T_START)
        self.sys.cast_vote(OWNER[2], issue, 2, "reject", now=T_START)
        return issue

    def test_manager_cannot_see_ballots_before_close(self):
        issue = self._issue_with_votes()
        for mgr in (COMMITTEE, TELLER, REVIEWER):
            with self.assertRaises(PrivacyError):
                self.sys.get_ballots(mgr, issue, now=T_START)

    def test_manager_cannot_see_ballots_after_close_before_publish(self):
        """截止但未公布（含已计票待复核）时，管理人仍不得查看明细。"""
        issue = self._issue_with_votes()
        with self.assertRaises(PrivacyError):
            self.sys.get_ballots(COMMITTEE, issue, now=T_AFTER)
        self.sys.tally_issue(TELLER, issue, now=T_AFTER)
        with self.assertRaises(PrivacyError):
            self.sys.get_ballots(COMMITTEE, issue, now=T_AFTER)

    def test_owner_sees_only_own_unit_before_close(self):
        issue = self._issue_with_votes()
        mine = self.sys.get_ballots(OWNER[1], issue, now=T_START)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["unit_id"], 1)
        self.assertEqual(mine[0]["choice"], "approve")

    def test_ballots_public_after_publish(self):
        issue = self._issue_with_votes()
        self.sys.tally_issue(TELLER, issue, now=T_AFTER)
        self.sys.publish_result(REVIEWER, issue, now=T_AFTER)
        all_rows = self.sys.get_ballots(COMMITTEE, issue, now=T_AFTER)
        self.assertEqual(len(all_rows), 2)

    def test_participation_stats_has_no_choice_data(self):
        """截止前管理人唯一可见口径：参与计数，不含任何选择分布。"""
        issue = self._issue_with_votes()
        stats = self.sys.participation_stats(issue)
        self.assertEqual(stats["voted_units"], 2)
        self.assertEqual(stats["total_units"], 5)
        self.assertNotIn("choices", stats)
        self.assertNotIn("approve", stats)

    def test_teller_and_reviewer_must_differ(self):
        issue = self._issue_with_votes()
        self.sys.tally_issue(TELLER, issue, now=T_AFTER)
        # 纯计票员没有复核角色 -> 直接越权
        with self.assertRaises(PermissionError_):
            self.sys.publish_result(TELLER, issue, now=T_AFTER)
        # 兼任两职的同一人：角色校验通过，但“不得为同一人”必须拦截
        issue2 = self.make_issue(title="另一议题")
        self.sys.tally_issue(DUAL, issue2, now=T_AFTER)
        with self.assertRaises(DomainError) as ctx:
            self.sys.publish_result(DUAL, issue2, now=T_AFTER)
        self.assertEqual(ctx.exception.code, "same_person")

    def test_publish_requires_reviewer_role(self):
        issue = self._issue_with_votes()
        self.sys.tally_issue(TELLER, issue, now=T_AFTER)
        with self.assertRaises(PermissionError_):
            self.sys.publish_result(COMMITTEE, issue, now=T_AFTER)
        with self.assertRaises(PermissionError_):
            self.sys.publish_result(OWNER[1], issue, now=T_AFTER)
        # 复核员复核后正常公布
        self.sys.publish_result(REVIEWER, issue, now=T_AFTER)
        self.assertEqual(self.sys.get_issue(issue)["status"], "published")

    def test_cannot_publish_before_tally(self):
        issue = self._issue_with_votes()
        with self.assertRaises(DomainError):
            self.sys.publish_result(REVIEWER, issue, now=T_AFTER)


if __name__ == "__main__":
    import unittest
    unittest.main()
