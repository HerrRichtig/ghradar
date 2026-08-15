#!/usr/bin/env bash
# 打包 ghradar 为可移植压缩包（代码 + 数据 + 一键安装脚本）。
# 用法: ./pack.sh           # 含 data/ 索引与模型缓存，目标机免抓取
#       ./pack.sh --no-data # 不含 data/，目标机自己 crawl+embed
set -euo pipefail
cd "$(dirname "$0")"

OUT="ghradar-portable.tar.gz"
INCLUDE_DATA=1
[ "${1:-}" = "--no-data" ] && INCLUDE_DATA=0

ITEMS=(ghradar pyproject.toml .env.example README.md PROMPTS.md install.sh)
if [ "$INCLUDE_DATA" = "1" ] && [ -d data ]; then
    ITEMS+=(data)
fi

echo "打包内容: ${ITEMS[*]}"
tar -czf "$OUT" --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' "${ITEMS[@]}"
echo "完成: $OUT  ($(du -h "$OUT" | cut -f1))"
echo
echo "目标机操作："
echo "  mkdir ghradar-portable && cd ghradar-portable"
echo "  tar -xzf /path/to/$OUT"
echo "  bash install.sh"
