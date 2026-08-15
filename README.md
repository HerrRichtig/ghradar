# ghradar — 定期更新的 GitHub 项目检索器

每当你有一个新想法时，不必急着从零实现——先看看 GitHub 上是否已有成熟项目可以借鉴。
`ghradar` 把一个「定期更新的 GitHub 项目索引」放在本地，让 AI（或你自己）用一句话就能
快速、省 token 地找到相关项目，而不是让 AI 每次去 GitHub 上从头翻一遍。

## 它解决什么问题

- **省 token**：AI 不再逐页浏览 GitHub / 反复调 Search API。它只调用一个 `search_repos` 工具，
  拿到的是已经过滤好的 top-N 结果（星数、语言、描述、话题、链接）。
- **想法 → 项目**：用中文或英文描述想法，通过**本地多语言向量 + 关键词**混合检索，
  匹配英文仓库的 name/description/topics，无需逐字命中。
- **定期更新**：索引按计划增量刷新，新项目、星数变化、近期活跃仓库都会持续进入索引。

## 架构

```
┌──────────────┐   GitHub Search API（分片穷举）   ┌────────────────────┐
│  crawler.py  │ ────────────────────────────────▶ │  SQLite (repos.db) │
│  星数桶→年份→语言 递归切分，限速+续传             │  + FTS5(trigram)   │
└──────────────┘                                   └─────────┬──────────┘
┌──────────────┐   fastembed 本地多语言模型（离线）  │  repo_ids.npy / vectors.npy
│   embed.py   │ ─────────────────────────────────▶│  （归一化向量，点积=余弦）
└──────────────┘                                   └─────────┬──────────┘
┌──────────────┐   语义 top-N + 关键词 FTS → RRF 融合  ◀────┘
│ retriever.py │ ─────────────────────────────────────────
└──────────────┘
        │
   ┌────┴─────┐
   │ CLI      │  ghradar search "本地大模型 RAG 框架"
   │ MCP server│  search_repos(query)  ← Claude / Cursor 等 AI 客户端
   └──────────┘
```

## 快速开始

要求：Python ≥ 3.11（已在 3.14 验证），可访问 GitHub API。

```bash
cd "github search"
python3 -m venv .venv
.venv/bin/pip install -e .            # 安装 ghradar 与 ghradar-mcp 命令
.venv/bin/pip install setuptools wheel  # 首次若报错需先装构建后端

# 1) 抓取索引（演示用 5000 星；完整全量用 --min-stars 100）
.venv/bin/ghradar crawl --min-stars 5000

# 2) 生成向量（首次会自动下载约 120MB 的多语言模型到 data/hf）
.venv/bin/ghradar embed

# 3) 检索
.venv/bin/ghradar search "本地运行的大模型 RAG 框架" --top 10
.venv/bin/ghradar search "self-hosted kubernetes monitoring" --lang go
.venv/bin/ghradar search "real-time collaborative editor" --topic llm --json
```

> 建议配置 `GITHUB_TOKEN`（见 `.env.example`），未认证 Search 限速 10 次/分钟，
> 认证后 30 次/分钟，全量抓取快约 3 倍。Token 只需只读权限。

## 命令

| 命令 | 说明 |
|------|------|
| `ghradar search QUERY [--top N] [--min-stars N] [--lang L] [--topic T] [--json]` | 混合检索 |
| `ghradar crawl [--min-stars N] [--max-shards N] [--force]` | 抓取/刷新索引（断点续传） |
| `ghradar update [--days 7]` | 增量更新：最近新建/活跃 + 过期分片重枚举 |
| `ghradar embed [--batch 64]` | 为新增/变更仓库生成向量 |
| `ghradar stats` | 索引统计 |
| `ghradar mcp` | 以 MCP stdio 服务运行（等价 `ghradar-mcp`） |

## 接入 AI 客户端（MCP）

`ghradar` 以 stdio MCP server 形式暴露三个工具：`search_repos`、`get_repo`、`index_stats`。

**Claude Desktop**（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "ghradar": {
      "command": "/home/yuze/Projects/github search/.venv/bin/ghradar-mcp",
      "env": { "HF_HOME": "/home/yuze/Projects/github search/data/hf" }
    }
  }
}
```

**Cursor / 其他支持 MCP 的客户端**：添加 command 为 `ghradar-mcp` 的 stdio server 即可。
之后直接对 AI 说：「用 ghradar 找找有没有现成的本地大模型 RAG 框架」，它就会调用
`search_repos` 而不是去逐页翻 GitHub。

## 移植到其他 harness / 环境

ghradar 是**自包含、可移植**的：代码（纯 Python 包）+ 数据（索引/向量/模型缓存）+ 一个
标准接口（MCP）。移植到任何新机器或新 harness 就三步：

1. **拷代码并重建环境**：把 `ghradar/` + `pyproject.toml` + `.env.example` 拷过去，
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install setuptools wheel && .venv/bin/pip install -e .
   ```
2. **拷数据或重建**（二选一）：
   - 直接拷 `data/` 目录（`repos.db` + `repo_ids.npy`/`vectors.npy` + `hf/` 模型缓存），拷完即用；
   - 或在新机器重建：`ghradar crawl --min-stars 100 && ghradar embed`。
3. **接目标 harness**，按其支持的接口三选一：
   - **MCP（stdio）**：Claude Desktop / Cursor / Cline / Continue / Windsurf 等，配置指向
     `ghradar-mcp` 的绝对路径即可（路径随机器改）。
   - **HTTP（streamable HTTP MCP）**：任何能发 HTTP 的 harness 都能用，起服务后走
     `http://<host>:8000/mcp`（标准 MCP streamable-HTTP 协议）：
     ```bash
     ghradar mcp --transport streamable-http --host 0.0.0.0 --port 8000
     # 旧式 SSE 用 --transport sse（端点 /sse）
     # 等价环境变量：GHRADAR_TRANSPORT / GHRADAR_HOST / GHRADAR_PORT
     ```
   - **纯命令行**：任何能执行子进程的 harness/脚本，直接 `ghradar search "想法" --json` 拿 JSON。

> 数据目录可用 `GHRADAR_DATA_DIR` 重定向，模型缓存跟数据目录走；无需全局安装、无需 Docker、
> 除抓取外无需联网。

## 定期更新

用 cron / systemd timer 定期跑 `update` 即可保持索引新鲜。例如每天凌晨增量更新：

```cron
# 每天 03:00 增量更新 + 补向量
0 3 * * * cd "/home/yuze/Projects/github search" && .venv/bin/ghradar update --days 7 && .venv/bin/ghradar embed
```

`crawl` 自带断点续传（`crawl_state` 表记录每个分片），中断后重跑会自动跳过 7 天内
已抓的分片；`--force` 可强制全量重抓。

## 抓取规模与原理

GitHub Search 单查询最多返回 1000 条，因此按「**星数桶 → pushed 年份 → 语言**」三级
递归切分，把每片压到 1000 条以内；仍超出的片按 stars 降序截断（优先保留最成熟的一批，
正好符合「找成熟项目」的目标）。

- `--min-stars 5000`：约几万条，几分钟到十几分钟。
- `--min-stars 100`：全语言成熟项目，约几十万条，未认证需数小时；建议配 Token 或后台跑。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `GITHUB_TOKEN` | 空 | GitHub 访问令牌（可选，建议） |
| `GHRADAR_DATA_DIR` | `./data` | 索引与模型缓存目录 |
| `GHRADAR_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 向量模型 |
| `GHRADAR_EMBED_DIM` | `384` | 向量维度（须与模型一致） |
| `GHRADAR_MIN_STARS` | `100` | 默认抓取星数下限 |

## 目录结构

```
ghradar/
  config.py      路径/参数/模型配置
  db.py          SQLite schema + FTS5(trigram) + 抓取状态
  crawler.py     GitHub Search 分片抓取（限速/退避/续传/增量）
  embed.py       fastembed 本地多语言向量，增量写盘
  retriever.py   语义 + 关键词 RRF 混合检索
  cli.py         命令行入口
  mcp_server.py  MCP stdio 服务
data/
  repos.db       仓库索引
  repo_ids.npy / vectors.npy   向量（归一化，点积=余弦）
  hf/            本地模型缓存
```
