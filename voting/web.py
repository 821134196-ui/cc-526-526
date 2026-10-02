"""HTTP 层：路由 + 服务端渲染页面。

视觉：白底、深灰正文、少量公示蓝点缀，适合正式阅读。
表决页同屏呈现议题原文、截止时间与当前选择；公布页并列展示户数比例与面积比例。
"""
import html
import json
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .db import connect
from .services import (CHOICE_LABELS, INVALID_LABELS, DomainError,
                       PermissionError_, PrivacyError, VotingSystem)

CSS = """
:root{--ink:#2b2f33;--muted:#6b7280;--blue:#0b5cad;--blue-soft:#eaf2fb;
--line:#e3e6ea;--soft:#f6f7f9;--red:#b3261e}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);
font:15px/1.7 "PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
header.top{border-bottom:1px solid var(--line);background:#fff}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
.topbar{display:flex;align-items:center;gap:22px;height:58px}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:16px}
.brand .mark{width:26px;height:26px;border-radius:6px;background:var(--blue);
color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px}
nav{display:flex;gap:4px;flex:1}
nav a{color:var(--ink);padding:6px 10px;border-radius:6px;font-size:14px}
nav a.active{color:var(--blue);background:var(--blue-soft);font-weight:600}
.who{font-size:13px;color:var(--muted)}
main{padding:26px 0 60px}
h1{font-size:21px;margin:6px 0 16px}
h2{font-size:16px;margin:0 0 10px}
.card{border:1px solid var(--line);border-radius:10px;padding:18px 20px;
margin-bottom:16px;background:#fff}
.card.soft{background:var(--soft)}
.muted{color:var(--muted)}
.small{font-size:13px}
.tag{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;
border:1px solid var(--line);color:var(--muted)}
.tag.blue{color:var(--blue);border-color:#bcd6f3;background:var(--blue-soft)}
.tag.green{color:#1a7f37;border-color:#b7e2c4;background:#eefaf1}
.tag.gray{color:var(--muted);background:var(--soft)}
.tag.red{color:var(--red);border-color:#eec5c2;background:#fdf1f0}
.btn{display:inline-block;border:1px solid var(--line);background:#fff;
color:var(--ink);border-radius:8px;padding:7px 16px;font-size:14px;cursor:pointer}
.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}
.btn.primary:hover{background:#0a4f94}
.btn.danger{color:var(--red);border-color:#eec5c2}
.btn:disabled{opacity:.45;cursor:not-allowed}
.choices{display:flex;gap:10px;margin:10px 0}
.choices .btn{flex:1;padding:12px 0;font-size:15px;text-align:center}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
th{color:var(--muted);font-weight:500;font-size:13px}
.grid2{display:grid;grid-template-columns:1.5fr 1fr;gap:16px;align-items:start}
.grid2eq{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.deadline{font-size:26px;font-weight:700;color:var(--blue);letter-spacing:.5px}
.body-text{white-space:pre-wrap;line-height:1.9}
.bar{height:14px;border-radius:7px;background:var(--soft);overflow:hidden;margin:4px 0 10px}
.bar>i{display:block;height:100%;background:var(--blue)}
.bar>i.gray{background:#9aa1a9}.bar>i.light{background:#c9ced4}
.row{display:flex;justify-content:space-between;font-size:14px;margin:2px 0}
.notice{border-left:3px solid var(--blue);background:var(--blue-soft);
padding:10px 14px;border-radius:0 8px 8px 0;font-size:14px;margin-bottom:16px}
.notice.err{border-color:var(--red);background:#fdf1f0;color:var(--red)}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;font:inherit;margin:4px 0 12px}
label{font-size:13px;color:var(--muted)}
.verdict{font-size:17px;font-weight:700}
.verdict.pass{color:#1a7f37}.verdict.fail{color:var(--red)}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:12px;
padding:14px 0;text-align:center}
"""

NAV = [("/issues", "议题公示"), ("/proxies", "代理授权"),
       ("/tally", "计票复核"), ("/admin", "管理")]


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_pct(x):
    return f"{x * 100:.1f}%"


def q(s):
    """URL 查询参数编码（重定向 Location 头必须是 latin-1 安全字符）。"""
    return quote(str(s), safe="")


def has_role(user, *roles):
    """用户可兼任多角色（role 逗号分隔）。"""
    if not user:
        return False
    owned = {r.strip() for r in user["role"].split(",")}
    return bool(owned & set(roles))


class App:
    def __init__(self, system: VotingSystem):
        self.sys = system

    # ---------------- 渲染骨架 ----------------
    def page(self, title, body, user=None, active="", msg=None, err=None):
        nav = "".join(
            f'<a href="{p}" class="{"active" if p == active else ""}">{t}</a>'
            for p, t in NAV)
        who = (f'{esc(user["name"])}（{self.role_label(user["role"])}） '
               f'<a href="/logout">退出</a>') if user else '<a href="/login">登录</a>'
        flash = ""
        if msg:
            flash += f'<div class="notice">{esc(msg)}</div>'
        if err:
            flash += f'<div class="notice err">{esc(err)}</div>'
        return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 业委会表决系统</title><style>{CSS}</style></head><body>
<header class="top"><div class="wrap topbar">
<div class="brand"><span class="mark">表决</span>业委会表决系统</div>
<nav>{nav}</nav><div class="who">{who}</div>
</div></header>
<main><div class="wrap">{flash}{body}</div></main>
<footer>业主大会电子表决演示系统 · 数据仅保存在本地 SQLite</footer>
</body></html>"""

    @staticmethod
    def role_label(role):
        labels = {"owner": "业主", "committee": "业委会",
                  "teller": "计票员", "reviewer": "复核员"}
        return "/".join(labels.get(r.strip(), r) for r in role.split(","))

    @staticmethod
    def status_tag(issue, now):
        s = issue["status"]
        if s == "open":
            if now < issue["start_at"]:
                return '<span class="tag">未开始</span>'
            if now > issue["end_at"]:
                return '<span class="tag gray">已截止待计票</span>'
            return '<span class="tag blue">投票中</span>'
        return {"tallied": '<span class="tag gray">已计票待复核</span>',
                "published": '<span class="tag green">结果已公布</span>',
                "voided": '<span class="tag red">已作废</span>'}[s]

    # ---------------- 页面 ----------------
    def issues_page(self, user, msg, err):
        conn = self.sys._conn()
        rows = conn.execute(
            "SELECT * FROM issues ORDER BY id DESC").fetchall()
        conn.close()
        now = self.sys.clock()
        items = ""
        for i in rows:
            items += f"""<div class="card">
<div style="display:flex;justify-content:space-between;align-items:center">
<div><a href="/issues/{i['id']}"><b>#{i['id']} {esc(i['title'])}</b></a>
<span class="tag">v{i['version']}</span> {self.status_tag(i, now)}</div>
<div class="small muted">{esc(i['start_at'])} 至 {esc(i['end_at'])}</div></div>
<div class="small muted" style="margin-top:6px">{esc(i['body'][:90])}……</div>
<div class="small" style="margin-top:6px">
<a href="/issues/{i['id']}/roster">资格名册</a> ·
<a href="/issues/{i['id']}/result">表决结果</a>
{f'· <span class="muted">作废原因：' + esc(i['void_reason']) + '</span>' if i['void_reason'] else ''}
</div></div>"""
        new_btn = ('<a class="btn primary" href="/issues/new">发起议题</a>'
                   if has_role(user, "committee") else "")
        body = f"""<div style="display:flex;justify-content:space-between;
align-items:center"><h1>议题公示</h1><div>{new_btn}</div></div>{items}"""
        return self.page("议题公示", body, user, "/issues", msg, err)

    def issue_detail(self, user, issue_id, msg, err):
        issue = self.sys.get_issue(issue_id)
        now = self.sys.clock()
        rule = json.loads(issue["rule_json"])

        # 左栏：议题原文（公示）
        rule_txt = (f"参与门槛：户数≥{fmt_pct(rule['quorum_units'])} 且 "
                    f"面积≥{fmt_pct(rule['quorum_area'])}；通过条件：赞成户数占参与"
                    f"≥{fmt_pct(rule['pass_units'])} 且 赞成面积占参与"
                    f"≥{fmt_pct(rule['pass_area'])}")
        left = f"""<div class="card">
<h2>议题原文 <span class="tag">版本 v{issue['version']}</span></h2>
<div class="body-text">{esc(issue['body'])}</div>
<hr style="border:none;border-top:1px solid var(--line);margin:14px 0">
<div class="small muted">计票规则（发起时冻结）：{esc(rule_txt)}</div>
<div class="small muted">投票期：{esc(issue['start_at'])} ～ {esc(issue['end_at'])}</div>
</div>"""

        # 右栏：截止时间 + 当前选择 + 投票操作（同一屏）
        stats = self.sys.participation_stats(issue_id)
        right = f"""<div class="card soft" style="text-align:center">
<div class="small muted">投票截止</div>
<div class="deadline">{esc(issue['end_at'][:16])}</div>
<div class="small muted">状态：{self.status_tag(issue, now)} ·
已投 {stats['voted_units']} / {stats['total_units']} 户</div></div>"""

        if user:
            right += self._ballot_box(user, issue, now)
        else:
            right += ('<div class="card"><div class="muted small">'
                      '<a href="/login">登录</a> 后可查看本户当前选择并投票。</div></div>')

        ops = ""
        if has_role(user, "committee") and issue["status"] in ("open", "tallied"):
            ops = (f'<div class="card"><a class="btn" '
                   f'href="/issues/{issue_id}/revise">变更议题内容（重新发起）</a>'
                   f'<div class="small muted" style="margin-top:6px">'
                   f'议题内容一经发布不可直接修改，变更将作废本议题并生成新版本。</div></div>')

        body = f"""<h1>#{issue['id']} {esc(issue['title'])}
{self.status_tag(issue, now)}</h1>
<div class="grid2"><div>{left}{ops}</div><div>{right}</div></div>"""
        return self.page(issue["title"], body, user, "/issues", msg, err)

    def _ballot_box(self, user, issue, now):
        """当前选择 + 投票/撤票 + 投票历史 + 代理代投入口。"""
        conn = self.sys._conn()
        entries = conn.execute(
            "SELECT * FROM roster_entries WHERE issue_id=? AND owner_id=?",
            (issue["id"], user["id"])).fetchall()
        proxies = conn.execute(
            """SELECT p.*, r.unit_code FROM proxies p
               JOIN roster_entries r ON r.issue_id=p.issue_id
                AND r.unit_id=p.unit_id
               WHERE p.issue_id=? AND p.holder_id=? AND p.revoked_at IS NULL""",
            (issue["id"], user["id"])).fetchall()
        conn.close()

        is_mgr = has_role(user, "committee", "teller", "reviewer")
        if is_mgr and not entries:
            return ('<div class="card"><h2>当前选择</h2><div class="muted small">'
                    '投票截止前，管理人不得查看任何个人选择。</div></div>')

        out = ""
        open_now = issue["status"] == "open" and issue["start_at"] <= now <= issue["end_at"]

        def unit_box(unit_id, unit_code, proxy_id=None, proxy_note=""):
            cur = self.sys.current_choice(issue["id"], unit_id)
            cur_txt = (f'<span class="tag blue">{CHOICE_LABELS[cur["choice"]]}'
                       f'</span> <span class="small muted">第 {cur["version_no"]} 版 · '
                       f'{esc(cur["submitted_at"][:16])}</span>'
                       if cur else '<span class="muted">尚未投票</span>')
            box = f'<div class="card"><h2>{esc(unit_code)} 当前选择{proxy_note}</h2>' \
                  f'<div style="margin-bottom:8px">{cur_txt}</div>'
            if open_now:
                box += f'<form method="post" action="/issues/{issue["id"]}/vote">'
                if proxy_id:
                    box += f'<input type="hidden" name="proxy_id" value="{proxy_id}">'
                box += f'<input type="hidden" name="unit_id" value="{unit_id}">' \
                       '<div class="choices">'
                for c, label in CHOICE_LABELS.items():
                    box += (f'<button class="btn" name="choice" value="{c}" '
                            f'type="submit">{label}</button>')
                box += '</div></form>'
                if cur:
                    box += (f'<form method="post" action="/issues/{issue["id"]}'
                            f'/withdraw"><input type="hidden" name="unit_id" '
                            f'value="{unit_id}"><button class="btn danger" '
                            f'type="submit">撤回本户当前票</button></form>')
            elif issue["status"] == "open":
                box += '<div class="small muted">不在投票有效期内。</div>'
            history = self.sys.ballot_history(issue["id"], unit_id)
            if history:
                rows = "".join(
                    f'<tr><td>v{h["version_no"]}</td><td>{esc(h["voter_name"])}</td>'
                    f'<td>{CHOICE_LABELS[h["choice"]]}</td>'
                    f'<td class="small">{esc(h["submitted_at"][:16])}</td>'
                    f'<td class="small">{esc(h["state"])}</td></tr>'
                    for h in history)
                box += (f'<details style="margin-top:10px"><summary class="small">'
                        f'投票版本记录（{len(history)} 条，可追溯）</summary>'
                        f'<table><tr><th>版本</th><th>提交人</th><th>选择</th>'
                        f'<th>时间</th><th>状态</th></tr>{rows}</table></details>')
            return box + '</div>'

        for e in entries:
            out += unit_box(e["unit_id"], e["unit_code"])
        for p in proxies:
            out += unit_box(p["unit_id"], p["unit_code"], p["id"],
                            ' <span class="tag">受托代投</span>')
        if not entries and not proxies:
            out = ('<div class="card"><div class="muted small">'
                   '您不在本议题的投票资格名册中，也无受托代理。</div></div>')
        return out

    def roster_page(self, user, issue_id, msg, err):
        issue = self.sys.get_issue(issue_id)
        roster = self.sys.roster(issue_id)
        stats = self.sys.participation_stats(issue_id)
        rows = "".join(
            f'<tr><td>{esc(r["unit_code"])}</td><td>{esc(r["owner_name"])}</td>'
            f'<td>{r["area"]:.2f} ㎡</td><td><span class="tag blue">有资格</span></td></tr>'
            for r in roster)
        body = f"""<h1>资格核对 · #{issue['id']} {esc(issue['title'])}</h1>
<div class="notice">本名册于议题发起时（{esc(issue['created_at'][:16])}）冻结。
此后发生的房屋过户不影响本次表决资格；新业主自下一议题起进入名册。</div>
<div class="card"><div class="row"><span>名册户数</span><b>{stats['total_units']} 户</b></div>
<div class="row"><span>名册总面积</span><b>{stats['total_area']:.2f} ㎡</b></div>
<div class="row"><span>已投户数</span><b>{stats['voted_units']} 户</b></div></div>
<div class="card"><table><tr><th>房号</th><th>名册业主</th><th>专有面积</th>
<th>资格</th></tr>{rows}</table></div>
<div><a href="/issues/{issue['id']}">← 返回议题</a></div>"""
        return self.page("资格核对", body, user, "/issues", msg, err)

    def result_page(self, user, issue_id, msg, err):
        issue = self.sys.get_issue(issue_id)
        tally = self.sys.get_tally(issue_id)
        if not tally or issue["status"] != "published":
            state = "投票尚未截止或结果尚未公布。"
            if tally and issue["status"] == "tallied":
                state = "已计票，待复核员复核后公布。"
            body = (f'<h1>表决结果 · #{issue["id"]} {esc(issue["title"])}</h1>'
                    f'<div class="notice">{state}</div>'
                    f'<a href="/issues/{issue["id"]}">← 返回议题</a>')
            return self.page("表决结果", body, user, "/issues", msg, err)

        r = tally["result"]
        ch = r["choices"]
        p_u, p_a = r["participated_units"], r["participated_area"]

        def ratio_card(title, total, getter):
            rows = ""
            for key, label, cls in (("approve", "赞成", ""),
                                    ("reject", "反对", "gray"),
                                    ("abstain", "弃权", "light")):
                val = getter(ch[key])
                pct = val / total if total else 0
                rows += (f'<div class="row"><span>{label}</span>'
                         f'<b>{val:g}（{fmt_pct(pct)}）</b></div>'
                         f'<div class="bar"><i class="{cls}" '
                         f'style="width:{pct * 100:.1f}%"></i></div>')
            return f'<div class="card"><h2>{title}</h2>{rows}</div>'

        units_card = ratio_card("户数比例（占参与户数）", p_u,
                                lambda c: c["units"])
        area_card = ratio_card("面积比例（占参与面积 ㎡）", p_a,
                               lambda c: c["area"])
        verdict = ('<span class="verdict pass">表决通过</span>' if r["passed"]
                   else '<span class="verdict fail">表决未通过</span>')
        quorum = "达成" if r["quorum_met"] else "未达成"
        invalid_rows = "".join(
            f'<tr><td>{esc(v["unit_code"])}</td><td>{esc(v["reason_label"])}</td></tr>'
            for v in r["invalid"]) or '<tr><td colspan="2" class="muted">无</td></tr>'
        body = f"""<h1>表决结果 · #{issue['id']} {esc(issue['title'])}</h1>
<div class="card"><div class="row"><span>结论</span>{verdict}</div>
<div class="row"><span>参与情况</span><b>{p_u}/{r['total_units']} 户 ·
{p_a:.2f}/{r['total_area']:.2f} ㎡（法定参与比例{quorum}）</b></div>
<div class="row"><span>被取代的历史版本</span><b>{r['superseded_versions']} 条</b></div>
<div class="row"><span>已撤回</span><b>{r['withdrawn_count']} 条</b></div></div>
<div class="grid2eq">{units_card}{area_card}</div>
<div class="card"><h2>无效票及原因</h2>
<table><tr><th>房号</th><th>无效原因</th></tr>{invalid_rows}</table></div>
<div class="card small muted">计票员：{esc(tally.get('teller_name', '?'))} ·
计票时间：{esc(tally['tallied_at'][:16])} ｜ 复核员：
{esc(tally.get('reviewer_name', '?'))} · 公布时间：
{esc((tally['published_at'] or '')[:16])} ｜ 计票员与复核员为不同人员，
结果可追溯。</div>
<div><a href="/issues/{issue['id']}/ballots">查看投票明细（已公布）</a> ·
<a href="/issues/{issue['id']}">← 返回议题</a></div>"""
        return self.page("表决结果", body, user, "/issues", msg, err)

    def ballots_page(self, user, issue_id, msg, err):
        if not user:
            return None, "/login"
        try:
            ballots = self.sys.get_ballots(user["id"], issue_id)
        except PrivacyError as e:
            return None, f"/issues/{issue_id}?err={q(str(e))}"
        issue = self.sys.get_issue(issue_id)
        rows = "".join(
            f'<tr><td>{esc(b["unit_code"])}</td><td>v{b["version_no"]}</td>'
            f'<td>{esc(b["voter_name"])}</td><td>{CHOICE_LABELS[b["choice"]]}</td>'
            f'<td class="small">{esc(b["submitted_at"][:16])}</td>'
            f'<td class="small">{"已撤回" if b["withdrawn"] else (esc(b["invalid_reason"]) or "有效")}</td></tr>'
            for b in ballots)
        body = f"""<h1>投票明细 · #{issue['id']} {esc(issue['title'])}</h1>
<div class="card"><table><tr><th>房号</th><th>版本</th><th>提交人</th>
<th>选择</th><th>时间</th><th>状态</th></tr>{rows}</table></div>
<div><a href="/issues/{issue['id']}/result">← 返回结果</a></div>"""
        return self.page("投票明细", body, user, "/issues", msg, err), None

    def proxies_page(self, user, msg, err):
        if not user:
            return None, "/login"
        conn = self.sys._conn()
        proxies = [dict(r) for r in conn.execute(
            """SELECT p.*, i.title, r.unit_code, g.name AS grantor_name,
                      h.name AS holder_name
               FROM proxies p
               JOIN issues i ON i.id=p.issue_id
               JOIN roster_entries r ON r.issue_id=p.issue_id AND r.unit_id=p.unit_id
               JOIN users g ON g.id=p.grantor_id
               JOIN users h ON h.id=p.holder_id
               ORDER BY p.id DESC""").fetchall()]
        open_issues = conn.execute(
            "SELECT * FROM issues WHERE status='open' ORDER BY id DESC").fetchall()
        conn.close()

        rows = ""
        for p in proxies:
            state = (f'<span class="tag red">已撤销 {esc(p["revoked_at"][:16])}</span>'
                     if p["revoked_at"] else '<span class="tag blue">有效</span>')
            revoke = ""
            if (not p["revoked_at"] and user["id"] == p["grantor_id"]):
                revoke = (f'<form method="post" action="/proxies/{p["id"]}/revoke" '
                          f'style="display:inline"><button class="btn danger" '
                          f'type="submit">撤销</button></form>')
            rows += (f'<tr><td>#{p["issue_id"]} {esc(p["title"])}</td>'
                     f'<td>{esc(p["unit_code"])}</td><td>{esc(p["grantor_name"])}</td>'
                     f'<td>{esc(p["holder_name"])}</td><td>{state}</td>'
                     f'<td class="small"><a href="/proxy_docs/{p["id"]}">授权书</a> {revoke}</td></tr>')
        table = (f'<div class="card"><table><tr><th>议题</th><th>委托户</th>'
                 f'<th>委托人</th><th>被委托人</th><th>状态</th><th>操作</th></tr>'
                 f'{rows or "<tr><td colspan=6 class=muted>暂无授权</td></tr>"}</table></div>')

        form = ""
        if has_role(user, "owner") and open_issues:
            opts_i = "".join(f'<option value="{i["id"]}">#{i["id"]} '
                             f'{esc(i["title"])}</option>' for i in open_issues)
            form = f"""<div class="card"><h2>发起代理授权</h2>
<form method="post" action="/proxies">
<label>议题</label><select name="issue_id">{opts_i}</select>
<label>委托户房号（须为名册中您名下的户）</label>
<input name="unit_code" placeholder="如 1-104">
<label>被委托人用户编号（须为名册内业主，见资格名册页）</label>
<input name="holder_id" placeholder="如 4">
<button class="btn primary" type="submit">生成授权书并生效</button>
<div class="small muted">授权书为本地虚构文件，保存于 data/proxy_docs/。</div>
</form></div>"""
        body = f"<h1>代理授权</h1>{table}{form}"
        return self.page("代理授权", body, user, "/proxies", msg, err), None

    def tally_page(self, user, msg, err):
        if not has_role(user, "teller", "reviewer", "committee"):
            return None, "/login?err=" + q("请以计票员/复核员身份登录")
        conn = self.sys._conn()
        issues = conn.execute("SELECT * FROM issues ORDER BY id DESC").fetchall()
        conn.close()
        now = self.sys.clock()
        cards = ""
        for i in issues:
            if i["status"] == "voided":
                continue
            tally = self.sys.get_tally(i["id"])
            actions = ""
            if (i["status"] == "open" and now > i["end_at"]
                    and has_role(user, "teller")):
                actions = (f'<form method="post" action="/issues/{i["id"]}/tally">'
                           f'<button class="btn primary" type="submit">执行计票</button></form>')
            elif i["status"] == "tallied" and has_role(user, "reviewer"):
                if tally and tally["teller_id"] == user["id"]:
                    actions = ('<div class="small" style="color:var(--red)">'
                               '计票员与复核员不得为同一人，您不能复核本议题。</div>')
                else:
                    actions = (f'<form method="post" action="/issues/{i["id"]}'
                               f'/publish"><button class="btn primary" '
                               f'type="submit">复核并公布</button></form>')
            summary = ""
            if tally:
                r = tally["result"]
                ch = r["choices"]
                summary = (f'<div class="small muted">参与 {r["participated_units"]}'
                           f'/{r["total_units"]} 户 · 赞成 {ch["approve"]["units"]} 户 '
                           f'/ 反对 {ch["reject"]["units"]} 户 / 弃权 '
                           f'{ch["abstain"]["units"]} 户 · 无效票 '
                           f'{len(r["invalid"])} 张 · 计票员 '
                           f'{esc(tally.get("teller_name", "?"))}</div>')
            cards += (f'<div class="card"><div style="display:flex;'
                      f'justify-content:space-between;align-items:center"><div>'
                      f'<b>#{i["id"]} {esc(i["title"])}</b> '
                      f'{self.status_tag(i, now)}</div><div>{actions}</div></div>'
                      f'{summary}</div>')
        body = f"""<h1>计票复核工作台</h1>
<div class="notice">计票须在投票截止后由计票员执行；复核员复核后方可公布，
且计票员与复核员不得为同一人。</div>{cards}"""
        return self.page("计票复核", body, user, "/tally", msg, err), None

    def admin_page(self, user, msg, err):
        if not has_role(user, "committee"):
            return None, "/login?err=" + q("请以业委会身份登录")
        conn = self.sys._conn()
        units = conn.execute(
            """SELECT u.*, w.name AS owner_name FROM units u
               LEFT JOIN ownerships o ON o.unit_id=u.id AND o.until IS NULL
               LEFT JOIN users w ON w.id=o.owner_id ORDER BY u.code""").fetchall()
        transfers = conn.execute(
            """SELECT t.*, u.code, a.name AS from_name, b.name AS to_name
               FROM transfers t JOIN units u ON u.id=t.unit_id
               JOIN users a ON a.id=t.from_owner_id
               JOIN users b ON b.id=t.to_owner_id ORDER BY t.id DESC""").fetchall()
        notifs = conn.execute(
            """SELECT n.*, u.name FROM notifications n
               JOIN users u ON u.id=n.user_id
               ORDER BY n.id DESC LIMIT 12""").fetchall()
        users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        conn.close()
        unit_rows = "".join(
            f'<tr><td>{esc(u["code"])}</td><td>{u["area"]:.2f}</td>'
            f'<td>{esc(u["owner_name"] or "—")}</td></tr>' for u in units)
        tr_rows = "".join(
            f'<tr><td>{esc(t["code"])}</td><td>{esc(t["from_name"])} → '
            f'{esc(t["to_name"])}</td><td class="small">{esc(t["transferred_at"][:16])}</td></tr>'
            for t in transfers) or '<tr><td colspan="3" class="muted">暂无</td></tr>'
        nt_rows = "".join(
            f'<tr><td class="small">{esc(n["created_at"][:16])}</td>'
            f'<td>{esc(n["name"])}</td><td class="small">{esc(n["template"])}</td>'
            f'<td class="small muted">{esc(n["payload"][:60])}</td></tr>'
            for n in notifs) or '<tr><td colspan="4" class="muted">暂无</td></tr>'
        user_rows = "".join(
            f'<tr><td>{u["id"]}</td><td>{esc(u["name"])}</td>'
            f'<td>{self.role_label(u["role"])}</td></tr>' for u in users)
        unit_opts = "".join(f'<option value="{u["id"]}">{esc(u["code"])}'
                            f'（现业主 {esc(u["owner_name"] or "—")}）</option>'
                            for u in units)
        body = f"""<h1>管理</h1>
<div class="grid2eq">
<div class="card"><h2>房屋过户登记</h2>
<form method="post" action="/admin/transfer">
<label>房屋</label><select name="unit_id">{unit_opts}</select>
<label>新业主姓名</label><input name="new_owner_name" placeholder="如 郑新">
<button class="btn primary" type="submit">登记过户</button>
<div class="small muted">过户只影响此后发起的议题；进行中的表决仍以冻结名册为准。</div>
</form></div>
<div class="card"><h2>用户与角色</h2>
<table><tr><th>编号</th><th>姓名</th><th>角色</th></tr>{user_rows}</table></div>
</div>
<div class="grid2eq">
<div class="card"><h2>房屋台账</h2>
<table><tr><th>房号</th><th>面积㎡</th><th>当前业主</th></tr>{unit_rows}</table></div>
<div class="card"><h2>过户记录</h2>
<table><tr><th>房号</th><th>变更</th><th>时间</th></tr>{tr_rows}</table></div>
</div>
<div class="card"><h2>通知投递记录（模拟适配器）</h2>
<table><tr><th>时间</th><th>收件人</th><th>模板</th><th>内容</th></tr>{nt_rows}</table></div>"""
        return self.page("管理", body, user, "/admin", msg, err), None

    def issue_form(self, user, msg, err, revise_of=None):
        if not has_role(user, "committee"):
            return None, "/login?err=" + q("请以业委会身份登录")
        old = self.sys.get_issue(revise_of) if revise_of else None
        title = f'变更议题内容 · 原议题 #{revise_of}' if old else "发起议题"
        warn = ""
        if old:
            warn = (f'<div class="notice err">变更后原议题 #{revise_of} 将作废'
                    f'（已有投票记录保留可追溯），本表单将以 v{old["version"] + 1} '
                    f'重新发起并重新冻结名册。</div>')
        body = f"""<h1>{title}</h1>{warn}
<div class="card"><form method="post"
action="{'/issues/' + str(revise_of) + '/revise' if old else '/issues'}">
<label>议题标题</label>
<input name="title" value="{esc(old['title']) if old else ''}" required>
<label>议题原文</label>
<textarea name="body" rows="7" required>{esc(old['body']) if old else ''}</textarea>
<div class="grid2eq"><div>
<label>开始时间</label><input name="start_at" placeholder="2026-10-05 09:00" required>
</div><div>
<label>截止时间</label><input name="end_at" placeholder="2026-10-15 18:00" required>
</div></div>
<button class="btn primary" type="submit">
{'作废原议题并重新发起' if old else '冻结名册并发布'}</button>
<div class="small muted" style="margin-top:8px">发布即冻结投票资格名册与计票规则
（默认：参与双三分之二、同意双过半）。</div>
</form></div>"""
        return self.page(title, body, user, "/issues", msg, err), None

    def login_page(self, msg, err):
        conn = self.sys._conn()
        users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        conn.close()
        rows = "".join(
            f'<tr><td>{u["id"]}</td><td>{esc(u["name"])}</td>'
            f'<td>{self.role_label(u["role"])}</td>'
            f'<td><form method="post" action="/login" style="display:inline">'
            f'<input type="hidden" name="uid" value="{u["id"]}">'
            f'<button class="btn" type="submit">以此身份登录</button></form></td></tr>'
            for u in users)
        body = f"""<h1>选择身份登录（演示环境，免密）</h1>
<div class="card"><table><tr><th>编号</th><th>姓名</th><th>角色</th>
<th></th></tr>{rows}</table></div>"""
        return self.page("登录", body, None, "", msg, err), None


# ---------------------------------------------------------------- HTTP 骨架
class Handler(BaseHTTPRequestHandler):
    app: App = None  # 由 serve() 注入
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ----
    def _send_html(self, text, status=200, extra_headers=None):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, extra_headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    def _current_user(self):
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
        except cookies.CookieError:
            return None
        morsel = jar.get("uid")
        if not morsel:
            return None
        conn = self.app.sys._conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (morsel.value,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _query(self):
        q = parse_qs(urlparse(self.path).query)
        return q.get("msg", [None])[0], q.get("err", [None])[0]

    def log_message(self, fmt, *args):
        pass  # 保持控制台干净

    # ---- 路由 ----
    def do_GET(self):
        app, user = self.app, self._current_user()
        path = urlparse(self.path).path.rstrip("/") or "/"
        msg, err = self._query()
        try:
            if path == "/":
                return self._redirect("/issues")
            if path == "/login":
                page, redir = app.login_page(msg, err)
                return self._send_html(page)
            if path == "/logout":
                return self._redirect("/issues", [("Set-Cookie",
                                                   "uid=; Path=/; Max-Age=0")])
            if path == "/issues":
                return self._send_html(app.issues_page(user, msg, err))
            if path == "/issues/new":
                page, redir = app.issue_form(user, msg, err)
                return self._redirect(redir) if redir else self._send_html(page)
            if path == "/proxies":
                page, redir = app.proxies_page(user, msg, err)
                return self._redirect(redir) if redir else self._send_html(page)
            if path == "/tally":
                page, redir = app.tally_page(user, msg, err)
                return self._redirect(redir) if redir else self._send_html(page)
            if path == "/admin":
                page, redir = app.admin_page(user, msg, err)
                return self._redirect(redir) if redir else self._send_html(page)

            parts = path.strip("/").split("/")
            if parts[0] == "issues" and len(parts) >= 2 and parts[1].isdigit():
                iid = int(parts[1])
                tail = parts[2] if len(parts) > 2 else ""
                if tail == "":
                    return self._send_html(app.issue_detail(user, iid, msg, err))
                if tail == "roster":
                    return self._send_html(app.roster_page(user, iid, msg, err))
                if tail == "result":
                    return self._send_html(app.result_page(user, iid, msg, err))
                if tail == "revise":
                    page, redir = app.issue_form(user, msg, err, revise_of=iid)
                    return self._redirect(redir) if redir else self._send_html(page)
                if tail == "ballots":
                    page, redir = app.ballots_page(user, iid, msg, err)
                    return self._redirect(redir) if redir else self._send_html(page)
            if parts[0] == "proxy_docs" and len(parts) == 2 and parts[1].isdigit():
                return self._serve_proxy_doc(int(parts[1]), user)
            self._send_html(app.page("404", "<h1>页面不存在</h1>", user),
                            status=404)
        except DomainError as e:
            self._send_html(app.page("错误", f'<div class="notice err">'
                                             f'{esc(str(e))}</div>'
                                             f'<a href="/issues">返回议题列表</a>',
                                     user), status=400)

    def do_POST(self):
        app, user = self.app, self._current_user()
        path = urlparse(self.path).path.rstrip("/")
        form = self._form()
        try:
            if path == "/login":
                uid = form.get("uid", "")
                return self._redirect(
                    "/issues", [("Set-Cookie", f"uid={uid}; Path=/; HttpOnly")])
            if not user:
                return self._redirect("/login")
            if path == "/issues":
                iid = app.sys.create_issue(
                    user["id"], form["title"], form["body"],
                    form["start_at"], form["end_at"])
                return self._redirect(f"/issues/{iid}?msg=" +
                                      q("议题已发布，名册已冻结"))
            if path == "/proxies":
                conn = app.sys._conn()
                unit = conn.execute("SELECT * FROM units WHERE code=?",
                                    (form.get("unit_code", "").strip(),)).fetchone()
                conn.close()
                if not unit:
                    raise DomainError("no_unit", "未找到该房号")
                pid = app.sys.grant_proxy(user["id"], int(form["issue_id"]),
                                          unit["id"], int(form["holder_id"]))
                return self._redirect("/proxies?msg=" +
                                      q(f"授权 #{pid} 已生效，授权书已生成"))
            parts = path.strip("/").split("/")
            if parts[0] == "proxies" and len(parts) == 3 and parts[2] == "revoke":
                app.sys.revoke_proxy(user["id"], int(parts[1]))
                return self._redirect("/proxies?msg=" + q("授权已撤销"))
            if parts[0] == "admin" and len(parts) == 2 and parts[1] == "transfer":
                app.sys.transfer_unit(user["id"], int(form["unit_id"]),
                                      form["new_owner_name"])
                return self._redirect("/admin?msg=" + q("过户已登记"))
            if parts[0] == "issues" and len(parts) >= 2 and parts[1].isdigit():
                iid = int(parts[1])
                action = parts[2] if len(parts) > 2 else ""
                if action == "vote":
                    proxy_id = form.get("proxy_id")
                    vid, invalid = app.sys.cast_vote(
                        user["id"], iid, int(form["unit_id"]), form["choice"],
                        proxy_id=int(proxy_id) if proxy_id else None)
                    if invalid:
                        return self._redirect(
                            f"/issues/{iid}?err=" + q(
                                f"已记录为无效票（第 {vid} 号记录）："
                                f"{INVALID_LABELS[invalid]}，可申诉追溯"))
                    return self._redirect(f"/issues/{iid}?msg=" +
                                          q("投票成功，可在本页随时更改选择"))
                if action == "withdraw":
                    app.sys.withdraw_vote(user["id"], iid, int(form["unit_id"]))
                    return self._redirect(f"/issues/{iid}?msg=" + q("已撤票"))
                if action == "revise":
                    new_id = app.sys.revise_issue(
                        user["id"], iid, form["title"], form["body"],
                        form["start_at"], form["end_at"])
                    return self._redirect(f"/issues/{new_id}?msg=" +
                                          q(f"原议题已作废，新版本 #{new_id} 已发布"))
                if action == "tally":
                    app.sys.tally_issue(user["id"], iid)
                    return self._redirect("/tally?msg=" + q("计票完成，待复核"))
                if action == "publish":
                    app.sys.publish_result(user["id"], iid)
                    return self._redirect(f"/issues/{iid}/result?msg=" +
                                          q("结果已公布"))
            self._redirect("/issues")
        except (DomainError, PermissionError_, KeyError, ValueError) as e:
            back = self.headers.get("Referer", "/issues")
            glue = "&" if "?" in back else "?"
            self._redirect(f"{back}{glue}err=" + q(str(e)))

    def _serve_proxy_doc(self, proxy_id, user):
        conn = self.app.sys._conn()
        proxy = conn.execute("SELECT * FROM proxies WHERE id=?",
                             (proxy_id,)).fetchone()
        conn.close()
        if not proxy:
            return self._send_html("授权不存在", status=404)
        try:
            with open(proxy["doc_path"], encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = "（授权书文件缺失）"
        page = self.app.page(
            "代理授权书",
            f'<h1>代理授权书 #{proxy_id}</h1><div class="card">'
            f'<pre class="body-text">{esc(content)}</pre></div>'
            f'<a href="/proxies">← 返回</a>', user, "/proxies")
        self._send_html(page)


def serve(system: VotingSystem, port=8000):
    app = App(system)
    Handler.app = app
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"业委会表决系统已启动：http://127.0.0.1:{port}", flush=True)
    print("演示账号见登录页（业委会/计票员/复核员/业主，免密切换）。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
