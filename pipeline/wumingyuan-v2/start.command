#!/bin/bash
# 无名园 一键启动（macOS：双击本文件）
# 首次运行自动建虚拟环境、装依赖。
set -e
cd "$(dirname "$0")"

PORT=8888

echo "==> 无名园 启动中…"

if [ ! -d ".venv" ]; then
  echo "==> 首次运行：创建虚拟环境并安装依赖（约 1-3 分钟）"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

echo "==> 启动网页服务：http://127.0.0.1:${PORT}"
echo "    把 PDF 放入书籍目录，然后在网页点击「扫描并导入」。"
( sleep 2 && open "http://127.0.0.1:${PORT}" ) &
exec ./.venv/bin/python server.py
