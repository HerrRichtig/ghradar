"""每天首次使用时，后台自动触发增量更新（不阻塞当次检索）。

在 MCP 工具入口调用 maybe_trigger_background_update()：
  - 若当天（自然日）已触发过，则跳过；
  - 否则启动一个脱离的独立子进程顺序执行
        ghradar update --min-stars 5000 --days 7
        ghradar embed
    并即时返回，不影响当次检索。

使用两个 data/ 下的状态文件：
  - .last_auto_update  记录上次触发的日期（YYYY-MM-DD），用于同一天去重
  - .autoupdate.lock   文件锁，防止上一个后台任务未完成时又被触发
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys

from . import config

# 这些都可以用环境变量覆盖（对应 ghradar CLI 的参数）
MIN_STARS = os.environ.get("GHRADAR_AUTO_MIN_STARS", "5000")
DAYS = os.environ.get("GHRADAR_AUTO_DAYS", "7")

_AUTO_DATE = config.DATA_DIR / ".last_auto_update"
_AUTO_LOCK = config.DATA_DIR / ".autoupdate.lock"
_AUTO_LOG = config.DATA_DIR / "autoupdate.log"


def _today() -> str:
    return datetime.date.today().isoformat()


def maybe_trigger_background_update() -> None:
    """MCP 工具入口调用：当天第一次触发一次后台更新（不阻塞）。

    任何异常都不会抛出（宁可跳过，也不影响当次检索）。
    """
    try:
        config.ensure_dirs()
        today = _today()

        # 1) 同一天已触发过 -> 跳过
        if _AUTO_DATE.exists():
            try:
                if _AUTO_DATE.read_text(encoding="utf-8").strip() == today:
                    return
            except Exception:
                pass

        # 2) 独占创建锁文件；已有后台任务在跑 -> 跳过
        try:
            fd = os.open(str(_AUTO_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        os.close(fd)

        # 3) 先记录日期（防止同一天内再次触发）
        try:
            _AUTO_DATE.write_text(today, encoding="utf-8")
        except Exception:
            pass

        # 4) 启动脱离的独立子进程执行 update + embed
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        with open(str(_AUTO_LOG), "a", encoding="utf-8", errors="replace") as f:
            subprocess.Popen(
                [sys.executable, "-m", "ghradar.autoupdate", "run"],
                cwd=str(config.ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=flags,
            )
    except Exception:
        # 任何异常都静默处理，绝不影响当次检索
        pass


def run() -> None:
    """后台子进程入口：顺序执行 update + embed，结束后清理锁文件。"""
    from .cli import main as cli_main

    try:
        cli_main(["update", "--min-stars", MIN_STARS, "--days", DAYS])
        cli_main(["embed"])
    except Exception:
        pass
    finally:
        try:
            _AUTO_LOCK.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    run()