"""表决领域服务：议题、名册快照、投票版本、代理、过户、计票复核、隐私。

设计要点
- 名册在议题发起时冻结（roster_entries 快照），此后房屋过户不影响本次资格。
- 议题内容不可改；变更即作废旧议题并以 version+1 重新发起，全程可追溯。
- 投票以“户”为单位追加版本行；并发/重复提交只取该户最新的有效版本计票。
- 逾期、非名册、代理无效等提交仍入库并标注 invalid_reason，供争议追溯。
- 截止前，管理人（committee/teller/reviewer）不得查看任何个人选择。
- 计票员与复核员不得为同一人，服务层强制校验。
"""
import json
import os
from datetime import datetime

from .db import connect

CHOICES = ("approve", "reject", "abstain")
CHOICE_LABELS = {"approve": "赞成", "reject": "反对", "abstain": "弃权"}

# 无效票原因 -> 公示文案
INVALID_LABELS = {
    "not_in_roster": "提交人不在投票资格名册（如表决开始后过户的新业主）",
    "proxy_invalid": "代理授权无效或已撤销",
    "not_started": "投票期尚未开始",
    "late": "超过截止时间提交",
}

# 默认计票规则：参与双三分之二、同意双过半（民法典第二百七十八条口径）
DEFAULT_RULE = {
    "quorum_units": 2 / 3,   # 参与户数 / 总户数 下限
    "quorum_area": 2 / 3,    # 参与面积 / 总面积 下限
    "pass_units": 0.5,       # 赞成户数 / 参与户数 下限
    "pass_area": 0.5,        # 赞成面积 / 参与面积 下限
}


class DomainError(Exception):
    """业务规则冲突（如未截止计票、重复授权）。"""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PermissionError_(DomainError):
    """越权操作。"""
    def __init__(self, message="无权执行该操作"):
        super().__init__("forbidden", message)


class PrivacyError(PermissionError_):
    """截止前查看个人选择等隐私越权。"""
    def __init__(self, message="投票截止前，管理人不得查看个人选择"):
        super().__init__(message)
        self.code = "privacy"


def _now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _parse(ts):
    return datetime.fromisoformat(ts)


class VotingSystem:
    def __init__(self, db_path, docs_dir, notifier=None, clock=None):
        self.db_path = db_path
        self.docs_dir = docs_dir
        self.notifier = notifier          # MockNotifier，见 notify.py
        self.clock = clock or _now_iso    # 测试可注入固定时间
        os.makedirs(docs_dir, exist_ok=True)

    # ---------------------------------------------------------------- 基础
    def _conn(self):
        return connect(self.db_path)

    def _user(self, conn, user_id):
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise DomainError("no_such_user", f"用户 #{user_id} 不存在")
        return row

    @staticmethod
    def roles_of(user_row):
        """用户可兼任多角色（role 字段逗号分隔），如 'teller,reviewer'。"""
        return {r.strip() for r in user_row["role"].split(",") if r.strip()}

    def _require_role(self, conn, user_id, *roles):
        user = self._user(conn, user_id)
        if not self.roles_of(user) & set(roles):
            raise PermissionError_(f"需要 {'/'.join(roles)} 角色，当前为 {user['role']}")
        return user

    def get_issue(self, issue_id):
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
            if not row:
                raise DomainError("no_such_issue", f"议题 #{issue_id} 不存在")
            return dict(row)
        finally:
            conn.close()

    # ------------------------------------------------------------ 议题发起
    def create_issue(self, actor_id, title, body, start_at, end_at,
                     rule=None, supersedes=None, now=None):
        """发起议题：确定版本、名册、起止时间与计票规则，并冻结名册快照。"""
        now = now or self.clock()
        rule = rule or DEFAULT_RULE
        if not title.strip() or not body.strip():
            raise DomainError("empty_content", "议题标题与原文不能为空")
        if _parse(end_at) <= _parse(start_at):
            raise DomainError("bad_window", "截止时间必须晚于开始时间")
        conn = self._conn()
        try:
            self._require_role(conn, actor_id, "committee")
            with conn:
                if supersedes is not None:
                    old = conn.execute("SELECT * FROM issues WHERE id=?",
                                       (supersedes,)).fetchone()
                    if not old or old["status"] == "voided":
                        raise DomainError("bad_supersedes", "被取代的议题不存在或已作废")
                    version = old["version"] + 1
                else:
                    version = 1
                cur = conn.execute(
                    """INSERT INTO issues
                       (title, body, version, status, rule_json, start_at, end_at,
                        created_by, created_at, supersedes)
                       VALUES (?,?,?, 'open', ?,?,?,?,?,?)""",
                    (title, body, version, json.dumps(rule), start_at, end_at,
                     actor_id, now, supersedes))
                issue_id = cur.lastrowid
                # 冻结投票资格名册：当前权属关系 + 户档面积
                rows = conn.execute(
                    """SELECT u.id AS unit_id, u.code, u.area,
                              o.owner_id, w.name AS owner_name
                       FROM units u
                       JOIN ownerships o ON o.unit_id = u.id AND o.until IS NULL
                       JOIN users w ON w.id = o.owner_id
                       ORDER BY u.code""").fetchall()
                conn.executemany(
                    """INSERT INTO roster_entries
                       (issue_id, unit_id, unit_code, area, owner_id, owner_name)
                       VALUES (?,?,?,?,?,?)""",
                    [(issue_id, r["unit_id"], r["code"], r["area"],
                      r["owner_id"], r["owner_name"]) for r in rows])
            self._notify_roster(issue_id, "issue_published",
                                {"issue_id": issue_id, "title": title,
                                 "end_at": end_at})
            return issue_id
        finally:
            conn.close()

    def revise_issue(self, actor_id, issue_id, title, body, start_at, end_at,
                     rule=None, now=None):
        """议题内容变更：作废旧议题（保留全部投票记录），重新发起新版本。"""
        now = now or self.clock()
        conn = self._conn()
        try:
            self._require_role(conn, actor_id, "committee")
            old = conn.execute("SELECT * FROM issues WHERE id=?",
                               (issue_id,)).fetchone()
            if not old:
                raise DomainError("no_such_issue", f"议题 #{issue_id} 不存在")
            if old["status"] in ("published", "voided"):
                raise DomainError("cannot_revise", "已公布或已作废的议题不能再变更")
        finally:
            conn.close()
        new_id = self.create_issue(actor_id, title, body, start_at, end_at,
                                   rule=rule, supersedes=issue_id, now=now)
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE issues SET status='voided', void_reason=? WHERE id=?",
                    (f"议题内容变更，已重新发起为议题 #{new_id}", issue_id))
        finally:
            conn.close()
        self._notify_roster(issue_id, "issue_revised",
                            {"old_issue_id": issue_id, "new_issue_id": new_id})
        return new_id

    # ------------------------------------------------------------ 投票
    def cast_vote(self, voter_id, issue_id, unit_id, choice, proxy_id=None,
                  now=None):
        """提交投票。无论是否有效都追加版本行（可追溯），返回 (vote_id, invalid_reason)。

        并发安全：BEGIN IMMEDIATE 串行化同户版本号分配。
        """
        now = now or self.clock()
        if choice not in CHOICES:
            raise DomainError("bad_choice", f"非法选项 {choice}")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            issue = conn.execute("SELECT * FROM issues WHERE id=?",
                                 (issue_id,)).fetchone()
            if not issue:
                conn.rollback()
                raise DomainError("no_such_issue", f"议题 #{issue_id} 不存在")
            if issue["status"] != "open":
                conn.rollback()
                raise DomainError("issue_not_open",
                                  f"议题状态为 {issue['status']}，不再接受投票")
            self._user(conn, voter_id)
            entry = conn.execute(
                "SELECT * FROM roster_entries WHERE issue_id=? AND unit_id=?",
                (issue_id, unit_id)).fetchone()

            invalid = None
            if proxy_id is not None:
                # 代理提交：授权须存在、匹配议题与户、未撤销、在截止前已设立
                proxy = conn.execute("SELECT * FROM proxies WHERE id=?",
                                     (proxy_id,)).fetchone()
                if (not proxy or proxy["issue_id"] != issue_id
                        or proxy["unit_id"] != unit_id
                        or proxy["holder_id"] != voter_id
                        or proxy["revoked_at"] is not None
                        or proxy["created_at"] > now):
                    invalid = "proxy_invalid"
            else:
                # 本人提交：必须是名册快照中该户的业主
                if not entry or entry["owner_id"] != voter_id:
                    invalid = "not_in_roster"

            if invalid is None:
                if now < issue["start_at"]:
                    invalid = "not_started"
                elif now > issue["end_at"]:
                    invalid = "late"

            row = conn.execute(
                "SELECT COALESCE(MAX(version_no),0) AS v FROM votes "
                "WHERE issue_id=? AND unit_id=?", (issue_id, unit_id)).fetchone()
            version_no = row["v"] + 1
            cur = conn.execute(
                """INSERT INTO votes
                   (issue_id, unit_id, voter_id, proxy_id, choice, version_no,
                    submitted_at, invalid_reason)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (issue_id, unit_id, voter_id, proxy_id, choice, version_no,
                 now, invalid))
            conn.commit()
            return cur.lastrowid, invalid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def withdraw_vote(self, actor_id, issue_id, unit_id, now=None):
        """撤回该户当前有效票（保留记录）。仅名册业主本人或该票提交人可撤。"""
        now = now or self.clock()
        conn = self._conn()
        try:
            with conn:
                issue = conn.execute("SELECT * FROM issues WHERE id=?",
                                     (issue_id,)).fetchone()
                if not issue or issue["status"] != "open":
                    raise DomainError("issue_not_open", "议题不在投票期，无法撤票")
                if now > issue["end_at"]:
                    raise DomainError("closed", "投票已截止，不能撤票")
                latest = self._latest_valid(conn, issue_id, unit_id)
                if not latest:
                    raise DomainError("nothing_to_withdraw", "该户当前没有有效票")
                entry = conn.execute(
                    "SELECT * FROM roster_entries WHERE issue_id=? AND unit_id=?",
                    (issue_id, unit_id)).fetchone()
                if not (entry and entry["owner_id"] == actor_id
                        or latest["voter_id"] == actor_id):
                    raise PermissionError_("只有名册业主本人或该票提交人可以撤票")
                conn.execute(
                    "UPDATE votes SET withdrawn=1, withdrawn_at=? WHERE id=?",
                    (now, latest["id"]))
        finally:
            conn.close()

    def _latest_valid(self, conn, issue_id, unit_id):
        """该户最新的有效版本（未撤回且无无效标记）——计票与“当前选择”的口径。"""
        return conn.execute(
            """SELECT * FROM votes
               WHERE issue_id=? AND unit_id=? AND withdrawn=0
                 AND invalid_reason IS NULL
               ORDER BY version_no DESC LIMIT 1""",
            (issue_id, unit_id)).fetchone()

    def current_choice(self, issue_id, unit_id):
        conn = self._conn()
        try:
            row = self._latest_valid(conn, issue_id, unit_id)
            return dict(row) if row else None
        finally:
            conn.close()

    def ballot_history(self, issue_id, unit_id):
        """该户全部投票版本及状态标注（可追溯）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT v.*, u.name AS voter_name FROM votes v
                   JOIN users u ON u.id = v.voter_id
                   WHERE v.issue_id=? AND v.unit_id=?
                   ORDER BY v.version_no""",
                (issue_id, unit_id)).fetchall()
            valid_ids = [r["id"] for r in rows
                         if not r["withdrawn"] and not r["invalid_reason"]]
            current_id = None
            if valid_ids:
                cur = self._latest_valid(conn, issue_id, unit_id)
                current_id = cur["id"] if cur else None
            out = []
            for r in rows:
                r = dict(r)
                if r["withdrawn"]:
                    r["state"] = "已撤回"
                elif r["invalid_reason"]:
                    r["state"] = f"无效：{INVALID_LABELS[r['invalid_reason']]}"
                elif r["id"] == current_id:
                    r["state"] = "当前有效"
                else:
                    r["state"] = "已被更新版本取代"
                out.append(r)
            return out
        finally:
            conn.close()

    # ------------------------------------------------------------ 代理授权
    def grant_proxy(self, actor_id, issue_id, unit_id, holder_id, now=None):
        """名册业主将本户表决权委托给另一名名册业主，授权书存本地文件。"""
        now = now or self.clock()
        conn = self._conn()
        try:
            with conn:
                issue = conn.execute("SELECT * FROM issues WHERE id=?",
                                     (issue_id,)).fetchone()
                if not issue or issue["status"] != "open":
                    raise DomainError("issue_not_open", "议题不在投票期，无法授权")
                entry = conn.execute(
                    "SELECT * FROM roster_entries WHERE issue_id=? AND unit_id=?",
                    (issue_id, unit_id)).fetchone()
                if not entry or entry["owner_id"] != actor_id:
                    raise PermissionError_("只有名册中该户的业主可以授权他人代投")
                holder_ok = conn.execute(
                    "SELECT 1 FROM roster_entries WHERE issue_id=? AND owner_id=?",
                    (issue_id, holder_id)).fetchone()
                if not holder_ok:
                    raise DomainError("holder_not_eligible",
                                      "被委托人必须是本议题名册内的业主")
                if holder_id == actor_id:
                    raise DomainError("self_proxy", "不能委托自己")
                dup = conn.execute(
                    "SELECT 1 FROM proxies WHERE issue_id=? AND unit_id=? "
                    "AND revoked_at IS NULL", (issue_id, unit_id)).fetchone()
                if dup:
                    raise DomainError("proxy_exists", "该户已有有效授权，请先撤销")
                holder = self._user(conn, holder_id)
                doc_path = os.path.join(
                    self.docs_dir, f"proxy_{issue_id}_{unit_id}.txt")
                with open(doc_path, "w", encoding="utf-8") as f:
                    f.write(
                        "业主表决权代理授权书（虚构样例，仅用于系统演示）\n"
                        f"议题：#{issue_id} {issue['title']}\n"
                        f"委托户：{entry['unit_code']}（业主 {entry['owner_name']}）\n"
                        f"被委托人：{holder['name']}\n"
                        f"授权范围：就上述议题代为行使表决权\n"
                        f"授权时间：{now}\n"
                        f"有效期限：至议题投票截止（{issue['end_at']}）\n"
                        "委托人签名：＿＿＿＿＿＿\n")
                cur = conn.execute(
                    """INSERT INTO proxies
                       (issue_id, unit_id, grantor_id, holder_id, doc_path,
                        created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (issue_id, unit_id, actor_id, holder_id, doc_path, now))
                return cur.lastrowid
        finally:
            conn.close()

    def revoke_proxy(self, actor_id, proxy_id, now=None):
        now = now or self.clock()
        conn = self._conn()
        try:
            with conn:
                proxy = conn.execute("SELECT * FROM proxies WHERE id=?",
                                     (proxy_id,)).fetchone()
                if not proxy:
                    raise DomainError("no_such_proxy", f"授权 #{proxy_id} 不存在")
                if proxy["revoked_at"] is not None:
                    raise DomainError("already_revoked", "该授权已撤销")
                if proxy["grantor_id"] != actor_id:
                    raise PermissionError_("只有委托人可以撤销授权")
                conn.execute("UPDATE proxies SET revoked_at=? WHERE id=?",
                             (now, proxy_id))
        finally:
            conn.close()

    # ------------------------------------------------------------ 过户
    def transfer_unit(self, actor_id, unit_id, new_owner_name, now=None):
        """房屋过户：关闭旧权属、登记新业主。已冻结的名册快照不受影响。"""
        now = now or self.clock()
        conn = self._conn()
        try:
            with conn:
                self._require_role(conn, actor_id, "committee")
                cur = conn.execute(
                    "SELECT * FROM ownerships WHERE unit_id=? AND until IS NULL",
                    (unit_id,)).fetchone()
                if not cur:
                    raise DomainError("no_ownership", "该户当前没有权属登记")
                if not new_owner_name.strip():
                    raise DomainError("empty_name", "新业主姓名不能为空")
                conn.execute("UPDATE ownerships SET until=? WHERE id=?",
                             (now, cur["id"]))
                new_owner = conn.execute(
                    "INSERT INTO users (name, role) VALUES (?, 'owner')",
                    (new_owner_name.strip(),))
                conn.execute(
                    "INSERT INTO ownerships (unit_id, owner_id, since) "
                    "VALUES (?,?,?)", (unit_id, new_owner.lastrowid, now))
                conn.execute(
                    """INSERT INTO transfers
                       (unit_id, from_owner_id, to_owner_id, transferred_at)
                       VALUES (?,?,?,?)""",
                    (unit_id, cur["owner_id"], new_owner.lastrowid, now))
                return new_owner.lastrowid
        finally:
            conn.close()

    # ------------------------------------------------------------ 计票与公布
    def tally_issue(self, teller_id, issue_id, now=None):
        """截止后由计票员计票：汇总户数与面积，列出无效票及原因。"""
        now = now or self.clock()
        conn = self._conn()
        try:
            with conn:
                self._require_role(conn, teller_id, "teller")
                issue = conn.execute("SELECT * FROM issues WHERE id=?",
                                     (issue_id,)).fetchone()
                if not issue:
                    raise DomainError("no_such_issue", f"议题 #{issue_id} 不存在")
                if issue["status"] != "open":
                    raise DomainError("bad_status",
                                      f"议题状态为 {issue['status']}，不能计票")
                if now <= issue["end_at"]:
                    raise DomainError("voting_not_closed", "投票尚未截止，不能计票")
                roster = conn.execute(
                    "SELECT * FROM roster_entries WHERE issue_id=?",
                    (issue_id,)).fetchall()
                all_votes = conn.execute(
                    "SELECT * FROM votes WHERE issue_id=? ORDER BY version_no",
                    (issue_id,)).fetchall()

                by_unit = {}
                for v in all_votes:
                    by_unit.setdefault(v["unit_id"], []).append(dict(v))

                choices = {c: {"units": 0, "area": 0.0} for c in CHOICES}
                invalid, participated = [], set()
                superseded = withdrawn = 0
                for e in roster:
                    versions = by_unit.get(e["unit_id"], [])
                    valid = [v for v in versions
                             if not v["withdrawn"] and not v["invalid_reason"]]
                    superseded += max(0, len(valid) - 1)
                    withdrawn += sum(1 for v in versions if v["withdrawn"])
                    if valid:
                        latest = max(valid, key=lambda v: v["version_no"])
                        choices[latest["choice"]]["units"] += 1
                        choices[latest["choice"]]["area"] += e["area"]
                        participated.add(e["unit_id"])
                    else:
                        dead = [v for v in versions if v["invalid_reason"]]
                        if dead:
                            latest_bad = max(dead, key=lambda v: v["version_no"])
                            invalid.append({
                                "unit_code": e["unit_code"],
                                "reason": latest_bad["invalid_reason"],
                                "reason_label":
                                    INVALID_LABELS[latest_bad["invalid_reason"]],
                            })

                total_units = len(roster)
                total_area = sum(e["area"] for e in roster)
                p_units = len(participated)
                p_area = sum(e["area"] for e in roster
                             if e["unit_id"] in participated)
                rule = json.loads(issue["rule_json"])
                quorum_met = (
                    total_units > 0
                    and p_units / total_units >= rule["quorum_units"]
                    and total_area > 0
                    and p_area / total_area >= rule["quorum_area"])
                passed = bool(
                    quorum_met and p_units > 0 and p_area > 0
                    and choices["approve"]["units"] / p_units
                        >= rule["pass_units"]
                    and choices["approve"]["area"] / p_area >= rule["pass_area"])
                result = {
                    "rule": rule,
                    "total_units": total_units,
                    "total_area": round(total_area, 2),
                    "participated_units": p_units,
                    "participated_area": round(p_area, 2),
                    "choices": {k: {"units": v["units"],
                                    "area": round(v["area"], 2)}
                                for k, v in choices.items()},
                    "invalid": invalid,
                    "superseded_versions": superseded,
                    "withdrawn_count": withdrawn,
                    "quorum_met": quorum_met,
                    "passed": passed,
                }
                cur = conn.execute(
                    "INSERT INTO tallies (issue_id, teller_id, tallied_at, "
                    "result_json) VALUES (?,?,?,?)",
                    (issue_id, teller_id, now, json.dumps(result)))
                conn.execute("UPDATE issues SET status='tallied' WHERE id=?",
                             (issue_id,))
                return cur.lastrowid
        finally:
            conn.close()

    def publish_result(self, reviewer_id, issue_id, now=None):
        """复核员复核并公布。计票员与复核员不得为同一人。"""
        now = now or self.clock()
        conn = self._conn()
        try:
            with conn:
                self._require_role(conn, reviewer_id, "reviewer")
                issue = conn.execute("SELECT * FROM issues WHERE id=?",
                                     (issue_id,)).fetchone()
                if not issue or issue["status"] != "tallied":
                    raise DomainError("bad_status", "议题尚未计票或已公布")
                tally = conn.execute("SELECT * FROM tallies WHERE issue_id=?",
                                     (issue_id,)).fetchone()
                if tally["teller_id"] == reviewer_id:
                    raise DomainError("same_person",
                                      "计票员与复核员不得为同一人")
                conn.execute(
                    "UPDATE tallies SET reviewed_by=?, reviewed_at=?, "
                    "published_at=? WHERE id=?",
                    (reviewer_id, now, now, tally["id"]))
                conn.execute("UPDATE issues SET status='published' WHERE id=?",
                             (issue_id,))
        finally:
            conn.close()
        issue = self.get_issue(issue_id)
        self._notify_roster(issue_id, "result_published",
                            {"issue_id": issue_id, "title": issue["title"]})

    def get_tally(self, issue_id):
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM tallies WHERE issue_id=?",
                               (issue_id,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["result"] = json.loads(out["result_json"])
            for key, uid in (("teller", out["teller_id"]),
                             ("reviewer", out["reviewed_by"])):
                if uid:
                    name = conn.execute("SELECT name FROM users WHERE id=?",
                                        (uid,)).fetchone()
                    out[f"{key}_name"] = name["name"] if name else "?"
            return out
        finally:
            conn.close()

    # ------------------------------------------------------------ 查询（含隐私）
    def roster(self, issue_id):
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM roster_entries WHERE issue_id=? "
                "ORDER BY unit_code", (issue_id,)).fetchall()]
        finally:
            conn.close()

    def get_ballots(self, viewer_id, issue_id, now=None):
        """个人选择明细。公布前仅名册业主可看自己户；管理人一律拒绝。"""
        now = now or self.clock()
        conn = self._conn()
        try:
            issue = conn.execute("SELECT * FROM issues WHERE id=?",
                                 (issue_id,)).fetchone()
            if not issue:
                raise DomainError("no_such_issue", f"议题 #{issue_id} 不存在")
            published = issue["status"] == "published"
            viewer_entry = conn.execute(
                "SELECT * FROM roster_entries WHERE issue_id=? AND owner_id=?",
                (issue_id, viewer_id)).fetchone()
            if not published:
                if not viewer_entry:
                    raise PrivacyError()
                unit_filter = viewer_entry["unit_id"]
            else:
                unit_filter = None
            sql = """SELECT v.*, r.unit_code, u.name AS voter_name
                     FROM votes v
                     JOIN roster_entries r
                       ON r.issue_id=v.issue_id AND r.unit_id=v.unit_id
                     JOIN users u ON u.id=v.voter_id
                     WHERE v.issue_id=?"""
            args = [issue_id]
            if unit_filter is not None:
                sql += " AND v.unit_id=?"
                args.append(unit_filter)
            sql += " ORDER BY r.unit_code, v.version_no"
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def participation_stats(self, issue_id):
        """截止前对管理人唯一开放的口径：参与计数，不含任何选择信息。"""
        conn = self._conn()
        try:
            roster = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(area),0) AS a "
                "FROM roster_entries WHERE issue_id=?", (issue_id,)).fetchone()
            voted = 0
            for row in conn.execute(
                    "SELECT DISTINCT unit_id FROM votes WHERE issue_id=?",
                    (issue_id,)).fetchall():
                if self._latest_valid(conn, issue_id, row["unit_id"]):
                    voted += 1
            return {"total_units": roster["c"],
                    "total_area": round(roster["a"], 2),
                    "voted_units": voted}
        finally:
            conn.close()

    # ------------------------------------------------------------ 通知
    def _notify_roster(self, issue_id, template, payload):
        if not self.notifier:
            return
        for entry in self.roster(issue_id):
            self.notifier.send(entry["owner_id"], template, payload)
