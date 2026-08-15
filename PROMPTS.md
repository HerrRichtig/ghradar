# 如何让 AI 使用 ghradar（提示指南）

## 一句话定位

ghradar = 一个**本地定期更新的 GitHub 项目索引**，让 AI（或你）用一句话想法就能
快速、省 token 地找到 GitHub 上已有的成熟项目，而不是每次去 GitHub 逐页翻。

它有**两种形态**，你按场景选：

| 形态 | 命令 | 给谁用 |
|------|------|--------|
| MCP 服务 | `ghradar-mcp` | Claude / Cursor / 其他 AI 客户端，作为工具被调用 |
| 命令行 | `ghradar` | 你自己或脚本 |

---

## 一、AI 客户端（MCP）方式

### 1. 一次性配置（只做一次）

Claude Desktop 的 `claude_desktop_config.json`：

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

Cursor / 其他客户端：添加 command 为 `ghradar-mcp` 的 stdio server 即可。
配好后，AI 会自动看到 3 个工具：

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `search_repos` | 按想法检索项目 | `query`(必填) `top_k` `min_stars` `language` `topic` |
| `get_repo` | 查单个项目详情 | `full_name`（如 `infiniflow/ragflow`） |
| `index_stats` | 看索引有多少项目 | 无 |

### 2. 怎么提示（核心心法）

**用「一句话描述想法」而不是「关键词」；把约束（语言/星数/领域）写进去。**

✅ 好提示：

```
我想做一个「本地运行的大模型 RAG 框架」，先别自己写，
用 ghradar 找找 GitHub 上有没有成熟的现成项目，只要 Python、1000 星以上的，给我前 10 个对比。
```

```
用 search_repos 查一下有没有现成的 kubernetes 监控方案（self-hosted），
返回 top 5，附上星数、语言和一句话说明。
```

❌ 差提示（把它当普通关键词搜索）：

```
搜 rag
```

——语义检索靠整句话的“意思”匹配，关键词片段召回会差很多。

### 3. 可直接复制的「系统提示」

想让它养成“先查再写”的习惯，可把下面这段粘到 AI 的 system prompt /
项目规则 / 自定义指令里：

```
在动手实现任何新想法之前，先用 ghradar 的 search_repos 工具查一遍
GitHub 上是否已有成熟的开源项目可以借鉴。
查询时：用一句完整的话描述想法（中英文皆可），
并写明约束（语言 language、最低星数 min_stars、话题 topic）。
拿到结果后，先总结 2-3 个最匹配的项目及其星数/语言/差异，再决定是参考还是自研。
```

---

## 二、命令行（自己/脚本）方式

```bash
cd "/home/yuze/Projects/github search"

# 检索（AI 调 MCP 时底层走的就是这个）
.venv/bin/ghradar search "本地运行的大模型 RAG 框架" --top 10
.venv/bin/ghradar search "self-hosted kubernetes monitoring" --lang go --min-stars 1000
.venv/bin/ghradar search "real-time collaborative editor" --topic llm --json

# 维护索引
.venv/bin/ghradar crawl --min-stars 100      # 全量抓取（建议先配 GITHUB_TOKEN）
.venv/bin/ghradar update --days 7            # 增量更新
.venv/bin/ghradar embed                      # 为新增/变更仓库补向量
.venv/bin/ghradar stats                      # 看索引状态
```

定期更新（cron）：

```cron
0 3 * * * cd "/home/yuze/Projects/github search" && .venv/bin/ghradar update --days 7 && .venv/bin/ghradar embed
```

---

## 三、当前索引范围（重要）

- 现在 `data/` 里是**演示索引：约 6200 个、≥5000 星**的项目（覆盖主流最成熟项目）。
- 要覆盖**低星但垂直的领域**（比如“GitHub 搜索工具”这种整体 <5000 星的领域），
  需要跑全量 `crawl --min-stars 100`（未认证限速 10 次/分，需数小时；配 `GITHUB_TOKEN` 快 3 倍）。
- 提示 AI 时，如果目标领域偏冷门，可先确认索引有没有覆盖：让 AI 调 `index_stats`，
  或你自己 `ghradar stats`。
