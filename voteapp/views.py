"""页面渲染：白底、深灰正文、少量公示蓝，适合正式阅读。"""
from html import escape as esc

from .services import PHASE_LABEL, ROLE_LABEL, fmt

CSS = """
:root{--blue:#1a56a8;--ink:#2f3337;--muted:#6b7280;--line:#e2e4e8;--soft:#f6f7f9}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);
 font:15px/1.75 "PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
header.top{border-bottom:1px solid var(--line);background:#fff}
.top-inner{max-width:1000px;margin:0 auto;padding:13px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.brand{font-size:17px;font-weight:700;color:var(--blue);letter-spacing:1px}
nav a{color:var(--ink);margin-right:14px;font-size:14px}
nav a.on{color:var(--blue);font-weight:600}
.userbox{margin-left:auto;color:var(--muted);font-size:13px}
main{max-width:1000px;margin:24px auto 60px;padding:0 20px}
h1{font-size:21px;margin:0 0 6px}
h2{font-size:16px;margin:26px 0 10px;color:#1f2328;border-left:3px solid var(--blue);padding-left:8px}
.card{border:1px solid var(--line);border-radius:8px;padding:18px 20px;margin:14px 0}
.soft{background:var(--soft)}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{background:var(--soft);font-weight:600}
.badge{display:inline-block;padding:1px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--muted);background:#fff}
.badge.blue{background:var(--blue);border-color:var(--blue);color:#fff}
.btn{display:inline-block;background:var(--blue);color:#fff;border:none;border-radius:6px;
 padding:7px 16px;font-size:14px;cursor:pointer;font-family:inherit}
.btn:hover{text-decoration:none;opacity:.92}
.btn.gray{background:#fff;color:var(--ink);border:1px solid #c9ccd1}
.btn.sm{padding:3px 10px;font-size:13px}
input,select,textarea{font:inherit;font-size:14px;padding:6px 8px;border:1px solid #c9ccd1;
 border-radius:6px;width:100%;max-width:420px;background:#fff}
input[type=radio],input[type=checkbox]{width:auto}
textarea{max-width:100%}
label{display:block;margin:10px 0 4px;font-size:13px;color:var(--muted)}
.msg{border:1px solid #b7d7a8;background:#f0f7ea;color:#3a6b2a;padding:8px 14px;border-radius:6px;margin:12px 0}
.err{border:1px solid #e3b7b7;background:#faf0f0;color:#8a3a3a;padding:8px 14px;border-radius:6px;margin:12px 0}
.deadline{font-weight:700;color:var(--blue)}
.bar-row{display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px}
.bar-label{width:110px;text-align:right;color:var(--muted)}
.bar-track{flex:1;background:var(--soft);border-radius:4px;height:18px;overflow:hidden}
.bar-fill{background:var(--blue);height:100%}
.bar-val{width:190px}
.muted{color:var(--muted)}.small{font-size:13px}
.choice-current{border:1px solid var(--blue);background:#eef4fc;border-radius:6px;padding:10px 14px;margin:8px 0}
.option-line{display:block;margin:6px 0;font-size:15px;color:var(--ink)}
footer{max-width:1000px;margin:0 auto;padding:18px 20px;color:var(--muted);font-size:12px;
 border-top:1px solid var(--line)}
"""


def badge(phase):
    cls = "blue" if phase in ("voting", "published") else ""
    return f'<span class="badge {cls}">{esc(PHASE_LABEL[phase])}</span>'


def page(title, user, body, msg=None, err=None, active=""):
    def navlink(href, label, key):
        cls = ' class="on"' if active == key else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    if user:
        role = ROLE_LABEL[user["role"]]
        userbox = (f'{esc(user["name"])}（{role}） · '
                   f'<a href="/logout">退出</a>')
        nav = navlink("/", "议题列表", "topics") + navlink("/notifications", "通知记录", "notifications")
        if user["role"] in ("manager", "scrutineer"):
            nav += navlink("/audit", "审计日志", "audit")
        if user["role"] == "manager":
            nav += navlink("/admin/transfers", "过户登记", "transfers")
            nav += navlink("/topics/new", "发起议题", "new")
    else:
        userbox = '<a href="/login">登录</a>'
        nav = ""
    flash = ""
    if msg:
        flash += f'<div class="msg">{esc(msg)}</div>'
    if err:
        flash += f'<div class="err">{esc(err)}</div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 小区业主表决系统</title><style>{CSS}</style></head>
<body>
<header class="top"><div class="top-inner">
<span class="brand">小区业主表决系统</span><nav>{nav}</nav>
<span class="userbox">{userbox}</span>
</div></header>
<main>{flash}{body}</main>
<footer>本系统为演示环境 · 投票资格以投票开始时冻结的名册为准 ·
个人选择在投票截止前对所有人保密 · 所有关键操作均留有审计记录</footer>
</body></html>"""


def kv(rows):
    """键值说明表。"""
    trs = "".join(f"<tr><th style='width:150px'>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return f"<table>{trs}</table>"


def time_row(label, value):
    return (label, f'<span class="deadline">{esc(fmt(value))}</span>' if value else "—")


def bar(label, pct, val_text):
    pct = max(0.0, min(100.0, pct))
    return (f'<div class="bar-row"><span class="bar-label">{esc(label)}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="bar-val">{esc(val_text)}（{pct:.1f}%）</span></div>')
