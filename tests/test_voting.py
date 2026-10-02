"""核心规则测试：资格快照、截止边界、票数与面积汇总、隐私权限，
以及并发只留最新版本、代理投票、计票员≠复核员、议题公示后不可改。

运行：python3 -m unittest discover -s tests -v
"""
import http.client
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone

from voteapp import services as svc
from voteapp.db import DB
from voteapp.notify import MockNotifier
from voteapp.services import BizError, PermissionDenied
from voteapp.web import serve

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
START = T0
END = T0 + timedelta(days=7)
OPTS = ["同意", "反对", "弃权"]


def make_world(areas=(100.0, 100.0, 50.0, 200.0)):
    """4 户业主 + 1 名无房新业主 + 业委会/监票/计票/复核。"""
    db = DB(":memory:")
    ids = {}
    with db.tx() as c:
        for uname, role in [("mgr", "manager"), ("scr", "scrutineer"),
                            ("cnt", "counter"), ("rev", "reviewer")]:
            cur = c.execute("INSERT INTO users(username,name,role) VALUES(?,?,?)",
                            (uname, uname.upper(), role))
            ids[uname] = cur.lastrowid
        for i, (code, area) in enumerate(
                [(f"1-10{i}", a) for i, a in enumerate(areas, start=1)], start=1):
            cur = c.execute("INSERT INTO users(username,name,role) VALUES(?,?,?)",
                            (f"owner{i}", f"业主{i}", "owner"))
            uid = cur.lastrowid
            cur = c.execute("INSERT INTO owners(user_id,name) VALUES(?,?)", (uid, f"业主{i}"))
            oid = cur.lastrowid
            cur = c.execute("INSERT INTO units(code,area) VALUES(?,?)", (code, area))
            unit_id = cur.lastrowid
            c.execute("INSERT INTO unit_ownership(unit_id,owner_id,since) VALUES(?,?,?)",
                      (unit_id, oid, "2024-01-01T00:00:00+00:00"))
            ids[f"o{i}"] = oid
            ids[f"u{i}"] = unit_id
        cur = c.execute("INSERT INTO users(username,name,role) VALUES(?,?,?)",
                        ("newcomer", "新业主", "owner"))
        uid = cur.lastrowid
        cur = c.execute("INSERT INTO owners(user_id,name) VALUES(?,?)", (uid, "新业主"))
        ids["o_new"] = cur.lastrowid
    return db, ids


def user(db, uid):
    return svc.get_user(db, uid)


def announced_topic(db, ids, start=START, end=END, now=T0):
    mgr = user(db, ids["mgr"])
    tid = svc.create_topic(db, mgr, "测试议题", "议题正文", OPTS, now=now)
    svc.announce(db, mgr, tid, start, end, "规则说明", 2 / 3, 0.5, now=now)
    return tid


class RosterSnapshotTest(unittest.TestCase):
    """资格快照：名册以投票开始时冻结为准，冻结后过户不影响本次资格。"""

    def test_freeze_then_transfer(self):
        db, ids = make_world()
        tid = announced_topic(db, ids)
        # 投票开始（名册冻结）后，1-101 过户给新业主
        svc.register_transfer(db, user(db, ids["mgr"]), ids["u1"], ids["o_new"],
                              START + timedelta(days=1), now=START + timedelta(days=1))
        # 名册仍是原业主
        _, entries = svc.roster_of(db, tid, START + timedelta(days=2))
        entry = {e["unit_id"]: e for e in entries}[ids["u1"]]
        self.assertEqual(entry["owner_id"], ids["o1"])
        # 新业主投票被拒；原业主（名册业主）仍可投
        with self.assertRaises(BizError):
            svc.cast_vote(db, ids["o_new"], tid, ids["u1"], "同意",
                          now=START + timedelta(days=2))
        vid = svc.cast_vote(db, ids["o1"], tid, ids["u1"], "同意",
                            now=START + timedelta(days=2))
        self.assertTrue(vid)
        # 过户后新发起的议题，名册用新业主
        t2 = announced_topic(db, ids, start=START + timedelta(days=2),
                             end=END + timedelta(days=2), now=START + timedelta(days=2))
        _, entries2 = svc.roster_of(db, t2, START + timedelta(days=2))
        entry2 = {e["unit_id"]: e for e in entries2}[ids["u1"]]
        self.assertEqual(entry2["owner_id"], ids["o_new"])
        # 过户与名册冻结均有审计留痕
        actions = [r["action"] for r in db.q("SELECT * FROM audit_log")]
        self.assertIn("freeze_roster", actions)
        self.assertIn("transfer", actions)


class DeadlineBoundaryTest(unittest.TestCase):
    """截止边界：有效期内可投可改，到达截止时刻即拒绝并留痕。"""

    def test_boundary(self):
        db, ids = make_world()
        tid = announced_topic(db, ids)
        # 截止前 1 秒：可投
        svc.cast_vote(db, ids["o1"], tid, ids["u1"], "同意", now=END - timedelta(seconds=1))
        # 截止前 1 秒：可改
        svc.cast_vote(db, ids["o1"], tid, ids["u1"], "反对", now=END - timedelta(seconds=1))
        self.assertEqual(svc.my_vote(db, tid, ids["u1"])["choice"], "反对")
        # 恰好截止时刻：拒绝
        with self.assertRaises(BizError):
            svc.cast_vote(db, ids["o2"], tid, ids["u2"], "同意", now=END)
        # 截止后：拒绝
        with self.assertRaises(BizError):
            svc.cast_vote(db, ids["o2"], tid, ids["u2"], "同意",
                          now=END + timedelta(seconds=1))
        # 截止后撤票同样拒绝
        with self.assertRaises(BizError):
            svc.withdraw_vote(db, ids["o1"], tid, ids["u1"], now=END + timedelta(hours=1))
        # 被拒绝的尝试已留痕（截止时间争议可追溯）
        rejected = db.q("SELECT * FROM audit_log WHERE action='cast_vote' AND result='rejected'")
        self.assertEqual(len(rejected), 2)
        self.assertIn("已过截止时间", rejected[0]["detail"])


class TallyTest(unittest.TestCase):
    """票数与面积汇总：按冻结名册统计户数与面积权重，无效票除外并注明原因。"""

    def test_tally_households_and_area(self):
        db, ids = make_world()
        tid = announced_topic(db, ids)
        t = START + timedelta(hours=1)
        svc.cast_vote(db, ids["o1"], tid, ids["u1"], "同意", now=t)          # 100㎡
        svc.cast_vote(db, ids["o2"], tid, ids["u2"], "反对", now=t)          # 100㎡
        # 先投后撤再重投：仅最新有效版本计票
        svc.cast_vote(db, ids["o3"], tid, ids["u3"], "弃权", now=t)
        svc.withdraw_vote(db, ids["o3"], tid, ids["u3"], now=t + timedelta(hours=1))
        svc.cast_vote(db, ids["o3"], tid, ids["u3"], "同意", now=t + timedelta(hours=2))  # 50㎡
        svc.cast_vote(db, ids["o4"], tid, ids["u4"], "同意", now=t)          # 200㎡，后被核定无效
        # 第一次计票：4 户全部参与，同意 3户/350㎡ → 通过
        run1 = svc.run_count(db, user(db, ids["cnt"]), tid, now=END + timedelta(hours=1))
        res1 = json.loads(db.one("SELECT result FROM counting_runs WHERE id=?", (run1,))["result"])
        self.assertTrue(res1["verdict"]["quorum_ok"])
        self.assertTrue(res1["verdict"]["passed"])
        # 截止后：计票员核定 u4 票无效，重新计票
        svc.invalidate_vote(db, user(db, ids["cnt"]),
                            db.one("SELECT id FROM votes WHERE topic_id=? AND unit_id=? AND status='active'",
                                   (tid, ids["u4"]))["id"],
                            "代理授权文件缺失", now=END + timedelta(hours=1))
        run_id = svc.run_count(db, user(db, ids["cnt"]), tid, now=END + timedelta(hours=2))
        res = json.loads(db.one("SELECT result FROM counting_runs WHERE id=?", (run_id,))["result"])
        per = {o["choice"]: o for o in res["options"]}
        # 户数：同意 2（u1,u3），反对 1（u2）；u4 无效不计
        self.assertEqual(per["同意"]["households"], 2)
        self.assertEqual(per["反对"]["households"], 1)
        self.assertEqual(per["弃权"]["households"], 0)
        # 面积：同意 100+50=150㎡，反对 100㎡
        self.assertAlmostEqual(per["同意"]["area"], 150.0)
        self.assertAlmostEqual(per["反对"]["area"], 100.0)
        # 参与：3/4 户、250/450 ㎡
        self.assertEqual(res["totals"]["voted_households"], 3)
        self.assertEqual(res["totals"]["roster_households"], 4)
        self.assertAlmostEqual(res["totals"]["voted_area"], 250.0)
        self.assertAlmostEqual(res["totals"]["roster_area"], 450.0)
        # 无效票带原因
        self.assertEqual(res["invalid"][0]["unit"], "1-104")
        self.assertEqual(res["invalid"][0]["reason"], "代理授权文件缺失")
        # 判定：u4 无效后参与面积 250/450=55.6% < 2/3 → 参与门槛未达到，不通过
        self.assertFalse(res["verdict"]["quorum_ok"])
        self.assertFalse(res["verdict"]["passed"])

    def test_tally_area_dimension_blocks_pass(self):
        """同意户数过半但同意面积未过半 → 不通过（双维度缺一不可）。"""
        db, ids = make_world(areas=(50.0, 50.0, 50.0, 300.0))
        tid = announced_topic(db, ids)
        t = START + timedelta(hours=1)
        svc.cast_vote(db, ids["o1"], tid, ids["u1"], "同意", now=t)   # 50㎡
        svc.cast_vote(db, ids["o2"], tid, ids["u2"], "同意", now=t)   # 50㎡
        svc.cast_vote(db, ids["o3"], tid, ids["u3"], "同意", now=t)   # 50㎡
        svc.cast_vote(db, ids["o4"], tid, ids["u4"], "反对", now=t)   # 300㎡
        run_id = svc.run_count(db, user(db, ids["cnt"]), tid, now=END + timedelta(hours=1))
        res = json.loads(db.one("SELECT result FROM counting_runs WHERE id=?", (run_id,))["result"])
        per = {o["choice"]: o for o in res["options"]}
        self.assertEqual(per["同意"]["households"], 3)      # 户数 3/4 = 75% 过半
        self.assertAlmostEqual(per["同意"]["area"], 150.0)  # 面积 150/450 = 33% 未过半
        self.assertTrue(res["verdict"]["quorum_ok"])        # 参与 100% 达标
        self.assertFalse(res["verdict"]["passed"])          # 面积维度未过半 → 不通过

    def test_concurrent_submissions_keep_latest_only(self):
        """并发提交：同户永远只有一条 active 版本（唯一索引兜底）。"""
        db, ids = make_world()
        tid = announced_topic(db, ids)
        errors = []

        def worker(choice):
            try:
                svc.cast_vote(db, ids["o1"], tid, ids["u1"], choice,
                              now=START + timedelta(hours=1))
            except Exception as e:  # noqa
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(c,))
                   for c in ["同意", "反对", "弃权"] * 4]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        active = db.q("SELECT * FROM votes WHERE topic_id=? AND unit_id=? AND status='active'",
                      (tid, ids["u1"]))
        self.assertEqual(len(active), 1)  # 仅最新有效版本
        total = db.q("SELECT * FROM votes WHERE topic_id=? AND unit_id=?", (tid, ids["u1"]))
        self.assertEqual(len(total), 12)  # 全部版本留痕
        self.assertEqual(active[0]["seq"], 12)


class PrivacyTest(unittest.TestCase):
    """隐私权限：截止前任何人（含管理人）看不到个人选择；计票期仅计票/复核/监票可见。"""

    def test_ballot_privacy(self):
        db, ids = make_world()
        tid = announced_topic(db, ids)
        svc.cast_vote(db, ids["o1"], tid, ids["u1"], "同意", now=START + timedelta(hours=1))
        # 截止前：所有角色都被拒
        for uname in ("mgr", "scr", "cnt", "rev"):
            with self.assertRaises(PermissionDenied, msg=uname):
                svc.list_ballots(db, user(db, ids[uname]), tid, now=START + timedelta(hours=2))
        # 业主本人只能看本户当前选择
        mine = svc.my_vote(db, tid, ids["u1"])
        self.assertEqual(mine["choice"], "同意")
        # 截止后计票期：计票/复核/监票可见；管理人仍不可见
        for uname in ("cnt", "rev", "scr"):
            ballots = svc.list_ballots(db, user(db, ids[uname]), tid, now=END + timedelta(hours=1))
            self.assertTrue(ballots)
        with self.assertRaises(PermissionDenied):
            svc.list_ballots(db, user(db, ids["mgr"]), tid, now=END + timedelta(hours=1))
        # 走完计票复核公布流程后：全员可见（唱票公开）
        run_id = svc.run_count(db, user(db, ids["cnt"]), tid, now=END + timedelta(hours=1))
        svc.review_count(db, user(db, ids["rev"]), run_id, "approved", now=END + timedelta(hours=2))
        svc.publish_result(db, user(db, ids["scr"]), tid, now=END + timedelta(hours=3))
        ballots = svc.list_ballots(db, user(db, ids["mgr"]), tid, now=END + timedelta(hours=4))
        self.assertTrue(ballots)
        # 隐私相关的被拒访问也有审计留痕
        denied = db.q("SELECT * FROM audit_log WHERE action='list_ballots' AND result='rejected'")
        self.assertTrue(denied)

    def test_http_privacy(self):
        """HTTP 层：未登录 403；管理人截止前访问选票明细被拦截。"""
        db, ids = make_world()
        tid = announced_topic(db, ids)
        svc.cast_vote(db, ids["o1"], tid, ids["u1"], "同意", now=START + timedelta(hours=1))
        server = serve(db, b"test-secret", "/tmp/auth_files_test", port=0)
        port = server.server_address[1]
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            # 未登录 → 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", f"/topics/{tid}/ballots")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 403)
            conn.close()
            # 登录管理人 → 截止前被重定向拦截，页面不含任何选择内容
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/login", body=f"user_id={ids['mgr']}",
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = conn.getresponse()
            resp.read()
            cookie = resp.getheader("Set-Cookie").split(";")[0]
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", f"/topics/{tid}/ballots", headers={"Cookie": cookie})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 303)
            self.assertIn("err=", resp.getheader("Location"))
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


class CountingReviewTest(unittest.TestCase):
    """计票员与复核员不得为同一人；公布须复核通过且由监票人执行。"""

    def test_counter_not_reviewer(self):
        db, ids = make_world()
        tid = announced_topic(db, ids)
        svc.cast_vote(db, ids["o1"], tid, ids["u1"], "同意", now=START + timedelta(hours=1))
        run_id = svc.run_count(db, user(db, ids["cnt"]), tid, now=END + timedelta(hours=1))
        # 计票员自己复核 → 拒绝
        with self.assertRaises(BizError):
            svc.review_count(db, user(db, ids["cnt"]), run_id, "approved",
                             now=END + timedelta(hours=2))
        # 非复核员复核 → 拒绝
        with self.assertRaises(PermissionDenied):
            svc.review_count(db, user(db, ids["mgr"]), run_id, "approved",
                             now=END + timedelta(hours=2))
        # 复核员复核通过 → 监票人公布
        svc.review_count(db, user(db, ids["rev"]), run_id, "approved", "无误",
                         now=END + timedelta(hours=2))
        svc.publish_result(db, user(db, ids["scr"]), tid, now=END + timedelta(hours=3))
        payload, _ = svc.get_result(db, tid)
        self.assertEqual(payload["meta"]["title"], "测试议题")
        # 非监票人公布 → 拒绝（另起一个议题验证）
        t2 = announced_topic(db, ids)
        svc.cast_vote(db, ids["o1"], t2, ids["u1"], "同意", now=START + timedelta(hours=1))
        r2 = svc.run_count(db, user(db, ids["cnt"]), t2, now=END + timedelta(hours=1))
        svc.review_count(db, user(db, ids["rev"]), r2, "approved", now=END + timedelta(hours=2))
        with self.assertRaises(PermissionDenied):
            svc.publish_result(db, user(db, ids["mgr"]), t2, now=END + timedelta(hours=3))


class ProxyTest(unittest.TestCase):
    """代理投票：授权登记、代投计入委托户、撤销后不可再投。"""

    def test_proxy_flow(self):
        db, ids = make_world()
        tid = announced_topic(db, ids)
        mgr = user(db, ids["mgr"])
        # 业主3 委托 业主2 代投 1-103
        pid = svc.register_proxy(db, mgr, tid, ids["u3"], ids["o3"], ids["o2"],
                                 "data/auth_files/test_proxy.txt", now=START + timedelta(hours=1))
        svc.cast_vote(db, ids["o2"], tid, ids["u3"], "同意", via_proxy_id=pid,
                      now=START + timedelta(hours=2))
        v = svc.my_vote(db, tid, ids["u3"])
        self.assertEqual(v["choice"], "同意")
        self.assertEqual(v["via_proxy_id"], pid)
        # 撤销代理后，受托人不能再投
        svc.revoke_proxy(db, mgr, pid, now=START + timedelta(hours=3))
        with self.assertRaises(BizError):
            svc.cast_vote(db, ids["o2"], tid, ids["u3"], "反对", via_proxy_id=pid,
                          now=START + timedelta(hours=4))
        # 非名册业主不能作为委托人登记
        with self.assertRaises(BizError):
            svc.register_proxy(db, mgr, tid, ids["u3"], ids["o_new"], ids["o2"],
                               "data/auth_files/x.txt", now=START + timedelta(hours=4))


class TopicImmutabilityTest(unittest.TestCase):
    """议题公示后内容不可改，变更须重新发起。"""

    def test_immutable_after_announce(self):
        db, ids = make_world()
        mgr = user(db, ids["mgr"])
        tid = svc.create_topic(db, mgr, "原议题", "原文", OPTS, now=T0)
        svc.edit_draft(db, mgr, tid, "原议题", "修改后的草稿", OPTS, now=T0)  # 草稿可改
        self.assertEqual(svc.latest_version(db, tid)["version_no"], 2)
        svc.announce(db, mgr, tid, START, END, "规则", 2 / 3, 0.5, now=T0)
        with self.assertRaises(BizError):
            svc.edit_draft(db, mgr, tid, "原议题", "公示后试图修改", OPTS, now=T0)
        new_id = svc.reinitiate(db, mgr, tid, now=T0)
        self.assertEqual(svc.get_topic(db, new_id)["status"], "draft")
        self.assertEqual(svc.get_topic(db, new_id)["reinitiated_from"], tid)
        self.assertEqual(svc.latest_version(db, new_id)["content"], "修改后的草稿")


class NotifyTest(unittest.TestCase):
    """模拟通知适配器：公示与公布均写入出站记录。"""

    def test_notify_outbox(self):
        db, ids = make_world()
        notifier = MockNotifier(db)
        mgr = user(db, ids["mgr"])
        tid = svc.create_topic(db, mgr, "通知测试", "正文", OPTS, now=T0)
        svc.announce(db, mgr, tid, START, END, "规则", 2 / 3, 0.5,
                     notifier=notifier, now=T0)
        rows = db.q("SELECT * FROM outbox WHERE event='topic_announced'")
        self.assertEqual(len(rows), 5)  # 5 名业主（含无房新业主）


if __name__ == "__main__":
    unittest.main()
