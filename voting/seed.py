"""样例数据：含已公布结果的议题、投票中的议题（代理/过户/同户多人/逾期/撤票）、
以及“内容变更重新发起”的议题版本样例。所有时间相对启动时刻生成。"""
from datetime import datetime, timedelta

from .services import VotingSystem


def _ts(dt):
    return dt.replace(microsecond=0).isoformat(sep=" ")


def seed(sys: VotingSystem):
    now = datetime.now()
    conn = sys._conn()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO users (name, role) VALUES (?,?)",
                [("王主任", "committee"), ("李计票", "teller"),
                 ("赵复核", "reviewer")])
            owners = ["陈一", "刘二", "张三", "李四", "王五",
                      "赵六", "钱七", "孙八", "周九", "吴十"]
            conn.executemany("INSERT INTO users (name, role) VALUES (?,'owner')",
                             [(n,) for n in owners])
            areas = [88.0, 92.5, 88.0, 120.3, 75.0,
                     88.0, 101.6, 96.4, 88.0, 142.3]
            for i, area in enumerate(areas):
                code = f"{i // 5 + 1}-{101 + i % 5}"
                conn.execute("INSERT INTO units (code, area) VALUES (?,?)",
                             (code, area))
            t0 = _ts(now - timedelta(days=60))
            conn.executemany(
                "INSERT INTO ownerships (unit_id, owner_id, since) "
                "VALUES (?,?,?)",
                [(u, u + 3, t0) for u in range(1, 11)])  # 业主 user_id 从 4 起
    finally:
        conn.close()

    committee, teller, reviewer = 1, 2, 3
    owner = {i: i + 3 for i in range(1, 11)}   # 户号 -> 业主 user_id（职员占 1-3）

    # ---- 议题1：快递柜方案，已截止并公布结果 --------------------------
    s1, e1 = _ts(now - timedelta(days=15)), _ts(now - timedelta(days=5))
    issue1 = sys.create_issue(
        committee, "公共区域增设智能快递柜方案",
        "拟在小区南门东侧公共区域安装智能快递柜两组，占地约 6 平方米，"
        "由运营方承担设备与维护费用，场地租金每年 3000 元计入公共收益。"
        "现就该方案提请全体业主表决。",
        s1, e1, now=s1)
    votes1 = [(1, "approve", 12), (2, "approve", 12), (3, "approve", 11),
              (4, "reject", 11), (5, "approve", 10), (6, "reject", 10),
              (7, "abstain", 9), (8, "approve", 9)]
    for unit, choice, days_ago in votes1:
        sys.cast_vote(owner[unit], issue1, unit, choice,
                      now=_ts(now - timedelta(days=days_ago)))
    # 9 号户：先投后撤（可追溯，不计入参与）
    sys.cast_vote(owner[9], issue1, 9, "approve",
                  now=_ts(now - timedelta(days=9)))
    sys.withdraw_vote(owner[9], issue1, 9, now=_ts(now - timedelta(days=8)))
    # 10 号户：截止后提交 -> 无效票（逾期），留痕可查
    sys.cast_vote(owner[10], issue1, 10, "approve",
                  now=_ts(now - timedelta(days=4)))
    sys.tally_issue(teller, issue1, now=_ts(now - timedelta(days=4, hours=-1)))
    sys.publish_result(reviewer, issue1, now=_ts(now - timedelta(days=3)))

    # ---- 议题2：电梯广告服务合同，投票中 ------------------------------
    s2, e2 = (_ts(now - timedelta(days=1)),
              _ts(now + timedelta(days=7)))
    issue2 = sys.create_issue(
        committee, "电梯广告位服务合同（2026—2028）",
        "业委会拟与“城市视界传媒有限公司”签订电梯广告位服务合同："
        "轿厢广告位每部电梯每年 4800 元，合同期两年，收益归入公共收益账户，"
        "用于冲抵物业费及公共设施维护。合同全文已张贴于公告栏，"
        "现就签订该合同提请表决。",
        s2, e2, now=s2)

    # 代理样例：4 号户李四委托 1 号户陈一代投（授权书存 data/proxy_docs/）
    proxy4 = sys.grant_proxy(owner[4], issue2, 4, owner[1],
                             now=_ts(now - timedelta(hours=20)))
    sys.cast_vote(owner[1], issue2, 4, "approve", proxy_id=proxy4,
                  now=_ts(now - timedelta(hours=18)))
    # 同户多人提交样例：9 号户周九先自行投弃权，后又委托陈一改投赞成，
    # 计票只取该户最新有效版本
    sys.cast_vote(owner[9], issue2, 9, "abstain",
                  now=_ts(now - timedelta(hours=22)))
    proxy9 = sys.grant_proxy(owner[9], issue2, 9, owner[1],
                             now=_ts(now - timedelta(hours=6)))
    sys.cast_vote(owner[1], issue2, 9, "approve", proxy_id=proxy9,
                  now=_ts(now - timedelta(hours=5)))
    # 普通投票
    sys.cast_vote(owner[1], issue2, 1, "approve",
                  now=_ts(now - timedelta(hours=19)))
    sys.cast_vote(owner[2], issue2, 2, "reject",
                  now=_ts(now - timedelta(hours=16)))
    sys.cast_vote(owner[5], issue2, 5, "approve",
                  now=_ts(now - timedelta(hours=8)))

    # 过户样例：8 号户在议题2投票期内过户给新业主“郑新”。
    # 名册以议题发起时冻结为准，郑新提交将记为无效票（不在名册）。
    new_owner = sys.transfer_unit(committee, 8, "郑新",
                                  now=_ts(now - timedelta(hours=12)))
    sys.cast_vote(new_owner, issue2, 8, "approve",
                  now=_ts(now - timedelta(hours=2)))   # -> not_in_roster
    sys.cast_vote(owner[8], issue2, 8, "approve",
                  now=_ts(now - timedelta(hours=1)))   # 名册业主，有效

    # ---- 议题3/4：内容变更须重新发起 ----------------------------------
    s3, e3 = _ts(now - timedelta(days=3)), _ts(now + timedelta(days=4))
    issue3 = sys.create_issue(
        committee, "地下车库月租调整方案",
        "拟将地下车库月租由 300 元/月调整为 350 元/月。",
        s3, e3, now=s3)
    sys.cast_vote(owner[1], issue3, 1, "reject",
                  now=_ts(now - timedelta(days=2)))
    sys.revise_issue(
        committee, issue3, "地下车库月租调整方案",
        "拟将地下车库月租由 300 元/月调整为 320 元/月，"
        "同时对临停车辆按每小时 5 元计费，收益归入公共收益账户。"
        "（本版本根据业主意见调整了涨幅并补充临停计费规则。）",
        _ts(now - timedelta(hours=2)), _ts(now + timedelta(days=5)),
        now=_ts(now - timedelta(hours=2)))

    print("样例数据已生成：", flush=True)
    print(f"  议题#1 快递柜方案 —— 已公布结果（含逾期无效票、撤票样例）", flush=True)
    print(f"  议题#2 电梯广告合同 —— 投票中（含代理、过户、同户多人样例）", flush=True)
    print(f"  议题#3 车库月租 v1 —— 已作废（内容变更）", flush=True)
    print(f"  议题#4 车库月租 v2 —— 重新发起，投票中", flush=True)
