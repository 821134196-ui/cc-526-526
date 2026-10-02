"""种子数据：12 户业主、4 个议题（投票中/待复核/已公布/草稿），
包含过户样例（1-2-202 投票期间过户）与代理样例（2-1-301 委托邻居代投）。
授权书为虚构文件，写入本地 data/auth_files/。
"""
import json
import os
from datetime import timedelta

from . import services as svc
from .notify import MockNotifier

RULE_TEXT = ("依据《民法典》第二百七十八条：应当由专有部分面积占比三分之二以上的业主"
             "且人数占比三分之二以上的业主参与表决；经参与表决专有部分面积过半数且"
             "参与表决人数过半数同意即为通过。")

OWNERS = [  # (username, 姓名, 户号, 面积)
    ("zhouming", "周明", "1-1-101", 89.5),
    ("wufang", "吴芳", "1-1-102", 89.5),
    ("zhengqiang", "郑强", "1-2-201", 110.2),
    ("wangxue", "王雪", "1-2-202", 110.2),
    ("liwei", "李伟", "2-1-301", 96.0),
    ("zhangmin", "张敏", "2-1-302", 96.0),
    ("liuyang", "刘洋", "2-2-401", 128.4),
    ("zhaojing", "赵静", "2-2-402", 128.4),
    ("sunpeng", "孙鹏", "3-1-501", 75.3),
    ("linxia", "林霞", "3-1-502", 75.3),
    ("heyong", "何勇", "3-2-601", 140.8),
    ("gaoli", "高丽", "3-2-602", 140.8),
]
STAFF = [  # (username, 姓名, role)
    ("wangjh", "王建华", "manager"),
    ("zhaodz", "赵德柱", "scrutineer"),
    ("qianjin", "钱进", "counter"),
    ("sunli", "孙立", "reviewer"),
]

PROXY_AUTH_TEMPLATE = """授权委托书（虚构样例，仅用于系统演示）

委托人：{grantor}（{unit} 业主）
受托人：{proxy}

本人因故不能亲自参加业主大会表决，现委托受托人代为就议题
《{topic}》（编号 {code}）行使表决权。受托人在授权范围内
作出的表决视为本人真实意思表示。

委托期限：自本授权书签署之日起至该议题投票截止之日止。

委托人（签章）：{grantor}
受托人（签章）：{proxy}
"""


def _uid(db, username):
    return db.one("SELECT id FROM users WHERE username=?", (username,))["id"]


def _oid(db, username):
    return db.one(
        "SELECT o.id FROM owners o JOIN users u ON u.id=o.user_id WHERE u.username=?",
        (username,))["id"]


def _unit(db, code):
    return db.one("SELECT * FROM units WHERE code=?", (code,))


def seed(db, auth_dir="data/auth_files", now=None):
    now = now or svc.utcnow()
    os.makedirs(auth_dir, exist_ok=True)
    notifier = MockNotifier(db)

    with db.tx() as c:
        for uname, name, role in STAFF:
            c.execute("INSERT INTO users(username,name,role) VALUES(?,?,?)",
                      (uname, name, role))
        for uname, name, code, area in OWNERS:
            c.execute("INSERT INTO users(username,name,role) VALUES(?,?,?)",
                      (uname, name, "owner"))
        # 过户样例的新业主（当前不持有任何房屋）
        c.execute("INSERT INTO users(username,name,role) VALUES(?,?,?)",
                  ("chenchen", "陈晨", "owner"))
        for _, _, code, area in OWNERS:
            c.execute("INSERT INTO units(code,area) VALUES(?,?)", (code, area))
        for uname, name, code, _ in OWNERS:
            uid = _uid(db, uname)
            cur = c.execute("INSERT INTO owners(user_id,name) VALUES(?,?)", (uid, name))
            c.execute(
                "INSERT INTO unit_ownership(unit_id,owner_id,since) VALUES(?,?,?)",
                (_unit(db, code)["id"], cur.lastrowid, "2024-01-01T00:00:00+00:00"))
        cur = c.execute("INSERT INTO owners(user_id,name) VALUES(?,?)",
                        (_uid(db, "chenchen"), "陈晨"))
        chenchen_oid = cur.lastrowid

    manager = svc.get_user(db, _uid(db, "wangjh"))
    scrutineer = svc.get_user(db, _uid(db, "zhaodz"))
    counter = svc.get_user(db, _uid(db, "qianjin"))
    reviewer = svc.get_user(db, _uid(db, "sunli"))
    code_of = {u: c for u, n, c, a in OWNERS}  # 用户名 → 户号

    def oid(u):
        return _oid(db, u)

    def unit_id(code):
        return _unit(db, code)["id"]

    # ------------------------------------------------ T4 草稿
    svc.create_topic(db, manager, "地下车库新能源汽车充电桩安装方案",
                     "拟在地下车库 B 区统一安装 20 个新能源汽车充电桩，"
                     "由第三方运营商投资建设并运营，电费按实结算，"
                     "场地租金归入小区公共收益。现提交业主大会表决。",
                     ["同意", "反对", "弃权"], now=now - timedelta(days=1))

    # ------------------------------------------------ T3 已公布（历史议题）
    t3 = svc.create_topic(
        db, manager, "小区公共区域广告位出租方案",
        "拟将小区东门、南门两处公共墙面广告位整体出租给广告公司，"
        "租期两年，年租金 6 万元，收益归入小区公共收益账户，"
        "用于公共设施维护。合同范本已在物业办公室公示。",
        ["同意", "反对", "弃权"], now=now - timedelta(days=35))
    svc.announce(db, manager, t3,
                 now - timedelta(days=30), now - timedelta(days=20),
                 RULE_TEXT, 2 / 3, 0.5, notifier, now=now - timedelta(days=30))
    t3_votes = {"zhouming": "同意", "wufang": "同意", "zhengqiang": "同意",
                "wangxue": "同意", "liwei": "同意", "zhangmin": "同意",
                "liuyang": "同意", "zhaojing": "同意", "sunpeng": "反对",
                "heyong": "反对", "gaoli": "弃权", "linxia": "弃权"}
    for i, (uname, choice) in enumerate(t3_votes.items()):
        svc.cast_vote(db, oid(uname), t3, unit_id(code_of[uname]),
                      choice, now=now - timedelta(days=25, hours=i))
    # 林霞的票在计票期间被核定为无效（可追溯）
    linxia_vote = db.one(
        "SELECT v.id FROM votes v JOIN units u ON u.id=v.unit_id"
        " WHERE v.topic_id=? AND u.code='3-1-502' AND v.status='active'", (t3,))["id"]
    svc.invalidate_vote(db, scrutineer, linxia_vote,
                        "该户产权过户纠纷未决，投票人资格存疑，监票人核定无效",
                        now=now - timedelta(days=18))
    run3 = svc.run_count(db, counter, t3, now=now - timedelta(days=17))
    svc.review_count(db, reviewer, run3, "approved", "逐户复核无误", now=now - timedelta(days=16))
    svc.publish_result(db, scrutineer, t3, notifier, now=now - timedelta(days=15))

    # ------------------------------------------------ T2 已截止待复核
    t2 = svc.create_topic(
        db, manager, "物业服务合同续签（2026—2028 年度）",
        "现物业服务合同将于 2026 年底到期。业委会与物业公司协商拟定续签合同："
        "物业费维持 2.8 元/㎡·月不变，服务标准按原合同附件执行，"
        "新增每季度一次服务质量业主评议条款。合同文本已在公告栏公示。",
        ["同意", "反对", "弃权"], now=now - timedelta(days=12))
    svc.announce(db, manager, t2,
                 now - timedelta(days=9), now - timedelta(days=2),
                 RULE_TEXT, 2 / 3, 0.5, notifier, now=now - timedelta(days=9))
    t2_votes = {"zhouming": "同意", "wufang": "同意", "liwei": "同意",
                "zhangmin": "同意", "liuyang": "同意", "zhaojing": "同意",
                "sunpeng": "同意", "zhengqiang": "反对", "wangxue": "反对",
                "heyong": "反对"}
    for i, (uname, choice) in enumerate(t2_votes.items()):
        svc.cast_vote(db, oid(uname), t2, unit_id(code_of[uname]),
                      choice, now=now - timedelta(days=7, hours=i))
    # 代理样例（后被撤销）：高丽委托何勇代投，撤销后该票被核定无效
    p2 = svc.register_proxy(db, svc.get_user(db, _uid(db, "gaoli")), t2,
                            unit_id("3-2-602"), oid("gaoli"), oid("heyong"),
                            "data/auth_files/proxy_T2_3-2-602.txt",
                            notifier, now=now - timedelta(days=8))
    svc.cast_vote(db, oid("heyong"), t2, unit_id("3-2-602"), "同意",
                  via_proxy_id=p2, now=now - timedelta(days=7, hours=20))
    svc.revoke_proxy(db, svc.get_user(db, _uid(db, "gaoli")), p2,
                     now=now - timedelta(days=6))
    gaoli_vote = db.one(
        "SELECT v.id FROM votes v JOIN units u ON u.id=v.unit_id"
        " WHERE v.topic_id=? AND u.code='3-2-602' AND v.status='active'", (t2,))["id"]
    svc.invalidate_vote(db, counter, gaoli_vote,
                        "代理授权已于投票截止前撤销，授权文件失效，计票员核定无效",
                        now=now - timedelta(days=1, hours=2))
    svc.run_count(db, counter, t2, now=now - timedelta(days=1))

    # ------------------------------------------------ T1 投票中（含代理与改票、撤票样例）
    t1 = svc.create_topic(
        db, manager, "关于在小区东门公共区域设置智能快递柜的议题",
        "为缓解快递堆放占道问题，拟在东门内侧公共区域（约 15 ㎡）设置"
        "智能快递柜一组，由运营方承担全部建设运维费用，每年向小区"
        "公共收益账户支付场地使用费 3000 元。设置位置示意图已在公告栏公示。",
        ["同意", "反对", "弃权"], now=now - timedelta(days=2))
    svc.announce(db, manager, t1,
                 now - timedelta(days=1), now + timedelta(days=6),
                 RULE_TEXT, 2 / 3, 0.5, notifier, now=now - timedelta(days=1))

    # 代理样例：李伟委托邻居张敏代投，授权书存本地虚构文件
    topic1 = svc.get_topic(db, t1)
    auth_path = os.path.join(auth_dir, f"proxy_{topic1['code']}_2-1-301.txt")
    with open(auth_path, "w", encoding="utf-8") as f:
        f.write(PROXY_AUTH_TEMPLATE.format(
            grantor="李伟", proxy="张敏", unit="2-1-301",
            topic="关于在小区东门公共区域设置智能快递柜的议题", code=topic1["code"]))
    p1 = svc.register_proxy(db, svc.get_user(db, _uid(db, "liwei")), t1,
                            unit_id("2-1-301"), oid("liwei"), oid("zhangmin"),
                            auth_path, notifier, now=now - timedelta(hours=22))

    svc.cast_vote(db, oid("zhouming"), t1, unit_id("1-1-101"), "同意",
                  now=now - timedelta(hours=20))
    svc.cast_vote(db, oid("wufang"), t1, unit_id("1-1-102"), "同意",
                  now=now - timedelta(hours=18))
    # 改票样例：郑强先投反对，后改为弃权（仅最新版本有效）
    svc.cast_vote(db, oid("zhengqiang"), t1, unit_id("1-2-201"), "反对",
                  now=now - timedelta(hours=16))
    svc.cast_vote(db, oid("zhengqiang"), t1, unit_id("1-2-201"), "弃权",
                  now=now - timedelta(hours=3))
    # 代理投票样例：张敏代李伟投票
    svc.cast_vote(db, oid("zhangmin"), t1, unit_id("2-1-301"), "同意",
                  via_proxy_id=p1, now=now - timedelta(hours=10))
    # 撤票样例：刘洋投票后撤回
    svc.cast_vote(db, oid("liuyang"), t1, unit_id("2-2-401"), "同意",
                  now=now - timedelta(hours=8))
    svc.withdraw_vote(db, oid("liuyang"), t1, unit_id("2-2-401"),
                      now=now - timedelta(hours=2))

    # ------------------------------------------------ 过户样例：名册冻结后过户
    svc.register_transfer(db, manager, unit_id("1-2-202"), chenchen_oid,
                          now - timedelta(hours=12), notifier, now=now)

    return {"t1": t1, "t2": t2, "t3": t3}
