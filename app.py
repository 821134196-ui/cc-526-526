#!/usr/bin/env python3
"""业委会表决系统 —— 一条命令启动：

    python3 app.py            # 首次运行自动建库并灌入样例（含过户与代理）
    python3 app.py --reset    # 清空数据并重新生成样例
    python3 app.py --port 8080

测试：python3 -m unittest discover -s tests -v
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from voting.db import init_db, is_empty
from voting.notify import MockNotifier
from voting.seed import seed
from voting.services import VotingSystem
from voting.web import serve

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(description="业委会表决系统")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=os.path.join(BASE, "data", "vote.db"))
    parser.add_argument("--reset", action="store_true", help="清空并重建样例数据")
    args = parser.parse_args()

    docs_dir = os.path.join(BASE, "data", "proxy_docs")
    os.makedirs(docs_dir, exist_ok=True)

    if args.reset and os.path.exists(args.db):
        os.remove(args.db)
        for f in os.listdir(docs_dir):
            os.remove(os.path.join(docs_dir, f))

    init_db(args.db)
    system = VotingSystem(args.db, docs_dir, notifier=MockNotifier(args.db))
    if is_empty(args.db):
        seed(system)
    serve(system, port=args.port)


if __name__ == "__main__":
    main()
