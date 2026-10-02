#!/bin/sh
# 一条命令启动小区业主表决系统（含演示数据：过户样例 + 代理样例）
cd "$(dirname "$0")"
exec python3 app.py "$@"
