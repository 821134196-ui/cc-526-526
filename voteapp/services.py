"""业务服务层：议题、名册快照、投票、代理、过户、计票复核、公布、隐私权限。

设计要点：
- 所有时间用 aware datetime（UTC），内部转 ISO 字符串落库；now 可注入便于测试。
- 投票资格以投票开始时冻结的名册为准（maybe_freeze_roster 懒冻结）。
- 议题公示后内容不可改，变更须 reinitiate 重新发起。
- 同户并发/重复提交：事务内作废旧 active 行再插入新行，唯一部分索引兜底。
- 管理人及任何人在截止前都看不到个人选择（list_ballots 强制）。
- 计票员与复核员不得为同一人（review_count 强制）。
"""
import json
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))  # 展示用东八区


class BizError(Exception):
    """业务规则错误（HTTP 400）。"""


class PermissionDenied(BizError):
    """权限/隐私错误（HTTP 403）。"""


class NotFound(BizError):
    """对象不存在（HTTP 404）。"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


def fmt(s):
    """ISO(UTC) -> 东八区展示字符串。"""
    if not s:
        return "—"
    return parse(s).astimezone(CST).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 基础读取

def get_user(db, user_id):
    return db.one("SELECT * FROM users WHERE id=?", (user_id,))


def get_topic(db, topic_id):
    t = db.one("SELECT * FROM topics WHERE id=?", (topic_id,))
    if not t:
        raise NotFound("议题不存在")
    return t


def latest_version(db, topic_id):
    return db.one(
        "SELECT * FROM topic_versions WHERE topic_id=? ORDER BY version_no DESC LIMIT 1",
        (topic_id,))


def topic_phase(topic, now):
    """由时间与状态推导阶段：draft / announced(公示中,未开始) / voting / closed / published。"""
    if topic["status"] == "draft":
        return "draft"
    if topic["status"] == "published":
        return "published"
    if now < parse(topic["start_at"]):
        return "announced"
    if now < parse(topic["end_at"]):
        return "voting"
    return "closed"


PHASE_LABEL = {
    "draft": "草拟中",
    "announced": "已公示·未开始",
    "voting": "投票中",
    "closed": "已截止·待计票",
    "published": "已公布",
}

ROLE_LABEL = {
    "manager": "业委会",
    "owner": "业主",
    "scrutineer": "监票人",
    "counter": "计票员",
    "reviewer": "复核员",
}


def audit(db, actor_id, action, entity=None, entity_id=None, result="ok", detail="", now=None):
    with db.tx() as c:
        c.execute(
            "INSERT INTO audit_log(at,actor_id,action,entity,entity_id,result,detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (iso(now or utcnow()), actor_id, action, entity, entity_id, result, detail))


def _audit_tx(c, actor_id, action, entity, entity_id, result, detail, now):
    c.execute(
        "INSERT INTO audit_log(at,actor_id,action,entity,entity_id,result,detail)"
        " VALUES(?,?,?,?,?,?,?)",
        (iso(now), actor_id, action, entity, entity_id, result, detail))


# ---------------------------------------------------------------- 议题

def create_topic(db, actor, title, content, options, now=None):
    """创建草拟议题（版本 1）。"""
    now = now or utcnow()
    if actor["role"] != "manager":
        raise PermissionDenied("仅业委会可发起议题")
    if not title.strip() or not content.strip():
        raise BizError("标题与正文不能为空")
    if len(options) < 2:
        raise BizError("至少两个选项")
    with db.tx() as c:
        year = now.astimezone(CST).year
        n = c.execute("SELECT COUNT(*) n FROM topics").fetchone()["n"] + 1
        code = f"YT-{year}-{n:03d}"
        cur = c.execute(
            "INSERT INTO topics(code,status,created_by,created_at) VALUES(?,?,?,?)",
            (code, "draft", actor["id"], iso(now)))
        tid = cur.lastrowid
        c.execute(
            "INSERT INTO topic_versions(topic_id,version_no,title,content,options,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (tid, 1, title.strip(), content.strip(), json.dumps(options, ensure_ascii=False),
             actor["id"], iso(now)))
        _audit_tx(c, actor["id"], "create_topic", "topic", tid, "ok", f"创建议题《{title.strip()}》", now)
    return tid


def edit_draft(db, actor, topic_id, title, content, options, now=None):
    """草拟阶段修改内容，产生新版本；公示后禁止（须重新发起）。"""
    now = now or utcnow()
    if actor["role"] != "manager":
        raise PermissionDenied("仅业委会可修改议题")
    topic = get_topic(db, topic_id)
    if topic["status"] != "draft":
        audit(db, actor["id"], "edit_topic", "topic", topic_id, "rejected",
              "议题已公示，内容变更须重新发起", now)
        raise BizError("议题已公示，内容不可再修改；如需变更请重新发起新议题")
    if len(options) < 2:
        raise BizError("至少两个选项")
    with db.tx() as c:
        v = latest_version(db, topic_id)["version_no"] + 1
        c.execute(
            "INSERT INTO topic_versions(topic_id,version_no,title,content,options,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (topic_id, v, title.strip(), content.strip(),
             json.dumps(options, ensure_ascii=False), actor["id"], iso(now)))
        _audit_tx(c, actor["id"], "edit_topic", "topic", topic_id, "ok",
                  f"修改草稿，生成版本 v{v}", now)
    return v


def reinitiate(db, actor, topic_id, now=None):
    """议题内容变更：以最新版本内容复制生成新草拟议题（重新发起）。"""
    now = now or utcnow()
    if actor["role"] != "manager":
        raise PermissionDenied("仅业委会可重新发起议题")
    src = get_topic(db, topic_id)
    v = latest_version(db, topic_id)
    options = json.loads(v["options"])
    new_id = create_topic(db, actor, v["title"], v["content"], options, now)
    with db.tx() as c:
        c.execute("UPDATE topics SET reinitiated_from=? WHERE id=?", (topic_id, new_id))
        _audit_tx(c, actor["id"], "reinitiate", "topic", new_id, "ok",
                  f"由议题 {src['code']} 重新发起", now)
    return new_id


def announce(db, actor, topic_id, start_at, end_at, rule_text, quorum, pass_ratio, notifier=None, now=None):
    """公示议题：确定起止时间与计票规则。投票开始时冻结名册。"""
    now = now or utcnow()
    if actor["role"] != "manager":
        raise PermissionDenied("仅业委会可公示议题")
    topic = get_topic(db, topic_id)
    if topic["status"] != "draft":
        raise BizError("仅草拟中的议题可公示")
    if not (start_at < end_at):
        raise BizError("截止时间必须晚于开始时间")
    if not (0 < quorum <= 1) or not (0 < pass_ratio <= 1):
        raise BizError("门槛比例须在 (0,1] 区间")
    with db.tx() as c:
        c.execute(
            "UPDATE topics SET status='announced', start_at=?, end_at=?, rule_text=?,"
            " quorum=?, pass_ratio=? WHERE id=?",
            (iso(start_at), iso(end_at), rule_text, quorum, pass_ratio, topic_id))
        _audit_tx(c, actor["id"], "announce", "topic", topic_id, "ok",
                  f"公示议题，投票期 {fmt(iso(start_at))} ~ {fmt(iso(end_at))}", now)
    maybe_freeze_roster(db, topic_id, now)
    if notifier:
        owners = db.q("SELECT user_id FROM owners")
        notifier.broadcast([o["user_id"] for o in owners], "topic_announced",
                           {"topic_id": topic_id, "code": topic["code"]}, now)
    return True


def maybe_freeze_roster(db, topic_id, now):
    """投票开始后首次触及时冻结名册（以开始时刻的产权归属为准）。"""
    topic = get_topic(db, topic_id)
    if topic["status"] != "announced" or topic["roster_id"]:
        return topic["roster_id"]
    if now < parse(topic["start_at"]):
        return None
    with db.tx() as c:
        rows = c.execute(
            "SELECT u.id unit_id, u.code, u.area, o.id owner_id, o.name owner_name"
            " FROM units u JOIN unit_ownership uo ON uo.unit_id=u.id AND uo.until IS NULL"
            " JOIN owners o ON o.id=uo.owner_id ORDER BY u.code").fetchall()
        cur = c.execute("INSERT INTO rosters(topic_id,frozen_at) VALUES(?,?)",
                        (topic_id, iso(now)))
        rid = cur.lastrowid
        for r in rows:
            c.execute(
                "INSERT INTO roster_entries(roster_id,unit_id,unit_code,owner_id,owner_name,area)"
                " VALUES(?,?,?,?,?,?)",
                (rid, r["unit_id"], r["code"], r["owner_id"], r["owner_name"], r["area"]))
        c.execute("UPDATE topics SET roster_id=? WHERE id=?", (rid, topic_id))
        _audit_tx(c, None, "freeze_roster", "topic", topic_id, "ok",
                  f"投票开始，冻结名册 {len(rows)} 户", now)
    return rid


def roster_of(db, topic_id, now=None):
    """名册快照（未冻结返回 None）。调用前应先 maybe_freeze_roster。"""
    maybe_freeze_roster(db, topic_id, now or utcnow())
    topic = get_topic(db, topic_id)
    if not topic["roster_id"]:
        return None, []
    entries = db.q("SELECT * FROM roster_entries WHERE roster_id=? ORDER BY unit_code",
                   (topic["roster_id"],))
    return topic["roster_id"], entries


# ---------------------------------------------------------------- 过户

def register_transfer(db, actor, unit_id, to_owner_id, effective_at, notifier=None, now=None):
    """登记过户：关闭当前归属，开启新归属。已冻结名册不受影响。"""
    now = now or utcnow()
    if actor["role"] != "manager":
        raise PermissionDenied("仅业委会可登记过户")
    unit = db.one("SELECT * FROM units WHERE id=?", (unit_id,))
    if not unit:
        raise NotFound("房屋不存在")
    cur = db.one("SELECT * FROM unit_ownership WHERE unit_id=? AND until IS NULL", (unit_id,))
    if not cur:
        raise BizError("该房屋当前无归属记录")
    if cur["owner_id"] == to_owner_id:
        raise BizError("新业主与现业主相同")
    with db.tx() as c:
        c.execute("UPDATE unit_ownership SET until=? WHERE id=?",
                  (iso(effective_at), cur["id"]))
        c.execute("INSERT INTO unit_ownership(unit_id,owner_id,since) VALUES(?,?,?)",
                  (unit_id, to_owner_id, iso(effective_at)))
        c.execute(
            "INSERT INTO transfers(unit_id,from_owner_id,to_owner_id,effective_at,registered_by,registered_at)"
            " VALUES(?,?,?,?,?,?)",
            (unit_id, cur["owner_id"], to_owner_id, iso(effective_at), actor["id"], iso(now)))
        _audit_tx(c, actor["id"], "transfer", "unit", unit_id, "ok",
                  f"{unit['code']} 过户：业主#{cur['owner_id']} → 业主#{to_owner_id}，"
                  f"生效 {fmt(iso(effective_at))}；已冻结名册不受影响", now)
    if notifier:
        for oid in (cur["owner_id"], to_owner_id):
            u = db.one("SELECT user_id FROM owners WHERE id=?", (oid,))
            if u:
                notifier.send(u["user_id"], "transfer_registered",
                              {"unit": unit["code"], "effective_at": iso(effective_at)}, now)
    return True


# ---------------------------------------------------------------- 代理授权

def register_proxy(db, actor, topic_id, unit_id, grantor_owner_id, proxy_owner_id,
                   auth_file, notifier=None, now=None):
    """登记代理授权（授权书为本地虚构文件）。同户重复登记时旧授权作废。"""
    now = now or utcnow()
    topic = get_topic(db, topic_id)
    phase = topic_phase(topic, now)
    if phase in ("closed", "published"):
        raise BizError("投票已截止，不能再登记代理")
    if grantor_owner_id == proxy_owner_id:
        raise BizError("委托人与受托人不能是同一人")
    # 委托人须为当前业主（名册已冻结则以名册为准核对）
    rid, entries = roster_of(db, topic_id, now)
    if entries:
        entry = next((e for e in entries if e["unit_id"] == unit_id), None)
        if not entry or entry["owner_id"] != grantor_owner_id:
            audit(db, actor["id"], "register_proxy", "topic", topic_id, "rejected",
                  f"委托人不是名册业主（户 {unit_id}）", now)
            raise BizError("委托人不是该户名册业主，无权委托")
    else:
        cur = db.one("SELECT * FROM unit_ownership WHERE unit_id=? AND until IS NULL", (unit_id,))
        if not cur or cur["owner_id"] != grantor_owner_id:
            raise BizError("委托人不是该户当前业主")
    if actor["role"] == "owner":
        own = db.one("SELECT id FROM owners WHERE user_id=?", (actor["id"],))
        if not own or own["id"] not in (grantor_owner_id, proxy_owner_id):
            raise PermissionDenied("只能为本人相关的户登记代理")
    elif actor["role"] != "manager":
        raise PermissionDenied("仅业主本人或业委会可登记代理")
    with db.tx() as c:
        c.execute(
            "UPDATE proxies SET status='superseded', revoked_at=?"
            " WHERE topic_id=? AND unit_id=? AND status='active'",
            (iso(now), topic_id, unit_id))
        cur = c.execute(
            "INSERT INTO proxies(topic_id,unit_id,grantor_owner_id,proxy_owner_id,auth_file,"
            " created_at,created_by) VALUES(?,?,?,?,?,?,?)",
            (topic_id, unit_id, grantor_owner_id, proxy_owner_id, auth_file,
             iso(now), actor["id"]))
        pid = cur.lastrowid
        _audit_tx(c, actor["id"], "register_proxy", "proxy", pid, "ok",
                  f"议题#{topic_id} 户{unit_id}：业主#{grantor_owner_id} 委托 业主#{proxy_owner_id}，"
                  f"授权书 {auth_file}", now)
    if notifier:
        for oid in (grantor_owner_id, proxy_owner_id):
            u = db.one("SELECT user_id FROM owners WHERE id=?", (oid,))
            if u:
                notifier.send(u["user_id"], "proxy_registered",
                              {"topic_id": topic_id, "unit_id": unit_id}, now)
    return pid


def revoke_proxy(db, actor, proxy_id, now=None):
    now = now or utcnow()
    p = db.one("SELECT * FROM proxies WHERE id=?", (proxy_id,))
    if not p or p["status"] != "active":
        raise NotFound("代理授权不存在或已失效")
    topic = get_topic(db, p["topic_id"])
    if topic_phase(topic, now) in ("closed", "published"):
        raise BizError("投票已截止，不能撤销代理")
    if actor["role"] == "owner":
        own = db.one("SELECT id FROM owners WHERE user_id=?", (actor["id"],))
        if not own or own["id"] != p["grantor_owner_id"]:
            raise PermissionDenied("仅委托人本人可撤销")
    elif actor["role"] != "manager":
        raise PermissionDenied("无权撤销代理")
    with db.tx() as c:
        c.execute("UPDATE proxies SET status='revoked', revoked_at=? WHERE id=?",
                  (iso(now), proxy_id))
        _audit_tx(c, actor["id"], "revoke_proxy", "proxy", proxy_id, "ok", "撤销代理授权", now)
    return True


# ---------------------------------------------------------------- 投票

def _roster_entry(db, topic, unit_id):
    if not topic["roster_id"]:
        return None
    return db.one(
        "SELECT * FROM roster_entries WHERE roster_id=? AND unit_id=?",
        (topic["roster_id"], unit_id))


def cast_vote(db, actor_owner_id, topic_id, unit_id, choice, via_proxy_id=None, now=None):
    """投票/改票：同户仅保留最新有效版本。actor_owner_id 为提交人业主 id。"""
    now = now or utcnow()
    topic = get_topic(db, topic_id)
    maybe_freeze_roster(db, topic_id, now)
    topic = get_topic(db, topic_id)
    submitter = db.one("SELECT user_id FROM owners WHERE id=?", (actor_owner_id,))
    submitter_uid = submitter["user_id"] if submitter else None

    def reject(reason):
        # 被拒绝的投票尝试也留痕，便于截止时间争议等场景追溯
        audit(db, submitter_uid, "cast_vote", "topic", topic_id, "rejected",
              f"户{unit_id}：{reason}", now)
        raise BizError(reason)

    # 截止边界：start_at <= now < end_at，到达截止时刻即拒绝（服务端时间为准）
    if not topic["start_at"] or now < parse(topic["start_at"]):
        reject("投票尚未开始")
    if now >= parse(topic["end_at"]):
        reject("已过截止时间，投票无效")
    entry = _roster_entry(db, topic, unit_id)
    if not entry:
        reject("该户不在投票资格名册内（名册以投票开始时冻结为准）")
    if not entry["eligible"]:
        reject("该户名册资格无效")
    version = latest_version(db, topic_id)
    options = json.loads(version["options"])
    if choice not in options:
        reject("选项无效")
    # 身份核验：本人投票或持有效代理
    if via_proxy_id is None:
        if actor_owner_id != entry["owner_id"]:
            reject("提交人不是该户名册业主，且未使用代理授权")
    else:
        p = db.one("SELECT * FROM proxies WHERE id=?", (via_proxy_id,))
        if (not p or p["status"] != "active" or p["topic_id"] != topic_id
                or p["unit_id"] != unit_id or p["proxy_owner_id"] != actor_owner_id
                or p["grantor_owner_id"] != entry["owner_id"]):
            reject("代理授权无效或已撤销")
    with db.tx() as c:
        # 并发安全：同事务内作废旧版本；唯一部分索引 uq_vote_active 兜底
        c.execute(
            "UPDATE votes SET status='superseded'"
            " WHERE topic_id=? AND unit_id=? AND status='active'",
            (topic_id, unit_id))
        row = c.execute(
            "SELECT COALESCE(MAX(seq),0) s FROM votes WHERE topic_id=? AND unit_id=?",
            (topic_id, unit_id)).fetchone()
        seq = row["s"] + 1
        cur = c.execute(
            "INSERT INTO votes(topic_id,unit_id,voter_owner_id,via_proxy_id,choice,seq,submitted_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (topic_id, unit_id, actor_owner_id, via_proxy_id, choice, seq, iso(now)))
        vid = cur.lastrowid
        # 审计只记录行为不记录选项，避免未截止泄露
        _audit_tx(c, None, "cast_vote", "vote", vid, "ok",
                  f"户{entry['unit_code']} 提交第 {seq} 版选择"
                  + ("（代理提交）" if via_proxy_id else ""), now)
    return vid


def withdraw_vote(db, actor_owner_id, topic_id, unit_id, now=None):
    """撤票：截止前可撤回该户当前有效票。"""
    now = now or utcnow()
    topic = get_topic(db, topic_id)
    if topic["status"] != "announced" or not topic["end_at"] or now >= parse(topic["end_at"]):
        audit(db, None, "withdraw_vote", "topic", topic_id, "rejected", "已截止，不能撤票", now)
        raise BizError("投票已截止，不能撤票")
    v = db.one(
        "SELECT * FROM votes WHERE topic_id=? AND unit_id=? AND status='active'",
        (topic_id, unit_id))
    if not v:
        raise BizError("该户当前没有有效票")
    # 仅名册业主本人或该票提交人可撤
    entry = _roster_entry(db, topic, unit_id)
    if actor_owner_id not in (entry["owner_id"], v["voter_owner_id"]):
        raise PermissionDenied("仅该户名册业主或提交人可撤票")
    with db.tx() as c:
        c.execute("UPDATE votes SET status='withdrawn' WHERE id=?", (v["id"],))
        _audit_tx(c, None, "withdraw_vote", "vote", v["id"], "ok",
                  f"户{entry['unit_code']} 撤回第 {v['seq']} 版选择", now)
    return True


def my_vote(db, topic_id, unit_id):
    """业主查看本户当前选择（任何时点仅本人可见）。"""
    return db.one(
        "SELECT * FROM votes WHERE topic_id=? AND unit_id=? AND status='active'",
        (topic_id, unit_id))


def list_ballots(db, actor, topic_id, now=None):
    """逐户选票明细。截止前对所有人保密；截止后仅计票/复核/监票可见；公布后公开。"""
    now = now or utcnow()
    topic = get_topic(db, topic_id)
    phase = topic_phase(topic, now)
    if phase in ("announced", "voting"):
        audit(db, actor["id"], "list_ballots", "topic", topic_id, "rejected",
              "投票未截止，个人选择保密", now)
        raise PermissionDenied("投票未截止，个人选择保密")
    if phase == "closed" and actor["role"] not in ("counter", "reviewer", "scrutineer"):
        audit(db, actor["id"], "list_ballots", "topic", topic_id, "rejected",
              "仅计票员/复核员/监票人可在计票期间查看", now)
        raise PermissionDenied("仅计票员、复核员、监票人可在计票期间查看选票明细")
    # published：全体可见（唱票结果公开）
    return db.q(
        "SELECT v.*, e.unit_code, e.area, e.owner_name roster_owner, o.name voter_name"
        " FROM votes v"
        " JOIN roster_entries e ON e.roster_id=? AND e.unit_id=v.unit_id"
        " JOIN owners o ON o.id=v.voter_owner_id"
        " WHERE v.topic_id=? ORDER BY e.unit_code, v.seq",
        (topic["roster_id"], topic_id))


# ---------------------------------------------------------------- 计票与复核

def invalidate_vote(db, actor, vote_id, reason, now=None):
    """计票期间由计票员/监票人将某票核定为无效（须注明原因，可追溯）。"""
    now = now or utcnow()
    if actor["role"] not in ("counter", "scrutineer"):
        raise PermissionDenied("仅计票员或监票人可核定无效票")
    v = db.one("SELECT * FROM votes WHERE id=?", (vote_id,))
    if not v:
        raise NotFound("选票不存在")
    topic = get_topic(db, v["topic_id"])
    if topic_phase(topic, now) != "closed":
        raise BizError("仅截止后计票期间可核定无效票")
    if not reason.strip():
        raise BizError("必须注明无效原因")
    with db.tx() as c:
        c.execute("UPDATE votes SET status='invalid', invalid_reason=? WHERE id=?",
                  (reason.strip(), vote_id))
        _audit_tx(c, actor["id"], "invalidate_vote", "vote", vote_id, "ok",
                  f"核定无效：{reason.strip()}", now)
    return True


def run_count(db, actor, topic_id, now=None):
    """计票：按冻结名册汇总户数与面积权重，生成计票单（待复核）。"""
    now = now or utcnow()
    if actor["role"] != "counter":
        raise PermissionDenied("仅计票员可执行计票")
    topic = get_topic(db, topic_id)
    if topic_phase(topic, now) != "closed":
        raise BizError("投票截止后才能计票")
    rid, entries = roster_of(db, topic_id, now)
    total_h = len(entries)
    total_a = round(sum(e["area"] for e in entries), 4)
    version = latest_version(db, topic_id)
    options = json.loads(version["options"])

    active = db.q(
        "SELECT v.unit_id, v.choice, e.area FROM votes v"
        " JOIN roster_entries e ON e.roster_id=? AND e.unit_id=v.unit_id"
        " WHERE v.topic_id=? AND v.status='active'", (rid, topic_id))
    per = {o: {"households": 0, "area": 0.0} for o in options}
    voted_units = set()
    for v in active:
        if v["choice"] in per:
            per[v["choice"]]["households"] += 1
            per[v["choice"]]["area"] = round(per[v["choice"]]["area"] + v["area"], 4)
            voted_units.add(v["unit_id"])
    invalid = db.q(
        "SELECT v.id, e.unit_code, v.invalid_reason FROM votes v"
        " JOIN roster_entries e ON e.roster_id=? AND e.unit_id=v.unit_id"
        " WHERE v.topic_id=? AND v.status='invalid' ORDER BY e.unit_code", (rid, topic_id))
    part_h = len(voted_units)
    part_a = round(sum(e["area"] for e in entries if e["unit_id"] in voted_units), 4)
    quorum = topic["quorum"]
    pass_ratio = topic["pass_ratio"]
    quorum_ok = (total_h > 0 and part_h / total_h >= quorum
                 and total_a > 0 and part_a / total_a >= quorum)
    agree = per.get("同意", {"households": 0, "area": 0.0})
    passed = (quorum_ok and part_h > 0 and part_a > 0
              and agree["households"] / part_h > pass_ratio
              and agree["area"] / part_a > pass_ratio)
    result = {
        "options": [{"choice": o, "households": per[o]["households"],
                     "area": per[o]["area"]} for o in options],
        "totals": {"roster_households": total_h, "roster_area": total_a,
                   "voted_households": part_h, "voted_area": part_a},
        "invalid": [{"vote_id": v["id"], "unit": v["unit_code"],
                     "reason": v["invalid_reason"]} for v in invalid],
        "quorum": quorum, "pass_ratio": pass_ratio,
        "verdict": {"quorum_ok": quorum_ok, "passed": passed},
    }
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO counting_runs(topic_id,counter_id,created_at,result) VALUES(?,?,?,?)",
            (topic_id, actor["id"], iso(now), json.dumps(result, ensure_ascii=False)))
        run_id = cur.lastrowid
        _audit_tx(c, actor["id"], "run_count", "counting_run", run_id, "ok",
                  f"计票完成：参与 {part_h}/{total_h} 户，{part_a}/{total_a} ㎡", now)
    return run_id


def review_count(db, actor, run_id, decision, note="", now=None):
    """复核：复核员不得与计票员为同一人。"""
    now = now or utcnow()
    if actor["role"] != "reviewer":
        raise PermissionDenied("仅复核员可复核计票单")
    run = db.one("SELECT * FROM counting_runs WHERE id=?", (run_id,))
    if not run:
        raise NotFound("计票单不存在")
    if run["status"] != "pending":
        raise BizError("该计票单已复核")
    if run["counter_id"] == actor["id"]:
        audit(db, actor["id"], "review_count", "counting_run", run_id, "rejected",
              "计票员与复核员不得为同一人", now)
        raise BizError("计票员与复核员不得为同一人")
    if decision not in ("approved", "rejected"):
        raise BizError("复核结论无效")
    with db.tx() as c:
        c.execute("INSERT INTO reviews(run_id,reviewer_id,decision,note,at) VALUES(?,?,?,?,?)",
                  (run_id, actor["id"], decision, note, iso(now)))
        c.execute("UPDATE counting_runs SET status=? WHERE id=?", (decision, run_id))
        _audit_tx(c, actor["id"], "review_count", "counting_run", run_id, "ok",
                  f"复核{'通过' if decision == 'approved' else '驳回'}：{note}", now)
    return True


def publish_result(db, actor, topic_id, notifier=None, now=None):
    """监票人核对参与户数与面积权重后公布结果。"""
    now = now or utcnow()
    if actor["role"] != "scrutineer":
        raise PermissionDenied("仅监票人可公布结果")
    topic = get_topic(db, topic_id)
    if topic_phase(topic, now) != "closed":
        raise BizError("当前阶段不可公布")
    run = db.one(
        "SELECT * FROM counting_runs WHERE topic_id=? AND status='approved'"
        " ORDER BY id DESC LIMIT 1", (topic_id,))
    if not run:
        raise BizError("没有已通过复核的计票单")
    payload = json.loads(run["result"])
    rid, entries = roster_of(db, topic_id, now)
    payload["meta"] = {
        "code": topic["code"],
        "title": latest_version(db, topic_id)["title"],
        "rule_text": topic["rule_text"],
        "frozen_roster": len(entries),
        "counter_id": run["counter_id"],
        "reviewer": db.one("SELECT reviewer_id FROM reviews WHERE run_id=?", (run["id"],))["reviewer_id"],
        "scrutineer_id": actor["id"],
    }
    with db.tx() as c:
        c.execute(
            "INSERT INTO results(topic_id,run_id,published_by,published_at,payload)"
            " VALUES(?,?,?,?,?)",
            (topic_id, run["id"], actor["id"], iso(now),
             json.dumps(payload, ensure_ascii=False)))
        c.execute("UPDATE topics SET status='published', published_at=? WHERE id=?",
                  (iso(now), topic_id))
        _audit_tx(c, actor["id"], "publish", "topic", topic_id, "ok",
                  "监票人核对参与户数与面积权重，公布表决结果", now)
    if notifier:
        owners = db.q("SELECT user_id FROM owners")
        notifier.broadcast([o["user_id"] for o in owners], "result_published",
                           {"topic_id": topic_id, "code": topic["code"]}, now)
    return True


def get_result(db, topic_id):
    r = db.one("SELECT * FROM results WHERE topic_id=?", (topic_id,))
    if not r:
        return None
    return json.loads(r["payload"]), r
