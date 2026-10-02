#!/usr/bin/env python3
"""小区业主表决系统 —— 一条命令启动：

    python3 app.py            # 首次运行自动建库并写入演示数据（含过户与代理样例）
    python3 app.py --port 8080
    python3 app.py --reset    # 清空数据库重新生成演示数据

仅使用 Python 标准库（http.server + sqlite3），无需安装任何依赖。
"""
import argparse
import os
import secrets as _secrets

from voteapp import seed
from voteapp.db import DB
from voteapp.web import serve

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "vote.db")
SECRET_PATH = os.path.join(DATA, "secret.key")
AUTH_DIR = os.path.join(DATA, "auth_files")


def load_secret():
    os.makedirs(DATA, exist_ok=True)
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "rb") as f:
            return f.read()
    key = _secrets.token_bytes(32)
    with open(SECRET_PATH, "wb") as f:
        f.write(key)
    return key


def main():
    ap = argparse.ArgumentParser(description="小区业主表决系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="删除现有数据库并重新生成演示数据")
    args = ap.parse_args()

    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("已删除旧数据库")

    os.makedirs(DATA, exist_ok=True)
    os.makedirs(AUTH_DIR, exist_ok=True)
    first_run = not os.path.exists(DB_PATH)
    db = DB(DB_PATH)
    if first_run or db.one("SELECT COUNT(*) n FROM users")["n"] == 0:
        print("初始化演示数据（含过户样例与代理样例）……")
        seed.seed(db, auth_dir=AUTH_DIR)
        print("演示数据就绪。")

    server = serve(db, load_secret(), AUTH_DIR, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"""
┌──────────────────────────────────────────────────────────┐
│  小区业主表决系统已启动：{url}
│                                                          │
│  演示账号（登录页点击即可，无需密码）：                  │
│    业委会 王建华 · 监票人 赵德柱 · 计票员 钱进           │
│    复核员 孙立 · 业主 周明/吴芳/郑强/李伟/张敏 等        │
│                                                          │
│  演示场景：                                              │
│    · 议题一（投票中）：含代理投票、改票、撤票样例        │
│    · 1-2-202 在名册冻结后过户（王雪 → 陈晨）             │
│    · 议题二（已截止）：计票复核流程                      │
│    · 议题三（已公布）：双比例结果与无效票原因            │
│                                                          │
│  运行测试：python3 -m unittest discover -s tests -v      │
│  Ctrl+C 停止                                             │
└──────────────────────────────────────────────────────────┘
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
