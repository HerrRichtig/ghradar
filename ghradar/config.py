"""ghradar 全局配置：路径、抓取参数、embedding 模型。

所有可调项都能用环境变量覆盖，便于部署到 cron / MCP 客户端。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---- 路径 ----
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GHRADAR_DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "repos.db"
IDS_PATH = DATA_DIR / "repo_ids.npy"          # (N,) int64，与 vectors.npy 逐行对齐
VECTORS_PATH = DATA_DIR / "vectors.npy"       # (N, dim) float32，已归一化
MODELS_DIR = DATA_DIR / "hf"                  # HuggingFace 缓存

# 模型缓存固定在数据目录下，移植时无需再手动设 HF_HOME
os.environ.setdefault("HF_HOME", str(MODELS_DIR))
os.environ.setdefault("HF_HUB_CACHE", str(MODELS_DIR))

# ---- GitHub API ----
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
SEARCH_URL = "https://api.github.com/search/repositories"
USER_AGENT = "ghradar/0.1"
SEARCH_PER_PAGE = 100
MAX_PAGES_PER_QUERY = 10                      # GitHub Search 单查询最多 1000 条
# 未认证 10 req/min，认证 30 req/min；留一点余量
MIN_INTERVAL = 2.1 if GITHUB_TOKEN else 6.2

# ---- 抓取范围 ----
DEFAULT_MIN_STARS = int(os.environ.get("GHRADAR_MIN_STARS", "100"))
# 星数分桶（含下界，不含上界；None 表示无上界），覆盖 >100 星的全语言成熟项目
STAR_BUCKETS = [
    (100, 200), (200, 300), (300, 400), (400, 500), (500, 750),
    (750, 1000), (1000, 1500), (1500, 2000), (2000, 3000), (3000, 5000),
    (5000, 7500), (7500, 10000), (10000, 20000), (20000, 50000), (50000, None),
]
# 若某分片仍超 1000 条，用这些语言继续切分（常见语言，覆盖绝大多数仓库）
FALLBACK_LANGS = [
    "python", "javascript", "typescript", "go", "java", "c", "c++", "c#",
    "rust", "ruby", "php", "swift", "kotlin", "dart", "shell", "html",
    "css", "jupyter notebook", "scala", "haskell", "lua", "r", "objective-c",
    "perl", "elixir", "clojure", "erlang", "julia", "zig", "vue", "svelte",
]

# ---- embedding ----
EMBED_MODEL = os.environ.get(
    "GHRADAR_EMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
EMBED_DIM = int(os.environ.get("GHRADAR_EMBED_DIM", "384"))
EMBED_BATCH = int(os.environ.get("GHRADAR_EMBED_BATCH", "64"))

# ---- 检索 ----
DEFAULT_TOP_K = 20
SEMANTIC_CANDIDATES = 100   # 语义召回候选数（远大于 top_k，便于 RRF 融合）


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
