#!/usr/bin/env bash
# 在目标机上把打包好的 ghradar 一键装好（重建 venv + 装依赖 + 自检 + 给出 MCP 配置）。
# 用法: bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 检查 Python"
if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：未找到 python3，请先安装 Python 3.11+"; exit 1
fi
python3 --version

echo "==> 创建虚拟环境"
python3 -m venv .venv

echo "==> 安装依赖与本包（需要联网，一次性）"
.venv/bin/pip install --quiet setuptools wheel
.venv/bin/pip install --quiet -e .

echo "==> 自检"
if [ -f data/repos.db ]; then
    .venv/bin/ghradar stats
else
    echo "未发现 data/repos.db（未打包索引），请先建索引："
    echo "  .venv/bin/ghradar crawl --min-stars 100 && .venv/bin/ghradar embed"
fi

MCP_BIN="$(pwd)/.venv/bin/ghradar-mcp"
echo
echo "==> 安装完成！把下面这行填到目标 AI harness 的 MCP 配置里："
echo "    command: $MCP_BIN"
echo
echo "==> 可选：HTTP 模式（给远程/不支持 stdio 的 harness）"
echo "    .venv/bin/ghradar mcp --transport streamable-http --host 0.0.0.0 --port 8000"
echo "    （端点 http://<host>:8000/mcp）"
