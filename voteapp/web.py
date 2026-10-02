"""HTTP 层：基于标准库 http.server 的路由、会话与页面处理器。"""
import hashlib
import hmac
import json
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from html import escape as esc
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import services as svc
from . import views
from .notify import EVENT_LABEL, MockNotifier
from .services import BizError, NotFound, PermissionDenied

CST = svc.CST


def parse_local(s):
    """datetime-local 输入（按东八区解释）→ UTC aware datetime。"""
    dt = datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M")
    return dt.replace(tzinfo=CST).astimezone(timezone.utc)


def local_input_value(dt):
    return dt.astimezone(CST).strftime("%Y-%m-%dT%H:%M")


class App:
    def __init__(self, db, secret, auth_dir):
        self.db = db
        self.secret = secret
        self.auth_dir = auth_dir
        self.notifier = MockNotifier(db)

    # -------------------------------------------------- 会话
    def _sign(self, uid):
        return hmac.new(self.secret, str(uid).encode(), hashlib.sha256).hexdigest()[:32]

    def make_session(self, uid):
        return f"{uid}:{self._sign(uid)}"

    def parse_session(self, val):
        try:
            uid, sig = val.split(":", 1)
            if hmac.compare_digest(sig, self._sign(int(uid))):
                return int(uid)
        except (ValueError, AttributeError):
            pass
        return None


# ---------------------------------------------------------------- 请求上下文

class Ctx:
    def __init__(self, app, handler, method):
        self.app = app
        self.db = app.db
        self.h = handler
        self.method = method
        u = urlparse(handler.path)
        self.path = u.path
        self.query = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.form = {}
        if method == "POST":
            length = int(handler.headers.get("Content-Length") or 0)
            body = handler.rfile.read(length).decode("utf-8")
            self.form = {k: v[0] for k, v in parse_qs(body).items()}
        self.user = None
        raw = handler.headers.get("Cookie")
        if raw:
            jar = http_cookies.SimpleCookie(raw)
            if "sid" in jar:
                uid = app.parse_session(jar["sid"].value)
                if uid:
                    self.user = svc.get_user(self.db, uid)
        self.now = svc.utcnow()

    @property
    def msg(self):
        return self.query.get("msg")

    @property
    def err(self):
        return self.query.get("err")

    def my_owner(self):
        if not self.user:
            return None
        return self.db.one("SELECT * FROM owners WHERE user_id=?", (self.user["id"],))


def require_login(ctx):
    if not ctx.user:
        raise PermissionDenied("请先登录")
    return ctx.user


def require_role(ctx, *roles):
    u = require_login(ctx)
    if u["role"] not in roles:
        raise PermissionDenied("当前角色无权访问此页面")
    return u


def ok_url(path, msg=None, err=None):
    from urllib.parse import quote
    if msg:
        sep = "&" if "?" in path else "?"
        path += f"{sep}msg={quote(msg)}"
    if err:
        sep = "&" if "?" in path else "?"
        path += f"{sep}err={quote(err)}"
    return path


class Redirect(Exception):
    def __init__(self, location):
        self.location = location


# ---------------------------------------------------------------- 页面处理器

def home(ctx):
    topics = ctx.db.q("SELECT * FROM topics ORDER BY id DESC")
    rows = []
    for t in topics:
        v = svc.latest_version(ctx.db, t["id"])
        phase = svc.topic_phase(t, ctx.now)
        actions = [f'<a href="/topics/{t["id"]}">公示</a>']
        if t["status"] != "draft":
            actions.append(f'<a href="/topics/{t["id"]}/eligibility">资格核对</a>')
        if phase == "voting" and ctx.user and ctx.user["role"] == "owner":
            actions.append(f'<a href="/topics/{t["id"]}/vote">去投票</a>')
        if phase == "closed" and ctx.user and ctx.user["role"] in ("counter", "reviewer", "scrutineer"):
            actions.append(f'<a href="/topics/{t["id"]}/count">计票复核</a>')
        if phase == "published":
            actions.append(f'<a href="/topics/{t["id"]}/result">结果</a>')
        rows.append(
            f"<tr><td>{esc(t['code'])}</td>"
            f"<td><a href='/topics/{t['id']}'>{esc(v['title'])}</a></td>"
            f"<td>{views.badge(phase)}</td>"
            f"<td class='small'>{esc(svc.fmt(t['start_at']))}<br>至 {esc(svc.fmt(t['end_at']))}</td>"
            f"<td class='small'>{' · '.join(actions)}</td></tr>")
    body = (f"<h1>议题列表</h1><p class='muted small'>当前系统时间："
            f"{esc(ctx.now.astimezone(CST).strftime('%Y-%m-%d %H:%M:%S'))}（东八区）</p>"
            "<table><tr><th>编号</th><th>议题</th><th>状态</th><th>投票时间</th><th>操作</th></tr>"
            + "".join(rows) + "</table>")
    if not topics:
        body += "<p class='muted'>暂无议题。</p>"
    return views.page("议题列表", ctx.user, body, ctx.msg, ctx.err, "topics")


def login_form(ctx):
    groups = [("manager", "业委会"), ("scrutineer", "监票人"), ("counter", "计票员"),
              ("reviewer", "复核员"), ("owner", "业主")]
    parts = ["<h1>登录</h1><p class='muted'>演示环境：选择身份即可登录，无需密码。</p>"]
    for role, label in groups:
        users = ctx.db.q("SELECT * FROM users WHERE role=? ORDER BY id", (role,))
        btns = "".join(
            f"<form method='post' action='/login' style='display:inline'>"
            f"<input type='hidden' name='user_id' value='{u['id']}'>"
            f"<button class='btn gray sm'>{esc(u['name'])}</button></form> "
            for u in users)
        parts.append(f"<div class='card'><b>{label}</b><br>{btns}</div>")
    return views.page("登录", ctx.user, "".join(parts), ctx.msg, ctx.err)


def login_post(ctx):
    uid = int(ctx.form.get("user_id", "0"))
    if not svc.get_user(ctx.db, uid):
        raise NotFound("用户不存在")
    raise RedirectWithCookie("/", ctx.app.make_session(uid))


class RedirectWithCookie(Redirect):
    def __init__(self, location, sid):
        super().__init__(location)
        self.sid = sid


def logout(ctx):
    raise RedirectWithCookie(ok_url("/", msg="已退出登录"), "")


def topic_detail(ctx, tid):
    t = svc.get_topic(ctx.db, tid)
    v = svc.latest_version(ctx.db, tid)
    phase = svc.topic_phase(t, ctx.now)
    options = json.loads(v["options"])
    versions = ctx.db.q(
        "SELECT * FROM topic_versions WHERE topic_id=? ORDER BY version_no DESC", (tid,))
    body = [f"<h1>{esc(v['title'])}</h1><p>{views.badge(phase)} "
            f"<span class='muted small'>议题编号 {esc(t['code'])} · 当前版本 v{v['version_no']}</span></p>"]
    if t["reinitiated_from"]:
        src = svc.get_topic(ctx.db, t["reinitiated_from"])
        body.append(f"<p class='small muted'>本议题由 <a href='/topics/{src['id']}'>"
                    f"{esc(src['code'])}</a> 重新发起（原议题内容变更后须重新发起）。</p>")
    body.append("<h2>议题原文</h2>")
    body.append(f"<div class='card'><p style='white-space:pre-wrap;margin:0'>{esc(v['content'])}</p>"
                f"<p class='small muted'>表决选项：{' / '.join(esc(o) for o in options)}</p></div>")
    rows = [("创建时间", esc(svc.fmt(t["created_at"])))]
    if t["start_at"]:
        rows.append(("投票开始", f'<span class="deadline">{esc(svc.fmt(t["start_at"]))}</span>'))
        rows.append(("投票截止", f'<span class="deadline">{esc(svc.fmt(t["end_at"]))}</span>'))
    if t["rule_text"]:
        rows.append(("计票规则", esc(t["rule_text"])))
    if t["roster_id"]:
        r = ctx.db.one("SELECT * FROM rosters WHERE id=?", (t["roster_id"],))
        n = ctx.db.one("SELECT COUNT(*) n FROM roster_entries WHERE roster_id=?",
                       (t["roster_id"],))["n"]
        rows.append(("名册冻结", f"{esc(svc.fmt(r['frozen_at']))}（{n} 户，冻结后过户不影响本次资格）"))
    body.append("<h2>表决安排</h2>" + views.kv(rows))
    # 版本历史
    vh = "".join(f"<tr><td>v{x['version_no']}</td><td>{esc(x['title'])}</td>"
                 f"<td class='small'>{esc(svc.fmt(x['created_at']))}</td></tr>" for x in versions)
    body.append("<h2>版本历史</h2><table><tr><th>版本</th><th>标题</th><th>时间</th></tr>"
                + vh + "</table>")
    # 操作区
    acts = []
    if t["status"] != "draft":
        acts.append(f"<a class='btn gray sm' href='/topics/{tid}/eligibility'>资格核对</a>")
    if phase == "voting" and ctx.user and ctx.user["role"] == "owner":
        acts.append(f"<a class='btn sm' href='/topics/{tid}/vote'>前往投票</a>")
    if phase in ("announced", "voting") and ctx.user and ctx.user["role"] in ("owner", "manager"):
        acts.append(f"<a class='btn gray sm' href='/topics/{tid}/proxy'>代理授权</a>")
    if phase == "closed" and ctx.user and ctx.user["role"] in ("counter", "reviewer", "scrutineer"):
        acts.append(f"<a class='btn sm' href='/topics/{tid}/count'>计票复核</a>")
    if phase == "published":
        acts.append(f"<a class='btn sm' href='/topics/{tid}/result'>查看结果</a>")
    if ctx.user and ctx.user["role"] == "manager":
        if t["status"] == "draft":
            acts.append(f"<a class='btn gray sm' href='/topics/{tid}/edit'>编辑草稿</a>")
        else:
            acts.append(f"<form method='post' action='/topics/{tid}/reinitiate' style='display:inline'>"
                        "<button class='btn gray sm'>内容变更·重新发起</button></form>")
    if acts:
        body.append("<h2>操作</h2><p>" + " ".join(acts) + "</p>")
    # 草拟议题的公示表单
    if ctx.user and ctx.user["role"] == "manager" and t["status"] == "draft":
        now = svc.utcnow()
        body.append(f"""
<h2>公示并确定表决安排</h2>
<form method="post" action="/topics/{tid}/announce" class="card soft">
<label>投票开始时间</label><input type="datetime-local" name="start_at"
 value="{local_input_value(now)}" required>
<label>投票截止时间</label><input type="datetime-local" name="end_at"
 value="{local_input_value(now + timedelta(days=7))}" required>
<label>参与门槛（户数与面积均须达到的比例）</label>
<input type="number" name="quorum" step="0.001" min="0.01" max="1" value="0.667" required>
<label>通过门槛（同意占参与的比例）</label>
<input type="number" name="pass_ratio" step="0.01" min="0.01" max="1" value="0.5" required>
<label>计票规则说明</label>
<textarea name="rule_text" rows="3">{esc(seed_rule_text())}</textarea>
<p style="margin-top:12px"><button class="btn">公示议题（公示后内容不可修改）</button></p>
</form>""")
    return views.page(v["title"], ctx.user, "".join(body), ctx.msg, ctx.err, "topics")


def seed_rule_text():
    return ("依据《民法典》第二百七十八条：应当由专有部分面积占比三分之二以上的业主"
            "且人数占比三分之二以上的业主参与表决；经参与表决专有部分面积过半数且"
            "参与表决人数过半数同意即为通过。")


def topic_new(ctx):
    require_role(ctx, "manager")
    body = """
<h1>发起议题</h1>
<form method="post" action="/topics/new" class="card">
<label>议题标题</label><input name="title" required maxlength="120">
<label>议题原文</label><textarea name="content" rows="8" required></textarea>
<label>表决选项（每行一个，至少两项）</label>
<textarea name="options" rows="3" required>同意
反对
弃权</textarea>
<p style="margin-top:12px"><button class="btn">保存草稿（版本 v1）</button></p>
</form>"""
    return views.page("发起议题", ctx.user, body, ctx.msg, ctx.err, "new")


def _parse_options(raw):
    return [x.strip() for x in raw.replace("，", "\n").replace(",", "\n").splitlines() if x.strip()]


def topic_create(ctx):
    u = require_role(ctx, "manager")
    tid = svc.create_topic(ctx.db, u, ctx.form.get("title", ""), ctx.form.get("content", ""),
                           _parse_options(ctx.form.get("options", "")), now=ctx.now)
    raise Redirect(ok_url(f"/topics/{tid}", msg="议题草稿已创建（v1）"))


def topic_edit(ctx, tid):
    require_role(ctx, "manager")
    t = svc.get_topic(ctx.db, tid)
    if t["status"] != "draft":
        raise BizError("议题已公示，内容不可再修改；如需变更请重新发起")
    v = svc.latest_version(ctx.db, tid)
    options = "\n".join(json.loads(v["options"]))
    body = f"""
<h1>编辑议题草稿</h1>
<form method="post" action="/topics/{tid}/edit" class="card">
<label>议题标题</label><input name="title" required maxlength="120" value="{esc(v['title'])}">
<label>议题原文</label><textarea name="content" rows="8" required>{esc(v['content'])}</textarea>
<label>表决选项（每行一个）</label><textarea name="options" rows="3" required>{esc(options)}</textarea>
<p style="margin-top:12px"><button class="btn">保存为新版本</button></p>
</form>"""
    return views.page("编辑草稿", ctx.user, body, ctx.msg, ctx.err, "topics")


def topic_edit_post(ctx, tid):
    u = require_role(ctx, "manager")
    v = svc.edit_draft(ctx.db, u, tid, ctx.form.get("title", ""), ctx.form.get("content", ""),
                       _parse_options(ctx.form.get("options", "")), now=ctx.now)
    raise Redirect(ok_url(f"/topics/{tid}", msg=f"已保存为版本 v{v}"))


def announce_post(ctx, tid):
    u = require_role(ctx, "manager")
    svc.announce(ctx.db, u, tid,
                 parse_local(ctx.form["start_at"]), parse_local(ctx.form["end_at"]),
                 ctx.form.get("rule_text", ""), float(ctx.form["quorum"]),
                 float(ctx.form["pass_ratio"]), notifier=ctx.app.notifier, now=ctx.now)
    raise Redirect(ok_url(f"/topics/{tid}", msg="议题已公示，表决安排已确定"))


def reinitiate_post(ctx, tid):
    u = require_role(ctx, "manager")
    new_id = svc.reinitiate(ctx.db, u, tid, now=ctx.now)
    raise Redirect(ok_url(f"/topics/{new_id}/edit",
                          msg="已按最新版本内容重新发起新议题草稿，请修改后公示"))


def eligibility(ctx, tid):
    require_login(ctx)
    t = svc.get_topic(ctx.db, tid)
    v = svc.latest_version(ctx.db, tid)
    rid, entries = svc.roster_of(ctx.db, tid, ctx.now)
    body = [f"<h1>资格核对 · {esc(v['title'])}</h1>"]
    if not entries:
        body.append("<p class='muted'>名册尚未冻结（投票开始时冻结，以届时产权归属为准）。</p>")
        cur = ctx.db.q(
            "SELECT u.code, u.area, o.name FROM units u"
            " JOIN unit_ownership uo ON uo.unit_id=u.id AND uo.until IS NULL"
            " JOIN owners o ON o.id=uo.owner_id ORDER BY u.code")
        rows = "".join(f"<tr><td>{esc(r['code'])}</td><td>{esc(r['name'])}</td>"
                       f"<td>{r['area']:.1f}</td><td>（未冻结，仅供参考）</td></tr>" for r in cur)
        body.append("<h2>当前产权归属（预览）</h2><table><tr><th>户号</th><th>业主</th>"
                    f"<th>面积㎡</th><th>说明</th></tr>{rows}</table>")
    else:
        roster = ctx.db.one("SELECT * FROM rosters WHERE id=?", (rid,))
        body.append(f"<p class='small muted'>名册冻结于 "
                    f"<span class='deadline'>{esc(svc.fmt(roster['frozen_at']))}</span>"
                    "，此后发生的过户不影响本次投票资格。</p>")
        rows = "".join(
            f"<tr><td>{esc(e['unit_code'])}</td><td>{esc(e['owner_name'])}</td>"
            f"<td>{e['area']:.1f}</td><td>{'有效' if e['eligible'] else '无效'}</td>"
            f"<td class='small'>{esc(e['note'] or '')}</td></tr>" for e in entries)
        body.append("<table><tr><th>户号</th><th>名册业主</th><th>面积㎡</th>"
                    f"<th>资格</th><th>备注</th></tr>{rows}</table>")
        # 冻结后过户提示
        later = ctx.db.q(
            "SELECT t.*, u.code, of.name from_name, ot.name to_name FROM transfers t"
            " JOIN units u ON u.id=t.unit_id"
            " JOIN owners of ON of.id=t.from_owner_id JOIN owners ot ON ot.id=t.to_owner_id"
            " WHERE t.effective_at > ? ORDER BY t.effective_at", (roster["frozen_at"],))
        if later:
            trs = "".join(
                f"<tr><td>{esc(x['code'])}</td><td>{esc(x['from_name'])} → {esc(x['to_name'])}</td>"
                f"<td>{esc(svc.fmt(x['effective_at']))}</td>"
                "<td class='small'>名册冻结后过户，本次表决仍以名册业主为准</td></tr>" for x in later)
            body.append("<h2>冻结后发生的过户</h2><table><tr><th>户号</th><th>过户</th>"
                        f"<th>生效时间</th><th>对本次表决的影响</th></tr>{trs}</table>")
    body.append(f"<p><a href='/topics/{tid}'>← 返回议题</a></p>")
    return views.page("资格核对", ctx.user, "".join(body), ctx.msg, ctx.err, "topics")


def _actable_units(ctx, topic, my_owner_id):
    """当前用户可投票的户：本人名册户 + 持有有效代理的户。"""
    rid, entries = svc.roster_of(ctx.db, topic["id"], ctx.now)
    out = []
    if not entries:
        return out
    by_unit = {e["unit_id"]: e for e in entries}
    for e in entries:
        if e["owner_id"] == my_owner_id:
            out.append({"entry": e, "proxy": None})
    for p in ctx.db.q(
            "SELECT * FROM proxies WHERE topic_id=? AND proxy_owner_id=? AND status='active'",
            (topic["id"], my_owner_id)):
        e = by_unit.get(p["unit_id"])
        if e:
            out.append({"entry": e, "proxy": p})
    return out


def vote_page(ctx, tid):
    u = require_role(ctx, "owner")
    me = ctx.my_owner()
    if not me:
        raise PermissionDenied("当前账号不是业主")
    t = svc.get_topic(ctx.db, tid)
    svc.maybe_freeze_roster(ctx.db, tid, ctx.now)
    t = svc.get_topic(ctx.db, tid)
    v = svc.latest_version(ctx.db, tid)
    phase = svc.topic_phase(t, ctx.now)
    options = json.loads(v["options"])
    # 同一屏：议题原文 + 截止时间 + 当前选择
    body = [f"<h1>业主表决 · {esc(v['title'])}</h1>"]
    body.append("<h2>议题原文</h2>")
    body.append(f"<div class='card'><p style='white-space:pre-wrap;margin:0'>{esc(v['content'])}</p>"
                f"<p class='small muted'>版本 v{v['version_no']} · 选项："
                f"{' / '.join(esc(o) for o in options)}</p></div>")
    body.append("<div class='card soft'>表决截止时间："
                f"<span class='deadline'>{esc(svc.fmt(t['end_at']))}</span>"
                f"<span class='muted small'>（{views.PHASE_LABEL[phase]}；以服务器时间为准，"
                "截止后提交无效并留痕）</span></div>")
    units = _actable_units(ctx, t, me["id"])
    # 名下但名册业主不是本人的户（如冻结后过户取得）
    if t["roster_id"]:
        roster_owner = {e["unit_id"]: e["owner_id"] for e in ctx.db.q(
            "SELECT unit_id, owner_id FROM roster_entries WHERE roster_id=?", (t["roster_id"],))}
        mine_now = ctx.db.q(
            "SELECT u.id, u.code FROM unit_ownership uo JOIN units u ON u.id=uo.unit_id"
            " WHERE uo.owner_id=? AND uo.until IS NULL", (me["id"],))
        missing = [r["code"] for r in mine_now if roster_owner.get(r["id"]) != me["id"]]
        if missing:
            body.append(f"<div class='err'>您名下的 {'、'.join(esc(c) for c in missing)} "
                        "不在本次投票名册内（名册冻结后过户取得），本次表决无投票资格。</div>")
    if not units:
        body.append("<p class='muted'>您在本次表决中没有可投票的户。</p>")
    for item in units:
        e, proxy = item["entry"], item["proxy"]
        cur = svc.my_vote(ctx.db, tid, e["unit_id"])
        body.append(f"<h2>户号 {esc(e['unit_code'])}"
                    + (f"<span class='muted small'>（代理投票 · 委托人 {esc(e['owner_name'])}，"
                       f"授权书 {esc(proxy['auth_file'])}）</span>" if proxy else "")
                    + "</h2>")
        if cur:
            voter = ctx.db.one("SELECT name FROM owners WHERE id=?", (cur["voter_owner_id"],))["name"]
            via = "，代理提交" if cur["via_proxy_id"] else ""
            body.append(f"<div class='choice-current'>当前选择：<b>{esc(cur['choice'])}</b>"
                        f"<span class='small muted'>（第 {cur['seq']} 版，{esc(voter)} 提交于 "
                        f"{esc(svc.fmt(cur['submitted_at']))}{via}；重复提交以最新版本为准）</span></div>")
        else:
            body.append("<p class='muted small'>该户尚未提交选择。</p>")
        if phase == "voting":
            radios = "".join(
                f"<label class='option-line'><input type='radio' name='choice' value='{esc(o)}' required> {esc(o)}</label>"
                for o in options)
            proxy_field = (f"<input type='hidden' name='via_proxy_id' value='{proxy['id']}'>"
                           if proxy else "")
            label = "更改选择" if cur else "提交选择"
            body.append(f"""
<form method="post" action="/topics/{tid}/vote" class="card">
<input type="hidden" name="unit_id" value="{e['unit_id']}">{proxy_field}
{radios}
<p style="margin-top:10px"><button class="btn">{label}</button></p>
</form>""")
            if cur:
                body.append(f"""
<form method="post" action="/topics/{tid}/withdraw"
 onsubmit="return confirm('确定撤回该户当前选择吗？')">
<input type="hidden" name="unit_id" value="{e['unit_id']}">
<button class="btn gray sm">撤回选择</button></form>""")
        elif phase in ("announced",):
            body.append("<p class='muted small'>投票尚未开始。</p>")
        else:
            body.append("<p class='muted small'>投票已截止。</p>")
    body.append(f"<p><a href='/topics/{tid}'>← 返回议题公示</a></p>")
    return views.page("业主表决", ctx.user, "".join(body), ctx.msg, ctx.err, "topics")


def vote_post(ctx, tid):
    u = require_role(ctx, "owner")
    me = ctx.my_owner()
    unit_id = int(ctx.form["unit_id"])
    via = ctx.form.get("via_proxy_id")
    try:
        svc.cast_vote(ctx.db, me["id"], tid, unit_id, ctx.form.get("choice", ""),
                      via_proxy_id=int(via) if via else None, now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{tid}/vote", err=str(e)))
    raise Redirect(ok_url(f"/topics/{tid}/vote", msg="已提交，当前选择以最新版本为准"))


def withdraw_post(ctx, tid):
    u = require_role(ctx, "owner")
    me = ctx.my_owner()
    try:
        svc.withdraw_vote(ctx.db, me["id"], tid, int(ctx.form["unit_id"]), now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{tid}/vote", err=str(e)))
    raise Redirect(ok_url(f"/topics/{tid}/vote", msg="已撤回该户当前选择"))


def proxy_page(ctx, tid):
    u = require_role(ctx, "owner", "manager")
    t = svc.get_topic(ctx.db, tid)
    v = svc.latest_version(ctx.db, tid)
    rid, entries = svc.roster_of(ctx.db, tid, ctx.now)
    proxies = ctx.db.q(
        "SELECT p.*, u.code, og.name grantor, op.name proxy FROM proxies p"
        " JOIN units u ON u.id=p.unit_id"
        " JOIN owners og ON og.id=p.grantor_owner_id JOIN owners op ON op.id=p.proxy_owner_id"
        " WHERE p.topic_id=? ORDER BY p.id DESC", (tid,))
    rows = "".join(
        f"<tr><td>{esc(p['code'])}</td><td>{esc(p['grantor'])}</td><td>{esc(p['proxy'])}</td>"
        f"<td class='small'>{esc(p['auth_file'])}</td>"
        f"<td>{'有效' if p['status']=='active' else ('已撤销' if p['status']=='revoked' else '已失效')}</td>"
        f"<td class='small'>{esc(svc.fmt(p['created_at']))}</td>"
        + (f"<td><form method='post' action='/proxy/{p['id']}/revoke'>"
           "<button class='btn gray sm'>撤销</button></form></td>" if p["status"] == "active" else "<td></td>")
        + "</tr>" for p in proxies)
    body = [f"<h1>代理授权 · {esc(v['title'])}</h1>"]
    body.append("<table><tr><th>户号</th><th>委托人</th><th>受托人</th><th>授权书文件</th>"
                f"<th>状态</th><th>登记时间</th><th></th></tr>{rows or '<tr><td colspan=7 class=muted>暂无代理授权</td></tr>'}</table>")
    # 登记表单
    me = ctx.my_owner()
    if me and entries:
        my_units = [e for e in entries if e["owner_id"] == me["id"]]
    elif u["role"] == "manager" and entries:
        my_units = list(entries)
    else:
        my_units = []
    if my_units and svc.topic_phase(t, ctx.now) in ("announced", "voting"):
        owners = ctx.db.q("SELECT * FROM owners ORDER BY id")
        unit_opts = "".join(f"<option value='{e['unit_id']}'>"
                            f"{esc(e['unit_code'])}（名册业主 {esc(e['owner_name'])}）</option>"
                            for e in my_units)
        proxy_opts = "".join(f"<option value='{o['id']}'>{esc(o['name'])}</option>"
                             for o in owners)
        body.append(f"""
<h2>登记代理授权</h2>
<form method="post" action="/topics/{tid}/proxy" class="card soft">
<label>委托的户（委托人默认为该户名册业主）</label>
<select name="unit_id">{unit_opts}</select>
<label>受托人</label><select name="proxy_owner_id">{proxy_opts}</select>
<label>授权书内容（保存为本地文件，作为授权凭证）</label>
<textarea name="auth_content" rows="6" required></textarea>
<p style="margin-top:12px"><button class="btn">登记代理授权</button></p>
</form>""")
    body.append(f"<p><a href='/topics/{tid}'>← 返回议题</a></p>")
    return views.page("代理授权", ctx.user, "".join(body), ctx.msg, ctx.err, "topics")


def proxy_post(ctx, tid):
    u = require_role(ctx, "owner", "manager")
    t = svc.get_topic(ctx.db, tid)
    unit_id = int(ctx.form["unit_id"])
    proxy_owner_id = int(ctx.form["proxy_owner_id"])
    rid, entries = svc.roster_of(ctx.db, tid, ctx.now)
    entry = next((e for e in entries if e["unit_id"] == unit_id), None)
    if not entry:
        raise BizError("该户不在名册内")
    # 授权书内容写本地虚构文件
    os.makedirs(ctx.app.auth_dir, exist_ok=True)
    fname = f"proxy_{t['code']}_{entry['unit_code']}_{ctx.now.strftime('%Y%m%d%H%M%S')}.txt"
    fpath = os.path.join(ctx.app.auth_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(ctx.form.get("auth_content", ""))
    try:
        svc.register_proxy(ctx.db, u, tid, unit_id, entry["owner_id"], proxy_owner_id,
                           fpath, notifier=ctx.app.notifier, now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{tid}/proxy", err=str(e)))
    raise Redirect(ok_url(f"/topics/{tid}/proxy", msg=f"代理授权已登记，授权书已保存至 {fpath}"))


def proxy_revoke(ctx, pid):
    u = require_role(ctx, "owner", "manager")
    p = ctx.db.one("SELECT * FROM proxies WHERE id=?", (int(pid),))
    if not p:
        raise NotFound("代理授权不存在")
    try:
        svc.revoke_proxy(ctx.db, u, int(pid), now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{p['topic_id']}/proxy", err=str(e)))
    raise Redirect(ok_url(f"/topics/{p['topic_id']}/proxy", msg="代理授权已撤销"))


def ballots_page(ctx, tid):
    require_login(ctx)
    t = svc.get_topic(ctx.db, tid)
    v = svc.latest_version(ctx.db, tid)
    try:
        ballots = svc.list_ballots(ctx.db, ctx.user, tid, now=ctx.now)
    except PermissionDenied as e:
        raise Redirect(ok_url(f"/topics/{tid}", err=str(e)))
    rows = "".join(
        f"<tr><td>{esc(b['unit_code'])}</td><td>{esc(b['roster_owner'])}</td>"
        f"<td>{esc(b['choice'])}</td><td>{b['seq']}</td>"
        f"<td>{esc({'active':'有效','superseded':'已被新版替代','withdrawn':'已撤回','invalid':'无效'}[b['status']])}</td>"
        f"<td class='small'>{esc(b['invalid_reason'] or '')}</td>"
        f"<td class='small'>{esc(svc.fmt(b['submitted_at']))}</td></tr>" for b in ballots)
    body = (f"<h1>选票明细 · {esc(v['title'])}</h1>"
            "<table><tr><th>户号</th><th>名册业主</th><th>选择</th><th>版本</th>"
            f"<th>状态</th><th>无效原因</th><th>提交时间</th></tr>{rows}</table>"
            f"<p><a href='/topics/{tid}'>← 返回议题</a></p>")
    return views.page("选票明细", ctx.user, body, ctx.msg, ctx.err, "topics")


def count_page(ctx, tid):
    u = require_role(ctx, "counter", "reviewer", "scrutineer")
    t = svc.get_topic(ctx.db, tid)
    v = svc.latest_version(ctx.db, tid)
    phase = svc.topic_phase(t, ctx.now)
    if phase != "closed":
        raise Redirect(ok_url(f"/topics/{tid}", err="投票未截止，不能计票"))
    body = [f"<h1>计票复核 · {esc(v['title'])}</h1>"]
    # 有效票明细 + 核定无效（计票员/监票人）
    ballots = svc.list_ballots(ctx.db, u, tid, now=ctx.now)
    active = [b for b in ballots if b["status"] == "active"]
    invalid = [b for b in ballots if b["status"] == "invalid"]
    if u["role"] in ("counter", "scrutineer"):
        rows = "".join(
            f"<tr><td>{esc(b['unit_code'])}</td><td>{esc(b['roster_owner'])}</td>"
            f"<td>{esc(b['choice'])}</td><td class='small'>{esc(svc.fmt(b['submitted_at']))}</td>"
            f"<td><form method='post' action='/topics/{tid}/invalidate' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='vote_id' value='{b['id']}'>"
            f"<input name='reason' placeholder='无效原因（必填）' required style='max-width:220px'>"
            "<button class='btn gray sm'>核定为无效</button></form></td></tr>" for b in active)
        body.append("<h2>有效票（可核定无效）</h2><table><tr><th>户号</th><th>名册业主</th>"
                    f"<th>选择</th><th>提交时间</th><th>操作</th></tr>"
                    f"{rows or '<tr><td colspan=5 class=muted>无</td></tr>'}</table>")
    if invalid:
        rows = "".join(f"<tr><td>{esc(b['unit_code'])}</td><td>{esc(b['invalid_reason'])}</td></tr>"
                       for b in invalid)
        body.append(f"<h2>已核定的无效票</h2><table><tr><th>户号</th><th>无效原因</th></tr>{rows}</table>")
    # 计票单历史
    runs = ctx.db.q(
        "SELECT r.*, u.name counter_name FROM counting_runs r JOIN users u ON u.id=r.counter_id"
        " WHERE r.topic_id=? ORDER BY r.id DESC", (tid,))
    for r in runs:
        res = json.loads(r["result"])
        opt_txt = "；".join(f"{o['choice']} {o['households']}户/{o['area']:.1f}㎡"
                            for o in res["options"])
        tot = res["totals"]
        status = {"pending": "待复核", "approved": "复核通过", "rejected": "复核驳回"}[r["status"]]
        review = ctx.db.one(
            "SELECT rv.*, u.name rname FROM reviews rv JOIN users u ON u.id=rv.reviewer_id"
            " WHERE rv.run_id=?", (r["id"],))
        rv_txt = (f"复核人 {esc(review['rname'])}：{esc(review['note'] or '')}"
                  if review else "")
        body.append(
            f"<div class='card'><b>计票单 #{r['id']}</b>（计票员 {esc(r['counter_name'])}，"
            f"{esc(svc.fmt(r['created_at']))}，{status}）<br>"
            f"<span class='small'>{esc(opt_txt)}</span><br>"
            f"<span class='small muted'>参与 {tot['voted_households']}/{tot['roster_households']} 户、"
            f"{tot['voted_area']:.1f}/{tot['roster_area']:.1f} ㎡；无效票 {len(res['invalid'])} 张；"
            f"判定：{'通过' if res['verdict']['passed'] else '未通过'}</span>"
            + (f"<br><span class='small muted'>{rv_txt}</span>" if rv_txt else "")
            + "</div>")
        if u["role"] == "reviewer" and r["status"] == "pending":
            body.append(f"""
<form method="post" action="/count/{r['id']}/review" class="card soft">
<b>复核计票单 #{r['id']}</b>（计票员与复核员不得为同一人）
<label>复核意见</label><input name="note" placeholder="复核说明">
<p><button class="btn sm" name="decision" value="approved">复核通过</button>
<button class="btn gray sm" name="decision" value="rejected">驳回</button></p>
</form>""")
    if u["role"] == "counter":
        body.append(f"<form method='post' action='/topics/{tid}/count'>"
                    "<button class='btn'>执行计票（生成新计票单）</button></form>")
    if u["role"] == "scrutineer":
        approved = ctx.db.one(
            "SELECT * FROM counting_runs WHERE topic_id=? AND status='approved'"
            " ORDER BY id DESC LIMIT 1", (tid,))
        if approved:
            body.append(f"<form method='post' action='/topics/{tid}/publish' class='card soft'>"
                        f"计票单 #{approved['id']} 已通过复核。监票人核对参与户数与面积权重后："
                        f"<p><button class='btn'>核对无误，公布表决结果</button></p></form>")
        else:
            body.append("<p class='muted'>暂无复核通过的计票单，暂不能公布。</p>")
    body.append(f"<p><a href='/topics/{tid}'>← 返回议题</a></p>")
    return views.page("计票复核", ctx.user, "".join(body), ctx.msg, ctx.err, "topics")


def count_post(ctx, tid):
    u = require_role(ctx, "counter")
    try:
        run_id = svc.run_count(ctx.db, u, tid, now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{tid}/count", err=str(e)))
    raise Redirect(ok_url(f"/topics/{tid}/count", msg=f"计票单 #{run_id} 已生成，待复核"))


def review_post(ctx, run_id):
    u = require_role(ctx, "reviewer")
    run = ctx.db.one("SELECT * FROM counting_runs WHERE id=?", (int(run_id),))
    if not run:
        raise NotFound("计票单不存在")
    try:
        svc.review_count(ctx.db, u, int(run_id), ctx.form.get("decision", ""),
                         ctx.form.get("note", ""), now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{run['topic_id']}/count", err=str(e)))
    raise Redirect(ok_url(f"/topics/{run['topic_id']}/count", msg="复核意见已记录"))


def invalidate_post(ctx, tid):
    u = require_role(ctx, "counter", "scrutineer")
    try:
        svc.invalidate_vote(ctx.db, u, int(ctx.form["vote_id"]),
                            ctx.form.get("reason", ""), now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{tid}/count", err=str(e)))
    raise Redirect(ok_url(f"/topics/{tid}/count", msg="已核定为无效票并记录原因"))


def publish_post(ctx, tid):
    u = require_role(ctx, "scrutineer")
    try:
        svc.publish_result(ctx.db, u, tid, notifier=ctx.app.notifier, now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url(f"/topics/{tid}/count", err=str(e)))
    raise Redirect(ok_url(f"/topics/{tid}/result", msg="表决结果已公布"))


def result_page(ctx, tid):
    require_login(ctx)
    t = svc.get_topic(ctx.db, tid)
    got = svc.get_result(ctx.db, tid)
    if not got:
        raise Redirect(ok_url(f"/topics/{tid}", err="结果尚未公布"))
    payload, rec = got
    meta = payload["meta"]
    tot = payload["totals"]
    verdict = payload["verdict"]
    names = {}
    for key, label in (("counter_id", "计票员"), ("reviewer", "复核员"), ("scrutineer_id", "监票人")):
        names[label] = ctx.db.one("SELECT name FROM users WHERE id=?", (meta[key],))["name"]
    body = [f"<h1>表决结果 · {esc(meta['title'])}</h1>"]
    body.append(views.kv([
        ("议题编号", esc(meta["code"])),
        ("计票规则", esc(meta["rule_text"])),
        ("名册户数", f"{tot['roster_households']} 户 / {tot['roster_area']:.1f} ㎡"),
        ("计票员", esc(names["计票员"])),
        ("复核员", esc(names["复核员"])),
        ("监票人（公布人）", esc(names["监票人"])),
        ("公布时间", esc(svc.fmt(rec["published_at"]))),
    ]))
    ph = tot["voted_households"] / tot["roster_households"] * 100 if tot["roster_households"] else 0
    pa = tot["voted_area"] / tot["roster_area"] * 100 if tot["roster_area"] else 0
    body.append("<h2>参与情况（户数比例与面积比例）</h2>")
    body.append(views.bar("参与户数比例", ph, f"{tot['voted_households']}/{tot['roster_households']} 户"))
    body.append(views.bar("参与面积比例", pa, f"{tot['voted_area']:.1f}/{tot['roster_area']:.1f} ㎡"))
    body.append("<h2>各选项得票（户数比例与面积比例，按参与票计）</h2>")
    for o in payload["options"]:
        hp = o["households"] / tot["voted_households"] * 100 if tot["voted_households"] else 0
        ap = o["area"] / tot["voted_area"] * 100 if tot["voted_area"] else 0
        body.append(f"<div class='card'><b>{esc(o['choice'])}</b>")
        body.append(views.bar("户数比例", hp, f"{o['households']} 户"))
        body.append(views.bar("面积比例", ap, f"{o['area']:.1f} ㎡"))
        body.append("</div>")
    qtxt = "达到" if verdict["quorum_ok"] else "未达到"
    ptxt = "通过" if verdict["passed"] else "未通过"
    cls = "msg" if verdict["passed"] else "err"
    body.append(f"<div class='{cls}'>参与门槛（{payload['quorum']:.0%}）：{qtxt}；"
                f"表决结果：<b>{ptxt}</b>（同意须同时占参与户数与参与面积的 "
                f"{payload['pass_ratio']:.0%} 以上）</div>")
    if payload["invalid"]:
        rows = "".join(f"<tr><td>{esc(x['unit'])}</td><td>{esc(x['reason'])}</td></tr>"
                       for x in payload["invalid"])
        body.append("<h2>无效票及原因</h2><table><tr><th>户号</th><th>无效原因</th></tr>"
                    f"{rows}</table>")
    else:
        body.append("<p class='muted small'>本次表决无无效票。</p>")
    body.append(f"<p><a href='/topics/{tid}'>← 返回议题</a></p>")
    return views.page("表决结果", ctx.user, "".join(body), ctx.msg, ctx.err, "topics")


def transfers_page(ctx):
    require_role(ctx, "manager")
    units = ctx.db.q(
        "SELECT u.*, o.name owner_name, o.id owner_id FROM units u"
        " JOIN unit_ownership uo ON uo.unit_id=u.id AND uo.until IS NULL"
        " JOIN owners o ON o.id=uo.owner_id ORDER BY u.code")
    owners = ctx.db.q("SELECT * FROM owners ORDER BY id")
    unit_opts = "".join(f"<option value='{u['id']}'>"
                        f"{esc(u['code'])}（现业主 {esc(u['owner_name'])}）</option>" for u in units)
    owner_opts = "".join(f"<option value='{o['id']}'>{esc(o['name'])}</option>" for o in owners)
    now = svc.utcnow()
    body = f"""
<h1>过户登记</h1>
<form method="post" action="/admin/transfers" class="card soft">
<label>房屋</label><select name="unit_id">{unit_opts}</select>
<label>新业主</label><select name="to_owner_id">{owner_opts}</select>
<label>生效时间</label><input type="datetime-local" name="effective_at"
 value="{local_input_value(now)}" required>
<p style="margin-top:12px"><button class="btn">登记过户</button></p>
<p class="small muted">已冻结名册的议题不受影响，其投票资格仍以冻结时名册为准。</p>
</form>"""
    rows = ctx.db.q(
        "SELECT t.*, u.code, of.name from_name, ot.name to_name, ur.name reg_name FROM transfers t"
        " JOIN units u ON u.id=t.unit_id JOIN owners of ON of.id=t.from_owner_id"
        " JOIN owners ot ON ot.id=t.to_owner_id JOIN users ur ON ur.id=t.registered_by"
        " ORDER BY t.id DESC")
    trs = "".join(f"<tr><td>{esc(r['code'])}</td><td>{esc(r['from_name'])} → {esc(r['to_name'])}</td>"
                  f"<td>{esc(svc.fmt(r['effective_at']))}</td>"
                  f"<td class='small'>{esc(r['reg_name'])} 登记于 {esc(svc.fmt(r['registered_at']))}</td></tr>"
                  for r in rows)
    body += ("<h2>过户记录</h2><table><tr><th>户号</th><th>过户</th><th>生效时间</th>"
             f"<th>登记</th></tr>{trs or '<tr><td colspan=4 class=muted>暂无</td></tr>'}</table>")
    return views.page("过户登记", ctx.user, body, ctx.msg, ctx.err, "transfers")


def transfer_post(ctx):
    u = require_role(ctx, "manager")
    try:
        svc.register_transfer(ctx.db, u, int(ctx.form["unit_id"]),
                              int(ctx.form["to_owner_id"]),
                              parse_local(ctx.form["effective_at"]),
                              notifier=ctx.app.notifier, now=ctx.now)
    except BizError as e:
        raise Redirect(ok_url("/admin/transfers", err=str(e)))
    raise Redirect(ok_url("/admin/transfers", msg="过户已登记；已冻结名册不受影响"))


def audit_page(ctx):
    require_role(ctx, "manager", "scrutineer")
    rows = ctx.db.q(
        "SELECT a.*, u.name FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id"
        " ORDER BY a.id DESC LIMIT 300")
    trs = "".join(
        f"<tr><td class='small'>{esc(svc.fmt(r['at']))}</td>"
        f"<td>{esc(r['name'] or '系统')}</td><td>{esc(r['action'])}</td>"
        f"<td class='small'>{esc((r['entity'] or '') + ('#' + str(r['entity_id']) if r['entity_id'] else ''))}</td>"
        f"<td>{'成功' if r['result']=='ok' else '<b>被拒绝</b>'}</td>"
        f"<td class='small'>{esc(r['detail'] or '')}</td></tr>" for r in rows)
    body = ("<h1>审计日志</h1><p class='muted small'>含被拒绝的操作尝试；"
            "投票动作只记录行为不记录选项，截止前个人选择保密。</p>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th>"
            f"<th>结果</th><th>说明</th></tr>{trs}</table>")
    return views.page("审计日志", ctx.user, body, ctx.msg, ctx.err, "audit")


def notifications_page(ctx):
    u = require_login(ctx)
    rows = ctx.db.q("SELECT * FROM outbox WHERE user_id=? ORDER BY id DESC LIMIT 100",
                    (u["id"],))
    trs = "".join(
        f"<tr><td class='small'>{esc(svc.fmt(r['at']))}</td>"
        f"<td>{esc(EVENT_LABEL.get(r['event'], r['event']))}</td>"
        f"<td class='small'>{esc(r['payload'])}</td></tr>" for r in rows)
    body = ("<h1>通知记录</h1><p class='muted small'>模拟通知适配器："
            "所有通知写入本地出站记录，不发送真实短信/邮件。</p>"
            "<table><tr><th>时间</th><th>类型</th><th>内容</th></tr>"
            f"{trs or '<tr><td colspan=3 class=muted>暂无通知</td></tr>'}</table>")
    return views.page("通知记录", ctx.user, body, ctx.msg, ctx.err, "notifications")


# ---------------------------------------------------------------- 路由

ROUTES = [
    ("GET", r"^/$", home),
    ("GET", r"^/login$", login_form),
    ("POST", r"^/login$", login_post),
    ("GET", r"^/logout$", logout),
    ("GET", r"^/topics/new$", topic_new),
    ("POST", r"^/topics/new$", topic_create),
    ("GET", r"^/topics/(\d+)$", topic_detail),
    ("GET", r"^/topics/(\d+)/edit$", topic_edit),
    ("POST", r"^/topics/(\d+)/edit$", topic_edit_post),
    ("POST", r"^/topics/(\d+)/announce$", announce_post),
    ("POST", r"^/topics/(\d+)/reinitiate$", reinitiate_post),
    ("GET", r"^/topics/(\d+)/eligibility$", eligibility),
    ("GET", r"^/topics/(\d+)/vote$", vote_page),
    ("POST", r"^/topics/(\d+)/vote$", vote_post),
    ("POST", r"^/topics/(\d+)/withdraw$", withdraw_post),
    ("GET", r"^/topics/(\d+)/proxy$", proxy_page),
    ("POST", r"^/topics/(\d+)/proxy$", proxy_post),
    ("POST", r"^/proxy/(\d+)/revoke$", proxy_revoke),
    ("GET", r"^/topics/(\d+)/ballots$", ballots_page),
    ("GET", r"^/topics/(\d+)/count$", count_page),
    ("POST", r"^/topics/(\d+)/count$", count_post),
    ("POST", r"^/count/(\d+)/review$", review_post),
    ("POST", r"^/topics/(\d+)/invalidate$", invalidate_post),
    ("POST", r"^/topics/(\d+)/publish$", publish_post),
    ("GET", r"^/topics/(\d+)/result$", result_page),
    ("GET", r"^/admin/transfers$", transfers_page),
    ("POST", r"^/admin/transfers$", transfer_post),
    ("GET", r"^/audit$", audit_page),
    ("GET", r"^/notifications$", notifications_page),
]

ERROR_PAGES = {400: "请求有误", 403: "无权访问", 404: "页面不存在", 500: "服务器内部错误"}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, html, cookie=None):
            data = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if cookie is not None:
                if cookie == "":
                    self.send_header("Set-Cookie", "sid=; Path=/; Max-Age=0; HttpOnly")
                else:
                    self.send_header("Set-Cookie", f"sid={cookie}; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            ctx = Ctx(app, self, method)
            try:
                for m, pattern, fn in ROUTES:
                    if m != method:
                        continue
                    match = re.match(pattern, ctx.path)
                    if match:
                        html = fn(ctx, *match.groups())
                        self._send(200, html)
                        return
                raise NotFound("页面不存在")
            except RedirectWithCookie as r:
                if r.sid == "":
                    self._send_redirect(r.location, clear=True)
                else:
                    self._send_redirect(r.location, sid=r.sid)
            except Redirect as r:
                self._send_redirect(r.location)
            except PermissionDenied as e:
                self._send(403, views.page("无权访问", ctx.user,
                                           f"<h1>403 无权访问</h1><p>{esc(str(e))}</p>"
                                           "<p><a href='/'>返回首页</a></p>"))
            except NotFound as e:
                self._send(404, views.page("页面不存在", ctx.user,
                                           f"<h1>404 页面不存在</h1><p>{esc(str(e))}</p>"
                                           "<p><a href='/'>返回首页</a></p>"))
            except BizError as e:
                self._send(400, views.page("请求有误", ctx.user,
                                           f"<h1>400 请求有误</h1><p>{esc(str(e))}</p>"
                                           "<p><a href='/'>返回首页</a></p>"))
            except Exception:
                traceback.print_exc()
                self._send(500, views.page("服务器错误", ctx.user,
                                           "<h1>500 服务器内部错误</h1>"
                                           "<p><a href='/'>返回首页</a></p>"))

        def _send_redirect(self, location, sid=None, clear=False):
            self.send_response(303)
            self.send_header("Location", location)
            if clear:
                self.send_header("Set-Cookie", "sid=; Path=/; Max-Age=0; HttpOnly")
            elif sid:
                self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")
            self.end_headers()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            pass  # 静默访问日志

    return Handler


def serve(db, secret, auth_dir, host="127.0.0.1", port=8000):
    app = App(db, secret, auth_dir)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    return server
